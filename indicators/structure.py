"""Market structure: confirmed swing highs/lows, BOS, CHoCH.

REWRITTEN vs source: find_swings() now uses confirmed_index (the close of bar
i+lookback) instead of bar_index, eliminating any look-ahead bias.
"""
import pandas as pd
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass
class Swing:
    type: Literal["HIGH", "LOW"]
    price: float
    bar_index: int          # index of the pivot bar
    confirmed_index: int    # index of the candle whose CLOSE confirmed this swing
    confirmed_time: datetime


@dataclass
class StructureBreak:
    type: Literal["BOS", "CHoCH"]
    direction: Literal["BULLISH", "BEARISH"]
    broken_level: float
    time: datetime
    candle_idx: int = 0


def find_swings(df: pd.DataFrame, lookback: int = 5) -> list[Swing]:
    """
    Pivot-based swing detection — confirmed-only.

    A swing high at bar i requires `lookback` candles on each side with strictly
    lower highs. It is confirmed only at the CLOSE of bar i+lookback.

    The caller must pass closed candles (normally via ``strategies._bars.closed``).
    A pivot is exposed only after the candle at ``pivot + lookback`` has closed.
    """
    swings: list[Swing] = []
    n = len(df)
    highs = df["high"].values
    lows = df["low"].values
    max_confirmed = n - 1

    for i in range(lookback, n - lookback):
        confirmed_idx = i + lookback
        if confirmed_idx > max_confirmed:
            break

        is_sh = (
            all(highs[i] > highs[i - j] for j in range(1, lookback + 1)) and
            all(highs[i] > highs[i + j] for j in range(1, lookback + 1))
        )
        is_sl = (
            all(lows[i] < lows[i - j] for j in range(1, lookback + 1)) and
            all(lows[i] < lows[i + j] for j in range(1, lookback + 1))
        )

        confirmed_time = df.iloc[confirmed_idx]["time"]

        if is_sh:
            swings.append(Swing(
                type="HIGH",
                price=float(highs[i]),
                bar_index=i,
                confirmed_index=confirmed_idx,
                confirmed_time=confirmed_time,
            ))
        if is_sl:
            swings.append(Swing(
                type="LOW",
                price=float(lows[i]),
                bar_index=i,
                confirmed_index=confirmed_idx,
                confirmed_time=confirmed_time,
            ))

    swings.sort(key=lambda s: s.confirmed_index)
    return swings


def determine_bias(swings: list[Swing]) -> Literal["BULLISH", "BEARISH", "NEUTRAL"]:
    """Market bias from the last 2+ confirmed swing highs and lows."""
    highs = [s for s in swings if s.type == "HIGH"][-3:]
    lows = [s for s in swings if s.type == "LOW"][-3:]

    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"

    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        return "BULLISH"
    if lh and ll:
        return "BEARISH"
    return "NEUTRAL"


def detect_structure_breaks(
    df: pd.DataFrame,
    swings: list[Swing],
    current_bias: Literal["BULLISH", "BEARISH", "NEUTRAL"],
) -> list[StructureBreak]:
    """BOS = break in trend direction. CHoCH = break against bias."""
    breaks: list[StructureBreak] = []
    if len(df) < 2 or not swings:
        return breaks

    highs = [s for s in swings if s.type == "HIGH"]
    lows = [s for s in swings if s.type == "LOW"]

    hi_idx = 0
    lo_idx = 0
    active_sh: float | None = None
    active_sl: float | None = None

    for i in range(1, len(df)):
        prev_close = df.iloc[i - 1]["close"]
        curr_close = df.iloc[i]["close"]
        candle_time = df.iloc[i]["time"]

        while hi_idx < len(highs) and highs[hi_idx].confirmed_index < i:
            active_sh = highs[hi_idx].price
            hi_idx += 1
        while lo_idx < len(lows) and lows[lo_idx].confirmed_index < i:
            active_sl = lows[lo_idx].price
            lo_idx += 1

        if active_sh is not None and prev_close <= active_sh < curr_close:
            btype: Literal["BOS", "CHoCH"] = "BOS" if current_bias == "BULLISH" else "CHoCH"
            breaks.append(StructureBreak(btype, "BULLISH", active_sh, candle_time, candle_idx=i))
            active_sh = None

        if active_sl is not None and prev_close >= active_sl > curr_close:
            btype = "BOS" if current_bias == "BEARISH" else "CHoCH"
            breaks.append(StructureBreak(btype, "BEARISH", active_sl, candle_time, candle_idx=i))
            active_sl = None

    return breaks


def get_recent_choch(
    df: pd.DataFrame,
    swings: list[Swing],
    bias: Literal["BULLISH", "BEARISH", "NEUTRAL"],
    lookback_candles: int = 20,
) -> StructureBreak | None:
    recent_df = df.iloc[-lookback_candles:].reset_index(drop=True)
    breaks = detect_structure_breaks(recent_df, swings, bias)
    chochs = [b for b in breaks if b.type == "CHoCH"]
    return chochs[-1] if chochs else None
