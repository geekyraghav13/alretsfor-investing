"""Entry point. Runs each source once or in a polling loop."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time

from .config import LOG_PATH, POLL_INTERVAL_SECONDS
from .notifier import LogOnlyNotifier, Notifier, default_notifier
from .sources import congress, sec_13d
from .storage import init_db

log = logging.getLogger("alerts")


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def run_once(notifier: Notifier) -> None:
    log.info("==== poll start ====")
    try:
        congress.run(notifier)
    except Exception as e:
        log.exception("congress source failed: %s", e)
    try:
        sec_13d.run(notifier)
    except Exception as e:
        log.exception("sec_13d source failed: %s", e)
    log.info("==== poll done ====")


def main() -> int:
    p = argparse.ArgumentParser(description="Trading alerts service")
    p.add_argument("--once", action="store_true", help="Run a single poll then exit")
    p.add_argument("--test-notify", action="store_true", help="Fire a test notification and exit")
    p.add_argument("--log-only", action="store_true", help="Don't pop notifications, log to file only")
    args = p.parse_args()

    setup_logging()
    init_db()

    notifier: Notifier = LogOnlyNotifier() if args.log_only else default_notifier()

    if args.test_notify:
        notifier.send("Trading Alerts: test", "If you can see this popup, the notifier works.")
        return 0

    if args.once:
        run_once(notifier)
        return 0

    stop = {"flag": False}

    def _stop(signum, _frame):
        log.info("received signal %s, stopping after current poll", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info("starting poll loop, interval=%ds", POLL_INTERVAL_SECONDS)
    while not stop["flag"]:
        run_once(notifier)
        for _ in range(POLL_INTERVAL_SECONDS):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
