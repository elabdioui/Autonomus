"""Unit tests for S1–S4 strategies — synthetic DataFrames, no MT5."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import pytest
from datetime import datetime, timezone, timedelta

from strategies.base import MarketData
from strategies.s1_sweep_displacement import SweepDisplacement
from strategies.s2_orb_ny import OrbNy
from strategies.s3_meanrev_asia import MeanRevAsia
from strategies.s4_sfp_asia import SfpAsia
from config import cfg

# ── Low-level helpers ──────────────────────────────────────────────────────────

def _bar(t, o, h, l, c, v=100.0):
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return {"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat(n, price=2000.0, start=None, step_min=60):
    base = start or datetime(2024, 3, 1, tzinfo=timezone.utc)
    return pd.DataFrame([
        _bar(base + timedelta(minutes=i * step_min),
             price + (i % 3 - 1) * 0.05,
             price + 0.1, price - 0.1,
             price + (i % 3 - 1) * 0.05)
        for i in range(n)
    ])


# ── H1 bias helpers ────────────────────────────────────────────────────────────

def _bullish_h1():
    """34 H1 bars with two confirmed swing HIGHs (HH) and two swing LOWs (HL).

    ZigZag: descend→LOW1(bar5)→HIGH1(bar11)→LOW2(bar17)→HIGH2(bar23)→tail(bars24-32)→forming(33)
    lookback=5; HIGH2 confirmed_index=28; after iloc[:-1]=33 bars, max_confirmed=31 >= 28 ✓
    """
    base = datetime(2024, 3, 1, tzinfo=timezone.utc)
    closes = (
        [2001.0, 2000.8, 2000.6, 2000.4, 2000.2]   # bars 0-4
        + [1999.0]                                    # bar 5: LOW1 (low=1998.6)
        + [1999.1, 1999.3, 1999.6, 2000.0, 2000.5]  # bars 6-10
        + [2002.0]                                    # bar 11: HIGH1 (high=2002.4)
        + [2001.5, 2001.0, 2000.8, 2000.6, 2000.4]  # bars 12-16
        + [1999.5]                                    # bar 17: LOW2 (low=1999.1) > LOW1 ✓
        + [1999.8, 2000.2, 2001.0, 2001.5, 2002.0]  # bars 18-22
        + [2004.0]                                    # bar 23: HIGH2 (high=2004.4) > HIGH1 ✓
        + [2003.5, 2003.0, 2002.8, 2002.5, 2002.2,  # bars 24-28: tail for HIGH2 right side
           2002.0, 2001.8, 2001.6, 2001.4]           # bars 29-32: extra tail
        + [2001.2]                                    # bar 33: forming (excluded by iloc[:-1])
    )
    rows = []
    for i, c in enumerate(closes):
        rows.append(_bar(base + timedelta(hours=i), c, c + 0.4, c - 0.4, c))
    return pd.DataFrame(rows)


def _bearish_h1():
    """Mirror of _bullish_h1: LH+LL BEARISH bias."""
    df = _bullish_h1()
    mid = 2001.5
    for col in ("open", "close"):
        df[col] = 2 * mid - df[col]
    df["high"], df["low"] = 2 * mid - df["low"], 2 * mid - df["high"]
    return df


# ── M5 swing helpers ───────────────────────────────────────────────────────────

def _m5_with_swing_low(swing_price=1998.0, lookback=5, n_extra=3):
    """M5 df: confirmed swing LOW at bar `lookback`. Total = 2*lookback+1+n_extra+1 bars."""
    base = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = []
    flat = swing_price + 2.0
    for i in range(lookback):
        p = flat - i * 0.01
        rows.append(_bar(base + timedelta(minutes=i * 5), p, p + 0.5, p - 0.5, p))
    # swing low bar
    rows.append(_bar(base + timedelta(minutes=lookback * 5),
                     swing_price + 0.1, swing_price + 0.3, swing_price, swing_price + 0.2))
    for i in range(1, lookback + 1):
        p = flat - i * 0.01
        rows.append(_bar(base + timedelta(minutes=(lookback + i) * 5), p, p + 0.5, p - 0.5, p))
    for i in range(n_extra):
        p = flat + i * 0.1
        rows.append(_bar(base + timedelta(minutes=(2 * lookback + 1 + i) * 5), p, p + 0.5, p - 0.5, p))
    # forming candle
    rows.append(_bar(base + timedelta(minutes=(2 * lookback + 1 + n_extra) * 5),
                     flat + 0.5, flat + 2.0, flat - 1.0, flat + 0.5))
    return pd.DataFrame(rows)


def _m5_with_swing_high(swing_price=2002.0, lookback=5, n_extra=3):
    """M5 df: confirmed swing HIGH at bar `lookback`."""
    base = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = []
    flat = swing_price - 2.0
    for i in range(lookback):
        p = flat + i * 0.01
        rows.append(_bar(base + timedelta(minutes=i * 5), p, p + 0.5, p - 0.5, p))
    rows.append(_bar(base + timedelta(minutes=lookback * 5),
                     swing_price - 0.1, swing_price, swing_price - 0.3, swing_price - 0.2))
    for i in range(1, lookback + 1):
        p = flat + i * 0.01
        rows.append(_bar(base + timedelta(minutes=(lookback + i) * 5), p, p + 0.5, p - 0.5, p))
    for i in range(n_extra):
        p = flat - i * 0.1
        rows.append(_bar(base + timedelta(minutes=(2 * lookback + 1 + i) * 5), p, p + 0.5, p - 0.5, p))
    rows.append(_bar(base + timedelta(minutes=(2 * lookback + 1 + n_extra) * 5),
                     flat - 0.5, flat + 1.0, flat - 2.0, flat - 0.5))
    return pd.DataFrame(rows)


# ── M1 sweep + displacement + FVG ─────────────────────────────────────────────

def _m1_sweep_fvg(swing_price, direction, n_prefix=8):
    """M1: n_prefix flat bars, sweep candle, displacement(c1)+c2+c3 FVG bars, extra, forming.

    Total = n_prefix+5+1 = 14 bars. After iloc[:-1]=13 bars.
    recent = last 12 = bars[1..12]. Sweep at bar8 → recent index 7.
    post = recent[7:] = 5 bars. detect_fvg loop reaches i=2 (c1=disp, c3=fvg_c3) ✓
    displacement body = 0.5 pips (>= 0.4 threshold) ✓
    FVG gap = 0.30 (>= min_size 0.20) ✓
    """
    base = datetime(2024, 3, 1, 10, 0, tzinfo=timezone.utc)
    rows = []
    neutral = swing_price + 1.5 if direction == "LONG" else swing_price - 1.5

    for i in range(n_prefix):
        rows.append(_bar(base + timedelta(minutes=i), neutral, neutral + 0.1, neutral - 0.1, neutral))

    i = n_prefix
    if direction == "LONG":
        # Sweep: low = swing - 0.20 (2 pips beyond), close = swing+0.2 (back above)
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price + 0.3, swing_price + 0.5,
                         swing_price - 0.20, swing_price + 0.2))
        i += 1
        # Disp/c1: body=0.5 (open=swing, close=swing+0.5), high=swing+0.6 (key for FVG gap)
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price + 0.0, swing_price + 0.6,
                         swing_price - 0.1, swing_price + 0.5))
        i += 1
        # FVG c2
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price + 0.5, swing_price + 1.1,
                         swing_price + 0.4, swing_price + 1.0))
        i += 1
        # FVG c3: low=swing+0.9 > c1.high=swing+0.6 → gap=0.3 ≥ 0.20 ✓
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price + 1.0, swing_price + 1.5,
                         swing_price + 0.9, swing_price + 1.4))
        i += 1
        # Extra neutral bar (needed so post has 5 bars for detect_fvg to reach i=2)
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price + 1.3, swing_price + 1.4, swing_price + 1.2, swing_price + 1.35))
    else:  # SHORT
        # Sweep: high = swing + 0.20 (2 pips beyond), close = swing-0.2 (back below)
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price - 0.3, swing_price + 0.20,
                         swing_price - 0.5, swing_price - 0.2))
        i += 1
        # Disp/c1: body=0.5 (open=swing-0.1, close=swing-0.6), low=swing-0.7 (key for FVG gap)
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price - 0.1, swing_price + 0.0,
                         swing_price - 0.7, swing_price - 0.6))
        i += 1
        # FVG c2
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price - 0.6, swing_price - 0.5,
                         swing_price - 1.1, swing_price - 1.0))
        i += 1
        # FVG c3: high=swing-0.9 < c1.low=swing-0.7? No, need c3.high < c1.low:
        # c1.low=swing-0.7, c3.high must be < swing-0.7. Use swing-0.9. gap=0.2 ≥ 0.20 ✓
        # But use swing-1.0 to be safe: gap=0.3 ✓
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price - 1.0, swing_price - 1.0,
                         swing_price - 1.6, swing_price - 1.5))
        i += 1
        # Extra neutral bar
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price - 1.3, swing_price - 1.2, swing_price - 1.4, swing_price - 1.35))
    i += 1
    # Forming candle with wild values — excluded by iloc[:-1]
    if direction == "LONG":
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price - 5.0, swing_price - 4.0, swing_price - 6.0, swing_price - 4.5))
    else:
        rows.append(_bar(base + timedelta(minutes=i),
                         swing_price + 5.0, swing_price + 6.0, swing_price + 4.0, swing_price + 4.5))
    return pd.DataFrame(rows)


def _make_s1_data(direction="LONG"):
    swing = 1998.0 if direction == "LONG" else 2002.0
    # current_price: above FVG mid for LONG (FVG mid ≈ swing + 0.65), below for SHORT
    cp = swing + 1.3 if direction == "LONG" else swing - 1.3
    now = datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc)
    m5 = _m5_with_swing_low(swing) if direction == "LONG" else _m5_with_swing_high(swing)
    return MarketData(
        m1=_m1_sweep_fvg(swing, direction),
        m5=m5,
        m15=_flat(30, cp, step_min=15),
        h1=_bullish_h1() if direction == "LONG" else _bearish_h1(),
        current_price=cp,
        spread_pips=1.5,
        killzone="LONDON",
        now_utc=now,
    )


# ── S1 Tests ──────────────────────────────────────────────────────────────────

class TestS1SweepDisplacement:

    def test_long_signal_positive(self):
        sig = SweepDisplacement().scan(_make_s1_data("LONG"), "LONG")
        assert sig is not None, "Expected LONG signal"
        assert sig.strategy == "S1"
        assert sig.direction == "LONG"
        assert sig.entry_type == "LIMIT"
        assert sig.sl < sig.entry_price
        assert sig.tp1 > sig.entry_price
        assert sig.tp2 > sig.tp1
        assert "Sweep" in sig.confluences
        assert sig.tp1 == pytest.approx(sig.entry_price + cfg.TP1_PIPS * 0.10, abs=0.02)

    def test_short_signal_positive(self):
        sig = SweepDisplacement().scan(_make_s1_data("SHORT"), "SHORT")
        assert sig is not None, "Expected SHORT signal"
        assert sig.sl > sig.entry_price
        assert sig.tp1 < sig.entry_price

    def test_no_sweep_returns_none(self):
        data = _make_s1_data("LONG")
        # Replace M1 with flat data entirely above swing — no dip
        flat_m1 = _flat(14, price=2001.0, step_min=1)
        d2 = MarketData(m1=flat_m1, m5=data.m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert SweepDisplacement().scan(d2, "LONG") is None

    def test_wrong_h1_bias_returns_none(self):
        data = _make_s1_data("LONG")
        d2 = MarketData(m1=data.m1, m5=data.m5, m15=data.m15,
                        h1=_bearish_h1(),
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert SweepDisplacement().scan(d2, "LONG") is None

    def test_sl_pips_consistent(self):
        sig = SweepDisplacement().scan(_make_s1_data("LONG"), "LONG")
        assert sig is not None
        assert sig.sl_pips > 0
        assert sig.sl_pips == pytest.approx(abs(sig.entry_price - sig.sl) / 0.10, abs=0.3)

    def test_forming_candle_not_used(self):
        """Wild forming M1 candle (perfect sweep if read) must not change output."""
        data = _make_s1_data("LONG")
        sig_before = SweepDisplacement().scan(data, "LONG")

        extra = _bar(datetime(2024, 3, 1, 10, 30, tzinfo=timezone.utc),
                     1985.0, 1986.0, 1984.0, 1985.5)
        new_m1 = pd.concat([data.m1, pd.DataFrame([extra])], ignore_index=True)
        d2 = MarketData(m1=new_m1, m5=data.m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        sig_after = SweepDisplacement().scan(d2, "LONG")

        if sig_before is None:
            assert sig_after is None
        else:
            assert sig_after is not None
            assert sig_before.entry_price == sig_after.entry_price


# ── S2 helpers ─────────────────────────────────────────────────────────────────

def _make_s2_data(direction="LONG", or_high=2001.0, or_low=2000.0):
    """OR = 10 pips (within 20-pip cap). Breakout candle already closed."""
    base_ny = datetime(2024, 3, 1, 13, 0, tzinfo=timezone.utc)
    # M5: 3 OR bars + a few post-OR + forming
    rows_m5 = []
    mid = (or_high + or_low) / 2
    for i in range(3):
        rows_m5.append(_bar(base_ny + timedelta(minutes=i * 5), mid, or_high, or_low, mid))
    for i in range(3, 8):
        rows_m5.append(_bar(base_ny + timedelta(minutes=i * 5), mid, mid + 0.2, mid - 0.2, mid))
    rows_m5.append(_bar(base_ny + timedelta(minutes=40), mid, mid + 0.1, mid - 0.1, mid))  # forming
    m5 = pd.DataFrame(rows_m5)

    breakout = or_high + 0.2 if direction == "LONG" else or_low - 0.2
    base_m1 = datetime(2024, 3, 1, 13, 0, tzinfo=timezone.utc)
    rows_m1 = [_bar(base_m1 + timedelta(minutes=i), mid, mid + 0.1, mid - 0.1, mid) for i in range(10)]
    rows_m1.append(_bar(base_m1 + timedelta(minutes=10), mid, breakout + 0.05, mid - 0.05, breakout))
    rows_m1.append(_bar(base_m1 + timedelta(minutes=11), breakout, breakout + 0.3, breakout - 0.1, breakout + 0.2))
    m1 = pd.DataFrame(rows_m1)

    return MarketData(
        m1=m1, m5=m5,
        m15=_flat(20, mid, step_min=15),
        h1=_flat(20, mid, step_min=60),
        current_price=breakout,
        spread_pips=1.5,
        killzone="NY_AM",
        now_utc=datetime(2024, 3, 1, 13, 25, tzinfo=timezone.utc),
    )


class TestS2OrbNy:

    def test_long_breakout_positive(self):
        sig = OrbNy().scan(_make_s2_data("LONG"), "LONG")
        assert sig is not None, "Expected LONG ORB signal"
        assert sig.strategy == "S2"
        assert sig.entry_type == "MARKET"
        assert sig.tp1 > sig.entry_price
        assert sig.sl < sig.entry_price

    def test_short_breakout_positive(self):
        sig = OrbNy().scan(_make_s2_data("SHORT"), "SHORT")
        assert sig is not None
        assert sig.tp1 < sig.entry_price

    def test_no_breakout_returns_none(self):
        data = _make_s2_data("LONG")
        # Replace M1 with price stuck inside OR
        base_m1 = datetime(2024, 3, 1, 13, 0, tzinfo=timezone.utc)
        rows = [_bar(base_m1 + timedelta(minutes=i), 2000.5, 2000.6, 2000.4, 2000.5) for i in range(12)]
        d2 = MarketData(m1=pd.DataFrame(rows), m5=data.m5, m15=data.m15, h1=data.h1,
                        current_price=2000.5, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert OrbNy().scan(d2, "LONG") is None

    def test_or_too_wide_returns_none(self):
        data = _make_s2_data("LONG", or_high=2005.0, or_low=2000.0)  # 50 pips
        assert OrbNy().scan(data, "LONG") is None

    def test_one_trade_per_direction_per_day(self):
        s = OrbNy()
        data = _make_s2_data("LONG")
        assert s.scan(data, "LONG") is not None
        assert s.scan(data, "LONG") is None   # second call same day

    def test_or_not_ready_returns_none(self):
        # Only 2 M5 OR bars available
        base_ny = datetime(2024, 3, 1, 13, 0, tzinfo=timezone.utc)
        rows = [_bar(base_ny + timedelta(minutes=i * 5), 2000.5, 2001.0, 2000.0, 2000.5) for i in range(2)]
        rows.append(_bar(base_ny + timedelta(minutes=10), 2000.5, 2001.0, 2000.0, 2000.5))  # forming
        m5 = pd.DataFrame(rows)
        data = _make_s2_data("LONG")
        d2 = MarketData(m1=data.m1, m5=m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert OrbNy().scan(d2, "LONG") is None


# ── S3 helpers ─────────────────────────────────────────────────────────────────

def _make_s3_data(direction="LONG"):
    """M5: 10 large-ATR bars (establishes mean), 20+5 tiny-ATR bars (range regime),
    trigger candle (outside BB), reversal candle, forming candle.
    """
    base = datetime(2024, 3, 1, tzinfo=timezone.utc)
    rows = []

    # Phase 1: 10 bars with large high-low range → high mean ATR
    for i in range(10):
        p = 2000.0 + (i % 2) * 0.5
        rows.append(_bar(base + timedelta(minutes=i * 5), p, p + 1.5, p - 1.5, p, v=100.0))

    # Phase 2: 25 bars with tiny oscillation → low current ATR → range regime
    small = 0.03
    base_p = 2000.0
    for i in range(10, 35):
        p = base_p + (i % 2) * 0.01
        rows.append(_bar(base + timedelta(minutes=i * 5), p, p + small, p - small, p, v=50.0))

    # Compute BB at trigger position (uses last 20 of the 25 small bars = indices 15..34)
    recent_c = [base_p + (i % 2) * 0.01 for i in range(15, 35)]
    sma = sum(recent_c) / cfg.BB_PERIOD
    import statistics
    std = statistics.stdev(recent_c)
    lower = sma - cfg.BB_STD * std
    upper = sma + cfg.BB_STD * std

    # Trigger candle: close beyond band
    i = 35
    if direction == "LONG":
        tc = lower - 0.10          # well below lower band
        tl = tc - small
        rows.append(_bar(base + timedelta(minutes=i * 5), tc, tc + small, tl, tc, v=50.0))
        i += 1
        # Reversal: bullish, low within 3 pips of trigger low
        rows.append(_bar(base + timedelta(minutes=i * 5),
                         tc, tc + 0.25, tl - 0.05, tc + 0.20, v=50.0))
    else:
        tc = upper + 0.10
        th = tc + small
        rows.append(_bar(base + timedelta(minutes=i * 5), tc, th, tc - small, tc, v=50.0))
        i += 1
        rows.append(_bar(base + timedelta(minutes=i * 5),
                         tc, th + 0.05, tc - 0.25, tc - 0.20, v=50.0))
    i += 1
    # Forming candle with extreme opposite values
    if direction == "LONG":
        rows.append(_bar(base + timedelta(minutes=i * 5),
                         upper + 5.0, upper + 6.0, upper + 4.0, upper + 5.5, v=300.0))
    else:
        rows.append(_bar(base + timedelta(minutes=i * 5),
                         lower - 5.0, lower - 4.0, lower - 6.0, lower - 5.5, v=300.0))

    m5 = pd.DataFrame(rows)
    cp = float(m5.iloc[-2]["close"])   # reversal candle close = entry
    return MarketData(
        m1=_flat(20, cp, step_min=1),
        m5=m5,
        m15=_flat(20, cp, step_min=15),
        h1=_flat(20, cp, step_min=60),
        current_price=cp,
        spread_pips=1.0,
        killzone="ASIA",
        now_utc=datetime(2024, 3, 1, 1, 30, tzinfo=timezone.utc),
    )


class TestS3MeanRevAsia:

    def test_long_positive(self):
        sig = MeanRevAsia().scan(_make_s3_data("LONG"), "LONG")
        assert sig is not None, "Expected LONG mean-reversion signal"
        assert sig.strategy == "S3"
        assert sig.entry_type == "MARKET"
        assert sig.tp1 == pytest.approx(sig.entry_price + cfg.S3_TP1_PIPS * 0.10, abs=0.02)
        assert sig.tp2 == pytest.approx(sig.entry_price + cfg.S3_TP2_PIPS * 0.10, abs=0.02)
        assert sig.context["regime"] == "range"

    def test_short_positive(self):
        sig = MeanRevAsia().scan(_make_s3_data("SHORT"), "SHORT")
        assert sig is not None, "Expected SHORT signal"
        assert sig.tp1 < sig.entry_price

    def test_wrong_regime_returns_none(self):
        data = _make_s3_data("LONG")
        m5 = data.m5.copy()
        # Explode the last few bars' ranges to make current_atr >> mean_atr → high_vol
        for idx in range(len(m5) - 5, len(m5)):
            m5.iloc[idx, m5.columns.get_loc("high")] = 2050.0
            m5.iloc[idx, m5.columns.get_loc("low")]  = 1950.0
        d2 = MarketData(m1=data.m1, m5=m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert MeanRevAsia().scan(d2, "LONG") is None

    def test_no_reversal_candle_returns_none(self):
        data = _make_s3_data("LONG")
        m5 = data.m5.copy()
        # Flip reversal candle (iloc[-2]) to bearish
        rev_idx = len(m5) - 2
        o = float(m5.iloc[rev_idx]["open"])
        m5.iloc[rev_idx, m5.columns.get_loc("close")] = o - 0.3
        d2 = MarketData(m1=data.m1, m5=m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert MeanRevAsia().scan(d2, "LONG") is None

    def test_session_cap_at_two(self):
        s = MeanRevAsia()
        # Fire sig1
        data = _make_s3_data("LONG")
        sig1 = s.scan(data, "LONG")
        assert sig1 is not None
        # Clear dedup so level doesn't block; fire sig2
        s._emitted.clear()
        data2 = _make_s3_data("LONG")
        data2 = MarketData(m1=data2.m1, m5=data2.m5, m15=data2.m15, h1=data2.h1,
                           current_price=data2.current_price + 0.01,
                           spread_pips=data2.spread_pips,
                           killzone=data2.killzone, now_utc=data2.now_utc)
        sig2 = s.scan(data2, "LONG")
        assert sig2 is not None
        # Third attempt blocked by session cap
        s._emitted.clear()
        assert s.scan(data2, "LONG") is None

    def test_forming_candle_not_used(self):
        """Replacing the forming candle with wild values must not change scan output."""
        data = _make_s3_data("LONG")
        sig_before = MeanRevAsia().scan(data, "LONG")
        assert sig_before is not None, "Precondition: positive case must fire"

        # Replace forming candle (iloc[-1]) in-place with extreme SHORT trigger
        new_m5 = data.m5.copy()
        wild = _bar(new_m5.iloc[-1]["time"], 2050.0, 2060.0, 2040.0, 2055.0, v=500.0)
        new_m5.iloc[-1] = pd.Series(wild)
        d2 = MarketData(m1=data.m1, m5=new_m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        sig_after = MeanRevAsia().scan(d2, "LONG")
        assert sig_after is not None, "Wild forming candle must not suppress the signal"
        assert sig_after.direction == "LONG"


# ── S4 helpers ─────────────────────────────────────────────────────────────────

def _make_s4_data(direction="LONG"):
    """Asia range 2000–2010. SFP confirmation candle = last closed M15.
    No M15 swings → OTE gate skipped. Volume: 200 vs avg 100 → passes.
    """
    asia_low, asia_high = 2000.0, 2010.0
    base_p = 2005.0

    # Asia M15 bars (01:00–04:45 UTC = 16 bars) — flat, no swings
    asia_start = datetime(2024, 3, 1, 1, 0, tzinfo=timezone.utc)
    rows_m15 = [
        _bar(asia_start + timedelta(minutes=i * 15),
             base_p, asia_high, asia_low, base_p, v=100.0)
        for i in range(16)
    ]

    # London bars 07:00–08:15 (6 regular bars) — flat
    london_start = datetime(2024, 3, 1, 7, 0, tzinfo=timezone.utc)
    for i in range(6):
        rows_m15.append(_bar(london_start + timedelta(minutes=i * 15),
                             base_p, base_p + 0.3, base_p - 0.3, base_p, v=100.0))

    # SFP bar (08:30 UTC) — last CLOSED M15 candle
    if direction == "LONG":
        # Wick below asia_low (1998.5), close above (2001.5)
        rows_m15.append(_bar(london_start + timedelta(minutes=6 * 15),
                             2001.0, 2002.0, 1998.5, 2001.5, v=200.0))
        cp = 2001.5
    else:
        # Wick above asia_high (2011.5), close below (2008.5)
        rows_m15.append(_bar(london_start + timedelta(minutes=6 * 15),
                             2009.0, 2011.5, 2008.0, 2008.5, v=200.0))
        cp = 2008.5

    # Forming M15 candle (08:45 UTC)
    rows_m15.append(_bar(london_start + timedelta(minutes=7 * 15),
                         base_p, base_p + 1.0, base_p - 1.0, base_p))

    return MarketData(
        m1=_flat(20, cp, step_min=1),
        m5=_flat(25, cp, step_min=5),
        m15=pd.DataFrame(rows_m15),
        h1=_bullish_h1() if direction == "LONG" else _bearish_h1(),
        current_price=cp,
        spread_pips=1.5,
        killzone="LONDON",
        now_utc=datetime(2024, 3, 1, 7, 45, tzinfo=timezone.utc),
    )


class TestS4SfpAsia:

    def test_long_sfp_positive(self):
        sig = SfpAsia().scan(_make_s4_data("LONG"), "LONG")
        assert sig is not None, "Expected LONG SFP signal"
        assert sig.strategy == "S4"
        assert "Asia_SFP" in sig.confluences
        assert "asia_low" in sig.context
        assert sig.tp1 == pytest.approx(sig.entry_price + cfg.TP1_PIPS * 0.10, abs=0.02)
        assert sig.tp2 == pytest.approx(sig.entry_price + cfg.TP2_PIPS * 0.10, abs=0.02)

    def test_short_sfp_positive(self):
        sig = SfpAsia().scan(_make_s4_data("SHORT"), "SHORT")
        assert sig is not None, "Expected SHORT SFP signal"
        assert sig.direction == "SHORT"

    def test_no_wick_beyond_returns_none(self):
        data = _make_s4_data("LONG")
        m15 = data.m15.copy()
        # The SFP candle is data.m15.iloc[-2]; set its low above asia_low
        m15.iloc[-2, m15.columns.get_loc("low")] = 2001.0
        d2 = MarketData(m1=data.m1, m5=data.m5, m15=m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert SfpAsia().scan(d2, "LONG") is None

    def test_outside_90min_window_returns_none(self):
        data = _make_s4_data("LONG")
        d2 = MarketData(m1=data.m1, m5=data.m5, m15=data.m15, h1=data.h1,
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone,
                        now_utc=datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc))
        assert SfpAsia().scan(d2, "LONG") is None

    def test_wrong_bias_returns_none(self):
        data = _make_s4_data("LONG")
        d2 = MarketData(m1=data.m1, m5=data.m5, m15=data.m15,
                        h1=_bearish_h1(),
                        current_price=data.current_price, spread_pips=data.spread_pips,
                        killzone=data.killzone, now_utc=data.now_utc)
        assert SfpAsia().scan(d2, "LONG") is None


# ── No look-ahead ─────────────────────────────────────────────────────────────

class TestNoLookAhead:

    def test_s1_wild_forming_no_sweep(self):
        """Forming M1 candle wicks below swing — scan sees no sweep (forming excluded)."""
        swing = 1998.0
        rows = [_bar(datetime(2024, 3, 1, 8, i, tzinfo=timezone.utc),
                     swing + 2.0, swing + 2.5, swing + 1.5, swing + 2.0)
                for i in range(12)]
        # Forming: dips below swing — would be a valid sweep if used
        rows.append(_bar(datetime(2024, 3, 1, 8, 12, tzinfo=timezone.utc),
                         swing + 0.3, swing + 0.5, swing - 0.50, swing + 0.3))
        data = MarketData(
            m1=pd.DataFrame(rows),
            m5=_m5_with_swing_low(swing),
            m15=_flat(30, swing + 2.0, step_min=15),
            h1=_bullish_h1(),
            current_price=swing + 2.0,
            spread_pips=1.5,
            killzone="LONDON",
            now_utc=datetime(2024, 3, 1, 9, 0, tzinfo=timezone.utc),
        )
        assert SweepDisplacement().scan(data, "LONG") is None

    def test_s3_wild_forming_no_signal(self):
        """Forming M5 candle looks like a SHORT trigger — LONG scan returns None (no real trigger)."""
        base = datetime(2024, 3, 1, tzinfo=timezone.utc)
        price = 2000.0
        rows = []
        # 10 large + 25 tiny (range regime) but trigger candle (iloc[-2] of closed) inside band
        for i in range(10):
            p = price + (i % 2) * 0.5
            rows.append(_bar(base + timedelta(minutes=i * 5), p, p + 1.5, p - 1.5, p, v=100.0))
        for i in range(10, 36):
            p = price + (i % 2) * 0.01
            rows.append(_bar(base + timedelta(minutes=i * 5), p, p + 0.03, p - 0.03, p, v=50.0))
        # Trigger (iloc[-2] of closed): inside band — no trigger
        rows.append(_bar(base + timedelta(minutes=36 * 5), price, price + 0.01, price - 0.01, price, v=50.0))
        # Forming: huge SHORT trigger — must be ignored
        import statistics
        rc = [price + (i % 2) * 0.01 for i in range(16, 36)]
        upper = sum(rc) / cfg.BB_PERIOD + cfg.BB_STD * statistics.stdev(rc)
        rows.append(_bar(base + timedelta(minutes=37 * 5),
                         upper + 5.0, upper + 6.0, upper + 4.0, upper + 5.5, v=500.0))
        m5 = pd.DataFrame(rows)
        data = MarketData(
            m1=_flat(20, price, step_min=1),
            m5=m5,
            m15=_flat(20, price, step_min=15),
            h1=_flat(20, price, step_min=60),
            current_price=price,
            spread_pips=1.0,
            killzone="ASIA",
            now_utc=datetime(2024, 3, 1, 2, 0, tzinfo=timezone.utc),
        )
        assert MeanRevAsia().scan(data, "LONG") is None
