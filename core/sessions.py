"""Killzone windows (UTC). ASIA updated to 23:00–03:00 UTC per SPEC 0 §7."""
from datetime import datetime
from typing import Literal
import pytz

from config import cfg

KillzoneName = Literal["ASIA", "LONDON", "NY_AM", "NY_PM", "OFF"]

# Each entry: (start_hour_utc, end_hour_utc)
# ASIA spans midnight: handled specially in get_active_killzone.
_KILLZONES_UTC: dict[str, tuple[int, int]] = {
    "ASIA":   (23, 3),    # 23:00–03:00 UTC (crosses midnight)
    "LONDON": (7, 10),    # 07:00–10:00 UTC
    "NY_AM":  (13, 16),   # 13:00–16:00 UTC
    "NY_PM":  (18, 20),   # 18:00–20:00 UTC
}


def _in_window(hour: int, start: int, end: int) -> bool:
    """Return True if hour is within [start, end). Handles midnight-crossing windows."""
    if start < end:
        return start <= hour < end
    # Crosses midnight (e.g. start=23, end=3)
    return hour >= start or hour < end


def get_active_killzone(dt: datetime | None = None) -> KillzoneName | None:
    if dt is None:
        dt = datetime.now(tz=pytz.utc)
    elif dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    hour = dt.astimezone(pytz.utc).hour
    for name, (start, end) in _KILLZONES_UTC.items():
        if name not in cfg.ENABLED_KILLZONES:
            continue
        if _in_window(hour, start, end):
            return name  # type: ignore[return-value]
    return None


def get_killzone_tag(dt: datetime | None = None) -> KillzoneName:
    """Return the descriptive UTC killzone tag; this value never gates scans."""
    if dt is None:
        dt = datetime.now(tz=pytz.utc)
    elif dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    hour = dt.astimezone(pytz.utc).hour
    for name, (start, end) in _KILLZONES_UTC.items():
        if _in_window(hour, start, end):
            return name  # type: ignore[return-value]
    return "OFF"


def is_in_killzone(dt: datetime | None = None) -> bool:
    return get_active_killzone(dt) is not None


def minutes_to_next_killzone(dt: datetime | None = None) -> int:
    """Return minutes until the next killzone opens (max 24h lookahead)."""
    if dt is None:
        dt = datetime.now(tz=pytz.utc)
    elif dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    utc = dt.astimezone(pytz.utc)
    current_minutes = utc.hour * 60 + utc.minute
    min_wait = 24 * 60
    for name, (start, _) in _KILLZONES_UTC.items():
        if name not in cfg.ENABLED_KILLZONES:
            continue
        start_minutes = start * 60
        if start_minutes > current_minutes:
            wait = start_minutes - current_minutes
        else:
            wait = (24 * 60 - current_minutes) + start_minutes
        min_wait = min(min_wait, wait)
    return min_wait
