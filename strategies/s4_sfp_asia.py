"""S4 — Swing Failure Pattern at Asia extreme. LONDON killzone, first 90 minutes.

Ported from xauusd-bot detector/strategy/tier_a.py::scan_sfp_asia with:
- Confirmed-only swings (rewritten find_swings)
- H1 bias instead of H4 (H4 not in scalper MarketData)
- TP1/TP2 = scalper fixed pips (not "opposite Asia range")
- Asia range bounds recorded in context for later comparison
- No estimated_winrate
"""
import uuid
import pandas as pd

from strategies.base import Strategy, MarketData
from core.models import Signal
from indicators.structure import find_swings, determine_bias
from indicators.fvg import detect_fvg, filter_unfilled_fvg
from indicators.asia_range import get_asia_range
from indicators.fibonacci import compute_fib_from_sweep, compute_fib_from_sweep_bearish
from config import cfg

_PIP = 0.10
_LONDON_START_UTC_HOUR = 7
_LONDON_WINDOW_MINUTES = 90  # first 90 min of LONDON only


def _avg_volume(df: pd.DataFrame, lookback: int) -> float | None:
    if "volume" not in df.columns or len(df) < lookback + 1:
        return None
    window = df["volume"].iloc[-(lookback + 1):-1]
    return float(window.mean()) if not window.empty else None


class SfpAsia(Strategy):
    id = "S4"
    name = "sfp_asia"
    magic = 20004
    sessions = {"LONDON"}

    def scan(self, data: MarketData, direction: str) -> Signal | None:
        # ── First 90 min of LONDON gate ─────────────────────────────────────
        now = data.now_utc
        now_minutes = now.hour * 60 + now.minute
        london_start = _LONDON_START_UTC_HOUR * 60
        if not (london_start <= now_minutes < london_start + _LONDON_WINDOW_MINUTES):
            return None

        m15 = data.m15.iloc[:-1]
        m5 = data.m5.iloc[:-1]
        h1 = data.h1.iloc[:-1]

        if len(m15) < cfg.SWING_LOOKBACK * 2 + 3 or len(m5) < 5 or len(h1) < cfg.SWING_LOOKBACK * 4 + 4:
            return None

        # ── H1 bias gate ─────────────────────────────────────────────────────
        h1_swings = find_swings(h1, lookback=cfg.SWING_LOOKBACK)
        if determine_bias(h1_swings) != ("BULLISH" if direction == "LONG" else "BEARISH"):
            return None

        # ── Asia range ────────────────────────────────────────────────────────
        # Pass full m15 (get_asia_range uses timestamps; iloc[-1] of the full df is fine)
        asia_high, asia_low = get_asia_range(data.m15)
        if asia_high is None or asia_low is None:
            return None

        # ── Confirmation candle = last closed M15 (iloc[-2] of FULL m15) ─────
        # m15 already has forming candle excluded; so iloc[-1] = last closed.
        if len(m15) < 2:
            return None
        confirm = m15.iloc[-1]  # last closed M15 candle

        # ── SFP geometry ──────────────────────────────────────────────────────
        if direction == "LONG":
            if not (confirm["low"] < asia_low and confirm["close"] > asia_low):
                return None
            sweep_wick = float(confirm["low"])
        else:
            if not (confirm["high"] > asia_high and confirm["close"] < asia_high):
                return None
            sweep_wick = float(confirm["high"])

        # ── Volume confirmation ───────────────────────────────────────────────
        avg_vol = _avg_volume(m15, cfg.SFP_VOLUME_LOOKBACK)
        if avg_vol is not None and float(confirm["volume"]) <= cfg.SFP_VOLUME_FACTOR * avg_vol:
            return None

        # ── OTE filter on M15 swing leg ───────────────────────────────────────
        m15_swings = find_swings(m15, lookback=cfg.SWING_LOOKBACK)
        if m15_swings:
            if direction == "LONG":
                swing_h = max((s.price for s in m15_swings if s.type == "HIGH"), default=None)
                if swing_h is not None:
                    fib = compute_fib_from_sweep(asia_low, swing_h, cfg.OTE_LOW, cfg.OTE_HIGH)
                    if not fib.is_in_ote(sweep_wick):
                        return None
            else:
                swing_l = min((s.price for s in m15_swings if s.type == "LOW"), default=None)
                if swing_l is not None:
                    fib = compute_fib_from_sweep_bearish(asia_high, swing_l, cfg.OTE_LOW, cfg.OTE_HIGH)
                    if not fib.is_in_ote(sweep_wick):
                        return None

        # ── Optional FVG confluence on M5 ────────────────────────────────────
        fvg_type = "BULLISH" if direction == "LONG" else "BEARISH"
        m5_fvgs = detect_fvg(m5.iloc[-20:], min_size_pips=cfg.FVG_MIN_SIZE_PIPS)
        m5_fvgs = filter_unfilled_fvg(m5_fvgs, data.current_price)
        recent_fvgs = [f for f in m5_fvgs if f.type == fvg_type and not f.filled]

        confluences = ["H1_Bias", "Asia_SFP", "Volume_Confirm", "OTE"]
        if recent_fvgs:
            confluences.append("FVG_M5")

        # ── Entry zone ────────────────────────────────────────────────────────
        if recent_fvgs:
            best = recent_fvgs[-1]
            entry_low, entry_high = best.bottom, best.top
        else:
            entry_low = min(float(confirm["open"]), float(confirm["close"]))
            entry_high = max(float(confirm["open"]), float(confirm["close"]))

        entry_ref = entry_high if direction == "LONG" else entry_low

        # ── SL: sweep extreme ± buffer ────────────────────────────────────────
        buf = cfg.SL_BUFFER_PIPS * _PIP
        if direction == "LONG":
            sl = round(sweep_wick - buf, 2)
            tp1 = round(entry_ref + cfg.TP1_PIPS * _PIP, 2)
            tp2 = round(entry_ref + cfg.TP2_PIPS * _PIP, 2)
        else:
            sl = round(sweep_wick + buf, 2)
            tp1 = round(entry_ref - cfg.TP1_PIPS * _PIP, 2)
            tp2 = round(entry_ref - cfg.TP2_PIPS * _PIP, 2)

        sl_pips = round(abs(entry_ref - sl) / _PIP, 1)

        key_level = round(asia_low if direction == "LONG" else asia_high, 1)
        if self._already_emitted(direction, key_level):
            return None

        sig = Signal(
            signal_id=uuid.uuid4().hex,
            ts_utc=data.now_utc,
            strategy=self.id,
            direction=direction,
            killzone=data.killzone or "LONDON",
            entry_type="LIMIT",
            entry_price=round(entry_ref, 2),
            entry_zone_low=round(entry_low, 2),
            entry_zone_high=round(entry_high, 2),
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            sl_pips=sl_pips,
            confluences=confluences,
            score=len(confluences),
            context={
                "asia_high": round(asia_high, 2),
                "asia_low": round(asia_low, 2),
                "sweep_wick": round(sweep_wick, 2),
                "confirm_time": str(confirm["time"]),
            },
        )
        self._mark_emitted(direction, key_level)
        return sig
