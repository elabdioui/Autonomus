"""Acceptance tests for the four SPEC D strategies."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from config import cfg
from core.scan_stats import stats
from strategies.base import ALL, MarketData
from strategies.s1_sweep_micro import SETUP as S1_SETUP, SweepMicro
from strategies.s2_momentum_breakout import SETUP as S2_SETUP, MomentumBreakout
from strategies.s3_mean_reversion import SETUP as S3_SETUP, MeanReversion
from strategies.s4_pullback_continuation import SETUP as S4_SETUP, PullbackContinuation


BASE = datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc)


def bar(index, open_, high, low, close, minutes=1):
    return {
        "time": BASE + timedelta(minutes=index * minutes),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": 100.0,
    }


def flat(count, price=2000.0, minutes=1, width=0.04):
    return pd.DataFrame([
        bar(i, price, price + width, price - width, price, minutes)
        for i in range(count)
    ])


def market(m1, m5=None, m15=None, h1=None, h4=None, hour=8):
    return MarketData(
        m1=m1,
        m5=m5 if m5 is not None else flat(12, minutes=5),
        m15=m15 if m15 is not None else flat(12, minutes=15),
        h1=h1 if h1 is not None else flat(30, minutes=60),
        h4=h4 if h4 is not None else flat(30, minutes=240),
        current_price=float(m1.iloc[-1]["close"]),
        spread_pips=1.7,
        killzone=None,
        now_utc=datetime(2026, 6, 22, hour, 30, tzinfo=timezone.utc),
    )


def s1_data(*, direction="LONG", reject=True, forming_sweep=False):
    rows = []
    for i in range(21):
        rows.append(bar(i, 2000.00, 2000.06, 1999.90, 2000.00))
    if direction == "LONG":
        close = 1999.95 if reject else 1999.86
        rows.append(bar(21, 1999.94, 2000.01, 1999.84, close))
        forming = bar(22, 2000.0, 2000.05, 1999.50 if forming_sweep else 1999.95, 2000.0)
    else:
        close = 2000.05 if reject else 2000.14
        rows.append(bar(21, 2000.06, 2000.16, 1999.99, close))
        forming = bar(22, 2000.0, 2000.50 if forming_sweep else 2000.05, 1999.95, 2000.0)
    rows.append(forming)
    return market(pd.DataFrame(rows))


class TestRegistryAndSchema:
    def test_all_setups_run_24_7_and_are_import_controlled(self):
        setups = [S1_SETUP, S2_SETUP, S3_SETUP, S4_SETUP]
        assert [s.name for s in setups] == [
            "S1_sweep_micro", "S2_momentum_breakout",
            "S3_mean_reversion", "S4_pullback_continuation",
        ]
        assert all(s.killzone_mode == "off" and s.killzones == ALL for s in setups)
        assert [s.magic for s in setups] == [20001, 20002, 20003, 20004]

    def test_signal_exposes_common_schema_and_tags(self):
        signal = SweepMicro().scan(s1_data(), "LONG")
        assert signal is not None
        assert signal.setup == "S1_sweep_micro"
        assert signal.sl_structural == signal.sl
        assert signal.tp_final == signal.tp2
        assert signal.meta is signal.context
        assert signal.tp1 - signal.entry_price == pytest.approx(20 * cfg.PIP)
        assert signal.tp_final - signal.entry_price == pytest.approx(40 * cfg.PIP)
        for key in ("killzone", "htf_bias", "bias_aligned", "news_red_active",
                    "spread_at_entry", "premium_discount"):
            assert key in signal.meta


class TestS1SweepMicro:
    @pytest.mark.parametrize("direction", ["LONG", "SHORT"])
    def test_same_closed_candle_sweep_rejection(self, direction):
        signal = SweepMicro().scan(s1_data(direction=direction), direction)
        assert signal is not None
        assert signal.direction == direction
        assert signal.meta["swept_level_type"] == ("SSL" if direction == "LONG" else "BSL")
        extreme = signal.meta["sweep_extreme"]
        assert signal.sl < extreme if direction == "LONG" else signal.sl > extreme

    def test_sweep_without_same_candle_reclaim_is_rejected(self):
        assert SweepMicro().scan(s1_data(reject=False), "LONG") is None

    def test_forming_candle_is_never_a_signal(self):
        data = s1_data(reject=False, forming_sweep=True)
        assert SweepMicro().scan(data, "LONG") is None

    def test_duplicate_closed_bar_is_explicitly_recorded(self):
        strategy = SweepMicro()
        data = s1_data()
        assert strategy.scan(data, "LONG") is not None
        before = stats.counts[("S1", "LONG", "DUPLICATE_CLOSED_BAR")]
        assert strategy.scan(data, "LONG") is None
        assert stats.counts[("S1", "LONG", "DUPLICATE_CLOSED_BAR")] == before + 1


def s2_data(*, direction="LONG", wick_only=False, weak=False, wide=False):
    rows = []
    width = 0.09 if wide else 0.04
    for i in range(cfg.S2_COMPRESSION_BARS):
        rows.append(bar(i, 2000.0, 2000.0 + width, 2000.0 - width, 2000.0))
    if direction == "LONG":
        close = 2000.02 if wick_only else (2000.06 if weak else 2000.18)
        rows.append(bar(10, 2000.0, 2000.22, 1999.99, close))
    else:
        close = 1999.98 if wick_only else (1999.94 if weak else 1999.82)
        rows.append(bar(10, 2000.0, 2000.01, 1999.78, close))
    rows.append(bar(11, 2100.0, 2200.0, 1900.0, 2150.0))  # forming
    return market(pd.DataFrame(rows))


class TestS2MomentumBreakout:
    @pytest.mark.parametrize("direction", ["LONG", "SHORT"])
    def test_close_breakout_with_displacement(self, direction):
        signal = MomentumBreakout().scan(s2_data(direction=direction), direction)
        assert signal is not None
        assert signal.meta["range_width_pips"] < cfg.S2_MAX_RANGE_PIPS
        assert signal.meta["displacement_body_ratio"] >= cfg.S2_DISPLACEMENT_BODY_RATIO
        assert signal.meta["breakout_side"] == ("HIGH" if direction == "LONG" else "LOW")

    def test_wick_only_breakout_is_rejected(self):
        assert MomentumBreakout().scan(s2_data(wick_only=True), "LONG") is None

    def test_weak_body_is_rejected(self):
        assert MomentumBreakout().scan(s2_data(weak=True), "LONG") is None

    def test_wide_range_is_not_compression(self):
        assert MomentumBreakout().scan(s2_data(wide=True), "LONG") is None


def s3_data(direction="SHORT", good_wick=True):
    rows = [bar(i, 2000.0, 2000.03, 1999.97, 2000.0) for i in range(20)]
    if direction == "SHORT":
        high = 2000.80 if good_wick else 2000.43
        rows.append(bar(20, 2000.40, high, 2000.25, 2000.30))
    else:
        low = 1999.20 if good_wick else 1999.57
        rows.append(bar(20, 1999.60, 1999.75, low, 1999.70))
    rows.append(bar(21, 2100.0, 2200.0, 1900.0, 2100.0))
    return market(pd.DataFrame(rows), hour=1)


class TestS3MeanReversion:
    @pytest.mark.parametrize("direction", ["LONG", "SHORT"])
    def test_extension_plus_exhaustion_reverts_to_ema(self, direction):
        signal = MeanReversion().scan(s3_data(direction), direction)
        assert signal is not None
        assert signal.meta["extension_pips"] >= cfg.S3_EXTENSION_PIPS
        assert signal.meta["wick_rejection_ratio"] >= cfg.S3_WICK_REJECTION_RATIO
        if direction == "LONG":
            assert signal.entry_price < signal.tp_final
        else:
            assert signal.entry_price > signal.tp_final
        assert signal.tp_final == pytest.approx(signal.meta["ema_target"], abs=0.001)

    def test_clean_extension_without_exhaustion_is_rejected(self):
        assert MeanReversion().scan(s3_data(good_wick=False), "SHORT") is None

    def test_forming_extension_is_ignored(self):
        data = s3_data(good_wick=False)
        assert MeanReversion().scan(data, "SHORT") is None


def trend_m5(direction="LONG", with_bos=True):
    sign = 1 if direction == "LONG" else -1
    rows = [bar(i, 2000.0, 2000.04, 1999.96, 2000.0, 5) for i in range(60)]
    if with_bos:
        pivot = 2000.20 if direction == "LONG" else 1999.80
        for idx, delta in ((38, -0.08), (39, -0.04), (40, 0.0), (41, -0.04), (42, -0.08)):
            center = pivot + sign * delta
            rows[idx] = bar(idx, center, center + 0.01, center - 0.01, center, 5)
        for idx in range(43, 60):
            close = 2000.30 if direction == "LONG" else 1999.70
            if idx == 43:
                close = 2000.25 if direction == "LONG" else 1999.75
            rows[idx] = bar(idx, close - sign * 0.03, close + 0.03, close - 0.03, close, 5)
        # Keep the BOS displacement overlapping the prior candle so this helper
        # exercises the EMA-only pullback path (dedicated FVG logic stays optional).
        if direction == "LONG":
            rows[43]["low"] = 2000.10
            rows[44]["low"] = 2000.10
        else:
            rows[43]["high"] = 1999.90
            rows[44]["high"] = 1999.90
    rows.append(bar(60, 2500.0, 2600.0, 2400.0, 2500.0, 5))
    return pd.DataFrame(rows)


def s4_data(direction="LONG", with_bos=True, close_back_through=True):
    m5 = trend_m5(direction, with_bos)
    closed_m5 = m5.iloc[:-1]
    ema = float(closed_m5["close"].ewm(span=cfg.S4_TREND_EMA, adjust=False).mean().iloc[-1])
    rows = [bar(i, ema, ema + 0.02, ema - 0.02, ema) for i in range(6)]
    if direction == "LONG":
        close = ema + 0.04 if close_back_through else ema - 0.01
        rows.append(bar(6, ema - 0.02, ema + 0.06, ema - 0.04, close))
    else:
        close = ema - 0.04 if close_back_through else ema + 0.01
        rows.append(bar(6, ema + 0.02, ema + 0.04, ema - 0.06, close))
    rows.append(bar(7, 2200.0, 2300.0, 1900.0, 2100.0))
    return market(pd.DataFrame(rows), m5=m5, m15=flat(60, minutes=15))


class TestS4PullbackContinuation:
    @pytest.mark.parametrize("direction", ["LONG", "SHORT"])
    def test_ema_bos_pullback_reaction(self, direction):
        signal = PullbackContinuation().scan(s4_data(direction), direction)
        assert signal is not None
        assert signal.meta["trend_source"] == "M5_EMA+BOS"
        assert signal.meta["retest_zone_type"] in {"EMA", "FVG", "OB"}
        assert signal.meta["bos_level"] is not None

    def test_no_bos_means_no_trend_setup(self):
        assert PullbackContinuation().scan(s4_data(with_bos=False), "LONG") is None

    def test_wick_touch_without_close_back_through_ema_is_rejected(self):
        assert PullbackContinuation().scan(
            s4_data(close_back_through=False), "LONG"
        ) is None

    def test_forming_reaction_is_ignored(self):
        data = s4_data(close_back_through=False)
        forming = data.m1.index[-1]
        data.m1.loc[forming, ["open", "high", "low", "close"]] = [
            1999.0, 2001.0, 1998.0, 2000.5,
        ]
        assert PullbackContinuation().scan(data, "LONG") is None
