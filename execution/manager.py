"""SPEC E position lifecycle: closed-M1 hits, net BE, trailing and journal."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import mt5_client
from config import cfg
from core.models import TradeRecord
from core.store import get_open_trades, insert_event, update_trade
from indicators.structure import find_swings

log = logging.getLogger(__name__)

_PIP = cfg.PIP
_ALL_MAGICS = [20001, 20002, 20003, 20004]
_ACTIVE_STATUSES = {"OPEN", "PARTIAL"}
_MAE_MFE_THRESH = 0.1
_DEAL_ENTRY_OUT = 1
_DEAL_ENTRY_OUT_BY = 3
_EXIT_TOLERANCE_PIPS = 2
_BE_MAX_RETRIES = 10

_be_pending: dict[str, float] = {}
_be_retries: dict[str, int] = {}


def _friday_flat_minutes() -> int:
    hour, minute = cfg.FRIDAY_FLAT_UTC.split(":")
    return int(hour) * 60 + int(minute)


_FRIDAY_FLAT_MIN = _friday_flat_minutes()


def manage_open_trades() -> None:
    """Manage every active DB trade from one closed-M1 snapshot."""
    trades = [trade for trade in get_open_trades() if trade.status in _ACTIVE_STATUSES]
    if not trades:
        return

    positions: dict[int, Any] = {}
    for magic in _ALL_MAGICS:
        for position in mt5_client.get_positions(magic):
            positions[position.ticket] = position

    closed_m1 = mt5_client.get_closed_ohlc("M1", count=90)
    now_utc = datetime.now(timezone.utc)
    for trade in trades:
        try:
            _manage_one(trade, positions, now_utc, closed_m1)
        except Exception as exc:
            log.error("Manager error trade=%s ticket=%s: %s",
                      trade.trade_id[:8], trade.mt5_ticket, exc, exc_info=True)


def _manage_one(trade: TradeRecord, positions: dict, now_utc: datetime,
                closed_m1: pd.DataFrame | None = None) -> None:
    bars = closed_m1 if closed_m1 is not None else mt5_client.get_closed_ohlc("M1", 90)
    bars = _bars_since_entry(trade, bars)
    if not bars.empty:
        _update_mae_mfe_from_bars(trade, bars)

    position = positions.get(trade.mt5_ticket)
    if position is None:
        _handle_exit(trade, now_utc, default_reason="closed_externally")
        return

    tick = mt5_client.get_tick()
    if _is_timeout(trade, now_utc):
        _close_for_reason(trade, now_utc, "timeout", "TIMEOUT_CLOSE", tick)
        return
    if _is_friday_flat(now_utc):
        _close_for_reason(trade, now_utc, "friday_flat", "FRIDAY_FLAT_CLOSE", tick)
        return
    if bars.empty:
        return

    last_bar = bars.iloc[-1]
    if _level_hit(trade.direction, last_bar, trade.tp_final or trade.tp2):
        _close_for_reason(trade, now_utc, "tp_final", "TP_FINAL_CLOSE", tick)
        return

    if trade.status == "OPEN":
        if trade.tp1_hit or _level_hit(trade.direction, last_bar, trade.tp1):
            if not trade.tp1_hit:
                trade.tp1_hit = True
                trade.lifecycle_state = "TP1_HIT"
                update_trade(trade.trade_id, tp1_hit=True, lifecycle_state="TP1_HIT")
                insert_event(trade.trade_id, "TP1_HIT", {
                    "bar_time": str(last_bar["time"]), "tp1": trade.tp1,
                })
            _execute_tp1_partial(trade)
        return

    # PARTIAL: finish a pending BE first; trailing is forbidden before BE.
    if trade.sl_current == trade.sl_initial or trade.lifecycle_state == "TP1_HIT":
        if trade.trade_id in _be_pending:
            _retry_be_modify(trade)
        else:
            _stateless_be_recheck(trade)
        return
    _apply_trailing(trade, bars, tick)


def _close_for_reason(trade: TradeRecord, now_utc: datetime, reason: str,
                      event: str, fallback_tick) -> None:
    result = mt5_client.close_position_full(trade.mt5_ticket)
    if result.success:
        _finalize_close(trade, now_utc, reason, fallback_tick=fallback_tick)
        insert_event(trade.trade_id, event, {"ticket": trade.mt5_ticket, "reason": reason})
        log.info("%s ticket=%d", event, trade.mt5_ticket)
    else:
        insert_event(trade.trade_id, "CLOSE_FAIL", {
            "reason": reason, "retcode": result.retcode,
        })


def _execute_tp1_partial(trade: TradeRecord) -> None:
    close_lot = round(trade.lot * cfg.PARTIAL_CLOSE_PCT / 100, 2)
    if close_lot < 0.01:
        insert_event(trade.trade_id, "TP1_PARTIAL_FAIL", {"reason": "below_min_lot"})
        return

    result = mt5_client.close_position_partial(trade.mt5_ticket, close_lot)
    if not result.success:
        update_trade(trade.trade_id, lifecycle_state="TP1_HIT", tp1_hit=True)
        insert_event(trade.trade_id, "TP1_PARTIAL_FAIL", {
            "lot": close_lot, "retcode": result.retcode,
        })
        log.warning("TP1 partial rejected ticket=%d retcode=%s",
                    trade.mt5_ticket, result.retcode)
        return

    fill_price = result.fill_price or trade.tp1
    trade.status = "PARTIAL"
    trade.partial_close_price = fill_price
    trade.lifecycle_state = "TP1_HIT"
    update_trade(
        trade.trade_id,
        status="PARTIAL",
        lifecycle_state="TP1_HIT",
        tp1_hit=True,
        partial_close_price=fill_price,
    )
    insert_event(trade.trade_id, "TP1_PARTIAL", {
        "lot": close_lot, "fill_price": fill_price, "ticket": trade.mt5_ticket,
    })
    log.info("TP1_PARTIAL ticket=%d lot=%.2f price=%s",
             trade.mt5_ticket, close_lot, fill_price)
    _activate_be(trade)


def _activate_be(trade: TradeRecord) -> None:
    be_price = _be_price(trade)
    if mt5_client.modify_position_sl(trade.mt5_ticket, be_price):
        trade.sl_current = be_price
        trade.lifecycle_state = "BE_ACTIVE"
        update_trade(
            trade.trade_id,
            status="PARTIAL",
            lifecycle_state="BE_ACTIVE",
            sl_current=be_price,
            be_target=None,
            be_retries=0,
        )
        insert_event(trade.trade_id, "SL_TO_BE_NET", {
            "new_sl": be_price,
            "cost_buffer_pips": cfg.BE_COST_BUFFER_PIPS,
        })
        log.info("SL_TO_BE_NET ticket=%d new_sl=%.3f", trade.mt5_ticket, be_price)
        return

    _be_pending[trade.trade_id] = be_price
    _be_retries[trade.trade_id] = 1
    update_trade(
        trade.trade_id,
        status="PARTIAL",
        lifecycle_state="TP1_HIT",
        be_target=be_price,
        be_retries=1,
    )
    insert_event(trade.trade_id, "MODIFY_FAIL", {"attempted_sl": be_price, "retry": 1})


def _retry_be_modify(trade: TradeRecord) -> None:
    be_price = _be_pending.get(trade.trade_id)
    if be_price is None:
        return
    attempt = _be_retries.get(trade.trade_id, 0) + 1
    if mt5_client.modify_position_sl(trade.mt5_ticket, be_price):
        update_trade(
            trade.trade_id,
            lifecycle_state="BE_ACTIVE",
            sl_current=be_price,
            be_target=None,
            be_retries=0,
        )
        insert_event(trade.trade_id, "SL_TO_BE_NET", {"new_sl": be_price, "retry": attempt})
        _be_pending.pop(trade.trade_id, None)
        _be_retries.pop(trade.trade_id, None)
        return

    _be_retries[trade.trade_id] = attempt
    update_trade(trade.trade_id, be_retries=attempt)
    if attempt >= _BE_MAX_RETRIES:
        insert_event(trade.trade_id, "BE_GIVEUP", {"attempted_sl": be_price, "retries": attempt})
        _be_pending.pop(trade.trade_id, None)
        _be_retries.pop(trade.trade_id, None)
    else:
        insert_event(trade.trade_id, "MODIFY_FAIL", {"attempted_sl": be_price, "retry": attempt})


def _stateless_be_recheck(trade: TradeRecord) -> None:
    if trade.be_retries >= _BE_MAX_RETRIES:
        return
    be_price = trade.be_target or _be_price(trade)
    attempt = trade.be_retries + 1
    if mt5_client.modify_position_sl(trade.mt5_ticket, be_price):
        update_trade(
            trade.trade_id,
            lifecycle_state="BE_ACTIVE",
            sl_current=be_price,
            be_target=None,
            be_retries=0,
            tp1_hit=True,
        )
        insert_event(trade.trade_id, "SL_TO_BE_NET", {"new_sl": be_price, "retry": attempt})
        return

    update_trade(
        trade.trade_id,
        lifecycle_state="TP1_HIT",
        be_target=be_price,
        be_retries=attempt,
        tp1_hit=True,
    )
    _be_pending[trade.trade_id] = be_price
    _be_retries[trade.trade_id] = attempt
    if attempt >= _BE_MAX_RETRIES:
        insert_event(trade.trade_id, "BE_GIVEUP", {"attempted_sl": be_price, "retries": attempt})
        _be_pending.pop(trade.trade_id, None)
        _be_retries.pop(trade.trade_id, None)
    else:
        insert_event(trade.trade_id, "MODIFY_FAIL", {"attempted_sl": be_price, "retry": attempt})


def _apply_trailing(trade: TradeRecord, bars: pd.DataFrame, tick) -> None:
    candidate = _trailing_candidate(trade, bars)
    if candidate is None:
        return
    current = float(trade.sl_current)
    improves = (candidate >= current + cfg.POINT if trade.direction == "LONG"
                else candidate <= current - cfg.POINT)
    if not improves:
        return

    # Never submit a stop on the wrong side of the current executable price.
    if tick is not None:
        valid_live = (candidate < tick.bid if trade.direction == "LONG"
                      else candidate > tick.ask)
        if not valid_live:
            return
    if not mt5_client.modify_position_sl(trade.mt5_ticket, candidate):
        insert_event(trade.trade_id, "TRAIL_MODIFY_FAIL", {"attempted_sl": candidate})
        return

    update_trade(trade.trade_id, sl_current=candidate, lifecycle_state="TRAILING")
    trade.sl_current = candidate
    trade.lifecycle_state = "TRAILING"
    insert_event(trade.trade_id, "TRAIL_UPDATE", {
        "new_sl": candidate, "mode": cfg.TRAIL_MODE,
    })


def _trailing_candidate(trade: TradeRecord, bars: pd.DataFrame) -> float | None:
    swings = find_swings(bars, lookback=cfg.TRAIL_SWING_LOOKBACK)
    wanted = "LOW" if trade.direction == "LONG" else "HIGH"
    matching = [swing for swing in swings if swing.type == wanted]
    if not matching:
        return None

    swing = matching[-1]
    buffer_price = cfg.TRAIL_BUFFER_PIPS * _PIP
    structure_sl = (swing.price - buffer_price if trade.direction == "LONG"
                    else swing.price + buffer_price)
    candidate = structure_sl
    if cfg.TRAIL_MODE == "atr":
        atr = _atr(bars, cfg.TRAIL_ATR_PERIOD)
        if atr is None:
            return None
        close = float(bars.iloc[-1]["close"])
        atr_sl = (close - cfg.ATR_MULT * atr if trade.direction == "LONG"
                  else close + cfg.ATR_MULT * atr)
        candidate = max(structure_sl, atr_sl) if trade.direction == "LONG" else min(structure_sl, atr_sl)
    elif cfg.TRAIL_MODE != "structure":
        log.error("Unknown TRAIL_MODE=%s; trailing disabled", cfg.TRAIL_MODE)
        return None
    return round(candidate, cfg.DIGITS)


def _atr(bars: pd.DataFrame, period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    previous_close = bars["close"].shift(1)
    true_range = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - previous_close).abs(),
        (bars["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    value = true_range.rolling(period).mean().iloc[-1]
    return None if pd.isna(value) else float(value)


def _finalize_close(trade: TradeRecord, now_utc: datetime, exit_reason: str,
                    fallback_tick=None, deals=None) -> None:
    deals = mt5_client.get_deal_history(trade.mt5_ticket) if deals is None else deals
    if deals:
        pnl_pips, pnl_net_usd = _compute_pnl(trade, deals)
        pnl_gross_usd = round(sum(float(getattr(d, "profit", 0.0)) for d in deals), 2)
        commission_signed = round(sum(float(getattr(d, "commission", 0.0)) for d in deals), 2)
        commission_usd = round(-commission_signed, 2)
        swap_usd = round(sum(float(getattr(d, "swap", 0.0)) for d in deals), 2)
        exit_ts = _exit_time_from_deals(deals) or now_utc
    else:
        pnl_pips = round(_excursion_pips(trade, fallback_tick), 1) if fallback_tick else 0.0
        pnl_net_usd = None
        pnl_gross_usd = None
        commission_usd = 0.0
        swap_usd = 0.0
        exit_ts = now_utc

    realized_r = round((pnl_pips or 0.0) / cfg.SL_CAP_PIPS, 4)
    risk_usd = cfg.SL_CAP_PIPS * cfg.PIP_VALUE_PER_LOT_USD * trade.lot
    if pnl_net_usd is not None and risk_usd > 0:
        realized_r_net = round(pnl_net_usd / risk_usd, 4)
    else:
        realized_r_net = round(((pnl_pips or 0.0) - cfg.BE_COST_BUFFER_PIPS)
                               / cfg.SL_CAP_PIPS, 4)
    duration_s = max(0, int((exit_ts - _aware(trade.entry_ts_utc)).total_seconds()))

    update_trade(
        trade.trade_id,
        status="CLOSED",
        lifecycle_state="CLOSED",
        exit_reason=exit_reason,
        exit_ts_utc=exit_ts,
        pnl_pips=pnl_pips,
        pnl_usd=pnl_net_usd,
        pnl_gross_usd=pnl_gross_usd,
        commission_usd=commission_usd,
        swap_usd=swap_usd,
        pnl_net_usd=pnl_net_usd,
        realized_r=realized_r,
        realized_r_net=realized_r_net,
        duration_s=duration_s,
    )
    _cleanup(trade.trade_id)


def _handle_exit(trade: TradeRecord, now_utc: datetime,
                 default_reason: str = "closed_externally") -> None:
    deals = mt5_client.get_deal_history(trade.mt5_ticket)
    exit_price = _exit_price_from_deals(deals)
    reason = _classify_exit(trade, exit_price, default_reason)
    _finalize_close(trade, now_utc, reason, deals=deals)


def reconcile_missing_position(trade: TradeRecord,
                               now_utc: datetime | None = None) -> None:
    """Idempotently finalize an active DB trade absent from MT5."""
    if trade.status not in _ACTIVE_STATUSES:
        return
    _handle_exit(trade, now_utc or datetime.now(timezone.utc), "closed_externally")


def _classify_exit(trade: TradeRecord, exit_price: float | None,
                   default_reason: str = "closed_externally") -> str:
    if exit_price is None:
        return default_reason
    tolerance = _EXIT_TOLERANCE_PIPS * _PIP
    if abs(exit_price - (trade.tp_final or trade.tp2)) <= tolerance:
        return "tp_final"
    if abs(exit_price - trade.sl_current) <= tolerance:
        if trade.lifecycle_state == "TRAILING":
            return "trail"
        if trade.tp1_hit or trade.status == "PARTIAL":
            return "be"
        return "sl"
    if abs(exit_price - trade.sl_initial) <= tolerance:
        return "sl"
    return default_reason


def _exit_price_from_deals(deals) -> float | None:
    outgoing = [d for d in deals if d.entry in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_OUT_BY)]
    return float(sorted(outgoing, key=lambda d: d.time)[-1].price) if outgoing else None


def _exit_time_from_deals(deals) -> datetime | None:
    outgoing = [d for d in deals if d.entry in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_OUT_BY)]
    if not outgoing:
        return None
    return datetime.fromtimestamp(sorted(outgoing, key=lambda d: d.time)[-1].time,
                                  tz=timezone.utc)


def _compute_pnl(trade: TradeRecord, deals) -> tuple[float | None, float | None]:
    outgoing = [d for d in deals if d.entry in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_OUT_BY)]
    total_volume = sum(float(d.volume) for d in outgoing)
    if not outgoing or total_volume <= 0:
        return None, None
    weighted_exit = sum(float(d.price) * float(d.volume) for d in outgoing) / total_volume
    sign = 1 if trade.direction == "LONG" else -1
    pnl_pips = sign * (weighted_exit - trade.entry_price_fill) / _PIP
    pnl_net_usd = sum(
        float(getattr(d, "profit", 0.0))
        + float(getattr(d, "commission", 0.0))
        + float(getattr(d, "swap", 0.0))
        for d in deals
    )
    return round(pnl_pips, 1), round(pnl_net_usd, 2)


def _bars_since_entry(trade: TradeRecord, bars: pd.DataFrame | None) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame(columns=bars.columns if bars is not None else None)
    result = bars.copy()
    if "time" in result.columns and trade.entry_ts_utc is not None:
        entry = _aware(trade.entry_ts_utc)
        times = pd.to_datetime(result["time"], utc=True)
        result = result.loc[times >= entry].copy()
    result = result.reset_index(drop=True)
    result.attrs["closed_only"] = True
    return result


def _update_mae_mfe_from_bars(trade: TradeRecord, bars: pd.DataFrame) -> None:
    entry = trade.entry_price_fill
    if trade.direction == "LONG":
        favorable = (float(bars["high"].max()) - entry) / _PIP
        adverse = (entry - float(bars["low"].min())) / _PIP
    else:
        favorable = (entry - float(bars["low"].min())) / _PIP
        adverse = (float(bars["high"].max()) - entry) / _PIP
    new_mfe = max(trade.mfe_pips, favorable, 0.0)
    new_mae = max(trade.mae_pips, adverse, 0.0)
    updates = {}
    if new_mfe - trade.mfe_pips >= _MAE_MFE_THRESH:
        updates["mfe_pips"] = round(new_mfe, 1)
        trade.mfe_pips = updates["mfe_pips"]
    if new_mae - trade.mae_pips >= _MAE_MFE_THRESH:
        updates["mae_pips"] = round(new_mae, 1)
        trade.mae_pips = updates["mae_pips"]
    if updates:
        update_trade(trade.trade_id, **updates)


def _update_mae_mfe(trade: TradeRecord, tick) -> None:
    """Legacy unit-test helper; production uses closed-M1 bars only."""
    excursion = _excursion_pips(trade, tick)
    new_mfe = max(trade.mfe_pips, excursion, 0.0)
    new_mae = max(trade.mae_pips, -excursion, 0.0)
    update_trade(trade.trade_id, mfe_pips=round(new_mfe, 1), mae_pips=round(new_mae, 1))


def _level_hit(direction: str, bar, level: float) -> bool:
    return float(bar["high"]) >= level if direction == "LONG" else float(bar["low"]) <= level


def _excursion_pips(trade: TradeRecord, tick) -> float:
    if trade.direction == "LONG":
        return (tick.bid - trade.entry_price_fill) / _PIP
    return (trade.entry_price_fill - tick.ask) / _PIP


def _be_price(trade: TradeRecord) -> float:
    buffer_price = cfg.BE_COST_BUFFER_PIPS * _PIP
    price = (trade.entry_price_fill + buffer_price if trade.direction == "LONG"
             else trade.entry_price_fill - buffer_price)
    return round(price, cfg.DIGITS)


def _is_timeout(trade: TradeRecord, now_utc: datetime) -> bool:
    if trade.entry_ts_utc is None:
        return False
    return (now_utc - _aware(trade.entry_ts_utc)).total_seconds() / 60 > cfg.TRADE_MAX_AGE_MINUTES


def _is_friday_flat(now_utc: datetime) -> bool:
    return (now_utc.weekday() == 4
            and now_utc.hour * 60 + now_utc.minute >= _FRIDAY_FLAT_MIN)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _cleanup(trade_id: str) -> None:
    _be_pending.pop(trade_id, None)
    _be_retries.pop(trade_id, None)
    update_trade(trade_id, be_target=None, be_retries=0)
