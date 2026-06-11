"""Strategy ABC."""
from abc import ABC, abstractmethod
import pandas as pd
from core.models import Signal


class Strategy(ABC):
    strategy_id: str
    magic: int

    @abstractmethod
    def scan(self, tf_data: dict[str, pd.DataFrame]) -> Signal | None:
        """Return a Signal if a setup is detected on the current bar, else None."""
        ...
