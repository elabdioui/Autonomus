"""Position management: TP1 partial-close, breakeven, TP2/SL/BE/timeout exit detection.

Called every tick for all OPEN and PARTIAL trades. MT5 holds SL + TP2 server-side;
TP1 partial close and breakeven SL move are software-managed here.
"""
import logging
from datetime import datetime, timezone
from typing import Any

import mt5_client
from config import cfg
from core.models import TradeRecord
from core.store import get_open_trades, update_trade, insert_event

log = logging.getLogger(__name__)

_PIP = 0.10
_ALL_MAGICS = [20001, 20002, 20003, 20004]
_BE_BUFFER_PIPS = 0.5            # SL placed this far in-profit from entry at BE move
_MAE_MFE_THRESH = 0.5            # min change (pips) before writing MAE/MFE to DB
_DEAL_ENTRY_OUT    = 1           # mt5 DEAL_ENTRY_OUT
_DEAL_ENTRY_OUT_BY = 3           # mt5 DEAL_ENTRY_OUT_BY
_EXIT_TOLERANCE_PIPS = 2         # price tolerance for classifying exit reason

# ── Module-level state (persists across ticks) ────────────────────────────────
# trade_id → target BE price (queued when modify failed)
_be_pending:  dict[str, float] = {}
# trade_id → number of BE-modify attempts so far
_be_retries:  dict[str, int]   = {}


def _friday_flat_minutes() -> int:
    h, m = cfg.FRIDAY_FLAT_UTC.split(":")
    return int(h) * 60 + int(m)

_FRIDAY_FLAT_MIN = _friday_flat_minutes()


# ── Public entry point ────────────────────────────────────────────────────────

def manage_open_trades() -> None:
    """Called every tick. Manages all OPEN/PARTIAL trades regardless of killzone."""
    trades = [t for t in get_open_trades() if t.status in ("OPEN", "PARTIAL")]
    if not trades:
        return

    # Single MT5 position query — indexed by ticket
    all_positions: dict[int, Any] = {}
    for magic in _ALL_MAGICS:
        for pos in mt5_client.get_positions(magic):
            all_positions[pos.ticket] = pos

    now_utc = datetime.now(timezone.utc)

    for trade in trades:
        try:
            _manage_one(trade, all_positions, now_utc)
        except Exception as exc:
            log.error("Manager error trade=%s ticket=%s: %s",
                      trade.trade_id[:8], trade.mt5_ticket, exc, exc_info=True)


# ── Per-trade management ──────────────────────────────────────────────────────

def _manage_one(trade: TradeRecord, positions: dict, now_utc: datetime) -> None:
    pos = positions.get(trade.mt5_ticket)

    # ── Position gone → exit detection ───────────────────────────────────────
    if pos is None:
        _handle_exit(trade, now_utc)
        return

    tick = mt5_client.get_tick()
    if tick is None:
        return

    # ── MAE / MFE update ─────────────────────────────────────────────────────
    _update_mae_mfe(trade, tick)

    # ── Retry any pending BE modify ───────────────────────────────────────────
    if trade.trade_id in _be_pending:
        _retry_be_modify(trade)

    # ── Timeout check ─────────────────────────────────────────────────────────
    if _is_timeout(trade, now_utc):
        result = mt5_client.close_position_full(trade.mt5_ticket)
        if result.success:
            pnl = _excursion_pips(trade, tick)
            update_trade(trade.trade_id, status="CLOSED",
                         exit_reason="TIMEOUT", exit_ts_utc=now_utc,
                         pnl_pips=round(pnl, 1))
            insert_event(trade.trade_id, "TIMEOUT_CLOSE",
                         {"ticket": trade.mt5_ticket, "reason": "timeout"})
            _cleanup(trade.trade_id)
            log.info("TIMEOUT_CLOSE ticket=%d", trade.mt5_ticket)
        else:
            insert_event(trade.trade_id, "CLOSE_FAIL",
                         {"reason": "timeout", "retcode": result.retcode})
        return

    # ── Friday flat check ─────────────────────────────────────────────────────
    if _is_friday_flat(now_utc):
        result = mt5_client.close_position_full(trade.mt5_ticket)
        if result.success:
            pnl = _excursion_pips(trade, tick)
            update_trade(trade.trade_id, status="CLOSED",
                         exit_reason="TIMEOUT", exit_ts_utc=now_utc,
                         pnl_pips=round(pnl, 1))
            insert_event(trade.trade_id, "TIMEOUT_CLOSE",
                         {"ticket": trade.mt5_ticket, "reason": "friday_flat"})
            _cleanup(trade.trade_id)
            log.info("FRIDAY_FLAT ticket=%d", trade.mt5_ticket)
        else:
            insert_event(trade.trade_id, "CLOSE_FAIL",
                         {"reason": "friday_flat", "retcode": result.retcode})
        return

    # ── TP1 check (OPEN only) ─────────────────────────────────────────────────
    if trade.status == "OPEN":
        _check_tp1(trade, tick)


