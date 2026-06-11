"""S4 — Swing Failure Pattern Asia. Stub — implemented in SPEC 2."""
import pandas as pd
from strategies.base import Strategy
from core.models import Signal


class SfpAsia(Strategy):
    strategy_id = "S4"
    magic = 20004

    def scan(self, tf_data: dict[str, pd.DataFrame]) -> Signal | None:
        return None
