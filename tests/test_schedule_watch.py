"""排程空窗偵測：純函式、假時間，零網路零 store。"""

from datetime import datetime

from ygo_sniper.schedule_watch import expected_next_gap_minutes, gap_alert


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_expected_gap_daytime_is_two_hours():
    assert expected_next_gap_minutes(_dt("2026-08-11T09:30:00")) == 120


def test_expected_gap_evening_is_thirty_minutes():
    assert expected_next_gap_minutes(_dt("2026-08-11T19:00:00")) == 30


def test_expected_gap_last_evening_slot_spans_overnight():
    # 22:30 之後的下一班是隔天 09:30 = 660 分鐘
    assert expected_next_gap_minutes(_dt("2026-08-11T22:30:00")) == 660


def test_no_alert_on_normal_cadence():
    assert gap_alert(
        "2026-08-11T19:00:00", "2026-08-11T19:03:00", _dt("2026-08-11T19:30:02")
    ) is None


def test_no_alert_on_overnight_window():
    assert gap_alert(
        "2026-08-11T22:30:00", "2026-08-11T22:35:00", _dt("2026-08-12T09:30:05")
    ) is None


def test_alert_when_evening_slots_were_skipped():
    # 08-10 事故的形狀：20:49 之後直接跳到 23:00（21:00-22:30 四班消失）
    msg = gap_alert(
        "2026-08-10T20:00:00", "2026-08-10T20:49:00", _dt("2026-08-10T23:00:44")
    )
    assert msg is not None and "空窗" in msg


def test_alert_when_previous_run_never_finished():
    msg = gap_alert(
        "2026-08-11T19:00:00", "2026-08-11T18:33:00", _dt("2026-08-11T19:30:00")
    )
    assert msg is not None and "收尾" in msg


def test_first_run_has_no_baseline():
    assert gap_alert(None, None, _dt("2026-08-11T09:30:00")) is None
