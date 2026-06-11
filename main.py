"""xauusd-scalper — main entry point.

Scan loop: connect MT5 → APScheduler tick → build MarketData → run strategies →
log all signals (DETECTED or SKIPPED_*) to SQLite. No orders (SPEC 3).
"""
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

import mt5_client
from config import cfg
from core.sessions import get_active_killzone
from core.store import init_db, insert_signal, upsert_heartbeat
from strategies.base import MarketData
from strategies.s1_sweep_displacement import SweepDisplacement
from strategies.s2_orb_ny import OrbNy
from strategies.s3_meanrev_asia import MeanRevAsia
from strategies.s4_sfp_asia import SfpAsia

# ── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
_file_handler = logging.handlers.RotatingFileHandler(
    "logs/scalper.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
logging.basicConfig(
    level=getattr(logging, cfg.LOG_LEVEL, logging.INFO),
    handlers=[_file_handler, _console],
)
log = logging.getLogger("scalper.main")

# ── Strategy registry ─────────────────────────────────────────────────────────
_ALL_STRATEGIES = [SweepDisplacement(), OrbNy(), MeanRevAsia(), SfpAsia()]

# ── Scan job ──────────────────────────────────────────────────────────────────

def scan_once() -> None:
    if not mt5_client.is_connected():
        log.warning("MT5 disconnected — skipping scan")
        return

    killzone = get_active_killzone()
    now_utc = datetime.now(timezone.utc)

    upsert_heartbeat(open_positions=0, last_scan_killzone=killzone)

    if killzone is None:
        return  # outside all sessions — nothing to scan

    log.debug("Scan tick — killzone=%s", killzone)

    # Fetch OHLC once, shared by all strategies
    tf_data = mt5_client.get_scalper_timeframes()
    for tf, df in tf_data.items():
        if df.empty:
            log.warning("Empty OHLC %s — data gap?", tf)

    current_price = mt5_client.get_current_price(cfg.SYMBOL)
    if current_price is None:
        log.warning("No current price — skipping scan")
        return

    spread_pips = mt5_client.get_spread_pips(cfg.SYMBOL)
    if spread_pips > cfg.MAX_SPREAD_PIPS:
        log.debug("Spread %.1f pips > MAX %.1f — not entering", spread_pips, cfg.MAX_SPREAD_PIPS)
        # NOTE: still run detection (log signals as SKIPPED_SPREAD in SPEC 3)

    data = MarketData(
        m1=tf_data.get("M1", __import__("pandas").DataFrame()),
        m5=tf_data.get("M5", __import__("pandas").DataFrame()),
        m15=tf_data.get("M15", __import__("pandas").DataFrame()),
        h1=tf_data.get("H1", __import__("pandas").DataFrame()),
        current_price=current_price,
        spread_pips=spread_pips,
        killzone=killzone,
        now_utc=now_utc,
    )

    for strat in _ALL_STRATEGIES:
        if strat.id not in cfg.ENABLED_STRATEGIES:
            continue
        if killzone not in strat.sessions:
            continue

        for direction in ("LONG", "SHORT"):
            try:
                sig = strat.scan(data, direction)
            except Exception as exc:
                log.error("Strategy %s scan error (%s): %s", strat.id, direction, exc, exc_info=True)
                continue

            if sig is None:
                continue

            if sig.sl_pips > cfg.SL_MAX_PIPS:
                insert_signal(sig, status="SKIPPED_SL_TOO_WIDE",
                              skip_reason=f"sl_pips={sig.sl_pips:.1f}")
                log.info("SKIPPED_SL_TOO_WIDE %s %s sl_pips=%.1f entry=%.2f",
                         strat.id, direction, sig.sl_pips, sig.entry_price)
            else:
                insert_signal(sig, status="DETECTED")
                log.info("SIGNAL %s %s kz=%s entry=%.2f sl=%.2f tp1=%.2f tp2=%.2f score=%d confl=%s",
                         strat.id, direction, killzone,
                         sig.entry_price, sig.sl, sig.tp1, sig.tp2,
                         sig.score, sig.confluences)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("xauusd-scalper starting — symbol=%s lot=%s strategies=%s",
             cfg.SYMBOL, cfg.LOT, cfg.ENABLED_STRATEGIES)

    init_db()
    mt5_client.connect()  # sys.exit(1) on any failure

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
