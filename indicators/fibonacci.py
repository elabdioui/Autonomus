"""Fibonacci retracement levels and OTE zone. Copied from xauusd-bot detector/indicators/fibonacci.py."""
from dataclasses import dataclass


@dataclass
class FibLevels:
    swing_high: float
    swing_low: float
    direction: str          # "BULLISH" (retracing down) | "BEARISH" (retracing up)
    ote_low_ratio: float = 0.618
    ote_high_ratio: float = 0.786

    @property
    def range(self) -> float:
        return self.swing_high - self.swing_low

    def level(self, ratio: float) -> float:
        if self.direction == "BULLISH":
            return self.swing_high - self.range * ratio
        return self.swing_low + self.range * ratio

    @property
    def ote_low(self) -> float:
        return self.level(self.ote_high_ratio) if self.direction == "BULLISH" else self.level(self.ote_low_ratio)

    @property
    def ote_high(self) -> float:
        return self.level(self.ote_low_ratio) if self.direction == "BULLISH" else self.level(self.ote_high_ratio)

    def is_in_ote(self, price: float) -> bool:
        lo = min(self.ote_low, self.ote_high)
        hi = max(self.ote_low, self.ote_high)
        return lo <= price <= hi


def compute_fib_from_sweep(sweep_low: float, swing_high: float,
                            ote_low: float = 0.618, ote_high: float = 0.786) -> FibLevels:
    return FibLevels(swing_high=swing_high, swing_low=sweep_low,
                     direction="BULLISH", ote_low_ratio=ote_low, ote_high_ratio=ote_high)


def compute_fib_from_sweep_bearish(sweep_high: float, swing_low: float,
                                    ote_low: float = 0.618, ote_high: float = 0.786) -> FibLevels:
    return FibLevels(swing_high=sweep_high, swing_low=swing_low,
                     direction="BEARISH", ote_low_ratio=ote_low, ote_high_ratio=ote_high)
