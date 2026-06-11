"""xauusd-scalper — main entry point.

Scan loop skeleton: connect MT5 → APScheduler tick → heartbeat + killzone log.
Strategy calls and execution wired in SPEC 2 / SPEC 3.
"""
import logging
import logging.handlers
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

import mt5_client
from config import cfg
from core.sessions import get_active_killzone
from core.store import init_db, upsert_heartbeat

# ── Logging setup ─────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

_handler = logging.handlers.RotatingFileHandler(
    "logs/scalper.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))

_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))

logging.basicConfig(level=getattr(logging, cfg.LOG_LEVEL, logging.INFO), handlers=[_handler, _console])
log = logging.getLogger("scalper.main")

# ── Scan job ──────────────────────────────────────────────────────────────────

def scan_once() -> None:
    if not mt5_client.is_connected():
        log.warning("MT5 disconnected — skipping scan")
        return

    killzone = get_active_killzone()
    log.debug("Scan tick — killzone=%s", killzone or "NONE")

    # Fetch OHLC data for all timeframes (strategies will use this in SPEC 2)
    tf_data = mt5_client.get_scalper_timeframes()
    for tf, df in tf_data.items():
        if df.empty:
            log.warning("Empty OHLC for %s — possible MT5 data gap", tf)

    # Heartbeat
    try:
        # Count open positions by magic numbers (proper count wired in SPEC 3)
        upsert_heartbeat(open_positions=0, last_scan_killzone=killzone)
    except Exception as exc:
        log.error("Heartbeat write failed: %s", exc)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("xauusd-scalper starting — symbol=%s lot=%s strategies=%s",
             cfg.SYMBOL, cfg.LOT, cfg.ENABLED_STRATEGIES)

    init_db()
    mt5_client.connect()  # exits with sys.exit(1) on any failure

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(scan_once, "interval", seconds=cfg.SCAN_INTERVAL_SECONDS, id="scan")
    log.info("Scheduler started — interval=%ss", cfg.SCAN_INTERVAL_SECONDS)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down")
        mt5_client.disconnect()


if __name__ == "__main__":
    main()
