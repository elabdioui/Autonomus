"""S3 — Mean Reversion Asia. Bollinger(20, 2.5) on M5, range regime required."""
import uuid
import pandas as pd

from strategies.base import Strategy, MarketData
from core.models import Signal
from indicators.regime import detect_regime
from config import cfg

_PIP = 0.10
_MAX_SIGNALS_PER_SESSION = 2


class MeanRevAsia(Strategy):
    id = "S3"
    name = "meanrev_asia"
    magic = 20003
    sessions = {"ASIA"}

    def __init__(self) -> None:
        super().__init__()
        # session-date-ISO → {"LONG": count, "SHORT": count}
        self._session_counts: dict[str, dict[str, int]] = {}

    def _session_key(self, now_utc) -> str:
        # ASIA spans midnight; key on the UTC date of the session open (23:xx → next day)
        # Simplify: use the date of now_utc (close enough for session counting)
        return now_utc.date().isoformat()

    def _session_count(self, direction: str, now_utc) -> int:
        return self._session_counts.get(self._session_key(now_utc), {}).get(direction, 0)

    def _inc_session_count(self, direction: str, now_utc) -> None:
        key = self._session_key(now_utc)
        self._session_counts.setdefault(key, {"LONG": 0, "SHORT": 0})
        self._session_counts[key][direction] += 1

    def _bollinger(self, closes: pd.Series) -> tuple[pd.Series, pd.Series]:
        sma = closes.rolling(cfg.BB_PERIOD).mean()
        std = closes.rolling(cfg.BB_PERIOD).std(ddof=1)
        return sma + cfg.BB_STD * std, sma - cfg.BB_STD * std

    def scan(self, data: MarketData, direction: str) -> Signal | None:
        m5 = data.m5.iloc[:-1]  # exclude forming candle

        if len(m5) < cfg.BB_PERIOD + 2:
            return None

        # ── Regime gate ──────────────────────────────────────────────────────
        regime = detect_regime(
            m5,
            atr_period=cfg.REGIME_ATR_PERIOD,
            range_multiplier=cfg.REGIME_RANGE_MULTIPLIER,
        )
        # S3 requires range regime
        if regime != "range":
            return None

        # ── Session signal count cap ─────────────────────────────────────────
        if self._session_count(direction, data.now_utc) >= _MAX_SIGNALS_PER_SESSION:
            return None

        # ── Bollinger bands ──────────────────────────────────────────────────
        closes = m5["close"]
        upper, lower = self._bollinger(closes)

        # Trigger candle = iloc[-2], reversal candle = iloc[-1]
        trigger = m5.iloc[-2]
        reversal = m5.iloc[-1]
        trigger_lower = lower.iloc[-2]
        trigger_upper = upper.iloc[-2]

        if direction == "LONG":
            # Trigger: closed below lower band
            if trigger["close"] >= trigger_lower:
                return None
            # Reversal: bullish close, low not more than 3 pips below trigger low
            if reversal["close"] <= reversal["open"]:
                return None
            if reversal["low"] < trigger["low"] - 3 * _PIP:
                return None
            extreme = trigger["low"]
        else:
            if trigger["close"] <= trigger_upper:
                return None
            if reversal["close"] >= reversal["open"]:
                return None
            if reversal["high"] > trigger["high"] + 3 * _PIP:
                return None
            extreme = trigger["high"]

        key_level = round(trigger_lower if direction == "LONG" else trigger_upper, 1)
        if self._already_emitted(direction, key_level):
            return None

        # ── Levels ───────────────────────────────────────────────────────────
        buf = cfg.SL_BUFFER_PIPS * _PIP
        entry = round(data.current_price, 2)
        if direction == "LONG":
            sl = round(extreme - buf, 2)
            tp1 = round(entry + cfg.S3_TP1_PIPS * _PIP, 2)
            tp2 = round(entry + cfg.S3_TP2_PIPS * _PIP, 2)
        else:
            sl = round(extreme + buf, 2)
            tp1 = round(entry - cfg.S3_TP1_PIPS * _PIP, 2)
            tp2 = round(entry - cfg.S3_TP2_PIPS * _PIP, 2)

        sl_pips = round(abs(entry - sl) / _PIP, 1)

        # S3's own hard SL cap — skip silently (not a global filter)
        if sl_pips > cfg.S3_SL_MAX_PIPS:
            return None

        sma_val = closes.rolling(cfg.BB_PERIOD).mean().iloc[-1]
        sig = Signal(
            signal_id=uuid.uuid4().hex,
            ts_utc=data.now_utc,
            strategy=self.id,
            direction=direction,
            killzone=data.killzone or "ASIA",
            entry_type="MARKET",
            entry_price=entry,
            entry_zone_low=round(lower.iloc[-2], 2),
            entry_zone_high=round(upper.iloc[-2], 2),
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            sl_pips=sl_pips,
            confluences=["BB_Extreme", "Reversal_Candle"],
            score=2,
            context={
                "regime": regime,
                "band_lower": round(float(trigger_lower), 2),
                "band_upper": round(float(trigger_upper), 2),
                "band_mid": round(float(sma_val), 2),
                "trigger_extreme": round(extreme, 2),
            },
        )
        self._inc_session_count(direction, data.now_utc)
        self._mark_emitted(direction, key_level)
        return sig
