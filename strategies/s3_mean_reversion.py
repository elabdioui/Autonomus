"""S3 - counter-trend M1 mean reversion after wick exhaustion."""
from __future__ import annotations

from config import cfg
from strategies import _bars
from strategies.base import ALL, MarketData, SetupSpec, Strategy, make_signal


class MeanReversion(Strategy):
    id = "S3"
    name = "S3_mean_reversion"
    magic = 20003

    def scan(self, data: MarketData, direction: str):
        m1 = _bars.closed(data.m1)
        if len(m1) < cfg.S3_EMA_PERIOD:
            return self._reject(direction, "INSUFFICIENT_CLOSED_BARS")

        exhaustion = m1.iloc[-1]
        if self._bar_was_emitted(direction, exhaustion["time"]):
            return self._reject(direction, "DUPLICATE_CLOSED_BAR")
        ema = float(m1["close"].ewm(span=cfg.S3_EMA_PERIOD, adjust=False).mean().iloc[-1])
        close = float(exhaustion["close"])
        candle_open = float(exhaustion["open"])
        high = float(exhaustion["high"])
        low = float(exhaustion["low"])
        candle_range = high - low
        if candle_range <= 0:
            return self._reject(direction, "ZERO_RANGE_EXHAUSTION_CANDLE")

        signed_extension_pips = (close - ema) / cfg.PIP
        if direction == "SHORT":
            if signed_extension_pips < cfg.S3_EXTENSION_PIPS:
                return self._reject(direction, "NO_BULLISH_EXTENSION")
            rejection_wick = high - max(candle_open, close)
            if close >= candle_open or abs(close - ema) >= abs(candle_open - ema):
                return self._reject(direction, "NO_BEARISH_RETURN_TOWARD_EMA")
            extreme = high
        else:
            if signed_extension_pips > -cfg.S3_EXTENSION_PIPS:
                return self._reject(direction, "NO_BEARISH_EXTENSION")
            rejection_wick = min(candle_open, close) - low
            if close <= candle_open or abs(close - ema) >= abs(candle_open - ema):
                return self._reject(direction, "NO_BULLISH_RETURN_TOWARD_EMA")
            extreme = low

        wick_ratio = rejection_wick / candle_range
        if wick_ratio < cfg.S3_WICK_REJECTION_RATIO:
            return self._reject(direction, "WICK_REJECTION_RATIO_TOO_LOW")

        buffer_price = cfg.S3_SL_BUFFER_PIPS * cfg.PIP
        structural_sl = extreme + buffer_price if direction == "SHORT" else extreme - buffer_price
        signal = make_signal(
            strategy_id=self.id,
            setup=self.name,
            data=data,
            direction=direction,
            entry=close,
            sl_structural=structural_sl,
            tp_final=ema,
            entry_zone=(min(close, ema), max(close, ema)),
            meta={
                "extension_pips": round(abs(signed_extension_pips), 2),
                "ema_distance_at_entry": round(abs(close - ema) / cfg.PIP, 2),
                "ema_target": round(ema, cfg.DIGITS),
                "wick_rejection_ratio": round(wick_ratio, 3),
                "confirmation_bar_time": str(exhaustion["time"]),
            },
            confluences=["EMA_EXTENSION", "WICK_EXHAUSTION"],
        )
        self._mark_bar_emitted(direction, exhaustion["time"])
        return signal


_STRATEGY = MeanReversion()
SETUP = SetupSpec(
    name=_STRATEGY.name,
    scan=_STRATEGY.scan,
    killzone_mode="off",
    killzones=ALL,
    cooldown_seconds=0,
    strategy_id=_STRATEGY.id,
    magic=_STRATEGY.magic,
)
