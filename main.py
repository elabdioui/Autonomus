"""xauusd-scalper — main entry point.

Tick order: heartbeat → reconcile pending/orphans → manage open positions (stub SPEC 4)
  → if in killzone: scan strategies → try_execute each DETECTED signal.
Management runs every tick regardless of killzone.

CLI flags:
  --inject-test-signal   Place a synthetic S1 LONG MARKET signal for integration testing.
"""
import argparse
import logging
import logging.handlers
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler

import mt5_client
from config import cfg
from core.models import Signal
from core.sessions import get_killzone_tag
from core.store import init_db, insert_signal, upsert_heartbeat
from execution.engine import try_execute, reconcile_pending_and_orphans
from execution.manager import manage_open_trades
from reporting.excel_export import run_export
from reporting.news_tagger import start_news_updater
from strategies.base import MarketData
from strategies.s1_sweep_micro import SETUP as S1
from strategies.s2_momentum_breakout import SETUP as S2
from strategies.s3_mean_reversion import SETUP as S3
from strategies.s4_pullback_continuation import SETUP as S4

# ── Logging ───────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)
_fh = logging.handlers.RotatingFileHandler(
    "logs/scalper.log", maxBytes=cfg.LOG_MAX_BYTES,
    backupCount=cfg.LOG_BACKUP_COUNT, encoding="utf-8"
)
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s"))
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
logging.basicConfig(level=getattr(logging, cfg.LOG_LEVEL, logging.INFO), handlers=[_fh, _ch])
log = logging.getLogger("scalper.main")

# ── Strategy registry ─────────────────────────────────────────────────────────
_ALL_STRATEGIES = [S1, S2, S3, S4]

_connection_state = True
_reconnect_failures = 0
_next_reconnect_at = 0.0




# ── Scan job ──────────────────────────────────────────────────────────────────
def scan_once() -> None:
    global _connection_state, _reconnect_failures, _next_reconnect_at
    if not mt5_client.is_connected():
        if _connection_state:
            log.warning("MT5 state: connected -> disconnected")
            _connection_state = False
        upsert_heartbeat(0, None)
        now_mono = time.monotonic()
        if now_mono >= _next_reconnect_at:
            if mt5_client.reconnect():
                _connection_state = True
                _reconnect_failures = 0
                _next_reconnect_at = 0.0
                log.info("MT5 state: disconnected -> reconnected")
            else:
                _reconnect_failures += 1
                delay = min(cfg.MT5_RETRY_BASE_SECONDS * (2 ** (_reconnect_failures - 1)),
                            cfg.MT5_RETRY_MAX_SECONDS)
                _next_reconnect_at = now_mono + delay
                log.warning("MT5 remains disconnected; next reconnect in %.1fs", delay)
        return

    now_utc = datetime.now(timezone.utc)
    killzone = get_killzone_tag(now_utc)

    # ── Always: heartbeat + reconcile + manage ────────────────────────────────
    open_pos_count = len([p for m in [20001, 20002, 20003, 20004]
                          for p in mt5_client.get_positions(m)])
    upsert_heartbeat(open_pos_count, killzone)
    reconcile_pending_and_orphans()
    manage_open_trades()

    log.debug("Scan tick — killzone_tag=%s", killzone)

    # ── Fetch OHLC once, shared by all strategies ─────────────────────────────
    tf_data = mt5_client.get_scalper_timeframes()
    for tf, df in tf_data.items():
        if df.empty:
            log.warning("Empty OHLC %s — data gap?", tf)
            return  # fail-closed: don't scan with missing data

    current_price = mt5_client.get_current_price(cfg.SYMBOL)
    if current_price is None:
        log.warning("No current price — skipping scan")
        return

    spread_pips = mt5_client.get_spread_pips()

    data = MarketData(
        m1=tf_data["M1"], m5=tf_data["M5"], m15=tf_data["M15"], h1=tf_data["H1"],
        h4=tf_data["H4"],
        current_price=current_price,
        spread_pips=spread_pips,
        killzone=killzone,
        now_utc=now_utc,
    )

    # ── Run strategies ────────────────────────────────────────────────────────
    for strat in _ALL_STRATEGIES:
        for direction in ("LONG", "SHORT"):
            try:
                sig = strat.scan(data, direction)
            except Exception as exc:
                log.error("Strategy %s/%s error: %s", strat.name, direction, exc, exc_info=True)
                continue

            if sig is None:
                continue

            insert_signal(sig, status="DETECTED")
            log.info("DETECTED %s %s entry=%.3f structural_sl=%.1fp score=%d",
                     strat.name, direction, sig.entry_price, sig.sl_pips, sig.score)
            try_execute(sig)


# ── Test signal injection ─────────────────────────────────────────────────────
def _inject_test_signal() -> None:
    """Place a synthetic S1 LONG MARKET signal through the full execution path."""
    if not mt5_client.is_connected():
        log.error("MT5 not connected — cannot inject test signal")
        return

    tick = mt5_client.get_current_price(cfg.SYMBOL)
    if tick is None:
        log.error("No price available for test signal")
        return

    spread = mt5_client.get_spread_pips()
    sl = round(tick - 20 * cfg.PIP, cfg.DIGITS)
    tp1 = round(tick + cfg.TP1_PIPS * cfg.PIP, cfg.DIGITS)
    tp2 = round(tick + cfg.TP2_PIPS * cfg.PIP, cfg.DIGITS)

    sig = Signal(
        signal_id=uuid.uuid4().hex,
        ts_utc=datetime.now(timezone.utc),
        strategy="S1",
        setup="S1_sweep_micro",
        direction="LONG",
        killzone="LONDON",
        entry_type="MARKET",
        entry_price=tick,
        entry_zone_low=tick - 0.5,
        entry_zone_high=tick + 0.5,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        sl_pips=20.0,
        confluences=["TEST"],
        score=0,
        context={"test": True},
    )
    insert_signal(sig, status="DETECTED")
    log.info("TEST SIGNAL injected — signal_id=%s entry=%.2f sl=%.2f spread=%.1f",
             sig.signal_id[:8], sig.entry_price, sig.sl, spread)
    try_execute(sig)
    log.info("TEST SIGNAL execution complete — check DB and MT5 terminal")


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="XAUUSD scalper bot")
    parser.add_argument("--inject-test-signal", action="store_true",
                        help="Inject a synthetic S1 LONG test order and exit")
    args = parser.parse_args()

    log.info("xauusd-scalper starting — symbol=%s lot=%s strategies=%s",
             cfg.SYMBOL, cfg.LOT, [setup.name for setup in _ALL_STRATEGIES])

    init_db()
    mt5_client.connect()
    start_news_updater()

    if args.inject_test_signal:
        _inject_test_signal()
        mt5_client.disconnect()
        return

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(scan_once, "interval", seconds=cfg.SCAN_INTERVAL_SECONDS, id="scan")
    scheduler.add_job(run_export, "cron", hour=22, minute=30, id="daily_export",
                      timezone="UTC", misfire_grace_time=300)
    log.info("Scheduler started — interval=%ss", cfg.SCAN_INTERVAL_SECONDS)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down")
        mt5_client.disconnect()


if __name__ == "__main__":
    main()
