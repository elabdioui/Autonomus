"""ATR-based market regime detection. Extracted from xauusd-bot detector/indicators/liquidity.py."""
import pandas as pd
from typing import Literal


def detect_regime(
    df: pd.DataFrame,
    atr_period: int = 14,
    vol_multiplier: float = 2.0,
    range_multiplier: float = 0.5,
) -> Literal["range", "normal", "high_vol"]:
    """
    Classify market regime using ATR relative to its rolling mean.

    high_vol : current ATR > vol_multiplier  × mean ATR  (news / erratic)
    range    : current ATR < range_multiplier × mean ATR  (no momentum)
    normal   : otherwise
    """
    if len(df) < atr_period + 2:
        return "normal"

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values

    tr = [
        max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        for i in range(1, len(df))
    ]
    atr = pd.Series(tr).rolling(atr_period).mean().dropna()

    if atr.empty:
        return "normal"

    current_atr = float(atr.iloc[-1])
    mean_atr = float(atr.mean())

    if mean_atr == 0:
        return "normal"

    if current_atr > vol_multiplier * mean_atr:
        return "high_vol"
    if current_atr < range_multiplier * mean_atr:
        return "range"
    return "normal"
