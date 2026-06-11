"""Finnhub US economic calendar — non-blocking news tagger.

Background thread refreshes every 10 min. is_red_news_window() never blocks.
Cache stored to data/news_cache.json; last-valid-cache principle: stale > 2h → None.
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

from config import cfg

log = logging.getLogger(__name__)

_CACHE_PATH = Path("data/news_cache.json")
_CACHE_MAX_AGE_H = 2
_REFRESH_MIN = 10
_WINDOW_MIN = 15   # ±15 min around event time

_lock = threading.Lock()
_cache: dict = {}   # {"fetched_at": ISO, "events": [...]}


def _fetch() -> list[dict]:
    """Fetch US high-impact events from Finnhub for today+tomorrow."""
    if not cfg.FINNHUB_API_KEY:
        return []
    try:
        now = datetime.now(timezone.utc)
        from_d = now.strftime("%Y-%m-%d")
        to_d   = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        url = (f"https://finnhub.io/api/v1/calendar/economic"
               f"?from={from_d}&to={to_d}&token={cfg.FINNHUB_API_KEY}")
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("economicCalendar", [])
        return [
            e for e in events
            if e.get("country", "").upper() in ("US", "USA", "UNITED STATES")
            and str(e.get("impact", "")).lower() in ("high", "1", "3")
        ]
    except Exception as exc:
        log.warning("Finnhub fetch failed: %s", exc)
        return []


def _save_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CACHE_PATH, "w") as f:
            json.dump(_cache, f)
    except Exception as exc:
        log.warning("News cache write failed: %s", exc)


def _load_cache() -> None:
    try:
        if _CACHE_PATH.exists():
            with open(_CACHE_PATH) as f:
                data = json.load(f)
            with _lock:
                _cache.update(data)
    except Exception:
        pass


def _refresh() -> None:
    events = _fetch()
    with _lock:
        _cache["fetched_at"] = datetime.now(timezone.utc).isoformat()
        _cache["events"] = events
    _save_cache()
    log.debug("News cache refreshed: %d red US events", len(events))


def start_news_updater() -> None:
    """Load cached data then start background refresh thread."""
    _load_cache()
    _refresh()   # immediate first fetch

    def _loop():
        while True:
            time.sleep(_REFRESH_MIN * 60)
            _refresh()

    threading.Thread(target=_loop, daemon=True).start()


def is_red_news_window(now_utc: datetime) -> bool | None:
    """Return True/False/None.

    None = cache empty or stale (> 2h). Non-blocking.
    """
    with _lock:
        fetched_at = _cache.get("fetched_at")
        events = list(_cache.get("events", []))

    if not fetched_at:
        return None

    age_h = (now_utc - datetime.fromisoformat(fetched_at)).total_seconds() / 3600
    if age_h > _CACHE_MAX_AGE_H:
        return None

    window = timedelta(minutes=_WINDOW_MIN)
    for e in events:
        try:
            raw = e.get("time") or e.get("datetime") or ""
            if not raw:
                continue
            raw = raw.replace(" ", "T")
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if abs(now_utc - dt) <= window:
                return True
        except Exception:
            continue

    return False
