"""SQLite-backed dedup + event log so we never alert twice on the same event."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_events (
    event_key TEXT PRIMARY KEY,
    rule       TEXT NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    payload    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_congress_trades (
    trade_id   TEXT PRIMARY KEY,
    member     TEXT,
    ticker     TEXT,
    amount_min INTEGER,
    amount_max INTEGER,
    traded_at  TEXT,
    seen_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_sec_filings (
    accession_no TEXT PRIMARY KEY,
    form         TEXT,
    filer_cik    TEXT,
    filer_name   TEXT,
    subject_cik  TEXT,
    subject_name TEXT,
    filed_at     TEXT,
    seen_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_congress_ticker_date
    ON seen_congress_trades(ticker, traded_at);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn(path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(SCHEMA)


def event_already_fired(event_key: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM alert_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        return row is not None


def record_event(event_key: str, rule: str, title: str, body: str, payload: str = "") -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO alert_events (event_key, rule, title, body, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (event_key, rule, title, body, payload, _utc_now()),
        )


def upsert_congress_trade(
    trade_id: str,
    member: str,
    ticker: str,
    amount_min: int,
    amount_max: int,
    traded_at: str,
) -> bool:
    """Return True if this is a new trade we haven't seen before."""
    with _conn() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO seen_congress_trades "
            "(trade_id, member, ticker, amount_min, amount_max, traded_at, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, member, ticker, amount_min, amount_max, traded_at, _utc_now()),
        )
        return cur.rowcount > 0


def members_buying_ticker(ticker: str, since_iso: str) -> list[str]:
    with _conn() as con:
        rows = con.execute(
            "SELECT DISTINCT member FROM seen_congress_trades "
            "WHERE ticker = ? AND traded_at >= ?",
            (ticker, since_iso),
        ).fetchall()
        return [r["member"] for r in rows if r["member"]]


def upsert_sec_filing(
    accession_no: str,
    form: str,
    filer_cik: str,
    filer_name: str,
    subject_cik: str,
    subject_name: str,
    filed_at: str,
) -> bool:
    with _conn() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO seen_sec_filings "
            "(accession_no, form, filer_cik, filer_name, subject_cik, subject_name, filed_at, seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (accession_no, form, filer_cik, filer_name, subject_cik, subject_name, filed_at, _utc_now()),
        )
        return cur.rowcount > 0
