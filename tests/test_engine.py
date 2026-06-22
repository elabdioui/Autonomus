"""Unit tests for the execution engine — gates, idempotency, pending lifecycle, orphan recovery.

All MT5 calls are patched; no live terminal required.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from core.models import Signal, TradeRecord
import core.store as store
from execution.engine import (
    try_execute, reconcile_pending_and_orphans, _already_placed, _cooldown_ok,
    ALL_MAGICS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    store.init_db()


def _sig(direction="LONG", entry_type="MARKET", strategy="S1",
         entry_price=2000.0, sl=1998.0, tp1=2001.0, tp2=2002.0, sl_pips=20.0) -> Signal:
    return Signal(
        signal_id=uuid.uuid4().hex,
        ts_utc=datetime.now(timezone.utc),
        strategy=strategy,
        direction=direction,
        killzone="LONDON",
        entry_type=entry_type,
        entry_price=entry_price,
        entry_zone_low=entry_price - 0.5,
        entry_zone_high=entry_price + 0.5,
        sl=sl, tp1=tp1, tp2=tp2, sl_pips=sl_pips,
        confluences=["Sweep"],
        score=3,
        context={},
    )


def _ok_market_result():
    from mt5_client import OrderResult
    return OrderResult(True, ticket=123456, fill_price=2000.1, retcode=10009, comment="done")


def _ok_limit_result():
    from mt5_client import OrderResult
    return OrderResult(True, ticket=999999, fill_price=None, retcode=10008, comment="placed")


def _fail_result(retcode=10006):
    from mt5_client import OrderResult
    return OrderResult(False, retcode=retcode, comment="rejected")


@pytest.fixture
def mt5_connected(monkeypatch):
    monkeypatch.setattr("mt5_client.is_connected", lambda: True)
    monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
    monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])
    monkeypatch.setattr("mt5_client.get_spread_pips", lambda symbol=None: 1.5)
    monkeypatch.setattr("reporting.news_tagger.is_red_news_window", lambda dt: False)
    # patch _vol_regime_now via the engine module
    import execution.engine as eng
    monkeypatch.setattr(eng, "_vol_regime_now", lambda: "normal")


# ── _already_placed / _cooldown_ok ────────────────────────────────────────────

class TestHelpers:
    def test_already_placed_false_when_no_trade(self):
        assert _already_placed("nonexistent") is False

    def test_already_placed_true_after_insert(self):
        sig = _sig()
        store.insert_signal(sig)
        trade = TradeRecord(
            trade_id=uuid.uuid4().hex, signal_id=sig.signal_id, mt5_ticket=1,
            strategy="S1", direction="LONG", lot=0.2, entry_price_fill=2000.0,
            entry_ts_utc=datetime.now(timezone.utc), sl_initial=1998.0,
            sl_current=1998.0, tp1=2001.0, tp2=2002.0, status="OPEN",
        )
        store.insert_trade(trade)
        assert _already_placed(sig.signal_id) is True

    def test_cooldown_ok_when_no_recent_close(self):
        assert _cooldown_ok("S1", "LONG") is True

    def test_cooldown_blocked_by_recent_close(self):
        sig = _sig()
        store.insert_signal(sig)
        trade = TradeRecord(
            trade_id=uuid.uuid4().hex, signal_id=sig.signal_id, mt5_ticket=1,
            strategy="S1", direction="LONG", lot=0.2, entry_price_fill=2000.0,
            entry_ts_utc=datetime.now(timezone.utc), sl_initial=1998.0,
            sl_current=1998.0, tp1=2001.0, tp2=2002.0, status="CLOSED",
            exit_ts_utc=datetime.now(timezone.utc), exit_reason="TP1_FULL",
        )
        store.insert_trade(trade)
        store.update_trade(trade.trade_id, status="CLOSED",
                           exit_ts_utc=datetime.now(timezone.utc))
        assert _cooldown_ok("S1", "LONG") is False

    def test_cooldown_ok_after_window(self):
        """A close older than COOLDOWN_MINUTES should not block."""
        from config import cfg
        sig = _sig()
        store.insert_signal(sig)
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=cfg.COOLDOWN_MINUTES + 1)
        trade = TradeRecord(
            trade_id=uuid.uuid4().hex, signal_id=sig.signal_id, mt5_ticket=1,
            strategy="S1", direction="LONG", lot=0.2, entry_price_fill=2000.0,
            entry_ts_utc=old_ts, sl_initial=1998.0, sl_current=1998.0,
            tp1=2001.0, tp2=2002.0, status="CLOSED",
            exit_ts_utc=old_ts, exit_reason="TP2",
        )
        store.insert_trade(trade)
        assert _cooldown_ok("S1", "LONG") is True


# ── Gate tests ────────────────────────────────────────────────────────────────

class TestGates:

    def test_contextual_conditions_are_recorded_not_gated(self, monkeypatch):
        fake_pos = MagicMock(); fake_pos.magic = 20001
        monkeypatch.setattr("mt5_client.is_connected", lambda: True)
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [fake_pos])
        monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])
        monkeypatch.setattr("mt5_client.get_spread_pips", lambda symbol=None: 9.9)
        monkeypatch.setattr("mt5_client.place_market", lambda **kw: _ok_market_result())
        monkeypatch.setattr("execution.engine._cooldown_ok", lambda *args: False)
        monkeypatch.setattr("execution.engine.is_red_news_window", lambda dt: True)
        monkeypatch.setattr("execution.engine._vol_regime_now", lambda: "normal")
        from config import cfg
        sig = _sig(sl_pips=cfg.SL_MAX_PIPS + 5)
        store.insert_signal(sig)
        try_execute(sig)
        con = sqlite3.connect(store.DB_PATH)
        con.row_factory = sqlite3.Row
        trade = con.execute("SELECT * FROM trades WHERE signal_id=?", (sig.signal_id,)).fetchone()
        status = con.execute("SELECT status FROM signals WHERE signal_id=?", (sig.signal_id,)).fetchone()[0]
        con.close()
        assert status == "EXECUTED"
        assert trade["would_block_position"] == 1
        assert trade["would_block_cooldown"] == 1
        assert trade["would_block_news"] == 1
        assert trade["would_block_spread"] == 1
        assert trade["sl_structural_pips"] == pytest.approx(cfg.SL_MAX_PIPS + 5)

    def test_gate5_disconnected_skipped(self, monkeypatch):
        monkeypatch.setattr("mt5_client.is_connected", lambda: False)
        monkeypatch.setattr("mt5_client.reconnect", lambda: False)

        sig = _sig(); store.insert_signal(sig)
        try_execute(sig)
        status = sqlite3.connect(store.DB_PATH).execute(
            "SELECT status FROM signals WHERE signal_id=?", (sig.signal_id,)
        ).fetchone()[0]
        assert status == "SKIPPED_ORDER_REJECTED"


# ── Successful execution ───────────────────────────────────────────────────────

class TestExecution:

    def test_market_order_placed_and_journaled(self, mt5_connected, monkeypatch):
        monkeypatch.setattr("mt5_client.place_market", lambda **kw: _ok_market_result())

        sig = _sig(entry_type="MARKET"); store.insert_signal(sig)
        try_execute(sig)

        con = sqlite3.connect(store.DB_PATH)
        sig_row = con.execute("SELECT status FROM signals WHERE signal_id=?",
                               (sig.signal_id,)).fetchone()
        trade_row = con.execute("SELECT status, mt5_ticket, entry_price_fill FROM trades "
                                 "WHERE signal_id=?", (sig.signal_id,)).fetchone()
        events = con.execute("SELECT event FROM trade_events WHERE trade_id IN "
                               "(SELECT trade_id FROM trades WHERE signal_id=?)",
                               (sig.signal_id,)).fetchall()
        con.close()

        assert sig_row[0] == "EXECUTED"
        assert trade_row[0] == "OPEN"
        assert trade_row[1] == 123456
        assert trade_row[2] == pytest.approx(2000.1, abs=0.01)
        assert any(e[0] == "FILLED" for e in events)

    def test_limit_order_placed_and_journaled(self, mt5_connected, monkeypatch):
        monkeypatch.setattr("mt5_client.place_limit", lambda **kw: _ok_limit_result())

        sig = _sig(entry_type="LIMIT"); store.insert_signal(sig)
        try_execute(sig)

        con = sqlite3.connect(store.DB_PATH)
        trade_row = con.execute("SELECT status, mt5_ticket FROM trades WHERE signal_id=?",
                                 (sig.signal_id,)).fetchone()
        events = con.execute("SELECT event FROM trade_events WHERE trade_id IN "
                               "(SELECT trade_id FROM trades WHERE signal_id=?)",
                               (sig.signal_id,)).fetchall()
        con.close()

        assert trade_row[0] == "PENDING"
        assert trade_row[1] == 999999
        assert any(e[0] == "PLACED" for e in events)

    def test_order_rejection_journaled(self, mt5_connected, monkeypatch):
        monkeypatch.setattr("mt5_client.place_market", lambda **kw: _fail_result())

        sig = _sig(); store.insert_signal(sig)
        try_execute(sig)

        status = sqlite3.connect(store.DB_PATH).execute(
            "SELECT status FROM signals WHERE signal_id=?", (sig.signal_id,)
        ).fetchone()[0]
        assert status == "SKIPPED_ORDER_REJECTED"

    def test_idempotency_two_calls_one_order(self, mt5_connected, monkeypatch):
        """Two try_execute calls for the same signal → exactly one order placed."""
        call_count = {"n": 0}
        def fake_place(**kw):
            call_count["n"] += 1
            return _ok_market_result()
        monkeypatch.setattr("mt5_client.place_market", fake_place)

        sig = _sig(); store.insert_signal(sig)
        try_execute(sig)
        try_execute(sig)   # second call

        assert call_count["n"] == 1

    def test_tp2_used_on_mt5_order(self, mt5_connected, monkeypatch):
        """The MT5 order must carry TP2, not TP1."""
        captured = {}
        def fake_place(**kw):
            captured.update(kw)
            return _ok_market_result()
        monkeypatch.setattr("mt5_client.place_market", fake_place)

        sig = _sig(tp1=2001.0, tp2=2003.0); store.insert_signal(sig)
        try_execute(sig)
        assert captured["tp"] == pytest.approx(2003.0, abs=0.01)

    def test_news_flag_written(self, mt5_connected, monkeypatch):
        monkeypatch.setattr("mt5_client.place_market", lambda **kw: _ok_market_result())
        monkeypatch.setattr("execution.engine.is_red_news_window", lambda dt: True)

        sig = _sig(); store.insert_signal(sig)
        try_execute(sig)

        row = sqlite3.connect(store.DB_PATH).execute(
            "SELECT news_flag FROM trades WHERE signal_id=?", (sig.signal_id,)
        ).fetchone()
        assert row[0] == 1


# ── Pending lifecycle ─────────────────────────────────────────────────────────

class TestPendingLifecycle:

    def _insert_pending_trade(self, ticket=999999, strategy="S1"):
        sig = _sig(entry_type="LIMIT", strategy=strategy)
        store.insert_signal(sig, status="EXECUTED")
        trade = TradeRecord(
            trade_id=uuid.uuid4().hex, signal_id=sig.signal_id, mt5_ticket=ticket,
            strategy=strategy, direction="LONG", lot=0.2, entry_price_fill=2000.0,
            entry_ts_utc=datetime.now(timezone.utc), sl_initial=1998.0,
            sl_current=1998.0, tp1=2001.0, tp2=2002.0, status="PENDING",
        )
        store.insert_trade(trade)
        return trade

    def test_pending_becomes_open_when_position_appears(self, monkeypatch):
        trade = self._insert_pending_trade(ticket=999999)

        fake_pos = MagicMock()
        fake_pos.ticket = 999999
        fake_pos.type = 0
        fake_pos.price_open = 2000.3
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [fake_pos])
        monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])

        reconcile_pending_and_orphans()

        row = sqlite3.connect(store.DB_PATH).execute(
            "SELECT status, entry_price_fill FROM trades WHERE trade_id=?",
            (trade.trade_id,)
        ).fetchone()
        assert row[0] == "OPEN"
        assert row[1] == pytest.approx(2000.3, abs=0.01)

        events = sqlite3.connect(store.DB_PATH).execute(
            "SELECT event FROM trade_events WHERE trade_id=?", (trade.trade_id,)
        ).fetchall()
        assert any(e[0] == "FILLED" for e in events)

    def test_pending_expires_when_order_vanishes(self, monkeypatch):
        trade = self._insert_pending_trade(ticket=999999)

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
        monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])

        reconcile_pending_and_orphans()

        row = sqlite3.connect(store.DB_PATH).execute(
            "SELECT status, exit_reason FROM trades WHERE trade_id=?",
            (trade.trade_id,)
        ).fetchone()
        assert row[0] == "CANCELLED"
        assert row[1] == "EXPIRED"

    def test_orphan_recovered(self, monkeypatch):
        """A position in MT5 with no DB trade → RECOVERED event created."""
        fake_pos = MagicMock()
        fake_pos.ticket = 777777
        fake_pos.type = 0
        fake_pos.price_open = 1999.5
        fake_pos.volume = 0.2
        fake_pos.sl = 1997.0
        fake_pos.tp = 2002.0
        fake_pos.time = int(datetime.now(timezone.utc).timestamp())
        fake_pos.comment = "SCALP|S1|abc123"
        fake_pos.magic = 20001

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [fake_pos])
        monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])

        reconcile_pending_and_orphans()

        con = sqlite3.connect(store.DB_PATH)
        trade = con.execute("SELECT * FROM trades WHERE mt5_ticket=?", (777777,)).fetchone()
        events = con.execute("SELECT event FROM trade_events WHERE trade_id=?",
                              (trade[0],)).fetchall()
        con.close()

        assert trade is not None
        assert any(e[0] == "RECOVERED" for e in events)

    def test_no_duplicate_recovery(self, monkeypatch):
        """Running reconcile twice for the same orphan creates only one trade row."""
        fake_pos = MagicMock()
        fake_pos.ticket = 888888
        fake_pos.type = 1   # SHORT
        fake_pos.price_open = 2005.0
        fake_pos.volume = 0.2
        fake_pos.sl = 2007.0
        fake_pos.tp = 2002.0
        fake_pos.time = int(datetime.now(timezone.utc).timestamp())
        fake_pos.comment = "SCALP|S2|xyz"
        fake_pos.magic = 20002

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [fake_pos])
        monkeypatch.setattr("mt5_client.get_pending_orders", lambda magic=None: [])

        reconcile_pending_and_orphans()
        reconcile_pending_and_orphans()   # second call

        con = sqlite3.connect(store.DB_PATH)
        count = con.execute("SELECT COUNT(*) FROM trades WHERE mt5_ticket=?",
                             (888888,)).fetchone()[0]
        con.close()
        assert count == 1
