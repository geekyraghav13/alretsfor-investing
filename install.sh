#!/usr/bin/env bash
# Installs the trading-alerts service to $HOME/trading-alerts and registers
# it as a systemd --user service.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="$HOME/trading-alerts"

echo "[1/6] Copying project to $DEST_DIR"
mkdir -p "$DEST_DIR"
cp -r "$SRC_DIR/alerts" "$DEST_DIR/"
cp "$SRC_DIR/requirements.txt" "$DEST_DIR/"
mkdir -p "$DEST_DIR/state" "$DEST_DIR/logs"

if [[ -f "$SRC_DIR/.env" && ! -f "$DEST_DIR/.env" ]]; then
  cp "$SRC_DIR/.env" "$DEST_DIR/.env"
  echo "[1/6] Copied .env from source dir"
elif [[ ! -f "$DEST_DIR/.env" ]]; then
  cat >"$DEST_DIR/.env" <<'EOF'
# FMP API key — required for Congress trade alerts.
# Free signup (no credit card): https://site.financialmodelingprep.com/developer/docs
FMP_API_KEY=

# Optional tuning
# ALERTS_POLL_INTERVAL=900
# ALERTS_CONGRESS_LOOKBACK_DAYS=60
# ALERTS_SEC_LOOKBACK_DAYS=3
EOF
  echo "[1/6] Created $DEST_DIR/.env — edit it to add your FMP_API_KEY"
fi
chmod 600 "$DEST_DIR/.env"

echo "[2/6] Creating venv"
python3 -m venv "$DEST_DIR/.venv"

echo "[3/6] Installing requirements"
"$DEST_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
"$DEST_DIR/.venv/bin/pip" install -r "$DEST_DIR/requirements.txt"

echo "[4/6] Priming dedup table (records existing recent filings without firing popups)"
cd "$DEST_DIR"
set -a; . "$DEST_DIR/.env"; set +a
"$DEST_DIR/.venv/bin/python" -m alerts.main --once --log-only || true

echo "[5/6] Installing systemd user unit"
mkdir -p "$HOME/.config/systemd/user"
cp "$SRC_DIR/trading-alerts.service" "$HOME/.config/systemd/user/trading-alerts.service"
systemctl --user daemon-reload
systemctl --user enable trading-alerts.service

echo "[6/6] Starting service"
systemctl --user restart trading-alerts.service
sleep 2
systemctl --user status trading-alerts.service --no-pager || true

cat <<EOF

Done. The service is running.

Next step (optional but recommended):
  Edit $DEST_DIR/.env and set FMP_API_KEY to enable Congress trade alerts.
  Then: systemctl --user restart trading-alerts

Useful commands:
  systemctl --user status trading-alerts        # status
  systemctl --user restart trading-alerts       # restart
  systemctl --user stop trading-alerts          # stop
  journalctl --user -u trading-alerts -f        # live logs
  tail -f $DEST_DIR/logs/alerts.log             # rotating log file

Test a single poll manually:
  cd $DEST_DIR && set -a && . .env && set +a
  $DEST_DIR/.venv/bin/python -m alerts.main --once

Fire a test notification popup:
  $DEST_DIR/.venv/bin/python -m alerts.main --test-notify
EOF
