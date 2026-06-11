"""Execution engine: gate sequence, order placement, pending lifecycle, orphan recovery."""
import logging
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta

import mt5_client
from config import cfg
from core.models import Signal, TradeRecord
import core.store as _store
from core.store import (
    insert_signal, update_signal_status, insert_trade,
    update_trade, insert_event, get_open_trades,
)
from indicators.regime import detect_regime
from reporting.news_tagger import is_red_news_window

log = logging.getLogger(__name__)

ALL_MAGICS = [20001, 20002, 20003, 20004]
_PIP = 0.10
_MAGIC_TO_STRATEGY = {20001: "S1", 20002: "S2", 20003: "S3", 20004: "S4"}
_STRATEGY_TO_MAGIC = {v: k for k, v in _MAGIC_TO_STRATEGY.items()}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _make_comment(strategy: str, signal_id: str) -> str:
    return f"SCALP|{strategy}|{signal_id[:8]}"[:31]


def _already_placed(signal_id: str) -> bool:
    con = sqlite3.connect(_store.DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM trades WHERE signal_id=?", (signal_id,)).fetchone()[0]
    con.close()
    return n > 0


def _cooldown_ok(strategy: str, direction: str) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=cfg.COOLDOWN_MINUTES)).isoformat()
    con = sqlite3.connect(_store.DB_PATH)
    n = con.execute(
        "SELECT COUNT(*) FROM trades WHERE strategy=? AND direction=? AND status='CLOSED' AND exit_ts_utc>?",
        (strategy, direction, cutoff),
    ).fetchone()[0]
    con.close()
    return n == 0


def _vol_regime_now() -> str:
    """Detect regime from current M5 data; fallback to 'normal'."""
    try:
        df = mt5_client.get_ohlc(cfg.SYMBOL, "M5", 50)
        if not df.empty:
            return detect_regime(df, cfg.REGIME_ATR_PERIOD,
                                 cfg.REGIME_VOL_MULTIPLIER, cfg.REGIME_RANGE_MULTIPLIER)
    except Exception:
        pass
    return "normal"


# ── Gate sequence + order placement ──────────────────────────────────────────

def try_execute(signal: Signal) -> None:
    """Try to place an order for a DETECTED signal. All outcomes written to DB."""

    # Idempotency
    if _already_placed(signal.signal_id):
        log.debug("Signal %s already placed — skip", signal.signal_id[:8])
        return

    # ── Gate 5: MT5 connection ────────────────────────────────────────────────
    if not mt5_client.is_connected():
        log.critical("MT5 disconnected — attempting soft reconnect")
        if not mt5_client.reconnect():
            log.critical("Reconnect failed — skipping entry this tick")
            update_signal_status(signal.signal_id, "SKIPPED_ORDER_REJECTED", "MT5_DISCONNECTED")
            return
        log.info("Reconnect succeeded")

    # ── Gate 1: Position / pending gate ──────────────────────────────────────
    active_positions = [p for m in ALL_MAGICS for p in mt5_client.get_positions(m)]
    active_orders    = [o for m in ALL_MAGICS for o in mt5_client.get_pending_orders(m)]
    if active_positions or active_orders:
        update_signal_status(
            signal.signal_id, "SKIPPED_POSITION_OPEN",
            f"pos={len(active_positions)} pend={len(active_orders)}",
        )
        return

    # ── Gate 2: Cooldown ──────────────────────────────────────────────────────
    if not _cooldown_ok(signal.strategy, signal.direction):
        update_signal_status(signal.signal_id, "SKIPPED_COOLDOWN",
                             f"cooldown={cfg.COOLDOWN_MINUTES}min")
        return

    # ── Gate 3: Spread ────────────────────────────────────────────────────────
    spread = mt5_client.get_spread_pips()
    if spread > cfg.MAX_SPREAD_PIPS:
        update_signal_status(signal.signal_id, "SKIPPED_SPREAD", f"spread={spread:.1f}")
        return

    # ── Gate 4: SL width (double-check) ──────────────────────────────────────
    if signal.sl_pips > cfg.SL_MAX_PIPS:
        update_signal_status(signal.signal_id, "SKIPPED_SL_TOO_WIDE",
                             f"sl_pips={signal.sl_pips:.1f}")
        return

    # ── News tagging (non-blocking) ───────────────────────────────────────────
    now_utc = datetime.now(timezone.utc)
    news_result = is_red_news_window(now_utc)
    news_flag = bool(news_result) if news_result is not None else False
    news_known = news_result is not None

    # ── Vol regime ────────────────────────────────────────────────────────────
    vol_regime = signal.context.get("regime") or _vol_regime_now()

    # ── Place order ───────────────────────────────────────────────────────────
    comment = _make_comment(signal.strategy, signal.signal_id)
    magic = _STRATEGY_TO_MAGIC.get(signal.strategy, 20001)

    if signal.entry_type == "MARKET":
        result = mt5_client.place_market(
            direction=signal.direction,
            lot=cfg.LOT,
            sl=signal.sl,
            tp=signal.tp2,   # TP2 on MT5; TP1 managed in software
            magic=magic,
            comment=comment,
        )
    else:
        expiry = now_utc + timedelta(minutes=cfg.PENDING_ORDER_EXPIRY_MIN)
        result = mt5_client.place_limit(
            direction=signal.direction,
            price=signal.entry_price,
            lot=cfg.LOT,
            sl=signal.sl,
            tp=signal.tp2,
            magic=magic,
            comment=comment,
            expiry_utc=expiry,
        )

    if not result.success:
        log.error("Order rejected %s %s: retcode=%s %s",
                  signal.strategy, signal.direction, result.retcode, result.comment)
        update_signal_status(signal.signal_id, "SKIPPED_ORDER_REJECTED",
                             f"retcode={result.retcode} {result.comment}"[:120])
        return

    # ── Write trade record ────────────────────────────────────────────────────
    trade_id = uuid.uuid4().hex
    fill_price = result.fill_price or signal.entry_price
    status = "OPEN" if signal.entry_type == "MARKET" else "PENDING"
    event_name = "FILLED" if status == "OPEN" else "PLACED"

    trade = TradeRecord(
        trade_id=trade_id,
        signal_id=signal.signal_id,
        mt5_ticket=result.ticket,
        strategy=signal.strategy,
        direction=signal.direction,
        lot=cfg.LOT,
        entry_price_fill=fill_price,
        entry_ts_utc=now_utc,
        sl_initial=signal.sl,
        sl_current=signal.sl,
        tp1=signal.tp1,
        tp2=signal.tp2,
        status=status,
        news_flag=news_flag,
        vol_regime=vol_regime,
        spread_at_entry_pips=spread,
    )
    insert_trade(trade)
    update_signal_status(signal.signal_id, "EXECUTED")
    insert_event(trade_id, event_name, {
        "ticket": result.ticket,
        "fill_price": fill_price,
        "spread_pips": spread,
        "news_flag": news_flag,
        "news_known": news_known,
    })

    log.info("%s %s %s ticket=%s entry=%.2f sl=%.2f tp1=%.2f tp2=%.2f spread=%.1f",
             event_name, signal.strategy, signal.direction, result.ticket,
             fill_price, signal.sl, signal.tp1, signal.tp2, spread)


