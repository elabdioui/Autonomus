"""Tests for killzone session windows including the ASIA midnight-crossing window."""
import pytest
from datetime import datetime, timezone

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.sessions import get_active_killzone, get_killzone_tag, is_in_killzone, _in_window


class TestInWindow:
    def test_normal_window(self):
        assert _in_window(7, 7, 10) is True
        assert _in_window(9, 7, 10) is True
        assert _in_window(10, 7, 10) is False
        assert _in_window(6, 7, 10) is False

    def test_midnight_crossing(self):
        # ASIA: start=23, end=3
        assert _in_window(23, 23, 3) is True
        assert _in_window(0, 23, 3) is True
        assert _in_window(2, 23, 3) is True
        assert _in_window(3, 23, 3) is False
        assert _in_window(4, 23, 3) is False
        assert _in_window(22, 23, 3) is False


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2024, 3, 1, hour, minute, tzinfo=timezone.utc)


class TestGetActiveKillzone:
    def test_asia_start(self):
        assert get_active_killzone(_utc(23)) == "ASIA"

    def test_asia_midnight(self):
        assert get_active_killzone(_utc(0)) == "ASIA"

    def test_asia_02(self):
        assert get_active_killzone(_utc(2)) == "ASIA"

    def test_asia_end(self):
        assert get_active_killzone(_utc(3)) != "ASIA"

    def test_london(self):
        assert get_active_killzone(_utc(7)) == "LONDON"
        assert get_active_killzone(_utc(9)) == "LONDON"
        assert get_active_killzone(_utc(10)) != "LONDON"

    def test_ny_am(self):
        assert get_active_killzone(_utc(13)) == "NY_AM"
        assert get_active_killzone(_utc(15)) == "NY_AM"
        assert get_active_killzone(_utc(16)) != "NY_AM"

    def test_ny_pm(self):
        assert get_active_killzone(_utc(18)) == "NY_PM"
        assert get_active_killzone(_utc(19)) == "NY_PM"
        assert get_active_killzone(_utc(20)) != "NY_PM"

    def test_dead_zone(self):
        # Between sessions (e.g. 11:00 UTC)
        assert get_active_killzone(_utc(11)) is None
        assert get_killzone_tag(_utc(11)) == "OFF"

    def test_is_in_killzone(self):
        assert is_in_killzone(_utc(8)) is True
        assert is_in_killzone(_utc(11)) is False
