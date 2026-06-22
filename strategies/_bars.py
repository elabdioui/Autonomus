"""Closed-candle helpers shared by every strategy.

MT5 returns the forming candle as the final row. Strategy code must cross this
boundary before doing any detection or indicator calculation.
"""
from __future__ import annotations

import pandas as pd


def closed(frame: pd.DataFrame | None) -> pd.DataFrame:
    """Return an isolated dataframe containing closed candles only."""
    if frame is None or frame.empty or len(frame) < 2:
        return pd.DataFrame(columns=frame.columns if frame is not None else None)
    result = frame.iloc[:-1].copy().reset_index(drop=True)
    result.attrs["closed_only"] = True
    return result
