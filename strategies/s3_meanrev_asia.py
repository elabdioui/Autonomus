"""S3 — Mean Reversion Asia. Stub — implemented in SPEC 2."""
import pandas as pd
from strategies.base import Strategy
from core.models import Signal


class MeanRevAsia(Strategy):
    strategy_id = "S3"
    magic = 20003

    def scan(self, tf_data: dict[str, pd.DataFrame]) -> Signal | None:
        return None
