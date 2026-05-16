# Alerts for Investing

A zero-infra alerting service that watches public regulatory filings and DMs you on Telegram the moment a high-signal event happens. Polls every ~15 minutes, runs on the GitHub Actions free tier, costs nothing.

## What it alerts on

Three rules, each backed by a separate data source:

| Rule | Trigger | Signal |
|---|---|---|
| **R1 — Congress single buy** | Any US Senator or Representative buying a stock where the disclosed amount lower-bound is **≥ $10,000** | Insider activity from people with non-public policy/regulatory information |
| **R2 — Congress cluster** | Two or more distinct Congress members buying the **same ticker** within a 30-day rolling window | Cross-member conviction; harder to dismiss as coincidence |
| **R3 — $10B+ fund takes 5%+ stake** | A filer matching the curated list of $10B+ hedge funds or PE firms files a **Schedule 13D/G** (US), **大量保有報告書** (Japan), or **주식등의대량보유상황보고서** (Korea) | Smart-money concentration; the form itself = 5%+ by regulatory definition |

R3 covers **US + Japan + Korea** out of the box. EU and UK are planned (see Roadmap).

## How it works

```
                  GitHub Actions cron (every 15 min)
                              │
                              ▼
         ┌────────────────────────────────────────┐
         │  python -m alerts.main --once          │
         │                                        │
         │   sources/                             │
         │     congress.py    → FMP latest feed   │
         │     sec_13d.py     → SEC EDGAR FTS     │
         │     japan_edinet.py → EDINET v2 API    │
         │     korea_dart.py   → Open DART API    │
         │                                        │
         │   filter → curated fund list / $ gate  │
         │   dedup  → SQLite (state/alerts.sqlite)│
         │   send   → Notifier interface          │
         └──────────────────┬─────────────────────┘
                            │
            ┌───────────────┴───────────────┐
            ▼               ▼               ▼
       Telegram         Desktop popup   Web push
       (cloud)          (local dev)     (future)
```

Each source is a self-contained module with the same shape:

```python
def run(notifier: Notifier) -> None:
    for item in fetch_recent(): 
        if storage.upsert(item) and matches_filter(item):
            notifier.send(title, body, url)
```

A failure in one source never blocks the others — they're each wrapped in their own `try/except` in the main loop.

### Stateful polling on a stateless runner

GitHub Actions runners are ephemeral, but the deduplication table needs to survive. Solution: cache the `state/` directory between runs.

```yaml
- uses: actions/cache/restore@v4
  with:
    path: state
    key: alerts-state-${{ github.run_id }}
    restore-keys: alerts-state-
```

`restore-keys` uses prefix matching — every run pulls the most recent cached state. The save step writes a new entry keyed by the current run ID. First run primes the dedup table silently (with `--log-only`) to avoid flooding the user with backfill on initial deployment.

### Notifier abstraction

```python
class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, body: str, url: Optional[str] = None) -> None: ...
```

Implementations: `TelegramNotifier`, `DesktopNotifier` (cross-platform via native CLI tools with `plyer` fallback), `LogOnlyNotifier`, `CompositeNotifier`. `default_notifier()` auto-selects based on environment variables — Telegram if creds are set, else native desktop popup, else stdout. This is the swap point for a future web push or HTTP-webhook delivery.

### Dedup keys

| Source | Dedup key |
|---|---|
| Congress | SHA-1 of `(chamber, member, ticker, trade_date, amount_range, type)` |
| SEC | Accession number |
| EDINET | Document ID |
| DART | Receipt number |

Stored in SQLite via `INSERT OR IGNORE` — `rowcount > 0` is the "this is new" signal.

## Data sources

