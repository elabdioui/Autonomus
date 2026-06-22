"""In-process counters for every negative strategy detection decision."""
from __future__ import annotations

import logging
from collections import Counter

log = logging.getLogger("SCAN_STATS")


class ScanStats:
    def __init__(self) -> None:
        self.counts: Counter[tuple[str, str, str]] = Counter()

    def record(self, strategy: str, direction: str, reason: str) -> None:
        key = (strategy, direction, reason)
        self.counts[key] += 1
        log.debug("strategy=%s direction=%s reason=%s count=%d",
                  strategy, direction, reason, self.counts[key])


stats = ScanStats()
