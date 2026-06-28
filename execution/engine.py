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
_PIP = cfg.PIP
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


def _execution_levels(signal: Signal) -> tuple[float, float, float] | None:
    """Return live entry, fixed 20-pip SL and SPEC E final target."""
    if signal.entry_type == "MARKET":
        tick = mt5_client.get_tick()
        if tick is None:
            # place_market validates again against the live executable bid/ask.
            entry = signal.entry_price
            log.warning("No preflight tick for %s %s; using signal entry %.3f",
                        signal.strategy, signal.direction, entry)
        else:
            entry = tick.ask if signal.direction == "LONG" else tick.bid
    else:
        entry = signal.entry_price
    risk = cfg.SL_CAP_PIPS * cfg.PIP
    is_s3 = (signal.setup or "").lower() == "s3_mean_reversion"
    if signal.direction == "LONG":
        sl = entry - risk
        if is_s3:
            if signal.tp_final <= entry:
                log.error("Invalid S3 EMA target: entry=%.3f ema=%.3f", entry, signal.tp_final)
                return None
            tp = signal.tp_final
        else:
            tp = entry + cfg.TP_FINAL_R * risk
    else:
        sl = entry + risk
        if is_s3:
            if signal.tp_final >= entry:
                log.error("Invalid S3 EMA target: entry=%.3f ema=%.3f", entry, signal.tp_final)
                return None
            tp = signal.tp_final
        else:
            tp = entry - cfg.TP_FINAL_R * risk
    return round(entry, cfg.DIGITS), round(sl, cfg.DIGITS), round(tp, cfg.DIGITS)


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

    # Measurement tags. Only the high runaway ceiling remains a hard gate.
    active_positions = [p for m in ALL_MAGICS for p in mt5_client.get_positions(m)]
    active_orders    = [o for m in ALL_MAGICS for o in mt5_client.get_pending_orders(m)]
    would_block_position = bool(active_positions or active_orders)
    if len(active_positions) + len(active_orders) >= cfg.MAX_OPEN_POSITIONS:
        update_signal_status(
            signal.signal_id, "SKIPPED_RUNAWAY_LIMIT",
            f"active={len(active_positions) + len(active_orders)} limit={cfg.MAX_OPEN_POSITIONS}",
        )
        log.critical("RUNAWAY_LIMIT signal=%s active=%d limit=%d",
                     signal.signal_id[:8], len(active_positions) + len(active_orders),
                     cfg.MAX_OPEN_POSITIONS)
        return

    would_block_cooldown = not _cooldown_ok(signal.strategy, signal.direction)

    spread = mt5_client.get_spread_pips()
    would_block_spread = spread > cfg.MAX_SPREAD_PIPS

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
    levels = _execution_levels(signal)
    if levels is None:
        update_signal_status(signal.signal_id, "SKIPPED_ORDER_REJECTED", "NO_TICK_FOR_STOPS")
        return
    execution_entry, execution_sl, execution_tp = levels
    execution_tp1 = round(
        execution_entry + (cfg.TP1_PIPS * cfg.PIP if signal.direction == "LONG"
                           else -cfg.TP1_PIPS * cfg.PIP),
        cfg.DIGITS,
    )

    if signal.entry_type == "MARKET":
        result = mt5_client.place_market(
            direction=signal.direction,
            lot=cfg.LOT_SIZE,
            sl=execution_sl,
            tp=execution_tp,
            magic=magic,
            comment=comment,
        )
    else:
        expiry = now_utc + timedelta(minutes=cfg.PENDING_ORDER_EXPIRY_MIN)
        result = mt5_client.place_limit(
            direction=signal.direction,
            price=execution_entry,
            lot=cfg.LOT_SIZE,
            sl=execution_sl,
            tp=execution_tp,
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
    fill_price = result.fill_price or execution_entry
    status = "OPEN" if signal.entry_type == "MARKET" else "PENDING"
    event_name = "FILLED" if status == "OPEN" else "PLACED"

    trade = TradeRecord(
        trade_id=trade_id,
        signal_id=signal.signal_id,
        mt5_ticket=result.ticket,
        strategy=signal.strategy,
        direction=signal.direction,
        lot=cfg.LOT_SIZE,
        entry_price_fill=fill_price,
        entry_ts_utc=now_utc,
        sl_initial=execution_sl,
        sl_current=execution_sl,
        tp1=execution_tp1,
        tp2=execution_tp,
        status=status,
        setup=signal.setup or signal.strategy,
        magic=magic,
        lifecycle_state="OPEN" if status == "OPEN" else "PENDING",
        sl_executed=execution_sl,
        tp_final=execution_tp,
        killzone=signal.killzone,
        htf_bias=str(signal.context.get("htf_bias", "NEUTRAL")),
        bias_aligned=bool(signal.context.get("bias_aligned", False)),
        news_red_active=str(signal.context.get("news_red_active", "unknown")).lower(),
        premium_discount=str(signal.context.get("premium_discount", "EQ")),
        news_flag=news_flag,
        vol_regime=vol_regime,
        spread_at_entry_pips=spread,
        sl_structural_pips=signal.sl_pips,
        would_block_position=would_block_position,
        would_block_cooldown=would_block_cooldown,
        would_block_news=news_flag,
        would_block_spread=would_block_spread,
    )
    insert_trade(trade)
    update_signal_status(signal.signal_id, "EXECUTED")
    insert_event(trade_id, event_name, {
        "ticket": result.ticket,
        "fill_price": fill_price,
        "spread_pips": spread,
        "news_flag": news_flag,
        "news_known": news_known,
        "sl_structural_pips": signal.sl_pips,
        "sl_executed_pips": cfg.SL_MAX_PIPS,
        "setup": signal.setup or signal.strategy,
        "tp_final": execution_tp,
        "would_block_position": would_block_position,
        "would_block_cooldown": would_block_cooldown,
        "would_block_news": news_flag,
        "would_block_spread": would_block_spread,
    })

    log.info("%s %s %s ticket=%s entry=%.2f sl=%.2f tp1=%.2f tp2=%.2f spread=%.1f",
             event_name, signal.strategy, signal.direction, result.ticket,
             fill_price, execution_sl, execution_tp1, execution_tp, spread)


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
