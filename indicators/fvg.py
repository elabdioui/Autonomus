"""Fair Value Gap (Imbalance) detection. Copied from xauusd-bot detector/indicators/fvg.py."""
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class FVG:
    type: str           # "BULLISH" | "BEARISH"
    top: float
    bottom: float
    mid: float
    size: float
    time: datetime
    candle_idx: int = 0
    filled: bool = False
    partially_filled: bool = False

    @property
    def label(self) -> str:
        return f"FVG_{self.type[0]}"


def detect_fvg(df: pd.DataFrame, min_size_pips: float = 3.0) -> list[FVG]:
    """
    3-candle pattern: gap between candle[i-1] and candle[i+1].
    Upper bound is len(df)-2 so c3 is always the last CLOSED candle — never the forming one.
    """
    if len(df) < 4:
        return []

    pip_unit = 0.10
    min_size = min_size_pips * pip_unit
    fvgs: list[FVG] = []

    for i in range(1, len(df) - 2):
        c1 = df.iloc[i - 1]
        c2 = df.iloc[i]
        c3 = df.iloc[i + 1]

        if c3["low"] > c1["high"]:
            gap = c3["low"] - c1["high"]
            if gap >= min_size:
                fvgs.append(FVG(
                    type="BULLISH",
                    top=c3["low"],
                    bottom=c1["high"],
                    mid=(c3["low"] + c1["high"]) / 2,
                    size=gap,
                    time=c2["time"],
                    candle_idx=i,
                ))

        if c3["high"] < c1["low"]:
            gap = c1["low"] - c3["high"]
            if gap >= min_size:
                fvgs.append(FVG(
                    type="BEARISH",
                    top=c1["low"],
                    bottom=c3["high"],
                    mid=(c1["low"] + c3["high"]) / 2,
                    size=gap,
                    time=c2["time"],
                    candle_idx=i,
                ))

    return fvgs


def filter_unfilled_fvg(fvgs: list[FVG], current_price: float) -> list[FVG]:
    result = []
    for fvg in fvgs:
        if fvg.type == "BULLISH":
            if current_price <= fvg.mid:
                fvg.filled = True
            elif current_price < fvg.top:
                fvg.partially_filled = True
        else:
            if current_price >= fvg.mid:
                fvg.filled = True
            elif current_price > fvg.bottom:
                fvg.partially_filled = True
        result.append(fvg)
    return result


def get_recent_fvg(fvgs: list[FVG], direction: str, n: int = 3) -> list[FVG]:
    matching = [f for f in fvgs if f.type == direction and not f.filled]
    return matching[-n:]
