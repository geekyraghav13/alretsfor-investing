"""South Korea DART — Major Shareholding Reports (주식등의대량보유상황보고서).

Under Korea's Financial Investment Services and Capital Markets Act §147,
any person beneficially owning 5% or more of a KRX-listed company's voting
stock must file a "주식등의대량보유상황보고서" (Major Shareholding Status
Report). Same 5% threshold as SEC 13D/G and Japan EDINET 大量保有.

Setup: free API key at https://opendart.fss.or.kr/ (Open DART). The signup
form is in Korean but Google Translate works. Set DART_API_KEY env var.
20,000 requests/day on the free tier — plenty for 15-min polling.

If DART_API_KEY is unset, this source gracefully no-ops with a setup hint.

Filer name matching: hedge_funds.json includes Hangul transliterations for
the most globally-active US funds (Blackstone → 블랙스톤, Citadel → 씨타델,
Elliott → 엘리엇, etc.). Most foreign filers on Korean filings appear with
the English name in flr_nm, but we keep Hangul variants for safety.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from ..config import DART_API_KEY, DART_LIST_URL, DART_LOOKBACK_DAYS, USER_AGENT
from ..notifier import Notifier
from ..storage import event_already_fired, record_event, upsert_sec_filing

log = logging.getLogger(__name__)

REPORT_NAME_KEYWORDS = ("대량보유",)  # matches both initial filing and amendments


from .sec_13d import _matches_qualifying_fund  # noqa: E402


@dataclass(frozen=True)
class DartFiling:
    rcept_no: str
    report_name: str
    filer_name: str
    issuer_name: str
    issuer_stock_code: str
    rcept_date: datetime

    def viewer_url(self) -> str:
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={self.rcept_no}"


def _parse_yyyymmdd(value: Optional[str]) -> Optional[datetime]:
    if not value or len(value) != 8:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_page(bgn_de: str, end_de: str, page_no: int) -> tuple[list[dict], int]:
    """Return (rows, total_page)."""
    if not DART_API_KEY:
        return [], 0
    try:
        r = requests.get(
            DART_LIST_URL,
            params={
                "crtfc_key": DART_API_KEY,
                "bgn_de": bgn_de,
                "end_de": end_de,
                "pblntf_ty": "D",
                "page_no": page_no,
                "page_count": 100,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        log.error("DART fetch failed page=%d: %s", page_no, e)
        return [], 0
    except ValueError as e:
        log.error("DART non-JSON: %s", e)
        return [], 0

    status = str(data.get("status") or "")
    if status not in ("000", "013"):  # 000=ok, 013=no data
        log.error("DART status=%s message=%s", status, data.get("message"))
        return [], 0
    return data.get("list") or [], int(data.get("total_page") or 0)


def fetch_recent_filings() -> list[DartFiling]:
    if not DART_API_KEY:
        log.warning(
            "DART_API_KEY is not set — Korea large-shareholding alerts are DISABLED. "
            "Free key signup: https://opendart.fss.or.kr/"
        )
        return []

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=DART_LOOKBACK_DAYS)
    bgn_de = start.strftime("%Y%m%d")
    end_de = today.strftime("%Y%m%d")

    out: list[DartFiling] = []
    page = 1
    while True:
        rows, total_page = _fetch_page(bgn_de, end_de, page)
        if not rows:
            break
        for r in rows:
            name = (r.get("report_nm") or "").strip()
            if not any(kw in name for kw in REPORT_NAME_KEYWORDS):
                continue
            rcept_no = (r.get("rcept_no") or "").strip()
            if not rcept_no:
                continue
            rcept_date = _parse_yyyymmdd(r.get("rcept_dt"))
            if not rcept_date:
                continue
            out.append(
                DartFiling(
                    rcept_no=rcept_no,
                    report_name=name,
                    filer_name=(r.get("flr_nm") or "").strip(),
                    issuer_name=(r.get("corp_name") or "").strip(),
                    issuer_stock_code=(r.get("stock_code") or "").strip(),
                    rcept_date=rcept_date,
                )
            )
        if page >= total_page:
            break
        page += 1
        if page > 20:  # safety bound; 2000 results plenty for 3-day window
            break

    log.info("DART: %d major-shareholding filings in last %dd", len(out), DART_LOOKBACK_DAYS)
    return out


def _fire(notifier: Notifier, f: DartFiling, fund_label: str) -> None:
    key = f"dart:{f.rcept_no}"
    if event_already_fired(key):
        return
    ticker_part = f" ({f.issuer_stock_code})" if f.issuer_stock_code else ""
    title = f"KR: {fund_label} filed 대량보유 on {f.issuer_name}{ticker_part}"
    body = (
        f"Filer: {f.filer_name}\n"
        f"Report: {f.report_name} (>= 5% stake by definition)\n"
        f"Issuer: {f.issuer_name}{ticker_part}\n"
        f"Submitted: {f.rcept_date.date()}\n"
        f"AUM gate: $10B+ (curated list match)"
    )
    notifier.send(title, body, url=f.viewer_url())
    record_event(key, "dart_5pct", title, body, payload=f.rcept_no)
    log.info("ALERT R3-KR: %s", title)


def run(notifier: Notifier) -> None:
    filings = fetch_recent_filings()
    for f in filings:
        is_new = upsert_sec_filing(
            f"dart:{f.rcept_no}",
            "대량보유",
            "",
            f.filer_name,
            f.issuer_stock_code,
            f.issuer_name,
            f.rcept_date.isoformat(),
        )
        if not is_new:
            continue
        fund_label = _matches_qualifying_fund(f.filer_name)
        if not fund_label:
            continue
        _fire(notifier, f, fund_label)
