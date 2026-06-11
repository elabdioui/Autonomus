"""Tests for confirmed-only swing detection (SPEC 1 §3)."""
import pandas as pd
import pytest
from datetime import datetime, timezone, timedelta

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from indicators.structure import find_swings, determine_bias


def _make_df(highs: list[float], lows: list[float] | None = None) -> pd.DataFrame:
    n = len(highs)
    if lows is None:
        lows = [h - 1.0 for h in highs]
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return pd.DataFrame({
        "time": [base + timedelta(minutes=i) for i in range(n)],
        "open":  [h - 0.5 for h in highs],
        "high":  highs,
        "low":   lows,
        "close": [h - 0.3 for h in highs],
    })


LOOKBACK = 3


class TestSwingConfirmation:
    def test_swing_absent_before_confirmation(self):
        """Swing high at i=3, lookback=3 — confirmed at i=6.
        Truncate df at i=5 (one bar before confirmation) → swing must NOT appear."""
        # clear swing high at index 3: 1,2,3,10,3,2,1 — but we cut before the last bar closes
        highs = [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0]
        df_full = _make_df(highs)
        # Truncate to i+lookback-1 = 3+3-1 = 5 (index 0..5, 6 rows)
        df_truncated = df_full.iloc[:6].reset_index(drop=True)
        swings = find_swings(df_truncated, lookback=LOOKBACK)
        sh = [s for s in swings if s.type == "HIGH"]
        assert len(sh) == 0, f"Expected no confirmed swing highs, got {sh}"

    def test_swing_present_after_confirmation(self):
        """Swing high at i=3, lookback=3 → confirmed at i=6.
        Need 8 bars so index 6 is the last CLOSED candle (index 7 is forming)."""
        highs = [1.0, 2.0, 3.0, 10.0, 3.0, 2.0, 1.0, 0.5]  # 8 bars; forming=7, last_closed=6
        df = _make_df(highs)
        swings = find_swings(df, lookback=LOOKBACK)
        sh = [s for s in swings if s.type == "HIGH"]
        assert len(sh) == 1
        assert sh[0].bar_index == 3
        assert sh[0].confirmed_index == 6  # 3 + 3

    def test_confirmed_index_is_not_forming_candle(self):
        """confirmed_index must always be <= len(df)-2."""
        highs = [1.0, 2.0, 5.0, 2.0, 1.0, 0.5, 0.3]
        df = _make_df(highs)
        swings = find_swings(df, lookback=LOOKBACK)
        last_allowed = len(df) - 2
        for s in swings:
            assert s.confirmed_index <= last_allowed, (
                f"Swing {s} uses forming candle: confirmed_index={s.confirmed_index} > {last_allowed}"
            )

    def test_monotonic_series_no_swings(self):
        """Strictly increasing highs produce no swing highs."""
        highs = [float(i) for i in range(1, 20)]
        df = _make_df(highs)
        swings = find_swings(df, lookback=LOOKBACK)
        sh = [s for s in swings if s.type == "HIGH"]
        assert len(sh) == 0

    def test_monotonic_decreasing_no_swing_lows(self):
        """Strictly decreasing lows produce no swing lows."""
        highs = [10.0] * 20
        lows = [10.0 - i for i in range(20)]
        df = _make_df(highs, lows)
        swings = find_swings(df, lookback=LOOKBACK)
        sl = [s for s in swings if s.type == "LOW"]
        assert len(sl) == 0

    def test_swing_low_confirmation(self):
        """Clear swing low at index 3, confirmed at index 6. Need 8 bars."""
        lows = [10.0, 9.0, 8.0, 1.0, 8.0, 9.0, 10.0, 11.0]  # 8 bars
        highs = [l + 1.0 for l in lows]
        df = _make_df(highs, lows)
        swings = find_swings(df, lookback=LOOKBACK)
        sl = [s for s in swings if s.type == "LOW"]
        assert len(sl) == 1
        assert sl[0].bar_index == 3
        assert sl[0].confirmed_index == 6

    def test_determine_bias_bullish(self):
        """HH + HL → BULLISH. Two clear swing highs and two swing lows, with extra tail bars."""
        # Swing highs at i=3 (h=10) and i=9 (h=12), confirmed at i=5 and i=11 (lookback=2).
        # Swing lows at i=2 (l=0.5) and i=8 (l=1.5), confirmed at i=4 and i=10.
        # Need at least confirmed_index+1 extra bars (forming candle).
        # With 15 bars: forming=14, max_confirmed=13 → both swings confirmed.
        highs = [3, 4, 5, 10, 5, 4, 3, 4, 5, 12, 5, 4, 3, 2, 1]
        lows  = [h - 2 for h in highs]
        lows[3] = 0.5    # swing low 1
        lows[9] = 1.5    # swing low 2 (higher → HL)
        df = _make_df(highs, lows)
        swings = find_swings(df, lookback=2)
        bias = determine_bias(swings)
        assert bias == "BULLISH"

    def test_determine_bias_neutral_too_few(self):
        """Too few candles → NEUTRAL."""
        df = _make_df([1.0, 2.0, 1.0])
        swings = find_swings(df, lookback=1)
        assert determine_bias(swings) == "NEUTRAL"