# ── TP1 partial + breakeven ───────────────────────────────────────────────────

def _check_tp1(trade: TradeRecord, tick) -> None:
    tp1_pips = cfg.S3_TP1_PIPS if trade.strategy == "S3" else cfg.TP1_PIPS
    if _excursion_pips(trade, tick) < tp1_pips:
        return

    # ── Partial close ─────────────────────────────────────────────────────────
    close_lot = round(trade.lot * cfg.PARTIAL_CLOSE_PCT / 100, 2)
    if close_lot >= 0.01:
        result = mt5_client.close_position_partial(trade.mt5_ticket, close_lot)
        if result.success:
            insert_event(trade.trade_id, "TP1_PARTIAL",
                         {"lot": close_lot, "fill_price": result.fill_price,
                          "ticket": trade.mt5_ticket})
            log.info("TP1_PARTIAL ticket=%d lot=%.2f price=%s",
                     trade.mt5_ticket, close_lot, result.fill_price)
        else:
            insert_event(trade.trade_id, "TP1_PARTIAL",
                         {"lot": close_lot, "skipped": True,
                          "retcode": result.retcode, "reason": "partial_rejected"})
            log.warning("TP1 partial rejected ticket=%d retcode=%s",
                        trade.mt5_ticket, result.retcode)
    else:
        insert_event(trade.trade_id, "TP1_PARTIAL",
                     {"skipped": True, "reason": "below_min_lot"})

    # ── Move SL to breakeven (always, regardless of partial result) ───────────
    be_price = _be_price(trade)
    ok = mt5_client.modify_position_sl(trade.mt5_ticket, be_price)
    if ok:
        insert_event(trade.trade_id, "SL_TO_BE",
                     {"new_sl": be_price, "ticket": trade.mt5_ticket})
        update_trade(trade.trade_id, status="PARTIAL", sl_current=be_price)
        log.info("SL_TO_BE ticket=%d new_sl=%.2f", trade.mt5_ticket, be_price)
    else:
        # Queue for retry — mark PARTIAL so we don't try TP1 again
        _be_pending[trade.trade_id] = be_price
        _be_retries[trade.trade_id] = 1
        insert_event(trade.trade_id, "MODIFY_FAIL",
                     {"attempted_sl": be_price, "retry": 1})
        update_trade(trade.trade_id, status="PARTIAL")
        log.warning("SL-to-BE failed ticket=%d — queued retry 1", trade.mt5_ticket)


def _retry_be_modify(trade: TradeRecord) -> None:
    be_price = _be_pending.get(trade.trade_id)
    if be_price is None:
        return

    attempt = _be_retries.get(trade.trade_id, 0) + 1
    ok = mt5_client.modify_position_sl(trade.mt5_ticket, be_price)

    if ok:
        insert_event(trade.trade_id, "SL_TO_BE",
                     {"new_sl": be_price, "retry": attempt})
        update_trade(trade.trade_id, sl_current=be_price)
        _be_pending.pop(trade.trade_id, None)
        _be_retries.pop(trade.trade_id, None)
        log.info("SL-to-BE retry %d succeeded ticket=%d", attempt, trade.mt5_ticket)
    else:
        _be_retries[trade.trade_id] = attempt
        insert_event(trade.trade_id, "MODIFY_FAIL",
                     {"attempted_sl": be_price, "retry": attempt,
                      "note": "keep_retrying"})
        log.error("SL-to-BE retry %d failed ticket=%d — retrying next tick",
                  attempt, trade.mt5_ticket)


# ── Exit detection ────────────────────────────────────────────────────────────

def _handle_exit(trade: TradeRecord, now_utc: datetime) -> None:
    deals = mt5_client.get_deal_history(trade.mt5_ticket)
    pnl_pips, pnl_usd = _compute_pnl(trade, deals)
    exit_price          = _exit_price_from_deals(deals)
    exit_ts             = _exit_time_from_deals(deals) or now_utc
    exit_reason         = _classify_exit(trade, exit_price)

    update_trade(
        trade.trade_id,
        status="CLOSED",
        exit_reason=exit_reason,
        exit_ts_utc=exit_ts,
        pnl_pips=pnl_pips,
        pnl_usd=pnl_usd,
    )
    _cleanup(trade.trade_id)
    log.info("CLOSED ticket=%d reason=%s pnl_pips=%s pnl_usd=%s",
             trade.mt5_ticket, exit_reason, pnl_pips, pnl_usd)


