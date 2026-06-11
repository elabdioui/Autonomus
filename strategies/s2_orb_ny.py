"""S2 — Opening Range Breakout NY. Stub — implemented in SPEC 2."""
import pandas as pd
from strategies.base import Strategy
from core.models import Signal


class OrbNy(Strategy):
    strategy_id = "S2"
    magic = 20002

    def scan(self, tf_data: dict[str, pd.DataFrame]) -> Signal | None:
        return None
