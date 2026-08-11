"""排程空窗偵測：純函式、假時間，零網路零 store。"""

import plistlib
from datetime import datetime
from pathlib import Path

import pytest

from ygo_sniper.schedule_watch import (
    _WINDOWS,
    PENDING_ALERT_KEY,
    RUN_FINISHED_KEY,
    RUN_STARTED_KEY,
    expected_next_gap_minutes,
    next_slot_after,
    resolve_alert,
    schedule_health,
)

_PLIST = Path(__file__).resolve().parents[1] / "scripts" / "com.jim.ygosniper.plist"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# expected_next_gap_minutes：衍生量，三個舊錨例維持不動（Fix 1 要求逐字保留）
# ---------------------------------------------------------------------------
def test_expected_gap_daytime_is_two_hours():
    assert expected_next_gap_minutes(_dt("2026-08-11T09:30:00")) == 120


def test_expected_gap_evening_is_thirty_minutes():
    assert expected_next_gap_minutes(_dt("2026-08-11T19:00:00")) == 30


def test_expected_gap_last_evening_slot_spans_overnight():
    # 22:30 之後的下一班是隔天 09:30 = 660 分鐘
    assert expected_next_gap_minutes(_dt("2026-08-11T22:30:00")) == 660


# ---------------------------------------------------------------------------
# Fix 1：next_slot_after 是絕對時間，drift 不能污染判準
# ---------------------------------------------------------------------------
def test_next_slot_after_is_monotonic_under_small_drift():
    """15:30 附近 1-5 分鐘的正常漂移，「該接棒的下一格」必須是同一個絕對時間
    ——這正是舊版非單調 bug（15:30→120 但 15:35→145）的反面。"""
    due = _dt("2026-08-11T17:30:00")
    assert next_slot_after(_dt("2026-08-11T15:30:00")) == due
    assert next_slot_after(_dt("2026-08-11T15:31:00")) == due
    assert next_slot_after(_dt("2026-08-11T15:35:00")) == due


def test_alert_fires_when_drifted_run_masks_missed_slot():
    """08-10 事故複測案例：18:15 起跑（比 18:00 晚 15 分，正常漂移），
    18:30 那個時段整個消失，下一輪實際在 19:00 才跑起來。
    舊版用「經過分鐘數 vs 理想間隔」比較會靜默；新版必須出聲。"""
    msg = schedule_health(
        "2026-08-10T18:15:00", "2026-08-10T18:18:00", _dt("2026-08-10T19:00:00")
    )
    assert msg is not None and "空窗" in msg


def test_alert_fires_when_drift_near_window_boundary_swallows_next_slot():
    """15:31 起跑（15:30 那格晚 1 分），17:30 整個消失，下一輪 18:00 才跑。"""
    msg = schedule_health(
        "2026-08-11T15:31:00", "2026-08-11T15:33:00", _dt("2026-08-11T18:00:00")
    )
    assert msg is not None and "空窗" in msg


# ---------------------------------------------------------------------------
# schedule_health：既有行為（沿用舊測試案例，函式已更名 gap_alert → schedule_health）
# ---------------------------------------------------------------------------
def test_no_alert_on_normal_cadence():
    assert schedule_health(
        "2026-08-11T19:00:00", "2026-08-11T19:03:00", _dt("2026-08-11T19:30:02")
    ) is None


def test_no_alert_on_overnight_window():
    assert schedule_health(
        "2026-08-11T22:30:00", "2026-08-11T22:35:00", _dt("2026-08-12T09:30:05")
    ) is None


def test_alert_when_evening_slots_were_skipped():
    # 08-10 事故的形狀：20:49 之後直接跳到 23:00（21:00-22:30 四班消失）
    msg = schedule_health(
        "2026-08-10T20:00:00", "2026-08-10T20:49:00", _dt("2026-08-10T23:00:44")
    )
    assert msg is not None and "空窗" in msg


def test_alert_when_previous_run_never_finished():
    msg = schedule_health(
        "2026-08-11T19:00:00", "2026-08-11T18:33:00", _dt("2026-08-11T19:30:00")
    )
    assert msg is not None and "收尾" in msg


def test_first_run_has_no_baseline():
    assert schedule_health(None, None, _dt("2026-08-11T09:30:00")) is None


# ---------------------------------------------------------------------------
# Fix 2：resolve_alert——pending 帳的合併規則（純函式，不碰 store）
# ---------------------------------------------------------------------------
def test_resolve_alert_nothing_new_nothing_pending():
    msg, pending = resolve_alert(None, None)
    assert msg is None
    assert pending == ""


def test_resolve_alert_first_detection_creates_pending():
    msg, pending = resolve_alert(None, "🚨 排程監督：空窗 2.0 小時")
    assert msg == "🚨 排程監督：空窗 2.0 小時"
    assert '"missed": 1' in pending


def test_resolve_alert_carries_forward_undelivered_pending_when_nothing_new():
    """這一輪沒有新問題，但上一輪的告警還沒送達——原樣帶下去，不能憑空消失。"""
    _, pending = resolve_alert(None, "🚨 排程監督：空窗 2.0 小時")
    msg, pending2 = resolve_alert(pending, None)
    assert msg == "🚨 排程監督：空窗 2.0 小時"
    assert pending2 == pending  # 沒有新事件，帳目原封不動


