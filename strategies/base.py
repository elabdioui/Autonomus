"""Shared contracts and non-blocking context tags for SPEC D strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable
import uuid

import pandas as pd

from config import cfg
from core.models import Signal
from core.scan_stats import stats
from core.sessions import get_killzone_tag
from indicators.structure import determine_bias, find_swings
from reporting.news_tagger import is_red_news_window
from strategies import _bars


ALL = frozenset({"ASIA", "LONDON", "NY_AM", "NY_PM", "OFF"})


@dataclass
class MarketData:
    m1: pd.DataFrame
    m5: pd.DataFrame
    m15: pd.DataFrame
    h1: pd.DataFrame
    current_price: float
    spread_pips: float
    killzone: str | None
    now_utc: datetime
    h4: pd.DataFrame | None = None


@dataclass(frozen=True)
class SetupSpec:
    """Registry entry. Imports in ``main.py`` are the sole activation switch."""

    name: str
    scan: Callable[[MarketData, str], Signal | None]
    killzone_mode: str = "off"
    killzones: frozenset[str] = field(default_factory=lambda: ALL)
    cooldown_seconds: int = 0
    strategy_id: str = ""
    magic: int = 0


def _frame_bias(raw: pd.DataFrame | None) -> str:
    frame = _bars.closed(raw)
    if len(frame) < cfg.SWING_LOOKBACK * 2 + 2:
        return "NEUTRAL"
    bias = determine_bias(find_swings(frame, cfg.SWING_LOOKBACK))
    return {"BULLISH": "LONG", "BEARISH": "SHORT"}.get(bias, "NEUTRAL")


def htf_bias(data: MarketData) -> str:
    """Combine confirmed H4 and H1 structure without ever gating a signal."""
    h4_bias = _frame_bias(data.h4)
    h1_bias = _frame_bias(data.h1)
    if h4_bias == h1_bias:
        return h1_bias
    if h4_bias == "NEUTRAL":
        return h1_bias
    if h1_bias == "NEUTRAL":
        return h4_bias
    return "NEUTRAL"


def premium_discount(data: MarketData, entry: float) -> str:
    h1 = _bars.closed(data.h1)
    if h1.empty:
        return "EQ"
    dealing_range = h1.iloc[-24:]
    high = float(dealing_range["high"].max())
    low = float(dealing_range["low"].min())
    equilibrium = (high + low) / 2
    if abs(entry - equilibrium) <= cfg.PIP:
        return "EQ"
    return "PREMIUM" if entry > equilibrium else "DISCOUNT"


def common_tags(data: MarketData, direction: str, entry: float) -> dict:
    bias = htf_bias(data)
    news = is_red_news_window(data.now_utc)
    return {
        "killzone": get_killzone_tag(data.now_utc),
        "htf_bias": bias,
        "bias_aligned": bias == direction,
        "news_red_active": "unknown" if news is None else bool(news),
        "spread_at_entry": round(float(data.spread_pips), 2),
        "premium_discount": premium_discount(data, entry),
    }


def make_signal(
    *,
    strategy_id: str,
    setup: str,
    data: MarketData,
    direction: str,
    entry: float,
    sl_structural: float,
    meta: dict,
    tp_final: float | None = None,
    entry_zone: tuple[float, float] | None = None,
    confluences: list[str] | None = None,
) -> Signal:
    """Build the common signal schema using the executed 20-pip R."""
    sign = 1 if direction == "LONG" else -1
    entry = round(float(entry), cfg.DIGITS)
    sl_structural = round(float(sl_structural), cfg.DIGITS)
    tp1 = round(entry + sign * cfg.TP1_PIPS * cfg.PIP, cfg.DIGITS)
    if tp_final is None:
        tp_final = entry + sign * cfg.TP2_PIPS * cfg.PIP
    tp_final = round(float(tp_final), cfg.DIGITS)
    zone_low, zone_high = entry_zone or (entry, entry)
    context = common_tags(data, direction, entry)
    context.update(meta)
    return Signal(
        signal_id=uuid.uuid4().hex,
        ts_utc=data.now_utc,
        strategy=strategy_id,
        setup=setup,
        direction=direction,
        killzone=context["killzone"],
        entry_type="MARKET",
        entry_price=entry,
        entry_zone_low=round(float(zone_low), cfg.DIGITS),
        entry_zone_high=round(float(zone_high), cfg.DIGITS),
        sl=sl_structural,
        tp1=tp1,
        tp2=tp_final,
        sl_pips=round(abs(entry - sl_structural) / cfg.PIP, 1),
        confluences=confluences or [],
        score=len(confluences or []),
        context=context,
    )


class Strategy(ABC):
    id: str
    name: str
    magic: int
    sessions: set[str] = set(ALL)

    def __init__(self) -> None:
        self._emitted: dict[tuple[str, float], datetime] = {}
        self._emitted_bars: set[tuple[str, str]] = set()

    def _already_emitted(self, direction: str, level: float) -> bool:
        key = (direction, round(level, cfg.DIGITS))
        ts = self._emitted.get(key)
        return bool(ts and (datetime.now(timezone.utc) - ts).total_seconds() < 3600)

    def _mark_emitted(self, direction: str, level: float) -> None:
        now = datetime.now(timezone.utc)
        self._emitted[(direction, round(level, cfg.DIGITS))] = now
        cutoff = now.timestamp() - 3600
        self._emitted = {k: v for k, v in self._emitted.items() if v.timestamp() > cutoff}

    def _bar_was_emitted(self, direction: str, bar_time) -> bool:
        return (direction, str(bar_time)) in self._emitted_bars

    def _mark_bar_emitted(self, direction: str, bar_time) -> None:
        self._emitted_bars.add((direction, str(bar_time)))
        if len(self._emitted_bars) > 100:
            self._emitted_bars = set(list(self._emitted_bars)[-50:])

    def _reject(self, direction: str, reason: str) -> None:
        stats.record(self.id, direction, reason)
        return None

    @abstractmethod
    def scan(self, data: MarketData, direction: str) -> Signal | None:
        """Return a signal or record one explicit rejection reason."""
        ...
