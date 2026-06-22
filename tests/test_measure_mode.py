"""Regression tests for the Raw/no-skip measurement cycle."""
from datetime import datetime, timezone
from types import SimpleNamespace
import sqlite3
import uuid

import pytest

import core.store as store
from config import cfg
from core.models import Signal
from core.sessions import get_killzone_tag
from execution.engine import try_execute
from mt5_client import OrderResult, _validated_prices


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "measure.db")
    store.init_db()


def signal(direction="LONG", structural_pips=36.4):
    sign = 1 if direction == "LONG" else -1
    entry = 2000.0
    return Signal(
        signal_id=uuid.uuid4().hex, ts_utc=datetime.now(timezone.utc),
        strategy="S1", direction=direction, killzone="ASIA", entry_type="MARKET",
        entry_price=entry, entry_zone_low=entry, entry_zone_high=entry,
        sl=entry - sign * structural_pips * cfg.PIP,
        tp1=entry + sign * 10 * cfg.PIP, tp2=entry + sign * 20 * cfg.PIP,
        sl_pips=structural_pips,
    )


def test_stop_validation_long_and_short_rounds_to_symbol_digits():
    assert _validated_prices("LONG", 2000.1234, 1999.9234, 2000.3234) == (
        1999.923, 2000.323, None)
    assert _validated_prices("SHORT", 2000.1234, 2000.3234, 1999.9234) == (
        2000.323, 1999.923, None)
    assert _validated_prices("LONG", 2000, 2000, 2001)[2] is not None


@pytest.mark.parametrize("hour,expected", [(1, "ASIA"), (8, "LONDON"),
                                              (14, "NY_AM"), (18, "NY_PM")])
def test_killzone_tag_is_continuous(hour, expected):
    dt = datetime(2026, 6, 22, hour, tzinfo=timezone.utc)
    assert get_killzone_tag(dt) == expected


def test_measure_mode_executes_and_records_would_block_tags(monkeypatch):
    monkeypatch.setattr("mt5_client.is_connected", lambda: True)
    monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [SimpleNamespace(ticket=1)])
    monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])
    monkeypatch.setattr("mt5_client.get_spread_pips", lambda symbol=None: 9.0)
    monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: SimpleNamespace(bid=1999.99, ask=2000.0))
    monkeypatch.setattr("execution.engine._cooldown_ok", lambda *args: False)
    monkeypatch.setattr("execution.engine.is_red_news_window", lambda dt: True)
    monkeypatch.setattr("execution.engine._vol_regime_now", lambda: "normal")
    captured = {}

    def place(**kwargs):
        captured.update(kwargs)
        return OrderResult(True, ticket=42, fill_price=2000.0, retcode=10009)

    monkeypatch.setattr("mt5_client.place_market", place)
    sig = signal()
    store.insert_signal(sig)
    try_execute(sig)

    con = sqlite3.connect(store.DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM trades WHERE signal_id=?", (sig.signal_id,)).fetchone()
    con.close()
    assert row["status"] == "OPEN"
    assert captured["sl"] == pytest.approx(2000.0 - 20 * cfg.PIP)
    assert row["sl_structural_pips"] == pytest.approx(36.4)
    assert row["would_block_position"] == 1
    assert row["would_block_cooldown"] == 1
    assert row["would_block_news"] == 1
    assert row["would_block_spread"] == 1