# ── Pending order lifecycle + orphan recovery ─────────────────────────────────

def reconcile_pending_and_orphans() -> None:
    """Run every tick: reconcile pending fills/expiries and recover orphan positions."""
    open_trades = get_open_trades()

    # ── Pending reconciliation ────────────────────────────────────────────────
    all_positions = {p.ticket: p for m in ALL_MAGICS for p in mt5_client.get_positions(m)}
    all_orders    = {o.ticket for m in ALL_MAGICS for o in mt5_client.get_pending_orders(m)}

    for trade in open_trades:
        if trade.status != "PENDING":
            continue
        ticket = trade.mt5_ticket
        if ticket in all_positions:
            pos = all_positions[ticket]
            fp = pos.price_open
            update_trade(trade.trade_id, status="OPEN",
                         entry_price_fill=fp, entry_ts_utc=datetime.now(timezone.utc))
            insert_event(trade.trade_id, "FILLED", {"fill_price": fp, "ticket": ticket})
            log.info("PENDING→OPEN ticket=%d fill=%.2f", ticket, fp)
        elif ticket not in all_orders:
            update_trade(trade.trade_id, status="CANCELLED",
                         exit_reason="EXPIRED", exit_ts_utc=datetime.now(timezone.utc))
            insert_event(trade.trade_id, "EXPIRED", {"ticket": ticket})
            log.info("Pending expired ticket=%d strategy=%s", ticket, trade.strategy)

    # ── Orphan recovery ───────────────────────────────────────────────────────
    db_tickets = {t.mt5_ticket for t in open_trades}
    seen = set()
    for magic in ALL_MAGICS:
        for pos in mt5_client.get_positions(magic):
            if pos.ticket not in db_tickets and pos.ticket not in seen:
                seen.add(pos.ticket)
                _recover_orphan(pos, magic)


def _recover_orphan(pos, magic: int) -> None:
    strategy = _MAGIC_TO_STRATEGY.get(magic, "S1")
    direction = "LONG" if pos.type == 0 else "SHORT"
    trade_id = uuid.uuid4().hex

    trade = TradeRecord(
        trade_id=trade_id,
        signal_id=None,   # no matching signal for crash-recovered positions
        mt5_ticket=pos.ticket,
        strategy=strategy,
        direction=direction,
        lot=pos.volume,
        entry_price_fill=pos.price_open,
        entry_ts_utc=datetime.fromtimestamp(pos.time, tz=timezone.utc),
        sl_initial=pos.sl,
        sl_current=pos.sl,
        tp1=pos.tp,
        tp2=pos.tp,
        status="OPEN",
    )
    insert_trade(trade)
    insert_event(trade_id, "RECOVERED", {
        "ticket": pos.ticket,
        "price_open": pos.price_open,
        "volume": pos.volume,
        "comment": pos.comment,
        "magic": magic,
    })
    log.warning("RECOVERED orphan ticket=%d strategy=%s direction=%s entry=%.2f",
                pos.ticket, strategy, direction, pos.price_open)
