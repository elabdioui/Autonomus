"""Strategy ABC and shared MarketData container."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import pandas as pd

from core.models import Signal


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


class Strategy(ABC):
    id: str
    name: str
    magic: int
    sessions: set[str]  # killzone names where this strategy may scan

    def __init__(self) -> None:
        # (direction, round(key_level, 1)) -> emission timestamp
        self._emitted: dict[tuple[str, float], datetime] = {}

    def _already_emitted(self, direction: str, level: float) -> bool:
        key = (direction, round(level, 1))
        ts = self._emitted.get(key)
        if ts is None:
            return False
        return (datetime.now(timezone.utc) - ts).total_seconds() < 3600

    def _mark_emitted(self, direction: str, level: float) -> None:
        now = datetime.now(timezone.utc)
        self._emitted[(direction, round(level, 1))] = now
        cutoff = now.timestamp() - 3600
        self._emitted = {k: v for k, v in self._emitted.items() if v.timestamp() > cutoff}

    @abstractmethod
    def scan(self, data: MarketData, direction: str) -> Signal | None:
        """Return a Signal if a setup fires on this tick, else None.
        The runner decides DETECTED vs SKIPPED_SL_TOO_WIDE; strategy always returns Signal."""
        ...