def test_resolve_alert_bounds_growth_across_repeated_failures():
    """連續好幾輪都偵測到問題、都沒送出去——存的字串不能無限變長，
    只能是「最新一句話 + 計數」，遺失的是舊訊息的確切文字，不遺失的是
    「有東西一直沒送達」這件事。"""
    pending = None
    for i in range(5):
        _, pending = resolve_alert(pending, f"🚨 排程監督：第 {i} 次空窗")
    msg, final_pending = resolve_alert(pending, "🚨 排程監督：第 5 次空窗")
    assert "第 5 次空窗" in msg
    assert "先前 5 次" in msg
    # 存的東西是常數大小的 JSON，不是六則訊息串接起來
    assert len(final_pending) < 200


def test_resolve_alert_only_caller_can_clear_pending():
    """`resolve_alert` 本身不清帳：detected=None 且沒有 prev pending 才會回傳
    空字串；只要 store 裡還有 pending，它必定被帶回來，直到呼叫端明確清除。"""
    _, pending = resolve_alert(None, "🚨 排程監督：問題")
    assert pending != ""
    msg, pending2 = resolve_alert(pending, None)
    assert msg is not None
    assert pending2 != ""


# ---------------------------------------------------------------------------
# Fix 5：_WINDOWS 是 plist 的手抄本，兩邊必須實測相等，不能只靠註解提醒
# ---------------------------------------------------------------------------
def test_windows_match_plist():
    with open(_PLIST, "rb") as f:
        plist = plistlib.load(f)
    plist_slots = {
        e["Hour"] * 60 + e["Minute"] for e in plist["StartCalendarInterval"]
    }
    code_slots = {m for lo, hi, step in _WINDOWS for m in range(lo, hi + 1, step)}
    assert code_slots == plist_slots, (
        f"schedule_watch._WINDOWS 與 {_PLIST.name} 的 StartCalendarInterval 不同步：\n"
        f"code only: {sorted(code_slots - plist_slots)}\n"
        f"plist only: {sorted(plist_slots - code_slots)}"
    )


# ---------------------------------------------------------------------------
# Fix 3／Fix 4：pending 機制真的接進 Pipeline／cli 之後——手動 scan、崩潰
# 這兩種「_run_notifications 沒被叫到」的路徑，都不能吃掉已經偵測到的告警。
#
# 用真的 Pipeline（掛鉤點在它身上）＋ tmp_path 的 db，不碰網路（no_fx_network
# 換掉 FxRates.refresh；沒有 watchlist/sources 設定，_scan() 本身也不會被呼叫
# ——這裡直接打 pipe._update_schedule_state / _finish_schedule_state 這兩個
# 私有方法，因為它們就是 scan() 唯一會呼叫排程監督的地方，測這兩個等於測
# 生產路徑本身，不必為了測排程監督另外把整條 _scan()（來源、comps、估價）
# 都跑一遍。
# ---------------------------------------------------------------------------

#: 保證觸發 schedule_health 的舊基準——早到不管測試在哪一天跑，
#: 「絕對時間的下一格」都已經遠遠落後於真正的 datetime.now()。
_ANCIENT_START = "2020-01-01T09:30:00"
_ANCIENT_FINISH = "2020-01-01T09:33:00"


@pytest.fixture
def no_fx_network(monkeypatch):
    from ygo_sniper.fx import FxRates

    monkeypatch.setattr(FxRates, "refresh", lambda self: None)


@pytest.fixture
def pipeline(tmp_path, no_fx_network):
    """真的 `Pipeline`，db 在 tmp_path、不碰網路——與 test_card_snipe.py 同款。"""
    from dataclasses import replace as dc_replace

    import ygo_sniper.config as config_mod
    from ygo_sniper.pipeline import Pipeline

    config_mod.load_config.cache_clear()
    base = config_mod.load_config()
    cfg = dc_replace(base, storage={**base.storage, "db_path": str(tmp_path / "p.db")})
    pipe = Pipeline(cfg)
    try:
        yield pipe
    finally:
        pipe.close()
        config_mod.load_config.cache_clear()


class _FakeNotifier:
    def __init__(self, ok: bool = True):
        self.sent: list[str] = []
        self._ok = ok

    def send_alert(self, text) -> bool:
        self.sent.append(str(text))
        return self._ok


def test_dry_run_and_watch_only_never_touch_schedule_state(pipeline):
    """`--dry-run` 與 `watch-scan`（watch_only）都不該碰排程基準——
    前者是「只掃不寫庫」的語意，後者是跟排程網格無關的獨立節奏（見
    pipeline.py `_update_schedule_state` 的 docstring）。"""
    pipeline._update_schedule_state(dry_run=True, watch_only=False)
    pipeline._finish_schedule_state(dry_run=True, watch_only=False)
    assert pipeline.store.get_meta(RUN_STARTED_KEY) is None
    assert pipeline._schedule_alert is None

    pipeline._update_schedule_state(dry_run=False, watch_only=True)
    pipeline._finish_schedule_state(dry_run=False, watch_only=True)
    assert pipeline.store.get_meta(RUN_STARTED_KEY) is None
    assert pipeline._schedule_alert is None