def _classify_exit(trade: TradeRecord, exit_price: float | None) -> str:
    if exit_price is None:
        return "MANUAL"
    tol = _EXIT_TOLERANCE_PIPS * _PIP

    if abs(exit_price - trade.tp2) <= tol:
        return "TP2"

    # BE: sl_current moved from initial (TP1 was hit)
    if trade.sl_current != trade.sl_initial:
        if abs(exit_price - trade.sl_current) <= tol:
            return "BE"

    if abs(exit_price - trade.sl_initial) <= tol:
        return "SL"

    log.warning("MANUAL exit: ticket=%d exit_price=%.2f tp2=%.2f sl=%.2f be=%.2f",
                trade.mt5_ticket, exit_price, trade.tp2,
                trade.sl_initial, trade.sl_current)
    return "MANUAL"


def _exit_price_from_deals(deals) -> float | None:
    out = [d for d in deals if d.entry in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_OUT_BY)]
    if not out:
        return None
    return float(sorted(out, key=lambda d: d.time)[-1].price)


def _exit_time_from_deals(deals) -> datetime | None:
    out = [d for d in deals if d.entry in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_OUT_BY)]
    if not out:
        return None
    ts = sorted(out, key=lambda d: d.time)[-1].time
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _compute_pnl(trade: TradeRecord, deals) -> tuple[float | None, float | None]:
    if not deals:
        return None, None
    try:
        out = [d for d in deals if d.entry in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_OUT_BY)]
        if not out:
            return None, None

        total_vol = sum(d.volume for d in out)
        if total_vol == 0:
            return None, None

        weighted_exit = sum(d.price * d.volume for d in out) / total_vol
        sign = 1 if trade.direction == "LONG" else -1
        pnl_pips = sign * (weighted_exit - trade.entry_price_fill) / _PIP

        pnl_usd = sum(
            getattr(d, "profit", 0.0) + getattr(d, "commission", 0.0)
            + getattr(d, "swap", 0.0)
            for d in deals
        )
        return round(pnl_pips, 1), round(pnl_usd, 2)
    except Exception as exc:
        log.warning("PnL computation failed ticket=%d: %s", trade.mt5_ticket, exc)
        return None, None


# ── MAE / MFE ─────────────────────────────────────────────────────────────────

def _update_mae_mfe(trade: TradeRecord, tick) -> None:
    exc = _excursion_pips(trade, tick)   # positive = favorable

    # MAE = largest adverse move (stored as positive magnitude)
    new_mae = max(trade.mae_pips, -min(0.0, exc))
    # MFE = largest favorable move (stored as positive magnitude)
    new_mfe = max(trade.mfe_pips, max(0.0, exc))

    updates: dict = {}
    if new_mae - trade.mae_pips >= _MAE_MFE_THRESH:
        updates["mae_pips"] = round(new_mae, 1)
    if new_mfe - trade.mfe_pips >= _MAE_MFE_THRESH:
        updates["mfe_pips"] = round(new_mfe, 1)

    if updates:
        update_trade(trade.trade_id, **updates)
        # Update local object to avoid double-writes on subsequent calls
        if "mae_pips" in updates:
            trade.mae_pips = updates["mae_pips"]
        if "mfe_pips" in updates:
            trade.mfe_pips = updates["mfe_pips"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _excursion_pips(trade: TradeRecord, tick) -> float:
    """Signed excursion in pips from entry. Positive = favorable for the trade."""
    if trade.direction == "LONG":
        return (tick.bid - trade.entry_price_fill) / _PIP
    else:
        return (trade.entry_price_fill - tick.ask) / _PIP


def _be_price(trade: TradeRecord) -> float:
    buf = _BE_BUFFER_PIPS * _PIP
    if trade.direction == "LONG":
        return round(trade.entry_price_fill + buf, 2)
    return round(trade.entry_price_fill - buf, 2)


def _is_timeout(trade: TradeRecord, now_utc: datetime) -> bool:
    if trade.entry_ts_utc is None:
        return False
    ts = trade.entry_ts_utc
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now_utc - ts).total_seconds() / 60 > cfg.TIMEOUT_MINUTES


def _is_friday_flat(now_utc: datetime) -> bool:
    return (now_utc.weekday() == 4
            and now_utc.hour * 60 + now_utc.minute >= _FRIDAY_FLAT_MIN)


def _cleanup(trade_id: str) -> None:
    _be_pending.pop(trade_id, None)
    _be_retries.pop(trade_id, None)
