"""S2 — Opening Range Breakout NY_AM. One trade per direction per day."""
import uuid
from datetime import datetime, timezone, timedelta

from strategies.base import Strategy, MarketData
from core.models import Signal
from indicators.structure import find_swings, determine_bias
from config import cfg

_PIP = 0.10
_OR_BARS = 3          # 3 × M5 = 15 min opening range
_OR_MAX_PIPS = 20.0   # range too wide if > 20 pips


class OrbNy(Strategy):
    id = "S2"
    name = "orb_ny"
    magic = 20002
    sessions = {"NY_AM"}

    def __init__(self) -> None:
        super().__init__()
        # date-ISO → set of directions already fired today
        self._daily_fired: dict[str, set[str]] = {}

    def _get_or(self, m5_closed, now_utc: datetime):
        """Return (or_high, or_low) from the first 15 min of today's NY_AM, or None."""
        today = now_utc.date()
        or_start = datetime(today.year, today.month, today.day, 13, 0, tzinfo=timezone.utc)
        or_end = or_start + timedelta(minutes=15)

        times = m5_closed["time"]
        if hasattr(times.iloc[0], "tzinfo") and times.iloc[0].tzinfo is None:
            import pytz
            times = times.dt.tz_localize("UTC")

        mask = (times >= or_start) & (times < or_end)
        or_df = m5_closed[mask.values]

        if len(or_df) < _OR_BARS:
            return None  # OR not yet complete
        if len(or_df) > _OR_BARS:
            or_df = or_df.iloc[:_OR_BARS]  # use only first 3

        or_high = float(or_df["high"].max())
        or_low = float(or_df["low"].min())
        if (or_high - or_low) / _PIP > _OR_MAX_PIPS:
            return None  # too wide
        return or_high, or_low

    def scan(self, data: MarketData, direction: str) -> Signal | None:
        m1 = data.m1.iloc[:-1]
        m5 = data.m5.iloc[:-1]
        h1 = data.h1.iloc[:-1]

        if len(m1) < 2 or len(m5) < _OR_BARS + 2:
            return None

        or_result = self._get_or(m5, data.now_utc)
        if or_result is None:
            return None
        or_high, or_low = or_result

        # Per-day, per-direction dedup
        date_key = data.now_utc.date().isoformat()
        fired = self._daily_fired.setdefault(date_key, set())
        if direction in fired:
            return None

        last_m1 = m1.iloc[-1]
        if direction == "LONG":
            if last_m1["close"] <= or_high:
                return None
            key_level = or_high
        else:
            if last_m1["close"] >= or_low:
                return None
            key_level = or_low

        if self._already_emitted(direction, key_level):
            return None

        # SL: opposite range boundary ± buffer
        buf = cfg.SL_BUFFER_PIPS * _PIP
        entry = round(data.current_price, 2)
        if direction == "LONG":
            sl_natural = or_low - buf
            tp1 = round(entry + cfg.TP1_PIPS * _PIP, 2)
            tp2 = round(entry + cfg.TP2_PIPS * _PIP, 2)
        else:
            sl_natural = or_high + buf
            tp1 = round(entry - cfg.TP1_PIPS * _PIP, 2)
            tp2 = round(entry - cfg.TP2_PIPS * _PIP, 2)

        sl_pips = abs(entry - sl_natural) / _PIP
        if sl_pips > cfg.SL_MAX_PIPS:
            # Try range midpoint as SL (structural fallback)
            or_mid = (or_high + or_low) / 2
            sl_natural = (or_mid - buf) if direction == "LONG" else (or_mid + buf)
            sl_pips = abs(entry - sl_natural) / _PIP
            # If still too wide, return signal; runner will record SKIPPED_SL_TOO_WIDE

        # Optional H1 bias (non-filtering — recorded in context only)
        h1_bias = "NEUTRAL"
        if len(h1) >= cfg.SWING_LOOKBACK * 4 + 4:
            h1_swings = find_swings(h1, lookback=cfg.SWING_LOOKBACK)
            h1_bias = determine_bias(h1_swings)

        sig = Signal(
            signal_id=uuid.uuid4().hex,
            ts_utc=data.now_utc,
            strategy=self.id,
            direction=direction,
            killzone=data.killzone or "NY_AM",
            entry_type="MARKET",
            entry_price=entry,
            entry_zone_low=round(or_low, 2),
            entry_zone_high=round(or_high, 2),
            sl=round(sl_natural, 2),
            tp1=tp1,
            tp2=tp2,
            sl_pips=round(sl_pips, 1),
            confluences=["ORB_Breakout"],
            score=1,
            context={
                "or_high": round(or_high, 2),
                "or_low": round(or_low, 2),
                "or_range_pips": round((or_high - or_low) / _PIP, 1),
                "h1_bias_agreement": h1_bias == ("BULLISH" if direction == "LONG" else "BEARISH"),
            },
        )
        fired.add(direction)
        self._mark_emitted(direction, key_level)
        return sig
