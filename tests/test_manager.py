"""Unit tests for execution/manager.py — position lifecycle state machine.

All MT5 calls are patched; no live terminal required.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import pytest

import core.store as store
from core.models import Signal, TradeRecord
from execution import manager as mgr
from execution.manager import manage_open_trades
from config import cfg

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    store.init_db()


@pytest.fixture(autouse=True)
def _clear_module_state():
    """Reset module-level BE state between tests."""
    mgr._be_pending.clear()
    mgr._be_retries.clear()
    yield
    mgr._be_pending.clear()
    mgr._be_retries.clear()


# ── Synthetic helpers ─────────────────────────────────────────────────────────

@dataclass
class FakeTick:
    bid: float
    ask: float


@dataclass
class FakePos:
    ticket: int
    type: int = 0          # 0=BUY (LONG), 1=SELL (SHORT)
    price_open: float = 2000.0
    volume: float = 0.2
    sl: float = 1998.0
    tp: float = 2002.0
    magic: int = 20001
    time: int = 0
    comment: str = ""


@dataclass
class FakeDeal:
    ticket: int
    entry: int             # 0=IN, 1=OUT, 3=OUT_BY
    price: float
    volume: float
    time: int
    profit: float = 0.0
    commission: float = 0.0
    swap: float = 0.0
    type: int = 0


def _make_trade(direction="LONG", strategy="S1", status="OPEN",
                entry=2000.0, sl=1998.0, tp1=2001.0, tp2=2002.0,
                lot=0.2, ticket=123456,
                entry_ts_utc: datetime | None = None) -> TradeRecord:
    sig = Signal(
        signal_id=uuid.uuid4().hex,
        ts_utc=datetime.now(timezone.utc),
        strategy=strategy, direction=direction,
        killzone="LONDON", entry_type="MARKET",
        entry_price=entry, entry_zone_low=entry-0.5, entry_zone_high=entry+0.5,
        sl=sl, tp1=tp1, tp2=tp2, sl_pips=abs(entry-sl)/0.10,
        confluences=[], score=0, context={},
    )
    store.insert_signal(sig, status="EXECUTED")
    trade = TradeRecord(
        trade_id=uuid.uuid4().hex,
        signal_id=sig.signal_id,
        mt5_ticket=ticket,
        strategy=strategy, direction=direction,
        lot=lot,
        entry_price_fill=entry,
        entry_ts_utc=entry_ts_utc or datetime.now(timezone.utc),
        sl_initial=sl, sl_current=sl,
        tp1=tp1, tp2=tp2,
        status=status,
    )
    store.insert_trade(trade)
    return trade


def _get_trade(trade_id: str) -> dict:
    con = sqlite3.connect(store.DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
    con.close()
    return dict(row) if row else {}


def _get_events(trade_id: str) -> list[str]:
    con = sqlite3.connect(store.DB_PATH)
    rows = con.execute("SELECT event FROM trade_events WHERE trade_id=? ORDER BY id",
                       (trade_id,)).fetchall()
    con.close()
    return [r[0] for r in rows]


# ── Helper to patch all MT5 calls for a "position exists, no triggers" scenario ──

def _patch_quiet(monkeypatch, trade: TradeRecord,
                  bid=2000.2, ask=2000.4):
    """Position open, price not at TP1, no modifications needed."""
    pos = FakePos(ticket=trade.mt5_ticket)
    monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
    monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: FakeTick(bid, ask))
    monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
    monkeypatch.setattr("mt5_client.close_position_partial",
                        lambda ticket, lot: __import__("mt5_client").OrderResult(True, ticket=ticket, fill_price=bid))
    monkeypatch.setattr("mt5_client.close_position_full",
                        lambda ticket: __import__("mt5_client").OrderResult(True, ticket=ticket, fill_price=bid))
    monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])


# ── MAE / MFE tests ───────────────────────────────────────────────────────────

class TestMaeMfe:

    def test_mae_increases_on_adverse_move(self, monkeypatch):
        trade = _make_trade("LONG", entry=2000.0)
        # Price 5 pips adverse (below entry)
        _patch_quiet(monkeypatch, trade, bid=1999.5, ask=1999.7)
        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["mae_pips"] >= 5.0 - 0.1   # 5 pips adverse

    def test_mfe_increases_on_favorable_move(self, monkeypatch):
        trade = _make_trade("LONG", entry=2000.0)
        _patch_quiet(monkeypatch, trade, bid=2000.8, ask=2001.0)
        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["mfe_pips"] >= 8.0 - 0.1

    def test_mae_never_decreases(self, monkeypatch):
        """After an adverse move, a price recovery must not reduce MAE."""
        trade = _make_trade("LONG", entry=2000.0)

        # Tick 1: 10 pips adverse
        _patch_quiet(monkeypatch, trade, bid=1999.0, ask=1999.2)
        manage_open_trades()
        row_after_adverse = _get_trade(trade.trade_id)
        mae_after_adverse = row_after_adverse["mae_pips"]
        assert mae_after_adverse >= 9.5

        # Tick 2: price recovers above entry — MAE must not drop
        _patch_quiet(monkeypatch, trade, bid=2001.0, ask=2001.2)
        # Reload trade from DB so the manager sees the updated mae_pips
        trades = store.get_open_trades()
        assert len(trades) == 1
        manage_open_trades()
        row_after_recovery = _get_trade(trade.trade_id)
        assert row_after_recovery["mae_pips"] >= mae_after_adverse - 0.1

    def test_mfe_never_decreases(self, monkeypatch):
        trade = _make_trade("LONG", entry=2000.0)
        _patch_quiet(monkeypatch, trade, bid=2001.5, ask=2001.7)
        manage_open_trades()
        row1 = _get_trade(trade.trade_id)

        _patch_quiet(monkeypatch, trade, bid=2000.2, ask=2000.4)
        manage_open_trades()
        row2 = _get_trade(trade.trade_id)
        assert row2["mfe_pips"] >= row1["mfe_pips"] - 0.1

    def test_short_mfe_on_downward_move(self, monkeypatch):
        """SHORT trade: price falling is favorable → MFE increases."""
        trade = _make_trade("SHORT", entry=2000.0, sl=2002.0, tp1=1999.0, tp2=1998.0)
        _patch_quiet(monkeypatch, trade, bid=1999.0, ask=1999.2)
        manage_open_trades()
        row = _get_trade(trade.trade_id)
        # ask=1999.2 for SHORT excursion = (2000-1999.2)/0.10 = 8 pips favorable
        assert row["mfe_pips"] >= 7.5


# ── TP1 partial + breakeven tests ────────────────────────────────────────────

class TestTp1Breakeven:

    def test_open_to_partial_on_tp1(self, monkeypatch):
        """TP1 trigger → status=PARTIAL, TP1_PARTIAL + SL_TO_BE events."""
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0, tp2=2002.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        # bid = entry + 10 pips = 2001.0 → exactly at TP1
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.0, ask=2001.2))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2001.0))
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["status"] == "PARTIAL"
        assert row["sl_current"] == pytest.approx(2000.0 + 0.5 * 0.10, abs=0.01)  # BE
        events = _get_events(trade.trade_id)
        assert "TP1_PARTIAL" in events
        assert "SL_TO_BE" in events

    def test_s3_uses_s3_tp1_pips(self, monkeypatch):
        """S3 uses S3_TP1_PIPS (8 pips) not the default TP1_PIPS (10)."""
        s3_tp1 = cfg.S3_TP1_PIPS * 0.10   # price offset
        trade = _make_trade("LONG", strategy="S3", entry=2000.0,
                            tp1=2000.0 + s3_tp1, tp2=2000.0 + cfg.S3_TP2_PIPS * 0.10)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        # bid exactly at S3_TP1 trigger
        # Use price 0.1 pip above the S3_TP1 threshold to avoid float precision issues
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2000.0 + s3_tp1 + 0.01, ask=2000.0 + s3_tp1 + 0.21))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2000.0 + s3_tp1 + 0.01))
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()
        assert _get_trade(trade.trade_id)["status"] == "PARTIAL"

    def test_no_tp1_when_below_trigger(self, monkeypatch):
        """Price 5 pips favorable < 10-pip TP1 → stays OPEN."""
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0)
        _patch_quiet(monkeypatch, trade, bid=2000.5, ask=2000.7)
        manage_open_trades()
        assert _get_trade(trade.trade_id)["status"] == "OPEN"

    def test_partial_close_rejected_still_moves_be(self, monkeypatch):
        """Partial close rejected → status PARTIAL anyway; SL_TO_BE event still fired."""
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.0, ask=2001.2))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(False, retcode=10006))
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()
        row = _get_trade(trade.trade_id)
        assert row["status"] == "PARTIAL"
        events = _get_events(trade.trade_id)
        assert "SL_TO_BE" in events

    def test_be_modify_fail_queues_retry(self, monkeypatch):
        """modify_position_sl fails → MODIFY_FAIL event, retried next tick."""
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.0, ask=2001.2))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2001.0))
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: False)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()

        assert trade.trade_id in mgr._be_pending
        assert "MODIFY_FAIL" in _get_events(trade.trade_id)

    def test_be_retry_succeeds_next_tick(self, monkeypatch):
        """Queued BE retry fires next tick and succeeds → SL_TO_BE event."""
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.0, ask=2001.2))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2001.0))

        # Tick 1: modify fails
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: False)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])
        manage_open_trades()
        assert trade.trade_id in mgr._be_pending

        # Tick 2: modify succeeds
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        manage_open_trades()

        assert trade.trade_id not in mgr._be_pending
        events = _get_events(trade.trade_id)
        sbe_events = [e for e in events if e == "SL_TO_BE"]
        assert len(sbe_events) >= 1

    def test_tp1_not_triggered_twice(self, monkeypatch):
        """After PARTIAL status, TP1 check is skipped on subsequent ticks."""
        trade = _make_trade("LONG", status="PARTIAL", entry=2000.0, tp1=2001.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        call_count = {"n": 0}
        def fake_partial(ticket, lot):
            call_count["n"] += 1
            return OrderResult(True, ticket=ticket, fill_price=2001.0)

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.5, ask=2001.7))
        monkeypatch.setattr("mt5_client.close_position_partial", fake_partial)
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()
        assert call_count["n"] == 0   # partial NOT called for PARTIAL trades


# ── Exit detection tests ──────────────────────────────────────────────────────

class TestExitDetection:

    def _exit_deals(self, exit_price, entry=2000.0, lot=0.2, entry_type=0,
                    profit=20.0, commission=-0.5, swap=0.0) -> list[FakeDeal]:
        import time as _time
        now_ts = int(_time.time())
        return [
            FakeDeal(ticket=1, entry=0, price=entry, volume=lot,
                     time=now_ts - 120, profit=0.0),   # entry deal
            FakeDeal(ticket=2, entry=1, price=exit_price, volume=lot,
                     time=now_ts, profit=profit, commission=commission, swap=swap),
        ]

    def test_tp2_exit_detected(self, monkeypatch):
        trade = _make_trade("LONG", entry=2000.0, tp2=2002.0)
        # Position gone → exit at TP2
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
        monkeypatch.setattr("mt5_client.get_deal_history",
                            lambda ticket: self._exit_deals(2002.0, profit=40.0))
        monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: FakeTick(2002.0, 2002.2))

        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "TP2"
        assert row["pnl_pips"] == pytest.approx(20.0, abs=0.5)  # (2002-2000)/0.10
        assert row["pnl_usd"] == pytest.approx(40.0 - 0.5, abs=0.1)

    def test_sl_exit_detected(self, monkeypatch):
        trade = _make_trade("LONG", entry=2000.0, sl=1998.0)
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
        monkeypatch.setattr("mt5_client.get_deal_history",
                            lambda ticket: self._exit_deals(1998.0, profit=-40.0))
        monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: FakeTick(1998.0, 1998.2))

        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["exit_reason"] == "SL"
        assert row["pnl_pips"] == pytest.approx(-20.0, abs=0.5)

    def test_be_exit_detected(self, monkeypatch):
        """Closed at breakeven SL → exit_reason=BE."""
        be_price = 2000.0 + 0.5 * 0.10  # 0.5 pips above entry
        trade = _make_trade("LONG", entry=2000.0, sl=1998.0, status="PARTIAL")
        # Set sl_current to BE price
        store.update_trade(trade.trade_id, sl_current=be_price)

        # Reload trade from DB
        trades = [t for t in store.get_open_trades() if t.trade_id == trade.trade_id]
        assert len(trades) == 1
        loaded = trades[0]

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
        monkeypatch.setattr("mt5_client.get_deal_history",
                            lambda ticket: self._exit_deals(be_price, profit=1.0))
        monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: FakeTick(be_price, be_price + 0.2))

        manage_open_trades()

        row = _get_trade(loaded.trade_id)
        assert row["exit_reason"] == "BE"

    def test_manual_exit_logged_as_manual(self, monkeypatch):
        trade = _make_trade("LONG", entry=2000.0, sl=1998.0, tp2=2002.0)
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
        # Exit at some random price (not near TP2, SL, or BE)
        monkeypatch.setattr("mt5_client.get_deal_history",
                            lambda ticket: self._exit_deals(2001.3, profit=13.0))
        monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: FakeTick(2001.3, 2001.5))

        manage_open_trades()
        assert _get_trade(trade.trade_id)["exit_reason"] == "MANUAL"

    def test_no_deal_history_still_closes(self, monkeypatch):
        """Even with empty deal history the trade is marked CLOSED."""
        trade = _make_trade("LONG")
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [])
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])
        monkeypatch.setattr("mt5_client.get_tick", lambda symbol=None: FakeTick(2001.0, 2001.2))

        manage_open_trades()
        assert _get_trade(trade.trade_id)["status"] == "CLOSED"


# ── Timeout tests ─────────────────────────────────────────────────────────────

class TestTimeout:

    def test_timeout_closes_position(self, monkeypatch):
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=cfg.TIMEOUT_MINUTES + 1)
        trade = _make_trade("LONG", entry_ts_utc=old_ts)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        closed = {"called": False}
        def fake_close(ticket):
            closed["called"] = True
            return OrderResult(True, ticket=ticket, fill_price=2000.5)

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2000.5, ask=2000.7))
        monkeypatch.setattr("mt5_client.close_position_full", fake_close)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()

        assert closed["called"]
        row = _get_trade(trade.trade_id)
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "TIMEOUT"
        assert "TIMEOUT_CLOSE" in _get_events(trade.trade_id)

    def test_no_timeout_within_window(self, monkeypatch):
        """Recent trade should not be timed out."""
        trade = _make_trade("LONG",
                             entry_ts_utc=datetime.now(timezone.utc) - timedelta(minutes=5))
        _patch_quiet(monkeypatch, trade)
        manage_open_trades()
        assert _get_trade(trade.trade_id)["status"] == "OPEN"


# ── Friday flat tests ─────────────────────────────────────────────────────────

class TestFridayFlat:

    def test_friday_flat_closes_at_cutoff(self, monkeypatch):
        trade = _make_trade("LONG")
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        from datetime import date
        # Build a Friday at 21:50 UTC
        friday = date(2024, 3, 1)   # 2024-03-01 is a Friday
        flat_time = datetime(friday.year, friday.month, friday.day,
                              21, 50, tzinfo=timezone.utc)

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2000.5, ask=2000.7))
        monkeypatch.setattr("mt5_client.close_position_full",
                            lambda ticket: OrderResult(True, ticket=ticket, fill_price=2000.5))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        # Inject now_utc directly into manager's _manage_one
        import execution.manager as m
        def patched_manage():
            trades = [t for t in store.get_open_trades() if t.status in ("OPEN", "PARTIAL")]
            all_pos = {pos.ticket: pos}
            for trade_ in trades:
                m._manage_one(trade_, all_pos, flat_time)
        monkeypatch.setattr(m, "manage_open_trades", patched_manage)

        m.manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["status"] == "CLOSED"
        events = _get_events(trade.trade_id)
        # Fix 1: must use FRIDAY_FLAT_CLOSE, not TIMEOUT_CLOSE
        assert "FRIDAY_FLAT_CLOSE" in events
        assert "TIMEOUT_CLOSE" not in events

    def test_not_friday_no_forced_close(self, monkeypatch):
        trade = _make_trade("LONG")
        _patch_quiet(monkeypatch, trade)
        # Monday at 21:50 — should NOT trigger friday flat
        import execution.manager as m
        from datetime import date
        monday = datetime(2024, 3, 4, 21, 50, tzinfo=timezone.utc)

        def patched_manage():
            trades = [t for t in store.get_open_trades() if t.status in ("OPEN", "PARTIAL")]
            pos = FakePos(ticket=trade.mt5_ticket)
            all_pos = {pos.ticket: pos}
            for trade_ in trades:
                m._manage_one(trade_, all_pos, monday)
        monkeypatch.setattr(m, "manage_open_trades", patched_manage)

        m.manage_open_trades()
        assert _get_trade(trade.trade_id)["status"] == "OPEN"


# ── Short trade tests ─────────────────────────────────────────────────────────

class TestShortTrades:

    def test_short_tp1_on_downward_move(self, monkeypatch):
        """SHORT: price falls 10 pips from entry → TP1 trigger."""
        entry = 2005.0
        trade = _make_trade("SHORT", entry=entry, sl=entry + 2.0,
                            tp1=entry - 1.0, tp2=entry - 2.0)
        pos = FakePos(ticket=trade.mt5_ticket, type=1)  # SELL

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        # ask at entry - 10 pips = 2004.0 → 10 pip favorable for SHORT
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2003.8, ask=2004.0))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2004.0))
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["status"] == "PARTIAL"
        be = row["sl_current"]
        # BE for SHORT = entry - 0.5 pip buffer
        assert be == pytest.approx(entry - 0.5 * 0.10, abs=0.01)


# ── Fix 1: FRIDAY_FLAT exit reason ────────────────────────────────────────────

class TestFridayFlatExitReason:

    def test_friday_flat_exit_reason_is_friday_flat(self, monkeypatch):
        """Friday-flat close must write exit_reason=FRIDAY_FLAT, not TIMEOUT."""
        trade = _make_trade("LONG")
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        from datetime import date
        friday = date(2024, 3, 1)
        flat_time = datetime(friday.year, friday.month, friday.day, 21, 50, tzinfo=timezone.utc)

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2000.5, ask=2000.7))
        monkeypatch.setattr("mt5_client.close_position_full",
                            lambda ticket: OrderResult(True, ticket=ticket, fill_price=2000.5))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        import execution.manager as m
        def patched_manage():
            trades = [t for t in store.get_open_trades() if t.status in ("OPEN", "PARTIAL")]
            all_pos = {pos.ticket: pos}
            for trade_ in trades:
                m._manage_one(trade_, all_pos, flat_time)
        monkeypatch.setattr(m, "manage_open_trades", patched_manage)
        m.manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["exit_reason"] == "FRIDAY_FLAT"
        events = _get_events(trade.trade_id)
        assert "FRIDAY_FLAT_CLOSE" in events
        assert "TIMEOUT_CLOSE" not in events


# ── Fix 2: accurate PnL on forced closes ─────────────────────────────────────

class TestFinalizeClosePnl:

    def _out_deals(self, exit_price, entry=2000.0, lot=0.2,
                   profit=15.0, commission=-0.5) -> list[FakeDeal]:
        import time as _time
        now_ts = int(_time.time())
        return [
            FakeDeal(ticket=1, entry=0, price=entry, volume=lot, time=now_ts - 120),
            FakeDeal(ticket=2, entry=1, price=exit_price, volume=lot,
                     time=now_ts, profit=profit, commission=commission),
        ]

    def test_timeout_pnl_from_deals(self, monkeypatch):
        """TIMEOUT close: pnl_pips and pnl_usd come from deal history, not tick excursion."""
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=cfg.TIMEOUT_MINUTES + 1)
        trade = _make_trade("LONG", entry=2000.0, entry_ts_utc=old_ts)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        exit_price = 2001.5   # 15 pips profit
        deals = self._out_deals(exit_price, profit=30.0)

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2000.1, ask=2000.3))  # tick: 1 pip
        monkeypatch.setattr("mt5_client.close_position_full",
                            lambda ticket: OrderResult(True, ticket=ticket, fill_price=exit_price))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: deals)

        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "TIMEOUT"
        # PnL must come from deals (15 pips), not tick excursion (1 pip)
        assert row["pnl_pips"] == pytest.approx(15.0, abs=0.5)
        assert row["pnl_usd"] == pytest.approx(30.0 - 0.5, abs=0.1)

    def test_timeout_pnl_fallback_when_no_deals(self, monkeypatch):
        """Empty deal history → excursion fallback, pnl_usd NULL, trade CLOSED."""
        old_ts = datetime.now(timezone.utc) - timedelta(minutes=cfg.TIMEOUT_MINUTES + 1)
        trade = _make_trade("LONG", entry=2000.0, entry_ts_utc=old_ts)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2002.0, ask=2002.2))  # 20 pips up
        monkeypatch.setattr("mt5_client.close_position_full",
                            lambda ticket: OrderResult(True, ticket=ticket, fill_price=2002.0))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        manage_open_trades()

        row = _get_trade(trade.trade_id)
        assert row["status"] == "CLOSED"
        assert row["exit_reason"] == "TIMEOUT"
        # Fallback: pnl_pips from tick excursion, pnl_usd NULL
        assert row["pnl_pips"] is not None
        assert row["pnl_usd"] is None


# ── Fix 3: breakeven DB persistence ──────────────────────────────────────────

class TestBeDbPersistence:

    def test_partial_trade_be_recheck_after_restart(self, monkeypatch):
        """PARTIAL trade with sl_current==sl_initial and be_target=NULL in DB:
        stateless re-check must call modify_position_sl with the computed BE price."""
        # Insert PARTIAL trade directly (simulates post-restart: in-memory dicts empty)
        trade = _make_trade("LONG", status="PARTIAL", entry=2000.0, sl=1998.0,
                            tp1=2001.0, tp2=2002.0)
        # sl_current == sl_initial, be_target = NULL (the crash-before-queue scenario)
        assert mgr._be_pending == {}

        pos = FakePos(ticket=trade.mt5_ticket)
        modify_calls = []

        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.5, ask=2001.7))
        monkeypatch.setattr("mt5_client.close_position_full",
                            lambda ticket: __import__("mt5_client").OrderResult(True, ticket=ticket))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        def fake_modify(ticket, sl):
            modify_calls.append(sl)
            return True
        monkeypatch.setattr("mt5_client.modify_position_sl", fake_modify)

        manage_open_trades()

        expected_be = round(2000.0 + 0.5 * 0.10, 2)
        assert any(abs(sl - expected_be) < 0.01 for sl in modify_calls), \
            f"modify_position_sl not called with BE price. calls={modify_calls}"

        row = _get_trade(trade.trade_id)
        # After success: be_target should be cleared
        assert row["be_target"] is None
        assert row["sl_current"] == pytest.approx(expected_be, abs=0.01)

    def test_be_target_persisted_and_cleared(self, monkeypatch):
        """Queued BE → be_target set in DB; successful modify → be_target NULL, sl_current updated."""
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0, tp2=2002.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.0, ask=2001.2))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2001.0))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])

        # Tick 1: modify fails → be_target written to DB
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: False)
        manage_open_trades()
        row = _get_trade(trade.trade_id)
        expected_be = round(2000.0 + 0.5 * 0.10, 2)
        assert row["be_target"] == pytest.approx(expected_be, abs=0.01)
        assert row["be_retries"] >= 1

        # Tick 2: modify succeeds → be_target cleared
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: True)
        manage_open_trades()
        row = _get_trade(trade.trade_id)
        assert row["be_target"] is None
        assert row["sl_current"] == pytest.approx(expected_be, abs=0.01)

    def test_be_giveup_after_max_retries(self, monkeypatch):
        """modify always fails → after cap, BE_GIVEUP event once, no further attempts."""
        from execution.manager import _BE_MAX_RETRIES
        trade = _make_trade("LONG", entry=2000.0, tp1=2001.0, tp2=2002.0)
        pos = FakePos(ticket=trade.mt5_ticket)

        from mt5_client import OrderResult
        monkeypatch.setattr("mt5_client.get_positions", lambda magic=None: [pos])
        monkeypatch.setattr("mt5_client.get_tick",
                            lambda symbol=None: FakeTick(bid=2001.0, ask=2001.2))
        monkeypatch.setattr("mt5_client.close_position_partial",
                            lambda ticket, lot: OrderResult(True, ticket=ticket, fill_price=2001.0))
        monkeypatch.setattr("mt5_client.get_deal_history", lambda ticket: [])
        monkeypatch.setattr("mt5_client.modify_position_sl", lambda ticket, sl: False)

        # Run enough ticks to hit the cap (first tick triggers TP1+queues, rest retry)
        for _ in range(_BE_MAX_RETRIES + 2):
            manage_open_trades()

        events = _get_events(trade.trade_id)
        giveup_events = [e for e in events if e == "BE_GIVEUP"]
        assert len(giveup_events) == 1, f"Expected exactly 1 BE_GIVEUP, got {len(giveup_events)}"

        # After give-up, further ticks must not call modify_position_sl anymore
        call_count = {"n": 0}
        def counting_modify(ticket, sl):
            call_count["n"] += 1
            return False
        monkeypatch.setattr("mt5_client.modify_position_sl", counting_modify)
        manage_open_trades()
        assert call_count["n"] == 0, "modify_position_sl called after BE_GIVEUP"


# ── Fix 1 + metrics: FRIDAY_FLAT in exits breakdown ──────────────────────────

class TestMetricsFridayFlat:

    def test_metrics_includes_friday_flat(self, monkeypatch):
        """compute_stats exits breakdown must include FRIDAY_FLAT key."""
        from reporting.metrics import compute_stats, _EXIT_REASONS

        assert "FRIDAY_FLAT" in _EXIT_REASONS, "_EXIT_REASONS missing FRIDAY_FLAT"

        # Insert a closed FRIDAY_FLAT trade directly
        trade = _make_trade("LONG", entry=2000.0)
        store.update_trade(trade.trade_id, status="CLOSED",
                           exit_reason="FRIDAY_FLAT", pnl_pips=5.0,
                           exit_ts_utc=datetime.now(timezone.utc))

        stats = compute_stats()
        assert "FRIDAY_FLAT" in stats.exits
        assert stats.exits["FRIDAY_FLAT"] == 1