def test_manual_scan_leaves_pending_for_next_daily_to_deliver(pipeline, monkeypatch):
    """Fix 3：`ygo-sniper scan`／dashboard 的手動掃描只會印，不會推播
    （`_run_notifications` 只在 `daily` 裡被呼叫）。用 pending 機制驗證：
    手動掃描偵測到問題之後，下一輪（即使它自己乾淨）仍然會把它送出去。"""
    import ygo_sniper.cli as cli_mod

    # 種一個保證觸發偵測的舊基準，模擬「上一輪很久以前開始、正常收尾」。
    pipeline.store.set_meta(RUN_STARTED_KEY, _ANCIENT_START)
    pipeline.store.set_meta(RUN_FINISHED_KEY, _ANCIENT_FINISH)

    # 模擬一次手動 `ygo-sniper scan`：偵測到空窗、正常收尾，但不推播。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    assert pipeline._schedule_alert is not None and "空窗" in pipeline._schedule_alert
    pipeline._finish_schedule_state(dry_run=False, watch_only=False)

    pending_after_manual_scan = pipeline.store.get_meta(PENDING_ALERT_KEY)
    assert pending_after_manual_scan  # 沒有人送過，pending 必須還在

    # 下一輪（例如緊接著的 `daily`）起跑：這次基準是新的（剛才手動 scan
    # 才寫的），本身不會再觸發新偵測，但舊 pending 必須被原樣帶出來。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    assert pipeline._schedule_alert is not None
    assert "空窗" in pipeline._schedule_alert

    fake = _FakeNotifier(ok=True)
    monkeypatch.setattr(pipeline, "notifier", fake)
    cli_mod._send_schedule_alert(pipeline)

    assert fake.sent == [pipeline._schedule_alert]
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) == ""  # 這次真的送達才清帳


def test_crashed_run_alert_survives_and_is_delivered_next_time(pipeline, monkeypatch):
    """Fix 4：`daily` 是 `try/finally`（沒有 `except`），`_scan()` 崩潰時
    `_run_notifications` 完全不會被叫到。這一輪偵測到的告警不能因此消失
    ——pending 帳撐住這個保證，下一輪成功收尾時要把它一起送出去。"""
    import ygo_sniper.cli as cli_mod

    pipeline.store.set_meta(RUN_STARTED_KEY, _ANCIENT_START)
    pipeline.store.set_meta(RUN_FINISHED_KEY, _ANCIENT_FINISH)

    # 這一輪偵測到空窗（等同 scan() 開頭呼叫 _update_schedule_state），
    # 然後「崩潰」——刻意不呼叫 _finish_schedule_state，模擬 _scan() 拋例外、
    # scan() 的 except 分支落狀態後直接 raise，never reaching finish。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    first_alert = pipeline._schedule_alert
    assert first_alert is not None and "空窗" in first_alert

    finished_before_crash = pipeline.store.get_meta(RUN_FINISHED_KEY)
    assert finished_before_crash == _ANCIENT_FINISH  # 還沒被這一輪覆寫

    # daily 的 try/finally 不會走到 _run_notifications，所以這裡不呼叫
    # _send_schedule_alert——崩潰的那一輪，Telegram 什麼都沒收到。

    # 下一輪成功執行：這次偵測會同時看到「有開始沒收尾」（RUN_FINISHED_KEY
    # 還停在崩潰前那個舊值，比新的 RUN_STARTED_KEY 早）。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    assert pipeline._schedule_alert is not None
    assert "收尾" in pipeline._schedule_alert
    # 崩潰前那則告警沒有消失——併進了這一輪的訊息裡（missed 計數 ≥2）。
    assert "先前 1 次" in pipeline._schedule_alert
    pipeline._finish_schedule_state(dry_run=False, watch_only=False)

    fake = _FakeNotifier(ok=True)
    monkeypatch.setattr(pipeline, "notifier", fake)
    cli_mod._send_schedule_alert(pipeline)

    assert len(fake.sent) == 1
    assert "先前 1 次" in fake.sent[0]
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) == ""


def test_failed_push_keeps_pending_for_retry(pipeline, monkeypatch):
    """Fix 2：送失敗（例如筆電剛醒來 Wi-Fi 還沒穩）不能清帳，
    否則下一輪比對到的是「乾淨」的新基準，這則告警就永遠消失了。"""
    import ygo_sniper.cli as cli_mod

    pipeline.store.set_meta(RUN_STARTED_KEY, _ANCIENT_START)
    pipeline.store.set_meta(RUN_FINISHED_KEY, _ANCIENT_FINISH)
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    assert pipeline._schedule_alert is not None

    fake = _FakeNotifier(ok=False)  # 模擬 Telegram 送出失敗
    monkeypatch.setattr(pipeline, "notifier", fake)
    cli_mod._send_schedule_alert(pipeline)

    assert fake.sent  # 有嘗試送
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) != ""  # 但沒清帳
