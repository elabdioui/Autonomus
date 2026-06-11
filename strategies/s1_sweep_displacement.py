"""S1 — Sweep + Displacement. All killzones."""
import uuid
import pandas as pd

from strategies.base import Strategy, MarketData
from core.models import Signal
from indicators.structure import find_swings, determine_bias
from indicators.fvg import detect_fvg, filter_unfilled_fvg
from config import cfg

_PIP = 0.10


class SweepDisplacement(Strategy):
    id = "S1"
    name = "sweep_displacement"
    magic = 20001
    sessions = {"LONDON", "NY_AM", "NY_PM", "ASIA"}

    def scan(self, data: MarketData, direction: str) -> Signal | None:
        m1 = data.m1.iloc[:-1]  # exclude forming candle
        m5 = data.m5.iloc[:-1]
        h1 = data.h1.iloc[:-1]

        if len(m1) < 12 or len(m5) < (cfg.SWING_LOOKBACK * 2 + 3) or len(h1) < (cfg.SWING_LOOKBACK * 4 + 4):
            return None

        # ── H1 bias gate ────────────────────────────────────────────────────
        h1_swings = find_swings(h1, lookback=cfg.SWING_LOOKBACK)
        if determine_bias(h1_swings) != ("BULLISH" if direction == "LONG" else "BEARISH"):
            return None

        # ── M5 swing for sweep target ────────────────────────────────────────
        m5_swings = find_swings(m5, lookback=cfg.SWING_LOOKBACK)
        candidates = [s for s in m5_swings if s.type == ("LOW" if direction == "LONG" else "HIGH")]
        if not candidates:
            return None
        target_swing = candidates[-1]
        swing_price = target_swing.price

        # ── Sweep detection on last 12 closed M1 candles ─────────────────────
        recent = m1.iloc[-12:].reset_index(drop=True)
        sweep_idx: int | None = None
        sweep_extreme: float | None = None

        for i in range(len(recent)):
            row = recent.iloc[i]
            if direction == "LONG":
                if row["low"] < swing_price - _PIP:                    # ≥1 pip beyond
                    reclaimed = row["close"] > swing_price
                    if not reclaimed and i + 1 < len(recent):
                        reclaimed = recent.iloc[i + 1]["close"] > swing_price
                    if reclaimed:
                        sweep_idx, sweep_extreme = i, float(row["low"])
                        break
            else:
                if row["high"] > swing_price + _PIP:
                    reclaimed = row["close"] < swing_price
                    if not reclaimed and i + 1 < len(recent):
                        reclaimed = recent.iloc[i + 1]["close"] < swing_price
                    if reclaimed:
                        sweep_idx, sweep_extreme = i, float(row["high"])
                        break

        if sweep_idx is None:
            return None

        if self._already_emitted(direction, swing_price):
            return None

        # ── Displacement: body ≥ 4 pips + M1 FVG after sweep ────────────────
        post = recent.iloc[sweep_idx:].reset_index(drop=True)
        if len(post) < 3:
            return None

        has_displacement = any(
            (row["close"] - row["open"] if direction == "LONG" else row["open"] - row["close"]) >= 0.4
            for _, row in post.iterrows()
        )
        if not has_displacement:
            return None

        fvg_type = "BULLISH" if direction == "LONG" else "BEARISH"
        fvgs = detect_fvg(post, min_size_pips=2.0)
        fvgs = filter_unfilled_fvg(fvgs, data.current_price)
        matching = [f for f in fvgs if f.type == fvg_type and not f.filled]
        if not matching:
            return None
        fvg = matching[-1]

        # ── Levels ───────────────────────────────────────────────────────────
        buf = cfg.SL_BUFFER_PIPS * _PIP
        entry = round(fvg.mid, 2)
        if direction == "LONG":
            sl = round(sweep_extreme - buf, 2)
            tp1 = round(entry + cfg.TP1_PIPS * _PIP, 2)
            tp2 = round(entry + cfg.TP2_PIPS * _PIP, 2)
        else:
            sl = round(sweep_extreme + buf, 2)
            tp1 = round(entry - cfg.TP1_PIPS * _PIP, 2)
            tp2 = round(entry - cfg.TP2_PIPS * _PIP, 2)

        sl_pips = round(abs(entry - sl) / _PIP, 1)
        disp_body = max(
            (row["close"] - row["open"] if direction == "LONG" else row["open"] - row["close"])
            for _, row in post.iterrows()
        )

        sig = Signal(
            signal_id=uuid.uuid4().hex,
            ts_utc=data.now_utc,
            strategy=self.id,
            direction=direction,
            killzone=data.killzone or "",
            entry_type="LIMIT",
            entry_price=entry,
            entry_zone_low=round(fvg.bottom, 2),
            entry_zone_high=round(fvg.top, 2),
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            sl_pips=sl_pips,
            confluences=["H1_Bias", "Sweep", "Displacement", "FVG_M1"],
            score=4,
            context={
                "swept_level": round(swing_price, 2),
                "swing_confirmed_time": target_swing.confirmed_time.isoformat(),
                "fvg_bottom": round(fvg.bottom, 2),
                "fvg_top": round(fvg.top, 2),
                "displacement_body_pips": round(disp_body / _PIP, 1),
                "sweep_extreme": round(sweep_extreme, 2),
            },
        )
        self._mark_emitted(direction, swing_price)
        return sig
