"""Round-trip tests for the SQLite store (SPEC 1 §5)."""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _patch_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp directory for each test."""
    import core.store as store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    store.init_db()


from core.models import Signal, TradeRecord
import core.store as store


def _sig() -> Signal:
    return Signal(
        signal_id=uuid.uuid4().hex,
        ts_utc=datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc),
        strategy="S1",
        direction="LONG",
        killzone="LONDON",
        entry_type="MARKET",
        entry_price=2300.0,
        entry_zone_low=2298.0,
        entry_zone_high=2302.0,
        sl=2290.0,
        tp1=2310.0,
        tp2=2320.0,
        sl_pips=10.0,
        confluences=["Sweep", "FVG_M5"],
        score=5,
        context={"swept_level": 2295.0},
    )


def _trade(sig: Signal) -> TradeRecord:
    return TradeRecord(
        trade_id=uuid.uuid4().hex,
        signal_id=sig.signal_id,
        mt5_ticket=123456,
        strategy="S1",
        direction="LONG",
        lot=0.2,
        entry_price_fill=2300.5,
        entry_ts_utc=datetime(2024, 3, 1, 10, 1, tzinfo=timezone.utc),
        sl_initial=2290.0,
        sl_current=2290.0,
        tp1=2310.0,
        tp2=2320.0,
        status="OPEN",
    )


class TestSignalRoundTrip:
    def test_insert_and_read_back(self):
        sig = _sig()
        store.insert_signal(sig, status="DETECTED")

        import sqlite3
        con = sqlite3.connect(store.DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM signals WHERE signal_id=?", (sig.signal_id,)).fetchone()
        assert row is not None
        assert row["strategy"] == "S1"
        assert row["direction"] == "LONG"
        assert row["status"] == "DETECTED"
        assert json.loads(row["confluences"]) == ["Sweep", "FVG_M5"]

    def test_update_status(self):
        sig = _sig()
        store.insert_signal(sig)
        store.update_signal_status(sig.signal_id, "SKIPPED_SPREAD", "spread=5.1")

        import sqlite3
        con = sqlite3.connect(store.DB_PATH)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT status, skip_reason FROM signals WHERE signal_id=?", (sig.signal_id,)).fetchone()
        assert row["status"] == "SKIPPED_SPREAD"
        assert row["skip_reason"] == "spread=5.1"

    def test_skipped_signal_still_stored(self):
        """Every signal must be written to DB even when skipped."""
        sig = _sig()
        store.insert_signal(sig, status="SKIPPED_SL_TOO_WIDE", skip_reason="sl_pips=25")
        import sqlite3
        con = sqlite3.connect(store.DB_PATH)
        count = con.execute("SELECT COUNT(*) FROM signals WHERE signal_id=?", (sig.signal_id,)).fetchone()[0]
        assert count == 1


class TestTradeRoundTrip:
    def test_insert_and_read_open_trades(self):
        sig = _sig()
        store.insert_signal(sig)
        trade = _trade(sig)
        store.insert_trade(trade)

        open_trades = store.get_open_trades()
        assert any(t.trade_id == trade.trade_id for t in open_trades)

    def test_get_trade_by_ticket(self):
        sig = _sig()
        store.insert_signal(sig)
        trade = _trade(sig)
        store.insert_trade(trade)

        found = store.get_trade_by_ticket(123456)
        assert found is not None
        assert found.strategy == "S1"
        assert found.lot == pytest.approx(0.2)

    def test_update_trade(self):
        sig = _sig()
        store.insert_signal(sig)
        trade = _trade(sig)
        store.insert_trade(trade)
        store.update_trade(trade.trade_id, status="CLOSED", pnl_pips=9.5, exit_reason="TP1_FULL")

        found = store.get_trade_by_ticket(123456)
        assert found.status == "CLOSED"
        assert found.pnl_pips == pytest.approx(9.5)
        assert found.exit_reason == "TP1_FULL"


class TestEventRoundTrip:
    def test_insert_event(self):
        sig = _sig()
        store.insert_signal(sig)
        trade = _trade(sig)
        store.insert_trade(trade)
        store.insert_event(trade.trade_id, "PLACED", {"price": 2300.5})

        import sqlite3
        con = sqlite3.connect(store.DB_PATH)
        rows = con.execute("SELECT * FROM trade_events WHERE trade_id=?", (trade.trade_id,)).fetchall()
        assert len(rows) == 1
        assert rows[0][3] == "PLACED"  # columns: id, trade_id, ts_utc, event, detail


class TestHeartbeat:
    def test_upsert_heartbeat(self):
        store.upsert_heartbeat(0, "LONDON")
        store.upsert_heartbeat(1, "NY_AM")

        import sqlite3
        con = sqlite3.connect(store.DB_PATH)
        row = con.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
        assert row is not None
        # Second upsert should win
        assert row[2] == 1 or row["open_positions"] == 1
