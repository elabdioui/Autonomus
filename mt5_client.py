"""MT5 connection, OHLC helpers, and order functions.

Startup and reconnect failures are recoverable; the process remains alive.
TP on MT5 orders = TP2; TP1 is managed in software (partial close in SPEC 4).
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from config import cfg

log = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    _MT5_AVAILABLE = False

_TIMEFRAME_MAP: dict = {}
_SUCCESS_CODES = frozenset({10008, 10009})   # PLACED, DONE
_REQUOTE_CODES = frozenset({10004, 10021})   # REQUOTE, PRICE_CHANGED
_INVALID_FILL  = 10030


@dataclass
class OrderResult:
    success: bool
    ticket: int | None = None
    fill_price: float | None = None
    retcode: int | None = None
    comment: str = ""


def _ensure_tf_map() -> None:
    if _MT5_AVAILABLE and not _TIMEFRAME_MAP:
        _TIMEFRAME_MAP.update({
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1,
        })


# ── Connection ────────────────────────────────────────────────────────────────

def _last_error_text() -> str:
    err = mt5.last_error() if _MT5_AVAILABLE else (-1, "MetaTrader5 unavailable")
    return f"code={err[0]} msg={err[1]}"


def _connect_once() -> bool:
    """Perform one complete initialize/login/symbol-select attempt."""
    if not _MT5_AVAILABLE:
        log.error("MetaTrader5 package not installed - waiting for availability")
        return False

    terminal_path = cfg.MT5_TERMINAL_PATH
    if not terminal_path:
        log.error("MT5_TERMINAL_PATH is not set in .env")
        return False
    if not Path(terminal_path).exists():
        log.error("MT5_TERMINAL_PATH does not exist: %s", terminal_path)
        return False

    _ensure_tf_map()
    ok = mt5.initialize(path=terminal_path, login=cfg.MT5_LOGIN,
                        password=cfg.MT5_PASSWORD, server=cfg.MT5_SERVER, timeout=30000)
    if not ok:
        log.error("MT5 initialize() failed - %s", _last_error_text())
        return False

    acc = mt5.account_info()
    if acc is None:
        log.error("MT5 init succeeded but account_info() is None - %s", _last_error_text())
        mt5.shutdown()
        return False
    if acc.login != cfg.MT5_LOGIN:
        log.error("MT5 login mismatch: expected=%s actual=%s", cfg.MT5_LOGIN, acc.login)
        mt5.shutdown()
        return False
    if not mt5.symbol_select(cfg.SYMBOL, True):
        log.error("symbol_select(%s) failed - %s", cfg.SYMBOL, _last_error_text())
        mt5.shutdown()
        return False

    log.info("MT5 connected — account=%s server=%s symbol=%s", acc.login, cfg.MT5_SERVER, cfg.SYMBOL)
    return True


def connect() -> bool:
    """Initial connection with bounded backoff; never terminates the process."""
    attempts = max(1, cfg.MT5_CONNECT_RETRIES)
    for attempt in range(1, attempts + 1):
        if _connect_once():
            return True
        delay = min(cfg.MT5_RETRY_BASE_SECONDS * (2 ** (attempt - 1)),
                    cfg.MT5_RETRY_MAX_SECONDS)
        log.warning("MT5 startup attempt %d/%d failed; retrying in %.1fs",
                    attempt, attempts, delay)
        if attempt < attempts:
            time.sleep(delay)
    log.error("MT5 unavailable after %d startup attempts; bot remains alive", attempts)
    return False


def reconnect() -> bool:
    """Soft reconnect (returns False on failure, never exits). Used mid-session."""
    if not _MT5_AVAILABLE:
        return False
    terminal_path = cfg.MT5_TERMINAL_PATH
    if not terminal_path or not Path(terminal_path).exists():
        log.error("reconnect: MT5_TERMINAL_PATH missing or not found: %s", terminal_path)
        return False
    try:
        mt5.shutdown()
    except Exception:
        pass
    if not _connect_once():
        log.warning("MT5 reconnect failed - %s", _last_error_text())
        return False
    log.info("MT5 state: disconnected -> connected (account=%s symbol=%s)",
             cfg.MT5_LOGIN, cfg.SYMBOL)
    return True


def disconnect() -> None:
    if _MT5_AVAILABLE:
        mt5.shutdown()


def is_connected() -> bool:
    if not _MT5_AVAILABLE:
        return False
    info = mt5.terminal_info()
    return info is not None and info.connected and mt5.account_info() is not None


# ── OHLC ──────────────────────────────────────────────────────────────────────

def get_ohlc(symbol: str, timeframe: str, count: int) -> pd.DataFrame:
    _ensure_tf_map()
    tf = _TIMEFRAME_MAP.get(timeframe)
    if tf is None:
        raise ValueError(f"Unknown timeframe: {timeframe}")
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    if rates is None or len(rates) == 0:
        log.warning("No data for %s %s — %s", symbol, timeframe, mt5.last_error())
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume": "volume"})
    return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def get_current_price(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    return (tick.bid + tick.ask) / 2 if tick else None


def get_spread_pips(symbol: str | None = None) -> float:
    sym = symbol or cfg.SYMBOL
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return 0.0
    return round((tick.ask - tick.bid) / cfg.PIP, 2)


def _validated_prices(direction: str, entry: float, sl: float, tp: float) -> tuple[float, float, str | None]:
    """Round and validate stops against the actual requested entry price."""
    entry = round(float(entry), cfg.DIGITS)
    sl = round(float(sl), cfg.DIGITS)
    tp = round(float(tp), cfg.DIGITS)
    valid = (sl < entry < tp) if direction == "LONG" else (tp < entry < sl)
    if not valid or sl == entry or tp == entry:
        reason = (f"invalid stops direction={direction} entry={entry:.3f} "
                  f"sl={sl:.3f} tp={tp:.3f}")
        log.error(reason)
        return sl, tp, reason
    return sl, tp, None


def get_scalper_timeframes(symbol: str | None = None) -> dict[str, pd.DataFrame]:
    sym = symbol or cfg.SYMBOL
    return {
        "M1":  get_ohlc(sym, "M1",  cfg.OHLC_COUNT_M1),
        "M5":  get_ohlc(sym, "M5",  cfg.OHLC_COUNT_M5),
        "M15": get_ohlc(sym, "M15", cfg.OHLC_COUNT_M15),
        "H1":  get_ohlc(sym, "H1",  cfg.OHLC_COUNT_H1),
        "H4":  get_ohlc(sym, "H4",  cfg.OHLC_COUNT_H4),
    }


# ── Order helpers ─────────────────────────────────────────────────────────────

def _send_with_retry(req: dict) -> "object":
    """Send order; retry once on requote/price-changed; fall back filling mode on INVALID_FILL."""
    res = mt5.order_send(req)
    if res is None:
        return res

    if res.retcode in _REQUOTE_CODES:
        tick = mt5.symbol_info_tick(req.get("symbol", cfg.SYMBOL))
        if tick:
            is_buy = req.get("type") in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_LIMIT,
                                          mt5.ORDER_TYPE_BUY_STOP)
            req["price"] = tick.ask if is_buy else tick.bid
        res = mt5.order_send(req)

    if res and res.retcode == _INVALID_FILL:
        cur = req.get("type_filling", mt5.ORDER_FILLING_IOC)
        req["type_filling"] = (mt5.ORDER_FILLING_FOK if cur == mt5.ORDER_FILLING_IOC
                                else mt5.ORDER_FILLING_IOC)
        res = mt5.order_send(req)

    return res


def _result(res) -> OrderResult:
    if res is None:
        err = mt5.last_error()
        return OrderResult(False, retcode=err[0] if err else -1, comment=str(err))
    ok = res.retcode in _SUCCESS_CODES
    return OrderResult(ok, ticket=res.order or None, fill_price=res.price or None,
                       retcode=res.retcode, comment=res.comment)


# ── Order placement ───────────────────────────────────────────────────────────

def place_market(direction: str, lot: float, sl: float, tp: float,
                 magic: int, comment: str) -> OrderResult:
    if not _MT5_AVAILABLE:
        return OrderResult(False, comment="MT5 unavailable")
    tick = mt5.symbol_info_tick(cfg.SYMBOL)
    if tick is None:
        return OrderResult(False, comment="No tick data")

    is_buy = direction == "LONG"
    entry = tick.ask if is_buy else tick.bid
    # Re-anchor market stops to the executable tick so price movement between
    # detection and order_send cannot put a stop on the wrong side.
    sl = entry - cfg.SL_MAX_PIPS * cfg.PIP if is_buy else entry + cfg.SL_MAX_PIPS * cfg.PIP
    if (is_buy and tp <= entry) or (not is_buy and tp >= entry):
        tp = entry + cfg.TP2_PIPS * cfg.PIP if is_buy else entry - cfg.TP2_PIPS * cfg.PIP
    sl, tp, error = _validated_prices(direction, entry, sl, tp)
    if error:
        return OrderResult(False, retcode=10016, comment=error)
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       cfg.SYMBOL,
        "volume":       float(lot),
        "type":         mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price":        round(entry, cfg.DIGITS),
        "sl":           float(sl),
        "tp":           float(tp),
        "magic":        magic,
        "comment":      comment[:31],
        "type_filling": mt5.ORDER_FILLING_IOC,
        "deviation":    20,
    }
    return _result(_send_with_retry(req))


def place_limit(direction: str, price: float, lot: float, sl: float, tp: float,
                magic: int, comment: str, expiry_utc: datetime) -> OrderResult:
    if not _MT5_AVAILABLE:
        return OrderResult(False, comment="MT5 unavailable")
    is_buy = direction == "LONG"
    price = round(float(price), cfg.DIGITS)
    sl, tp, error = _validated_prices(direction, price, sl, tp)
    if error:
        return OrderResult(False, retcode=10016, comment=error)
    req = {
        "action":       mt5.TRADE_ACTION_PENDING,
        "symbol":       cfg.SYMBOL,
        "volume":       float(lot),
        "type":         mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT,
        "price":        price,
        "sl":           float(sl),
        "tp":           float(tp),
        "magic":        magic,
        "comment":      comment[:31],
        "type_filling": mt5.ORDER_FILLING_IOC,
        "type_time":    mt5.ORDER_TIME_SPECIFIED,
        "expiration":   int(expiry_utc.timestamp()),
    }
    return _result(mt5.order_send(req))


def modify_position_sl(ticket: int, new_sl: float) -> bool:
    if not _MT5_AVAILABLE:
        return False
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning("modify_sl: ticket %d not found", ticket)
        return False
    pos = positions[0]
    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "symbol":   cfg.SYMBOL,
        "sl":       float(new_sl),
        "tp":       pos.tp,
        "magic":    pos.magic,
    }
    res = mt5.order_send(req)
    if res is None or res.retcode != 10009:
        log.error("modify_sl ticket=%d failed: retcode=%s",
                  ticket, res.retcode if res else mt5.last_error())
        return False
    return True


def close_position_partial(ticket: int, lot: float) -> OrderResult:
    if not _MT5_AVAILABLE:
        return OrderResult(False, comment="MT5 unavailable")
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return OrderResult(False, comment=f"Ticket {ticket} not found")
    pos = positions[0]
    tick = mt5.symbol_info_tick(cfg.SYMBOL)
    if tick is None:
        return OrderResult(False, comment="No tick data")

    is_long = pos.type == 0   # POSITION_TYPE_BUY == 0
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "position":     ticket,
        "symbol":       cfg.SYMBOL,
        "volume":       float(lot),
        "type":         mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
        "price":        tick.bid if is_long else tick.ask,
        "magic":        pos.magic,
        "comment":      "partial_close"[:31],
        "type_filling": mt5.ORDER_FILLING_IOC,
        "deviation":    20,
    }
    return _result(_send_with_retry(req))


def close_position_full(ticket: int) -> OrderResult:
    if not _MT5_AVAILABLE:
        return OrderResult(False, comment="MT5 unavailable")
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return OrderResult(False, comment=f"Ticket {ticket} not found")
    return close_position_partial(ticket, positions[0].volume)


# ── Position / order queries ──────────────────────────────────────────────────

def get_positions(magic: int | None = None) -> list:
    if not _MT5_AVAILABLE:
        return []
    positions = mt5.positions_get(symbol=cfg.SYMBOL)
    if positions is None:
        return []
    return [p for p in positions if magic is None or p.magic == magic]


def get_tick(symbol: str | None = None):
    """Return the current tick object (has .bid and .ask). None if unavailable."""
    if not _MT5_AVAILABLE:
        return None
    return mt5.symbol_info_tick(symbol or cfg.SYMBOL)


def get_deal_history(position_ticket: int) -> list:
    """Return all deals associated with a position ticket (last 30 days)."""
    if not _MT5_AVAILABLE:
        return []
    from_date = datetime.now(timezone.utc) - timedelta(days=30)
    to_date   = datetime.now(timezone.utc) + timedelta(hours=1)
    deals = mt5.history_deals_get(from_date, to_date, position=position_ticket)
    return list(deals) if deals is not None else []


def get_pending_orders(magic: int | None = None) -> list:
    if not _MT5_AVAILABLE:
        return []
    orders = mt5.orders_get(symbol=cfg.SYMBOL)
    if orders is None:
        return []
    return [o for o in orders if magic is None or o.magic == magic]
