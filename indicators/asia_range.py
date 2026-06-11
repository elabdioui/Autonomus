"""Asia session range computation. Extracted from xauusd-bot detector/strategy/tier_a.py."""
import logging
import pandas as pd
import pytz

log = logging.getLogger(__name__)

_NY_TZ = pytz.timezone("America/New_York")


def get_asia_range(m15: pd.DataFrame) -> tuple[float | None, float | None]:
    """
    Compute the Asia session range using timestamps, not row indices.

    Asia session = 20:00–00:00 New York time, converted to UTC with DST via pytz.
    Returns (asia_high, asia_low), or (None, None) if no candles fall in the window.
    Requires a tz-aware 'time' column in UTC (as produced by mt5_client.get_ohlc).
    """
    if m15 is None or m15.empty or "time" not in m15.columns:
        return None, None

    times = pd.to_datetime(m15["time"], utc=True)
    now_utc = times.iloc[-1]
    now_ny = now_utc.tz_convert(_NY_TZ)

    asia_start_ny = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)
    if now_ny.hour < 20:
        asia_start_ny = asia_start_ny - pd.Timedelta(days=1)
    asia_end_ny = asia_start_ny + pd.Timedelta(hours=4)

    asia_start_utc = asia_start_ny.tz_convert("UTC")
    asia_end_utc = asia_end_ny.tz_convert("UTC")

    mask = (times >= asia_start_utc) & (times < asia_end_utc)
    asia_df = m15[mask.values]
    if asia_df.empty:
        log.debug("Asia range empty for window %s–%s UTC", asia_start_utc, asia_end_utc)
        return None, None

    return float(asia_df["high"].max()), float(asia_df["low"].min())
