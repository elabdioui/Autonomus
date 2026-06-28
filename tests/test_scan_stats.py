"""Tests for scan-stat rejection observability."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging

import pytest

import core.store as store
from config import cfg
from core.scan_stats import ScanStats


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(cfg, "SCAN_STATS_PERSIST", False)
    store.init_db()


def test_record_increments_and_tracks_seen():
    stats = ScanStats()

    stats.record("S1", "LONG", "R")
    first = stats.first_seen[("S1", "LONG", "R")]
    stats.record("S1", "LONG", "R")

    assert stats.counts[("S1", "LONG", "R")] == 2
    assert stats.first_seen[("S1", "LONG", "R")] == first
    assert stats.last_seen[("S1", "LONG", "R")] >= first


def test_snapshot_sorted_desc():
    stats = ScanStats()
    stats.record("S1", "LONG", "R1")
    stats.record("S2", "SHORT", "R2")
    stats.record("S2", "SHORT", "R2")

    rows = stats.snapshot()

    assert [row["count"] for row in rows] == [2, 1]
    assert set(rows[0]) == {
        "strategy", "direction", "reason", "count",
        "first_seen_utc", "last_seen_utc",
    }


def test_log_summary_emits_info(caplog, monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_STATS_TOP_N", 5)
    stats = ScanStats()
    stats.record("S1", "LONG", "R")

    with caplog.at_level(logging.INFO, logger="SCAN_STATS"):
        stats.log_summary()

    messages = [record.getMessage() for record in caplog.records]
    assert any("SCAN_STATS" in msg for msg in messages)
    assert any("S1" in msg for msg in messages)


def test_flush_then_get_roundtrip(monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_STATS_PERSIST", True)
    stats = ScanStats()
    stats.record("S1", "LONG", "R")
    stats.record("S1", "LONG", "R")
    stats.record("S2", "SHORT", "X")

    stats.flush_to_db()
    rows = store.get_scan_stats()

    counts = {
        (row["strategy"], row["direction"], row["reason"]): row["count"]
        for row in rows
    }
    assert counts == dict(stats.counts)


def test_flush_is_replace_not_additive(monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_STATS_PERSIST", True)
    stats = ScanStats()
    stats.record("S1", "LONG", "R")

    stats.flush_to_db()
    stats.flush_to_db()

    rows = store.get_scan_stats()
    assert len(rows) == 1
    assert rows[0]["count"] == 1


def test_load_from_db_seeds_counts(monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_STATS_PERSIST", True)
    stats = ScanStats()
    stats.record("S1", "LONG", "R")
    stats.record("S1", "LONG", "R")
    stats.flush_to_db()

    fresh = ScanStats()
    fresh.load_from_db()

    key = ("S1", "LONG", "R")
    assert fresh.counts[key] == 2
    assert fresh.first_seen[key]
    assert fresh.last_seen[key]

    fresh.record("S1", "LONG", "R")
    assert fresh.counts[key] == 3


def test_persist_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(cfg, "SCAN_STATS_PERSIST", False)
    monkeypatch.setattr(store, "upsert_scan_stat", lambda *args: pytest.fail("touched DB"))
    monkeypatch.setattr(store, "get_scan_stats", lambda: pytest.fail("touched DB"))
    stats = ScanStats()
    stats.record("S1", "LONG", "R")

    stats.flush_to_db()
    stats.load_from_db()


def test_flush_never_raises_on_db_error(monkeypatch, caplog):
    monkeypatch.setattr(cfg, "SCAN_STATS_PERSIST", True)

    def boom(*args):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "upsert_scan_stat", boom)
    stats = ScanStats()
    stats.record("S1", "LONG", "R")

    with caplog.at_level(logging.ERROR, logger="SCAN_STATS"):
        stats.flush_to_db()

    assert any("scan_stats flush failed" in record.getMessage() for record in caplog.records)
