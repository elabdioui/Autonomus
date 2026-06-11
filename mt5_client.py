"""MT5 connection and OHLC helpers. Adapted from xauusd-bot detector/mt5_client.py.

Key changes vs source:
- initialize() passes path=cfg.MT5_TERMINAL_PATH (mandatory for multi-terminal VPS)
- Startup validation: missing path or failed init → sys.exit(1)
- Account login verification after connect to prevent attaching to the wrong terminal
- get_spread_pips() added
- Order functions NOT present here — added in SPEC 3 (execution/engine.py)
"""
import logging
import sys
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

_TIMEFRAME_MAP = {}

def _ensure_tf_map():
    if _MT5_AVAILABLE and not _TIMEFRAME_MAP:
        _TIMEFRAME_MAP.update({
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        })


def connect() -> bool:
    if not _MT5_AVAILABLE:
        log.critical("MetaTrader5 package not installed — cannot connect")
        sys.exit(1)

    terminal_path = cfg.MT5_TERMINAL_PATH
    if not terminal_path:
        log.critical("MT5_TERMINAL_PATH is not set in .env — refusing to start")
        sys.exit(1)

    if not Path(terminal_path).exists():
        log.critical("MT5_TERMINAL_PATH does not exist on disk: %s", terminal_path)
        sys.exit(1)

    _ensure_tf_map()

    ok = mt5.initialize(
        path=terminal_path,
        login=cfg.MT5_LOGIN,
        password=cfg.MT5_PASSWORD,
        server=cfg.MT5_SERVER,
        timeout=30000,
    )
    if not ok:
        err = mt5.last_error()
        log.critical("MT5 initialize() failed — code=%s msg=%s", err[0], err[1])
        sys.exit(1)

    acc = mt5.account_info()
    if acc is None:
        log.critical("MT5 init succeeded but account_info() is None — terminal not logged in")
        mt5.shutdown()
        sys.exit(1)

    if acc.login != cfg.MT5_LOGIN:
        log.critical(
            "MT5 terminal login mismatch: expected %s, got %s — wrong terminal attached",
            cfg.MT5_LOGIN, acc.login,
        )
        mt5.shutdown()
        sys.exit(1)

    if not mt5.symbol_select(cfg.SYMBOL, True):
        log.critical("symbol_select(%s) failed: %s", cfg.SYMBOL, mt5.last_error())
        mt5.shutdown()
        sys.exit(1)

    log.info("MT5 connected — account=%s server=%s symbol=%s", acc.login, cfg.MT5_SERVER, cfg.SYMBOL)
    return True


def disconnect() -> None:
    if _MT5_AVAILABLE:
        mt5.shutdown()


def is_connected() -> bool:
    if not _MT5_AVAILABLE:
        return False
    info = mt5.terminal_info()
    return info is not None and info.connected and mt5.account_info() is not None


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
    df = df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return df


def get_current_price(symbol: str) -> float | None:
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return None
    return (tick.bid + tick.ask) / 2


def get_spread_pips(symbol: str | None = None) -> float:
    """Return current spread in pips (1 pip = 0.10 on XAUUSD)."""
    sym = symbol or cfg.SYMBOL
    tick = mt5.symbol_info_tick(sym)
    if tick is None:
        return 0.0
    spread_price = tick.ask - tick.bid
    return round(spread_price / 0.10, 2)


def get_scalper_timeframes(symbol: str | None = None) -> dict[str, pd.DataFrame]:
    """Fetch the four timeframes used by all scalper strategies."""
    sym = symbol or cfg.SYMBOL
    return {
        "M1":  get_ohlc(sym, "M1",  cfg.OHLC_COUNT_M1),
        "M5":  get_ohlc(sym, "M5",  cfg.OHLC_COUNT_M5),
        "M15": get_ohlc(sym, "M15", cfg.OHLC_COUNT_M15),
        "H1":  get_ohlc(sym, "H1",  cfg.OHLC_COUNT_H1),
    }
