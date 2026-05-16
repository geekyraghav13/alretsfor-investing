"""US Congress / Senate stock trade alerts.

Data source: FinancialModelingPrep (FMP) free tier RSS feeds, which aggregate
official House Clerk + Senate EFD disclosures into a clean JSON stream.

Setup: get a free API key at https://site.financialmodelingprep.com (no
credit card needed), then set the FMP_API_KEY environment variable. Free
tier covers 250 calls/day; this service uses ~50 calls/day at the default
15-minute poll cadence.

Why FMP and not the official sources directly?
  - House Clerk publishes PTRs as PDFs that require OCR/parsing.
  - Senate EFD requires session cookies after accepting terms.
  - The original community aggregators (Stock Watcher) shut down in 2024.
  FMP normalizes both chambers and is the lowest-friction free path.

Alert rules:
  R1: Any single buy whose lower-bound disclosed amount is >= $10,000.
  R2: Two or more distinct members buying the same ticker within
      CONGRESS_CLUSTER_WINDOW_DAYS days (cluster signal).
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import requests
from dateutil import parser as dateparser

from ..config import (
    CONGRESS_CLUSTER_WINDOW_DAYS,
    CONGRESS_LOOKBACK_DAYS,
    CONGRESS_MIN_TRADE_USD,
    FMP_API_KEY,
    FMP_HOUSE_LATEST,
    FMP_SENATE_LATEST,
    USER_AGENT,
)
from ..notifier import Notifier
from ..storage import (
    event_already_fired,
    members_buying_ticker,
    record_event,
    upsert_congress_trade,
)

log = logging.getLogger(__name__)

_AMOUNT_RE = re.compile(r"\$?([\d,]+)")


@dataclass(frozen=True)
class CongressTrade:
    chamber: str
    member: str
    ticker: str
    asset_description: str
    transaction_type: str
    traded_at: datetime
    disclosed_at: datetime
    amount_min: int
    amount_max: int
    ptr_link: str

    @property
    def trade_id(self) -> str:
        raw = f"{self.chamber}|{self.member}|{self.ticker}|{self.traded_at.date()}|{self.amount_min}|{self.amount_max}|{self.transaction_type}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _parse_amount(amount: str) -> tuple[int, int]:
    if not amount:
        return (0, 0)
    nums = [int(m.replace(",", "")) for m in _AMOUNT_RE.findall(amount)]
    if not nums:
        return (0, 0)
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (nums[0], nums[1])


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _is_buy(tx_type: Optional[str]) -> bool:
    if not tx_type:
        return False
    t = tx_type.strip().lower()
    return "purchase" in t or t == "buy"


def _fetch(url: str) -> list[dict]:
    try:
        r = requests.get(
            url,
            params={"apikey": FMP_API_KEY},
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        if r.status_code in (401, 403):
            log.error(
                "FMP returned %d — check FMP_API_KEY at https://site.financialmodelingprep.com. body=%s",
                r.status_code, r.text[:200],
            )
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []
    except requests.RequestException as e:
        log.error("FMP fetch failed (%s): %s", url, e)
        return []
    except ValueError as e:
        log.error("FMP returned non-JSON: %s", e)
        return []


def _normalize(rows: Iterable[dict], chamber: str) -> Iterable[CongressTrade]:
    for r in rows:
        if not _is_buy(r.get("type")):
            continue
        ticker = (r.get("symbol") or r.get("ticker") or "").strip().upper()
        if not ticker or ticker in {"--", "N/A"}:
            continue
        traded_at = _parse_date(r.get("transactionDate"))
        disclosed_at = _parse_date(r.get("disclosureDate")) or traded_at
        if not traded_at:
            traded_at = disclosed_at
        if not traded_at:
            continue
        lo, hi = _parse_amount(r.get("amount") or "")
        first = (r.get("firstName") or "").strip()
        last = (r.get("lastName") or "").strip()
        member = (f"{first} {last}".strip()) or (r.get("office") or "").strip() or (r.get("representative") or "").strip()
        yield CongressTrade(
            chamber=chamber,
            member=member,
            ticker=ticker,
            asset_description=(r.get("assetDescription") or "").strip(),
            transaction_type=(r.get("type") or "").strip(),
            traded_at=traded_at,
            disclosed_at=disclosed_at or traded_at,
            amount_min=lo,
            amount_max=hi,
            ptr_link=(r.get("link") or "").strip(),
        )


def fetch_recent_trades() -> list[CongressTrade]:
    if not FMP_API_KEY:
        log.warning(
            "FMP_API_KEY is not set — Congress alerts are DISABLED. "
            "Get a free key at https://site.financialmodelingprep.com and set FMP_API_KEY. "
            "SEC 13D/13G alerts continue to work without a key."
        )
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=CONGRESS_LOOKBACK_DAYS)
    senate_raw = _fetch(FMP_SENATE_LATEST)
    house_raw = _fetch(FMP_HOUSE_LATEST)
    senate = list(_normalize(senate_raw, "Senate"))
    house = list(_normalize(house_raw, "House"))
    all_trades = [t for t in (house + senate) if t.traded_at >= cutoff]
    log.info(
        "Congress: %d recent buys after cutoff (house buys=%d senate buys=%d, raw house=%d senate=%d)",
        len(all_trades), len(house), len(senate), len(house_raw), len(senate_raw),
    )
    return all_trades


def _fire_single_trade(notifier: Notifier, t: CongressTrade) -> None:
    key = f"congress_single:{t.trade_id}"
    if event_already_fired(key):
        return
    title = f"{t.chamber}: {t.member} bought {t.ticker}"
    amount_str = f"${t.amount_min:,}–${t.amount_max:,}" if t.amount_max != t.amount_min else f"${t.amount_min:,}"
    body = (
        f"{t.asset_description or t.ticker}\n"
        f"Amount: {amount_str}\n"
        f"Trade date: {t.traded_at.date()}"
    )
    notifier.send(title, body, url=t.ptr_link or None)
    record_event(key, "congress_single_10k", title, body, payload=t.ptr_link or "")
    log.info("ALERT R1: %s", title)


def _fire_cluster(notifier: Notifier, ticker: str, members: list[str]) -> None:
    members_sorted = sorted(set(members))
    digest = hashlib.sha1(("|".join(members_sorted) + ticker).encode("utf-8")).hexdigest()[:12]
    key = f"congress_cluster:{ticker}:{digest}"
    if event_already_fired(key):
        return
    title = f"Cluster buy: {len(members_sorted)} members in {ticker}"
    body = "Members:\n  " + "\n  ".join(members_sorted)
    notifier.send(title, body)
    record_event(key, "congress_cluster", title, body, payload=",".join(members_sorted))
    log.info("ALERT R2: %s", title)


def run(notifier: Notifier) -> None:
    trades = fetch_recent_trades()
    new_tickers: set[str] = set()
    for t in trades:
        is_new = upsert_congress_trade(
            t.trade_id, t.member, t.ticker, t.amount_min, t.amount_max, t.traded_at.isoformat()
        )
        if not is_new:
            continue
        if t.amount_min >= CONGRESS_MIN_TRADE_USD:
            _fire_single_trade(notifier, t)
        new_tickers.add(t.ticker)

    cluster_since = (datetime.now(timezone.utc) - timedelta(days=CONGRESS_CLUSTER_WINDOW_DAYS)).isoformat()
    for ticker in new_tickers:
        members = members_buying_ticker(ticker, cluster_since)
        if len(members) >= 2:
            _fire_cluster(notifier, ticker, members)
