"""Central configuration for the trading-alerts service."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STATE_DIR = ROOT / "state"
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "alerts" / "data"

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = STATE_DIR / "alerts.sqlite"
LOG_PATH = LOG_DIR / "alerts.log"

CONTACT_EMAIL = os.environ.get("ALERTS_CONTACT_EMAIL", "raghav.sharma@kalagato.co")
USER_AGENT = f"TradingAlerts/1.0 ({CONTACT_EMAIL})"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

POLL_INTERVAL_SECONDS = int(os.environ.get("ALERTS_POLL_INTERVAL", "900"))

CONGRESS_MIN_TRADE_USD = 10_000
CONGRESS_CLUSTER_WINDOW_DAYS = 30
SEC_STAKE_MIN_PERCENT = 5.0
FUND_MIN_AUM_USD = 10_000_000_000

CONGRESS_LOOKBACK_DAYS = int(os.environ.get("ALERTS_CONGRESS_LOOKBACK_DAYS", "60"))
SEC_LOOKBACK_DAYS = int(os.environ.get("ALERTS_SEC_LOOKBACK_DAYS", "3"))

FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()
FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_SENATE_LATEST = f"{FMP_BASE}/senate-latest"
FMP_HOUSE_LATEST = f"{FMP_BASE}/house-latest"

SEC_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
SEC_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
