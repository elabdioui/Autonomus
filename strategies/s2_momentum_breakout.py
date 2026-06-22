"""S2 - M1 displacement breakout after a measured compression."""
from __future__ import annotations

from config import cfg
from strategies import _bars
from strategies.base import ALL, MarketData, SetupSpec, Strategy, make_signal


class MomentumBreakout(Strategy):
    id = "S2"
    name = "S2_momentum_breakout"
    magic = 20002

    @staticmethod
    def _atr(compression) -> float:
        true_ranges: list[float] = []
        previous_close = None
        for _, candle in compression.iterrows():
            high, low = float(candle["high"]), float(candle["low"])
            if previous_close is None:
                true_range = high - low
            else:
                true_range = max(high - low, abs(high - previous_close), abs(low - previous_close))
            true_ranges.append(true_range)
            previous_close = float(candle["close"])
        return sum(true_ranges) / len(true_ranges) if true_ranges else 0.0

    def scan(self, data: MarketData, direction: str):
        m1 = _bars.closed(data.m1)
        needed = cfg.S2_COMPRESSION_BARS + 1
        if len(m1) < needed:
            return self._reject(direction, "INSUFFICIENT_CLOSED_BARS")

        breakout = m1.iloc[-1]
        if self._bar_was_emitted(direction, breakout["time"]):
            return self._reject(direction, "DUPLICATE_CLOSED_BAR")
        compression = m1.iloc[-needed:-1]
        range_high = float(compression["high"].max())
        range_low = float(compression["low"].min())
        width_pips = (range_high - range_low) / cfg.PIP
        if width_pips >= cfg.S2_MAX_RANGE_PIPS:
            return self._reject(direction, "RANGE_NOT_COMPRESSED")

        candle_open = float(breakout["open"])
        candle_close = float(breakout["close"])
        if direction == "LONG":
            if candle_close <= range_high:
                return self._reject(direction, "NO_CLOSE_ABOVE_RANGE")
            if candle_close <= candle_open:
                return self._reject(direction, "BREAKOUT_BODY_WRONG_DIRECTION")
        else:
            if candle_close >= range_low:
                return self._reject(direction, "NO_CLOSE_BELOW_RANGE")
            if candle_close >= candle_open:
                return self._reject(direction, "BREAKOUT_BODY_WRONG_DIRECTION")

        atr = self._atr(compression)
        if atr <= 0:
            return self._reject(direction, "ZERO_COMPRESSION_ATR")
        body_ratio = abs(candle_close - candle_open) / atr
        if body_ratio < cfg.S2_DISPLACEMENT_BODY_RATIO:
            return self._reject(direction, "DISPLACEMENT_RATIO_TOO_LOW")

        buffer_price = cfg.S2_SL_BUFFER_PIPS * cfg.PIP
        structural_sl = (range_low - buffer_price if direction == "LONG"
                         else range_high + buffer_price)
        signal = make_signal(
            strategy_id=self.id,
            setup=self.name,
            data=data,
            direction=direction,
            entry=candle_close,
            sl_structural=structural_sl,
            entry_zone=(range_low, range_high),
            meta={
                "range_width_pips": round(width_pips, 2),
                "range_high": round(range_high, cfg.DIGITS),
                "range_low": round(range_low, cfg.DIGITS),
                "displacement_body_ratio": round(body_ratio, 3),
                "breakout_side": "HIGH" if direction == "LONG" else "LOW",
                "confirmation_bar_time": str(breakout["time"]),
            },
            confluences=["M1_COMPRESSION", "M1_DISPLACEMENT"],
        )
        self._mark_bar_emitted(direction, breakout["time"])
        return signal


_STRATEGY = MomentumBreakout()
SETUP = SetupSpec(
    name=_STRATEGY.name,
    scan=_STRATEGY.scan,
    killzone_mode="off",
    killzones=ALL,
    cooldown_seconds=0,
    strategy_id=_STRATEGY.id,
    magic=_STRATEGY.magic,
)