| Source | Endpoint | Auth | Cost |
|---|---|---|---|
| Congress trades | [FMP `/stable/senate-latest` + `/stable/house-latest`](https://financialmodelingprep.com/developer/docs) | Free API key | 250 calls/day free |
| SEC 13D/13G | [EDGAR full-text search](https://efts.sec.gov/LATEST/search-index) | None (User-Agent required) | Free |
| Japan EDINET | [EDINET v2 documents.json](https://api.edinet-fsa.go.jp/) | Free subscription key | Free |
| Korea DART | [Open DART list.json](https://opendart.fss.or.kr/) | Free API key | 20k calls/day free |

## Setup

### 1. Telegram bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts to get a `TELEGRAM_BOT_TOKEN`.
2. Start a chat with your new bot (or add it to a group), send any message.
3. Get your chat ID:
   ```bash
   TELEGRAM_BOT_TOKEN=<token> python scripts/get_chat_id.py
   ```

### 2. API keys

| Service | Sign-up | Required for |
|---|---|---|
| FMP | https://site.financialmodelingprep.com/developer/docs | Congress alerts (R1, R2) |
| EDINET | https://api.edinet-fsa.go.jp/ | Japan 5%-stake alerts (R3-JP) |
| Open DART | https://opendart.fss.or.kr/ | Korea 5%-stake alerts (R3-KR) |

SEC EDGAR works without a key. Missing keys disable just that source — the rest keep working.

### 3. Local run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export FMP_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
# optional:
export EDINET_API_KEY=...
export DART_API_KEY=...

python -m alerts.main --once         # single poll
python -m alerts.main --test-notify  # popup/Telegram test
python -m alerts.main                # daemon mode (polls every 15 min)
```

For Linux desktop deployment as a `systemd --user` service, run `./install.sh`.

### 4. GitHub Actions deployment (free, recommended)

1. Fork this repo (public, so Actions minutes are unlimited).
2. Add three repository secrets at `Settings → Secrets and variables → Actions`:
   - `FMP_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - (optionally) `EDINET_API_KEY`, `DART_API_KEY`
3. The workflow at `.github/workflows/poll.yml` triggers every 15 min via cron. First run primes the dedup table; subsequent runs only DM on genuinely new events.

## Project layout

```
alerts/
├── __init__.py
├── main.py                  Entry point: --once / --test-notify / --log-only / daemon mode
├── config.py                Env-var-backed config (URLs, thresholds, keys)
├── storage.py               SQLite dedup + event log
├── notifier.py              Notifier interface + Telegram/Desktop/Composite implementations
├── data/
│   └── hedge_funds.json     Curated $10B+ fund list (~95 names, EN+JP+KR patterns)
└── sources/
    ├── congress.py          R1 + R2 (FMP)
    ├── sec_13d.py           R3 US (SEC EDGAR)
    ├── japan_edinet.py      R3 JP (EDINET) — shared filter with sec_13d
    └── korea_dart.py        R3 KR (DART) — shared filter with sec_13d

.github/workflows/poll.yml   Cron + cache + secrets
scripts/get_chat_id.py       Telegram bot helper
install.sh                   Linux systemd installer
trading-alerts.service       systemd unit
```

## Tuning

### Hedge fund list

`alerts/data/hedge_funds.json` is the gate for R3. Each entry has a canonical `name` and a list of `patterns` (substring-matched, case-insensitive) against the filer name on the regulatory filing. Includes katakana and Hangul variants for the most globally-active funds so the same matcher works against Japan/Korea filings.

Add or remove entries to widen or narrow signal.

### Thresholds

Edit `alerts/config.py`:

| Constant | Default | What it controls |
|---|---|---|
| `CONGRESS_MIN_TRADE_USD` | 10_000 | R1 lower-bound amount filter |
| `CONGRESS_CLUSTER_WINDOW_DAYS` | 30 | R2 cluster window |
| `SEC_STAKE_MIN_PERCENT` | 5.0 | Informational only — form type implies threshold |
| `FUND_MIN_AUM_USD` | 10_000_000_000 | Informational only — curated list IS the gate |
| `POLL_INTERVAL_SECONDS` | 900 | Daemon mode poll cadence |

### Lookback windows

Most can be set via env var:

```
ALERTS_POLL_INTERVAL              = 900
ALERTS_CONGRESS_LOOKBACK_DAYS     = 60
ALERTS_SEC_LOOKBACK_DAYS          = 3
ALERTS_EDINET_LOOKBACK_DAYS       = 3
ALERTS_DART_LOOKBACK_DAYS         = 3
```

Congress uses 60 days because disclosures lag trades by up to 45 days (STOCK Act). SEC/EDINET/DART use 3 days because filings are nearly real-time.

## Why these rules?

- **Congress trades** are widely cited as an alpha signal in academic literature ([Ziobrowski et al. 2004](https://www.jstor.org/stable/30033365); [Karadas 2018](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2933015)). The STOCK Act forces disclosure within 45 days but enforcement is weak — so the data is messy but free.
- **Cluster activity** (R2) filters out idiosyncratic single-member trades. When 3+ members independently buy the same name, the signal-to-noise ratio jumps.
- **13D/13G filings** are the textbook activist / smart-money disclosure. The 5% threshold is regulatory, not chosen by us. Filtering by AUM ($10B+) gates for funds with research and conviction at scale, filtering out shell vehicles.

## Architectural choices

### Why GitHub Actions cron?

| Option | Cost | Always-on | Verdict |
|---|---|---|---|
| Personal VPS | $4–6/mo | Yes | Fine but unnecessary |
| Fly.io / Railway free tier | $0 (with credit card) | Yes | Works |
| Render free | $0 | **No** (sleeps) | Misses polls — rejected |
| **GitHub Actions cron** | **$0** (public repo) | Sort of (cron-driven, not always-on) | Picked |
| Local laptop | $0 | No (laptop sleeps) | Defeats the multi-device goal |

The user's latency tolerance is minutes, not milliseconds. SEC filings drop in batches; Congress disclosures appear at irregular times throughout the day. Cron is the right abstraction.

### Why Telegram for delivery?

Native desktop popups only work where the daemon runs. Email is slow and gets filtered. Web push needs a hosted page with a domain.

Telegram solves multi-device synchronously: any device logged into the user's Telegram account receives the DM instantly. Permission scoping is the Telegram chat itself — adding a teammate to the group adds them to the alert feed.

### Why not parse the filing bodies?

13D/G/EDINET/DART forms exist *because* the filer crossed 5%. The threshold is in the form's regulatory definition, not in the body. Skipping body parsing means:

- No PDF/HTML scraping
- No locale-specific number formatting headaches
- Sub-second per-filing processing

The trade-off is we don't report the exact stake percentage — just that it's ≥5%. For an alert, that's enough; the user clicks through to the filing for the precise number.

## Roadmap

- [ ] EU coverage (BaFin / AMF / CONSOB / CNMV TR-1 equivalents — each country has its own portal, fragmented)
- [ ] UK FCA TR-1 major shareholding notifications (data.fca.org.uk)
- [ ] Web dashboard with login + history view (when ready to leave GitHub Actions cron)
- [ ] Per-rule mute/throttle controls
- [ ] Backtesting harness: for each historical alert, what did the stock do over the next 5/30/90 days?

## License

MIT
