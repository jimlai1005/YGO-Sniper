"""排程時鐘（clock.py）：不論系統時區是什麼，排程時間都是台北牆上時間。

每個測試都把系統時區故意設成 America/Chicago，證明結果不受機器時區影響。
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from ygo_sniper import clock


@pytest.fixture
def chicago_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_schedule_now_is_taipei_wall_time_regardless_of_system_tz(chicago_tz):
    now = clock.schedule_now()
    assert now.tzinfo is None
    expected = datetime.now(clock.SCHEDULE_TZ).replace(tzinfo=None)
    assert abs((now - expected).total_seconds()) < 2


def test_schedule_naive_to_utc_uses_taipei_not_system(chicago_tz):
    # 台北 2026-10-19 09:00 = 01:00Z（Chicago 基準會算成 14:00Z）
    assert clock.schedule_naive_to_utc("2026-10-19T09:00:01") == datetime(2026, 10, 19, 1, 0, 1, tzinfo=UTC)


def test_utc_to_schedule_str(chicago_tz):
    assert clock.utc_to_schedule_str(datetime(2026, 10, 17, 13, 27, 11, tzinfo=UTC)) == "10-17 21:27"
