"""Japan EDINET — Large Shareholding Reports (大量保有報告書).

Under Japan's Financial Instruments and Exchange Act §27-23, any beneficial
owner crossing 5% of voting securities of a Japan-listed company must file a
大量保有報告書 (Large Shareholding Report). The 5% threshold is identical to
the SEC's Schedule 13D/G regime. So the existence of the form = ≥5% stake.

Document type codes filtered:
  350 - 大量保有報告書           (initial 5%+ filing)
  360 - 変更報告書               (amendment, e.g. crossing further thresholds)
  370 - 訂正大量保有報告書       (correction to 350)
  380 - 訂正変更報告書           (correction to 360)

Setup: EDINET v2 requires a free API key (added in late 2023).
  Sign up at https://api.edinet-fsa.go.jp/ (registration page is in Japanese
  but Google Translate / right-click translate works). The key is delivered
  by email. Set EDINET_API_KEY env var.

If EDINET_API_KEY is unset, this source gracefully no-ops with a setup hint.

Filer-name matching: our hedge_funds.json includes katakana / Japanese
patterns for the most globally-active US funds (Blackstone → ブラックストーン,
Citadel → シタデル, etc.). US filings against Japan-listed issuers usually
use the katakana name. Funds without katakana patterns won't match — add
them to hedge_funds.json as needed.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests
from dateutil import parser as dateparser

from ..config import (
    EDINET_API_KEY,
    EDINET_DOCS_URL,
    EDINET_LOOKBACK_DAYS,
    USER_AGENT,
)
from ..notifier import Notifier
from ..storage import event_already_fired, record_event, upsert_sec_filing

log = logging.getLogger(__name__)

LARGE_SHAREHOLDING_DOC_TYPES = {"350", "360", "370", "380"}

# Import the fund-matching helper from the SEC source so we share the curated
# list. (Same gate: $10B+ AUM, by name pattern.)
from .sec_13d import _matches_qualifying_fund  # noqa: E402


@dataclass(frozen=True)
class EdinetFiling:
    doc_id: str
    doc_type_code: str
    filer_name: str
    issuer_name: str
    issuer_sec_code: str
    doc_description: str
    submitted_at: datetime

    def viewer_url(self) -> str:
        return f"https://disclosure2.edinet-fsa.go.jp/WEEK0010.aspx?docID={self.doc_id}"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _fetch_day(day: date) -> list[dict]:
    if not EDINET_API_KEY:
        return []
    try:
        r = requests.get(
            EDINET_DOCS_URL,
            params={
                "date": day.isoformat(),
                "type": "2",
                "Subscription-Key": EDINET_API_KEY,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if r.status_code in (401, 403):
            log.error(
                "EDINET returned %d — check EDINET_API_KEY (free signup at https://api.edinet-fsa.go.jp/). body=%s",
                r.status_code, r.text[:200],
            )
            return []
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.error("EDINET fetch failed for %s: %s", day, e)
        return []
    except ValueError as e:
        log.error("EDINET non-JSON: %s", e)
        return []

    status = (data.get("metadata") or {}).get("status")
    if status and str(status) not in ("200", "OK"):
        log.warning("EDINET status=%s message=%s", status, (data.get("metadata") or {}).get("message"))
    return data.get("results", []) or []


def fetch_recent_filings() -> list[EdinetFiling]:
    if not EDINET_API_KEY:
        log.warning(
            "EDINET_API_KEY is not set — Japan large-shareholding alerts are DISABLED. "
            "Free key signup: https://api.edinet-fsa.go.jp/"
        )
        return []

    out: list[EdinetFiling] = []
    today = datetime.now(timezone.utc).date()
    for offset in range(EDINET_LOOKBACK_DAYS + 1):
        day = today - timedelta(days=offset)
        for r in _fetch_day(day):
            doc_type = str(r.get("docTypeCode") or "")
            if doc_type not in LARGE_SHAREHOLDING_DOC_TYPES:
                continue
            if str(r.get("withdrawalStatus") or "0") != "0":
                continue
            doc_id = (r.get("docID") or "").strip()
            if not doc_id:
                continue
            submitted_at = _parse_dt(r.get("submitDateTime"))
            if not submitted_at:
                continue
            out.append(
                EdinetFiling(
                    doc_id=doc_id,
                    doc_type_code=doc_type,
                    filer_name=(r.get("filerName") or "").strip(),
                    issuer_name=(r.get("issuerName") or r.get("docDescription") or "").strip(),
                    issuer_sec_code=(r.get("secCode") or "").strip(),
                    doc_description=(r.get("docDescription") or "").strip(),
                    submitted_at=submitted_at,
                )
            )
    log.info("EDINET: %d large-shareholding filings in last %dd", len(out), EDINET_LOOKBACK_DAYS)
    return out


def _fire(notifier: Notifier, f: EdinetFiling, fund_label: str) -> None:
    key = f"edinet:{f.doc_id}"
    if event_already_fired(key):
        return
    ticker_part = f" ({f.issuer_sec_code})" if f.issuer_sec_code else ""
    issuer_label = f.issuer_name or f.doc_description or "(issuer unknown)"
    title = f"JP: {fund_label} filed 大量保有 on {issuer_label}{ticker_part}"
    body = (
        f"Filer: {f.filer_name}\n"
        f"Doc type: {f.doc_type_code} (>= 5% stake by definition)\n"
        f"Issuer: {issuer_label}{ticker_part}\n"
        f"Submitted: {f.submitted_at.date()}\n"
        f"AUM gate: $10B+ (curated list match)"
    )
    notifier.send(title, body, url=f.viewer_url())
    record_event(key, "edinet_5pct", title, body, payload=f.doc_id)
    log.info("ALERT R3-JP: %s", title)


def run(notifier: Notifier) -> None:
    filings = fetch_recent_filings()
    for f in filings:
        is_new = upsert_sec_filing(
            f"edinet:{f.doc_id}",
            f.doc_type_code,
            "",
            f.filer_name,
            f.issuer_sec_code,
            f.issuer_name,
            f.submitted_at.isoformat(),
        )
        if not is_new:
            continue
        fund_label = _matches_qualifying_fund(f.filer_name)
        if not fund_label:
            continue
        _fire(notifier, f, fund_label)
