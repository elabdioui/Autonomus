"""S4 - continuation reaction after an EMA pullback in a BOS trend."""
from __future__ import annotations

from config import cfg
from indicators.fvg import detect_fvg
from indicators.structure import find_swings
from strategies import _bars
from strategies.base import ALL, MarketData, SetupSpec, Strategy, make_signal


class PullbackContinuation(Strategy):
    id = "S4"
    name = "S4_pullback_continuation"
    magic = 20004

    @staticmethod
    def _recent_bos(frame, direction: str) -> tuple[int, float] | None:
        swings = find_swings(frame, lookback=2)
        wanted_type = "HIGH" if direction == "LONG" else "LOW"
        earliest = max(1, len(frame) - cfg.S4_BOS_LOOKBACK_BARS)
        found: tuple[int, float] | None = None
        for swing in swings:
            if swing.type != wanted_type:
                continue
            start = max(swing.confirmed_index + 1, earliest)
            for idx in range(start, len(frame)):
                previous = float(frame.iloc[idx - 1]["close"])
                current = float(frame.iloc[idx]["close"])
                broke = (previous <= swing.price < current if direction == "LONG"
                         else previous >= swing.price > current)
                if broke and (found is None or idx > found[0]):
                    found = (idx, swing.price)
                    break
        return found

    @staticmethod
    def _zones(frame, direction: str, bos_index: int) -> list[tuple[str, float, float]]:
        zones: list[tuple[str, float, float]] = []
        wanted = "BULLISH" if direction == "LONG" else "BEARISH"
        for fvg in detect_fvg(frame, min_size_pips=0.0):
            if fvg.type == wanted and fvg.candle_idx >= max(1, bos_index - 2):
                zones.append(("FVG", float(fvg.bottom), float(fvg.top)))

        # Last opposite candle before the displacement close that broke structure.
        for idx in range(bos_index - 1, max(-1, bos_index - 10), -1):
            candle = frame.iloc[idx]
            opposite = (candle["close"] < candle["open"] if direction == "LONG"
                        else candle["close"] > candle["open"])
            if opposite:
                zones.append(("OB", float(candle["low"]), float(candle["high"])))
                break
        return zones

    def scan(self, data: MarketData, direction: str):
        m1 = _bars.closed(data.m1)
        m5 = _bars.closed(data.m5)
        m15 = _bars.closed(data.m15)
        if len(m1) < 6 or len(m5) < cfg.S4_TREND_EMA:
            return self._reject(direction, "INSUFFICIENT_CLOSED_BARS")

        reaction = m1.iloc[-1]
        if self._bar_was_emitted(direction, reaction["time"]):
            return self._reject(direction, "DUPLICATE_CLOSED_BAR")

        ema_series = m5["close"].ewm(span=cfg.S4_TREND_EMA, adjust=False).mean()
        ema = float(ema_series.iloc[-1])
        trend_close = float(m5.iloc[-1]["close"])
        if direction == "LONG" and trend_close <= ema:
            return self._reject(direction, "M5_NOT_ABOVE_TREND_EMA")
        if direction == "SHORT" and trend_close >= ema:
            return self._reject(direction, "M5_NOT_BELOW_TREND_EMA")

        bos = self._recent_bos(m5, direction)
        if bos is None:
            return self._reject(direction, "NO_RECENT_DIRECTIONAL_BOS")
        bos_index, bos_level = bos

        candle_open = float(reaction["open"])
        candle_close = float(reaction["close"])
        if direction == "LONG" and candle_close <= candle_open:
            return self._reject(direction, "NO_BULLISH_M1_REACTION_CLOSE")
        if direction == "SHORT" and candle_close >= candle_open:
            return self._reject(direction, "NO_BEARISH_M1_REACTION_CLOSE")

        zones = self._zones(m5, direction, bos_index)
        selected = next((zone for zone in reversed(zones)
                         if zone[1] <= candle_close <= zone[2]), None)
        close_in_fvg_ob = selected is not None
        tolerance = cfg.S4_PULLBACK_TOLERANCE_PIPS * cfg.PIP
        touched_ema = (float(reaction["low"]) - tolerance <= ema
                       <= float(reaction["high"]) + tolerance)
        if not close_in_fvg_ob and not touched_ema:
            return self._reject(direction, "PULLBACK_MISSED_EMA_AND_FVG_OB")
        if cfg.S4_REQUIRE_FVG_OR_OB and not close_in_fvg_ob:
            return self._reject(direction, "FVG_OR_OB_REQUIRED_BUT_ABSENT")

        if selected is None:
            zone_type, zone_low, zone_high = "EMA", ema - tolerance, ema + tolerance
        else:
            zone_type, zone_low, zone_high = selected

        # A zone entry is confirmed by the close. Wick-only contact never qualifies.
        if zone_type != "EMA" and not (zone_low <= candle_close <= zone_high):
            return self._reject(direction, "REACTION_WICK_ONLY_IN_RETEST_ZONE")
        if zone_type == "EMA":
            on_trend_side = candle_close >= ema if direction == "LONG" else candle_close <= ema
            if not on_trend_side:
                return self._reject(direction, "REACTION_CLOSE_NOT_BACK_THROUGH_EMA")

        pullback = m1.iloc[-6:]
        buffer_price = cfg.S4_SL_BUFFER_PIPS * cfg.PIP
        if direction == "LONG":
            invalidation = min(float(pullback["low"].min()), zone_low)
            structural_sl = invalidation - buffer_price
        else:
            invalidation = max(float(pullback["high"].max()), zone_high)
            structural_sl = invalidation + buffer_price

        m15_aligned = False
        if len(m15) >= cfg.S4_TREND_EMA:
            m15_ema = float(m15["close"].ewm(span=cfg.S4_TREND_EMA, adjust=False).mean().iloc[-1])
            m15_close = float(m15.iloc[-1]["close"])
            m15_aligned = m15_close > m15_ema if direction == "LONG" else m15_close < m15_ema

        signal = make_signal(
            strategy_id=self.id,
            setup=self.name,
            data=data,
            direction=direction,
            entry=candle_close,
            sl_structural=structural_sl,
            entry_zone=(zone_low, zone_high),
            meta={
                "trend_source": "M5_EMA+BOS",
                "trend_ema": round(ema, cfg.DIGITS),
                "retest_zone_type": zone_type,
                "bos_level": round(bos_level, cfg.DIGITS),
                "confluence_fvg_ob": close_in_fvg_ob,
                "m15_trend_aligned": m15_aligned,
                "confirmation_bar_time": str(reaction["time"]),
            },
            confluences=["M5_EMA", "M5_BOS", f"RETEST_{zone_type}"],
        )
        self._mark_bar_emitted(direction, reaction["time"])
        return signal


_STRATEGY = PullbackContinuation()
SETUP = SetupSpec(
    name=_STRATEGY.name,
    scan=_STRATEGY.scan,
    killzone_mode="off",
    killzones=ALL,
    cooldown_seconds=0,
    strategy_id=_STRATEGY.id,
    magic=_STRATEGY.magic,
)
