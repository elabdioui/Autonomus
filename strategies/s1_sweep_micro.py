"""S1 - ICT liquidity sweep with same-candle rejection on M1."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from config import cfg
from indicators.fvg import detect_fvg
from indicators.structure import detect_structure_breaks, determine_bias, find_swings
from strategies import _bars
from strategies.base import ALL, MarketData, SetupSpec, Strategy, make_signal


class SweepMicro(Strategy):
    id = "S1"
    name = "S1_sweep_micro"
    magic = 20001

    def __init__(self) -> None:
        super().__init__()
        self._last_emission_at: dict[str, datetime] = {}

    @staticmethod
    def _levels(frame: pd.DataFrame, side: str, timeframe: str) -> list[tuple[float, str, object]]:
        if frame.empty:
            return []
        values = "high" if side == "BSL" else "low"
        recent = frame.iloc[-cfg.S1_SWEEP_LOOKBACK_BARS:].reset_index(drop=True)
        levels: list[tuple[float, str, object]] = []

        swing_type = "HIGH" if side == "BSL" else "LOW"
        for swing in find_swings(recent, lookback=2):
            if swing.type == swing_type:
                levels.append((swing.price, timeframe, recent.iloc[swing.bar_index]["time"]))

        tolerance = cfg.S1_EQUAL_LEVEL_TOLERANCE_PIPS * cfg.PIP
        series = recent[values].astype(float).tolist()
        for idx in range(1, len(series)):
            prior = series[max(0, idx - 8):idx]
            matches = [value for value in prior if abs(value - series[idx]) <= tolerance]
            if matches:
                levels.append(((matches[-1] + series[idx]) / 2, timeframe, recent.iloc[idx]["time"]))

        # A recent range extreme remains an obvious pool even without equal levels.
        extreme_idx = recent[values].idxmax() if side == "BSL" else recent[values].idxmin()
        levels.append((float(recent.loc[extreme_idx, values]), timeframe,
                       recent.loc[extreme_idx, "time"]))
        return levels

    @staticmethod
    def _m5_confluence(m5: pd.DataFrame, direction: str) -> tuple[bool, bool]:
        if len(m5) < 7:
            return False, False
        swings = find_swings(m5, lookback=2)
        bias = determine_bias(swings)
        wanted = "BULLISH" if direction == "LONG" else "BEARISH"
        breaks = detect_structure_breaks(m5, swings, bias)
        has_choch = any(b.type == "CHoCH" and b.direction == wanted
                        for b in breaks[-5:])
        has_fvg = any(f.type == wanted for f in detect_fvg(m5.iloc[-30:].reset_index(drop=True), 0.0))

        # OB = last opposite candle immediately preceding a directional body.
        has_ob = False
        for idx in range(max(0, len(m5) - 10), len(m5) - 1):
            candle = m5.iloc[idx]
            nxt = m5.iloc[idx + 1]
            if direction == "LONG":
                has_ob |= candle["close"] < candle["open"] and nxt["close"] > nxt["open"]
            else:
                has_ob |= candle["close"] > candle["open"] and nxt["close"] < nxt["open"]
        return has_choch, bool(has_fvg or has_ob)

    def scan(self, data: MarketData, direction: str):
        m1 = _bars.closed(data.m1)
        m5 = _bars.closed(data.m5)
        if len(m1) < cfg.S1_SWEEP_LOOKBACK_BARS + 1 or len(m5) < 4:
            return self._reject(direction, "INSUFFICIENT_CLOSED_BARS")

        sweep = m1.iloc[-1]
        if self._bar_was_emitted(direction, sweep["time"]):
            return self._reject(direction, "DUPLICATE_CLOSED_BAR")

        side = "SSL" if direction == "LONG" else "BSL"
        levels = self._levels(m1.iloc[:-1], side, "M1")
        levels += self._levels(m5.iloc[:-1], side, "M5")
        if not levels:
            return self._reject(direction, "NO_RECENT_LIQUIDITY")

        if direction == "LONG":
            swept = [item for item in levels if float(sweep["low"]) < item[0] < float(sweep["close"])]
        else:
            swept = [item for item in levels if float(sweep["close"]) < item[0] < float(sweep["high"])]
        if not swept:
            return self._reject(direction, "NO_SAME_CANDLE_SWEEP_REJECTION")

        # Prefer the closest reclaimed level; it best represents the immediate pool.
        level, level_tf, _ = min(swept, key=lambda item: abs(float(sweep["close"]) - item[0]))
        has_choch, has_fvg_ob = self._m5_confluence(m5, direction)
        if cfg.S1_REQUIRE_CHOCH and not has_choch:
            return self._reject(direction, "CHOCH_REQUIRED_BUT_ABSENT")

        last_emission = self._last_emission_at.get(direction)
        elapsed = ((data.now_utc - last_emission).total_seconds()
                   if last_emission is not None else None)
        would_block_cooldown = bool(
            cfg.S1_COOLDOWN_SECONDS > 0 and elapsed is not None
            and elapsed < cfg.S1_COOLDOWN_SECONDS
        )

        entry = float(sweep["close"])
        buffer_price = cfg.S1_SL_BUFFER_PIPS * cfg.PIP
        extreme = float(sweep["low"] if direction == "LONG" else sweep["high"])
        sl = extreme - buffer_price if direction == "LONG" else extreme + buffer_price
        signal = make_signal(
            strategy_id=self.id,
            setup=self.name,
            data=data,
            direction=direction,
            entry=entry,
            sl_structural=sl,
            meta={
                "swept_level_type": side,
                "swept_level_tf": level_tf,
                "swept_level": round(level, cfg.DIGITS),
                "sweep_extreme": round(extreme, cfg.DIGITS),
                "confluence_choch": has_choch,
                "confluence_fvg_ob": has_fvg_ob,
                "would_block_cooldown": would_block_cooldown,
                "confirmation_bar_time": str(sweep["time"]),
            },
            confluences=[name for name, active in (
                ("CHoCH_M5", has_choch), ("FVG_OB_M5", has_fvg_ob)
            ) if active],
        )
        self._mark_bar_emitted(direction, sweep["time"])
        self._last_emission_at[direction] = data.now_utc
        return signal


_STRATEGY = SweepMicro()
SETUP = SetupSpec(
    name=_STRATEGY.name,
    scan=_STRATEGY.scan,
    killzone_mode="off",
    killzones=ALL,
    cooldown_seconds=cfg.S1_COOLDOWN_SECONDS,
    strategy_id=_STRATEGY.id,
    magic=_STRATEGY.magic,
)
