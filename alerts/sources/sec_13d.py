"""SEC 13D/13G filing alerts.

Background: Under §13(d) of the Securities Exchange Act, any beneficial owner
crossing 5% of a class of voting equity securities of a US-registered company
must file Schedule 13D (activist intent) or Schedule 13G (passive). Therefore
the existence of the filing IS the >=5% stake — we don't need to parse a
percentage out of the body.

Rule: alert when a 13D / 13G / 13D-A / 13G-A is filed by a hedge fund or
private equity firm with >=$10B AUM (from our curated list).

Data source: SEC EDGAR full-text search JSON API (free, no key; requires a
descriptive User-Agent per SEC fair-access policy).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from ..config import (
    DATA_DIR,
    FUND_MIN_AUM_USD,
    SEC_LOOKBACK_DAYS,
    SEC_SEARCH_URL,
    SEC_STAKE_MIN_PERCENT,
    USER_AGENT,
)
from ..notifier import Notifier
from ..storage import event_already_fired, record_event, upsert_sec_filing

log = logging.getLogger(__name__)

FORMS = ["SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"]


@dataclass(frozen=True)
class Filing:
    accession_no: str
    form: str
    filer_cik: str
    filer_name: str
    subject_cik: str
    subject_name: str
    filed_at: datetime

    def edgar_url(self) -> str:
        cik_int = int(self.filer_cik) if self.filer_cik.isdigit() else 0
        acc = self.accession_no.replace("-", "")
        return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_int}&type=SC+13&dateb=&owner=include&count=40"

    def filing_index_url(self) -> str:
        cik_int = int(self.filer_cik) if self.filer_cik.isdigit() else 0
        acc_dashes = self.accession_no
        acc_clean = self.accession_no.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{acc_dashes}-index.htm"


def _load_fund_patterns() -> list[tuple[str, list[str]]]:
    path = Path(DATA_DIR) / "hedge_funds.json"
    with open(path) as f:
        data = json.load(f)
    return [(f["name"], [p.lower() for p in f["patterns"]]) for f in data["funds"]]


_FUND_PATTERNS: list[tuple[str, list[str]]] = []


def _fund_patterns() -> list[tuple[str, list[str]]]:
    global _FUND_PATTERNS
    if not _FUND_PATTERNS:
        _FUND_PATTERNS = _load_fund_patterns()
    return _FUND_PATTERNS


def _matches_qualifying_fund(filer_name: str) -> Optional[str]:
    if not filer_name:
        return None
    name_lower = filer_name.lower()
    for canonical, patterns in _fund_patterns():
        for pat in patterns:
            if pat in name_lower:
                return canonical
    return None


def _split_display_names(hit_source: dict) -> tuple[str, str]:
    """SEC EDGAR display_names look like:
       ['SIEBERT FINANCIAL CORP  (SIEB)  (CIK 0000065596)', 'Gebbia Gloria E  (CIK 0001692471)']
    The first entry is conventionally the *subject company* (issuer of the
    securities). The remaining entries are filer(s). The form lacks explicit
    role tags, so we use position-based heuristics.
    """
    names = hit_source.get("display_names") or []
    cleaned = [re.split(r"\s\s\(CIK", n)[0].strip() for n in names]
    if not cleaned:
        return "", ""
    if len(cleaned) == 1:
        return cleaned[0], ""
    return cleaned[1], cleaned[0]


def _parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_recent_filings() -> list[Filing]:
    today = datetime.now(timezone.utc).date()
    startdt = (today - timedelta(days=SEC_LOOKBACK_DAYS)).isoformat()
    enddt = today.isoformat()
    out: list[Filing] = []

    for form in FORMS:
        params = {
            "q": "",
            "forms": form,
            "dateRange": "custom",
            "startdt": startdt,
            "enddt": enddt,
        }
        try:
            r = requests.get(
                SEC_SEARCH_URL,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.error("SEC search failed for form %s: %s", form, e)
            continue

        for hit in data.get("hits", {}).get("hits", []):
            src = hit.get("_source", {}) or {}
            ciks = src.get("ciks") or []
            accession = src.get("adsh") or hit.get("_id", "").split(":")[0]
            if not accession:
                continue

            filer_name, subject_name = _split_display_names(src)
            subject_cik = ciks[0] if ciks else ""
            filer_cik = ciks[1] if len(ciks) > 1 else ""

            filed_at = _parse_date(src.get("file_date") or src.get("filed_date") or "")
            if not filed_at:
                continue

            out.append(
                Filing(
                    accession_no=accession,
                    form=src.get("form") or form,
                    filer_cik=str(filer_cik),
                    filer_name=filer_name,
                    subject_cik=str(subject_cik),
                    subject_name=subject_name,
                    filed_at=filed_at,
                )
            )
    log.info("SEC: %d 13D/13G filings in last %dd", len(out), SEC_LOOKBACK_DAYS)
    return out


def _fire(notifier: Notifier, f: Filing, fund_label: str) -> None:
    key = f"sec_13:{f.accession_no}"
    if event_already_fired(key):
        return
    title = f"{fund_label} filed {f.form} on {f.subject_name or f.subject_cik}"
    body = (
        f"Filer: {f.filer_name}\n"
        f"Form: {f.form} (>= {SEC_STAKE_MIN_PERCENT:.0f}% stake by definition)\n"
        f"Subject: {f.subject_name or f.subject_cik}\n"
        f"Filed: {f.filed_at.date()}\n"
        f"AUM gate: ${FUND_MIN_AUM_USD/1e9:.0f}B+ (curated list match)"
    )
    notifier.send(title, body, url=f.filing_index_url())
    record_event(key, "sec_13d_g", title, body, payload=f.accession_no)
    log.info("ALERT R3: %s", title)


def run(notifier: Notifier) -> None:
    filings = fetch_recent_filings()
    for f in filings:
        is_new = upsert_sec_filing(
            f.accession_no, f.form, f.filer_cik, f.filer_name,
            f.subject_cik, f.subject_name, f.filed_at.isoformat()
        )
        if not is_new:
            continue
        fund_label = _matches_qualifying_fund(f.filer_name)
        if not fund_label:
            continue
        _fire(notifier, f, fund_label)
