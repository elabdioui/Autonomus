"""In-process counters for every negative strategy detection decision."""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

log = logging.getLogger("SCAN_STATS")


class ScanStats:
    def __init__(self) -> None:
        self.counts: Counter[tuple[str, str, str]] = Counter()
        self.first_seen: dict[tuple[str, str, str], str] = {}
        self.last_seen: dict[tuple[str, str, str], str] = {}

    def record(self, strategy: str, direction: str, reason: str) -> None:
        key = (strategy, direction, reason)
        self.counts[key] += 1
        now = datetime.now(timezone.utc).isoformat()
        self.first_seen.setdefault(key, now)
        self.last_seen[key] = now
        log.debug("strategy=%s direction=%s reason=%s count=%d",
                  strategy, direction, reason, self.counts[key])

    def snapshot(self) -> list[dict]:
        """Structured view, highest count first."""
        out = []
        for (strat, direction, reason), n in self.counts.most_common():
            key = (strat, direction, reason)
            out.append({
                "strategy": strat,
                "direction": direction,
                "reason": reason,
                "count": n,
                "first_seen_utc": self.first_seen.get(key),
                "last_seen_utc": self.last_seen.get(key),
            })
        return out

    def log_summary(self) -> None:
        """Emit one INFO summary block with a greppable SCAN_STATS prefix."""
        rows = self.snapshot()
        if not rows:
            log.info("SCAN_STATS summary - no rejections recorded yet")
            return

        total = sum(r["count"] for r in rows)
        log.info("SCAN_STATS summary - total_rejections=%d distinct=%d", total, len(rows))

        from collections import defaultdict
        from config import cfg

        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            grouped[(row["strategy"], row["direction"])].append(row)

        for (strat, direction), items in sorted(grouped.items()):
            sub_total = sum(item["count"] for item in items)
            top = ",".join(
                f"{item['reason']}:{item['count']}"
                for item in items[:cfg.SCAN_STATS_TOP_N]
            )
            log.info("SCAN_STATS %s %s total=%d top=%s", strat, direction, sub_total, top)

    def flush_to_db(self) -> None:
        """Persist cumulative counts. Telemetry failures never reach callers."""
        from config import cfg

        if not cfg.SCAN_STATS_PERSIST:
            return

        try:
            import core.store as _store

            for key, n in self.counts.items():
                strat, direction, reason = key
                _store.upsert_scan_stat(
                    strat,
                    direction,
                    reason,
                    n,
                    self.last_seen.get(key, datetime.now(timezone.utc).isoformat()),
                )
        except Exception as exc:
            log.error("scan_stats flush failed: %s", exc)

    def load_from_db(self) -> None:
        """Seed counters from persisted rows so counts survive restarts."""
        from config import cfg

        if not cfg.SCAN_STATS_PERSIST:
            return

        try:
            import core.store as _store

            for row in _store.get_scan_stats():
                key = (row["strategy"], row["direction"], row["reason"])
                self.counts[key] = row["count"]
                if row.get("first_seen_utc"):
                    self.first_seen[key] = row["first_seen_utc"]
                if row.get("last_seen_utc"):
                    self.last_seen[key] = row["last_seen_utc"]
        except Exception as exc:
            log.error("scan_stats load failed: %s", exc)


stats = ScanStats()
