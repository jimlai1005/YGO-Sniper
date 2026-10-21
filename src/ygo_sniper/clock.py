"""排程時鐘：整個專案的「排程時間」只有一個基準——台北牆上時間。

機器的系統時區會跟著使用者旅行（2026-10：Dallas → LA → 台北），但競標 18–22 點
結標是台北時間、launchd 的網格也是台北時間。`datetime.now()` 跟著系統走，
就會出現「launchd 用台北網格觸發、Python 用 Chicago 時間對帳」的混源比較
（CLAUDE.md 第三節第九事故：連續 12 天假警報）。

排程相關的 meta 鍵（`schedule_run_started_at` 等、`schedule_gap_window`）存的是
**台北 naive ISO**——與舊格式相容，只是基準明確了。要跟 UTC aware 的值（`sold_at`、
`scan_status`）比較，先經這裡的兩個換算函式。
"""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SCHEDULE_TZ_NAME = "Asia/Taipei"
SCHEDULE_TZ = ZoneInfo(SCHEDULE_TZ_NAME)


def schedule_now() -> datetime:
    """台北牆上時間，naive（排程帳的唯一時鐘；不要用 `datetime.now()`）。"""
    return datetime.now(SCHEDULE_TZ).replace(tzinfo=None)


def schedule_naive_to_utc(value: str | datetime) -> datetime:
    """台北 naive（ISO 字串或 datetime）→ UTC aware。"""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=SCHEDULE_TZ).astimezone(UTC)


def utc_to_schedule_str(dt: datetime, fmt: str = "%m-%d %H:%M") -> str:
    """UTC aware → 台北顯示字串。naive 輸入視為 UTC（store._now_iso 是 aware，這只是防呆）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(SCHEDULE_TZ).strftime(fmt)
