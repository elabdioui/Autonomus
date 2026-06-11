"""S1 — Sweep + Displacement. Stub — implemented in SPEC 2."""
import pandas as pd
from strategies.base import Strategy
from core.models import Signal


class SweepDisplacement(Strategy):
    strategy_id = "S1"
    magic = 20001

    def scan(self, tf_data: dict[str, pd.DataFrame]) -> Signal | None:
        return None
