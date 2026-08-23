"""排程空窗偵測：純函式、假時間，零網路零 store。"""

import plistlib
from datetime import datetime
from pathlib import Path

import pytest

from ygo_sniper.schedule_watch import (
    _ALL_SLOTS,
    _HIGH_ALL_SLOTS,
    PENDING_ALERT_KEY,
    PENDING_ALERT_KEY_HIGH,
    RUN_FINISHED_KEY,
    RUN_FINISHED_KEY_HIGH,
    RUN_STARTED_KEY,
    RUN_STARTED_KEY_HIGH,
    expected_next_gap_minutes,
    missed_slots_between,
    next_slot_after,
    resolve_alert,
    schedule_health,
    watchdog_message,
)

_PLIST = Path(__file__).resolve().parents[1] / "scripts" / "com.jim.ygosniper.plist"
_HIGH_PLIST = Path(__file__).resolve().parents[1] / "scripts" / "com.jim.ygosniper.high.plist"


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
    assert msg is not None and "漏跑" in msg and "18:30" in msg


def test_alert_fires_when_drift_near_window_boundary_swallows_next_slot():
    """15:31 起跑（15:30 那格晚 1 分），17:30 整個消失，下一輪 18:00 才跑。"""
    msg = schedule_health(
        "2026-08-11T15:31:00", "2026-08-11T15:33:00", _dt("2026-08-11T18:00:00")
    )
    assert msg is not None and "漏跑" in msg and "17:30" in msg


# ---------------------------------------------------------------------------
# Fix B：missed_slots_between——不再比「經過了多久」，改成直接列舉漏跑的格子。
#
# 事故背景（見 schedule_watch.py 模組開頭）：Fix 1 之後仍留了一個
# `now - prev > 20 分寬容值` 的判準，實測 10 天真實 log 出現 2 次假警報／
# 重複告警——36 分鐘的正常晚開機被報成「排程空窗 11.6 小時」（因為訊息把
# 整個隔夜空窗也算了進去）。Fix B 改成「這一輪自己代表哪一格、prev 與那一
# 格之間還有哪幾格沒人代表」的絕對集合運算，drift 不再需要任何寬容值。
# ---------------------------------------------------------------------------
def test_no_false_positive_on_normal_late_morning_start():
    """複查案例（08-08）：當天第一輪 10:06:33 才起跑，比 09:30 晚 36 分鐘
    ——純粹是筆電晚開機，那一整天 15 個時段一個都沒漏。prev 是前一晚最後
    一輪（22:32 開始），22:30→09:30 是刻意的夜間空窗，兩者之間沒有任何
    網格時間點，所以必須完全不出聲——這正是舊版會誤報的案例。"""
    msg = schedule_health(
        "2026-08-07T22:32:00", "2026-08-07T22:35:00", _dt("2026-08-08T10:06:33")
    )
    assert msg is None


def test_missed_slots_between_lists_exact_missed_times():
    """複查給的精確算式驗證：prev=15:45、now=18:30，漏跑 17:30 與 18:00
    兩格（18:30 是這一輪自己代表的那一格，不算漏跑）。"""
    missed = missed_slots_between(_dt("2026-08-10T15:45:00"), _dt("2026-08-10T18:30:00"))
    assert missed == [_dt("2026-08-10T17:30:00"), _dt("2026-08-10T18:00:00")]

    msg = schedule_health(
        "2026-08-10T15:45:00", "2026-08-10T15:48:00", _dt("2026-08-10T18:30:00")
    )
    assert msg is not None
    assert "漏跑 2 個時段（17:30、18:00）" in msg


def test_missed_slots_between_excludes_the_current_runs_own_slot():
    """`now` 剛好落在一個網格時間點上時，那一格本身不算漏跑——它就是
    這一輪的存在本身。"""
    assert missed_slots_between(_dt("2026-08-11T19:00:00"), _dt("2026-08-11T19:30:00")) == []


def test_missed_slots_between_truncates_long_outages_in_message():
    """筆電放了好幾天沒開機：訊息只報頭尾＋筆數，不要把幾十個時間戳
    全部塞進 Telegram 訊息。"""
    msg = schedule_health(
        "2026-08-05T09:30:00", "2026-08-05T09:33:00", _dt("2026-08-08T09:30:00")
    )
    assert msg is not None
    assert "個時段" in msg
    assert "～" in msg  # 頭尾格式，不是逐一列舉


def test_missed_slots_between_silent_when_clock_moves_backward():
    """時鐘被往回校正、prev 落在 now 之後——維持舊版「未來基準值不出聲」
    的行為，不崩潰、也不誤報。"""
    assert missed_slots_between(_dt("2026-08-11T20:00:00"), _dt("2026-08-11T19:00:00")) == []


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
    # 08-10 事故的形狀：20:49 之後直接跳到 23:00（21:00-22:30 四班消失，
    # 22:30 是這一輪自己代表的那一格，不算在漏跑清單裡）
    msg = schedule_health(
        "2026-08-10T20:00:00", "2026-08-10T20:49:00", _dt("2026-08-10T23:00:44")
    )
    assert msg is not None
    assert "漏跑 4 個時段（20:30、21:00、21:30、22:00）" in msg


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
# Fix 5：_ALL_SLOTS 是 plist 的手抄本，兩邊必須實測相等，不能只靠註解提醒
#
# **必須比對 `_ALL_SLOTS` 本尊**，不能自己重新展開 `_WINDOWS` 再比一份副本
# ——`next_slot_after` 讀的是 `_ALL_SLOTS`，測試比另一份獨立算出來的複本，
# 等於守門守在偵測器根本不會走到的地方（跟這次修的 Fix 1 同一種錯：拿
# 「重新推導出來的值」冒充「真正被使用的那個值」去比較）。
# 2026-08-12 複查時用 mutant 證實過：只改 `_ALL_SLOTS` 漏掉 22:30、
# `_WINDOWS` 與 plist 都不動，若測試比的是重新展開的 `_WINDOWS`，這條測試
# 仍然綠燈，但 22:30 那個尖峰時段的偵測會真的失聰。
# ---------------------------------------------------------------------------
def test_windows_match_plist():
    with open(_PLIST, "rb") as f:
        plist = plistlib.load(f)
    plist_slots = {
        e["Hour"] * 60 + e["Minute"] for e in plist["StartCalendarInterval"]
    }
    code_slots = set(_ALL_SLOTS)
    assert code_slots == plist_slots, (
        f"schedule_watch._ALL_SLOTS 與 {_PLIST.name} 的 StartCalendarInterval 不同步：\n"
        f"code only: {sorted(code_slots - plist_slots)}\n"
        f"plist only: {sorted(plist_slots - code_slots)}"
    )


# ---------------------------------------------------------------------------
# Task 6（高價帶排程）：_HIGH_ALL_SLOTS 是 com.jim.ygosniper.high.plist 的手抄本，
# 與低價帶 test_windows_match_plist 同一套理由與同一種 mutant 防線。
# ---------------------------------------------------------------------------
def test_high_windows_match_high_plist():
    with open(_HIGH_PLIST, "rb") as f:
        plist = plistlib.load(f)
    plist_slots = {
        e["Hour"] * 60 + e["Minute"] for e in plist["StartCalendarInterval"]
    }
    code_slots = set(_HIGH_ALL_SLOTS)
    assert code_slots == plist_slots, (
        f"schedule_watch._HIGH_ALL_SLOTS 與 {_HIGH_PLIST.name} 的 "
        f"StartCalendarInterval 不同步：\n"
        f"code only: {sorted(code_slots - plist_slots)}\n"
        f"plist only: {sorted(plist_slots - code_slots)}"
    )


def test_high_windows_do_not_overlap_low_band_windows():
    """兩帶的觸發分鐘必須零重疊——這是排程時刻表的定案約束
    （低價帶佔 :30 與 :00/:30，高價帶只落在偶數整點 :00 與晚間 :15）。"""
    assert set(_ALL_SLOTS).isdisjoint(set(_HIGH_ALL_SLOTS))


def test_high_band_missed_slot_detection_fires_on_drift():
    """高價帶網格的漏格偵測必須跟低價帶一樣有效：18:15 起跑（正常時刻），
    下一輪本該是 20:15，卻直接跳到 22:15，中間 20:15 那格必須被偵測出來。
    换成高價帶自己的網格（`slots=_HIGH_ALL_SLOTS`），比照既有
    `test_alert_fires_when_drifted_run_masks_missed_slot` 的漂移情境寫法。"""
    msg = schedule_health(
        "2026-08-10T18:15:00",
        "2026-08-10T18:18:00",
        _dt("2026-08-10T22:15:00"),
        _HIGH_ALL_SLOTS,
    )
    assert msg is not None and "漏跑" in msg and "20:15" in msg


def test_high_band_missed_slots_between_uses_high_grid():
    """`missed_slots_between` 直接吃 `_HIGH_ALL_SLOTS`：10:00 起跑、下一輪
    16:00 才跑起來，中間 12:00／14:00 兩格必須被列出（16:00 是這一輪自己
    代表的那一格，不算漏跑）。"""
    missed = missed_slots_between(
        _dt("2026-08-11T10:00:00"), _dt("2026-08-11T16:00:00"), _HIGH_ALL_SLOTS
    )
    assert missed == [_dt("2026-08-11T12:00:00"), _dt("2026-08-11T14:00:00")]


def test_high_band_schedule_keys_differ_from_low_band():
    """兩帶的基準鍵／pending 鍵必須各自獨立——防止一帶的告警消耗掉另一帶的，
    或一帶跑一輪覆寫掉另一帶的基準（CLAUDE.md 第三節第八事故的同一種錯誤，
    換成「哪兩條輪次共用一本帳」）。"""
    assert RUN_STARTED_KEY_HIGH != RUN_STARTED_KEY
    assert RUN_FINISHED_KEY_HIGH != RUN_FINISHED_KEY
    assert PENDING_ALERT_KEY_HIGH != PENDING_ALERT_KEY
    keys = {
        RUN_STARTED_KEY,
        RUN_FINISHED_KEY,
        PENDING_ALERT_KEY,
        RUN_STARTED_KEY_HIGH,
        RUN_FINISHED_KEY_HIGH,
        PENDING_ALERT_KEY_HIGH,
    }
    assert len(keys) == 6  # 六把鍵字面上兩兩不同，沒有任何一對意外撞名


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
    """真的 `Pipeline`，db 在 tmp_path、不碰網路——與 test_card_snipe.py 同款。

    `_read_watchdog_ledger`（Fix A）讀的帳本路徑算成
    `cfg.db_path.parent / "last_run_exit"`，不是 `cfg.root / "data" / ...`
    ——`db_path` 就是這裡覆寫成 `tmp_path` 的那個絕對路徑，所以帳本檔
    自動落在沙盒裡，不需要另外覆寫 `root`。見 pipeline.py
    `_read_watchdog_ledger` docstring 裡對這個選擇的完整說明
    （簡言之：改用 `root` 的話，全 repo 沒覆寫過 `root` 的既有測試
    ——例如 `test_card_snipe.py` 呼叫 `pipeline.scan(...)`——就會在跑
    測試時去讀、甚至刪掉正式環境真正的 `data/last_run_exit`）。
    """
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


def test_high_band_scan_never_touches_low_band_schedule_keys(pipeline):
    """主線程追加的必修項：`high_band=True` 的一輪完全不碰低價帶的三把鍵
    ——只寫 `*_HIGH` 那三把。用真的 `Pipeline._update_schedule_state`／
    `_finish_schedule_state`（`scan()` 唯一會呼叫排程監督的地方）驗證，
    不必為此另外把整條 `_scan()` 跑一遍（跟既有 Fix 3／Fix 4 測試同款理由，
    見本檔案上方那段說明）。"""
    pipeline._update_schedule_state(dry_run=False, watch_only=False, high_band=True)
    pipeline._finish_schedule_state(dry_run=False, watch_only=False, high_band=True)

    assert pipeline.store.get_meta(RUN_STARTED_KEY) is None
    assert pipeline.store.get_meta(RUN_FINISHED_KEY) is None
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) is None

    assert pipeline.store.get_meta(RUN_STARTED_KEY_HIGH) is not None
    assert pipeline.store.get_meta(RUN_FINISHED_KEY_HIGH) is not None


def test_low_band_scan_never_touches_high_band_schedule_keys(pipeline):
    """反向情境：一般輪（`high_band=False`，等同 `ygo-sniper daily`／`scan`）
    完全不碰高價帶的三把鍵——兩本帳的互不污染是雙向的。"""
    pipeline._update_schedule_state(dry_run=False, watch_only=False, high_band=False)
    pipeline._finish_schedule_state(dry_run=False, watch_only=False, high_band=False)

    assert pipeline.store.get_meta(RUN_STARTED_KEY_HIGH) is None
    assert pipeline.store.get_meta(RUN_FINISHED_KEY_HIGH) is None
    assert pipeline.store.get_meta(PENDING_ALERT_KEY_HIGH) is None

    assert pipeline.store.get_meta(RUN_STARTED_KEY) is not None
    assert pipeline.store.get_meta(RUN_FINISHED_KEY) is not None


def test_manual_scan_leaves_pending_for_next_daily_to_deliver(pipeline, monkeypatch):
    """Fix 3：`ygo-sniper scan`／dashboard 的手動掃描只會印，不會推播
    （`_run_notifications` 只在 `daily` 裡被呼叫）。用 pending 機制驗證：
    手動掃描偵測到問題之後，下一輪（即使它自己乾淨）仍然會把它送出去。"""
    import ygo_sniper.cli as cli_mod

    # 種一個保證觸發偵測的舊基準，模擬「上一輪很久以前開始、正常收尾」。
    pipeline.store.set_meta(RUN_STARTED_KEY, _ANCIENT_START)
    pipeline.store.set_meta(RUN_FINISHED_KEY, _ANCIENT_FINISH)

    # 模擬一次手動 `ygo-sniper scan`：偵測到漏跑、正常收尾，但不推播。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    assert pipeline._schedule_alert is not None and "漏跑" in pipeline._schedule_alert
    pipeline._finish_schedule_state(dry_run=False, watch_only=False)

    pending_after_manual_scan = pipeline.store.get_meta(PENDING_ALERT_KEY)
    assert pending_after_manual_scan  # 沒有人送過，pending 必須還在

    # 下一輪（例如緊接著的 `daily`）起跑：這次基準是新的（剛才手動 scan
    # 才寫的），本身不會再觸發新偵測，但舊 pending 必須被原樣帶出來。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    assert pipeline._schedule_alert is not None
    assert "漏跑" in pipeline._schedule_alert

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

    # 這一輪偵測到漏跑（等同 scan() 開頭呼叫 _update_schedule_state），
    # 然後「崩潰」——刻意不呼叫 _finish_schedule_state，模擬 _scan() 拋例外、
    # scan() 的 except 分支落狀態後直接 raise，never reaching finish。
    pipeline._update_schedule_state(dry_run=False, watch_only=False)
    first_alert = pipeline._schedule_alert
    assert first_alert is not None and "漏跑" in first_alert

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


def test_high_band_alert_delivery_clears_high_key_not_low_key(pipeline, monkeypatch):
    """主線程追加必修（Task 6 builder 發現）：`daily-high` 送達的告警要清
    `PENDING_ALERT_KEY_HIGH`，不能誤清低價帶的 `PENDING_ALERT_KEY`——否則
    高價帶自己的 pending 永遠不會被消耗，「先前 N 次未送達」無限疊加。"""
    import ygo_sniper.cli as cli_mod

    # 低價帶先留一筆看似無關的 pending，確保它在高價帶送達後原封不動。
    pipeline.store.set_meta(PENDING_ALERT_KEY, "低價帶自己的舊 pending")

    pipeline.store.set_meta(RUN_STARTED_KEY_HIGH, _ANCIENT_START)
    pipeline.store.set_meta(RUN_FINISHED_KEY_HIGH, _ANCIENT_FINISH)
    pipeline._update_schedule_state(dry_run=False, watch_only=False, high_band=True)
    assert pipeline._schedule_alert is not None and "漏跑" in pipeline._schedule_alert
    pipeline._finish_schedule_state(dry_run=False, watch_only=False, high_band=True)

    fake = _FakeNotifier(ok=True)
    monkeypatch.setattr(pipeline, "notifier", fake)
    cli_mod._send_schedule_alert(pipeline)

    assert fake.sent == [pipeline._schedule_alert]
    assert pipeline.store.get_meta(PENDING_ALERT_KEY_HIGH) == ""  # 高價帶帳清了
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) == "低價帶自己的舊 pending"  # 低價帶帳沒被動到


def test_low_band_alert_delivery_clears_low_key_not_high_key(pipeline, monkeypatch):
    """反向情境：`daily`／`scan`（`high_band=False`，既有行為）送達告警時，
    只清 `PENDING_ALERT_KEY`，不動高價帶自己的 `PENDING_ALERT_KEY_HIGH`。"""
    import ygo_sniper.cli as cli_mod

    pipeline.store.set_meta(PENDING_ALERT_KEY_HIGH, "高價帶自己的舊 pending")

    pipeline.store.set_meta(RUN_STARTED_KEY, _ANCIENT_START)
    pipeline.store.set_meta(RUN_FINISHED_KEY, _ANCIENT_FINISH)
    pipeline._update_schedule_state(dry_run=False, watch_only=False, high_band=False)
    assert pipeline._schedule_alert is not None and "漏跑" in pipeline._schedule_alert
    pipeline._finish_schedule_state(dry_run=False, watch_only=False, high_band=False)

    fake = _FakeNotifier(ok=True)
    monkeypatch.setattr(pipeline, "notifier", fake)
    cli_mod._send_schedule_alert(pipeline)

    assert fake.sent == [pipeline._schedule_alert]
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) == ""  # 低價帶帳清了
    assert pipeline.store.get_meta(PENDING_ALERT_KEY_HIGH) == "高價帶自己的舊 pending"  # 高價帶帳沒被動到


# ---------------------------------------------------------------------------
# Fix A：watchdog_message——純函式，把 run_daily.sh 寫的帳本 dict 翻成一句話。
#
# 事故背景（見 schedule_watch.py 模組開頭）：2026-08-10 卡死 8.5 小時的那次，
# 卡點在 pipe.close()／直譯器結束，發生在 RUN_FINISHED_KEY 已經正常寫下
# 之後——schedule_health 對此完全沒有偵測力。唯一的線索是 watchdog 的
# exit=124，但原本那則失敗通知的 curl 結果被直接丟棄，筆電剛醒 Wi-Fi
# 沒穩時會送不出去，導致整個事故 100% 無聲。
# ---------------------------------------------------------------------------
def test_watchdog_message_none_when_ledger_missing_or_malformed():
    assert watchdog_message(None) is None
    assert watchdog_message([1, 2, 3]) is None
    assert watchdog_message({}) is None
    assert watchdog_message({"exit": "not-an-int"}) is None
    assert watchdog_message({"exit": True}) is None  # bool 是 int 子類，故意排除


def test_watchdog_message_none_on_success():
    assert watchdog_message({"exit": 0, "notify_attempted": False}) is None


def test_watchdog_message_fires_on_watchdog_kill_when_notify_not_delivered():
    msg = watchdog_message(
        {"exit": 124, "notify_attempted": False, "notify_http": "000", "notify_curl_exit": 0}
    )
    assert msg is not None and "watchdog" in msg and "124" in msg


def test_watchdog_message_fires_when_curl_itself_failed():
    """通知有嘗試但 curl 自己失敗（筆電剛醒 Wi-Fi 還沒穩）——一樣要出聲，
    這正是 Fix A 要解的事故形狀。"""
    msg = watchdog_message(
        {"exit": 124, "notify_attempted": True, "notify_http": "000", "notify_curl_exit": 6}
    )
    assert msg is not None and "watchdog" in msg


def test_watchdog_message_silent_when_notification_already_delivered():
    """run_daily.sh 自己那則失敗通知已經確認送達（http=200）——不重複講，
    否則兩套告警管道疊在一起就是 Fix B 想解決的那種雜訊。"""
    msg = watchdog_message(
        {"exit": 124, "notify_attempted": True, "notify_http": "200", "notify_curl_exit": 0}
    )
    assert msg is None


def test_watchdog_message_generic_wording_for_non_watchdog_failure():
    """一般失敗（非 124）要用泛用文案，不能被誤標成 watchdog 終止
    ——與 test_run_daily_script.py 的 test_ordinary_failure_exit_7 呼應。"""
    msg = watchdog_message(
        {"exit": 7, "notify_attempted": False, "notify_http": "000", "notify_curl_exit": 0}
    )
    assert msg is not None
    assert "exit=7" in msg
    assert "watchdog" not in msg


# ---------------------------------------------------------------------------
# Fix A：`_read_watchdog_ledger` / `_consume_watchdog_ledger`——不純的那
# 一半，讀真的檔案、（在正確的時機）真的會刪檔案。用 tmp_path（透過
# pipeline fixture 的 db_path.parent）而不是真的 data/。
#
# 2026-08-12 再修一次（先落帳再刪證據）：原本讀取與刪除是同一步
# （`_fold_watchdog_ledger` 折出訊息後立刻 unlink），順序在
# `set_meta(PENDING_ALERT_KEY, …)` **之前**——中間被殺掉的話，這份
# watchdog 證據會憑空消失（雖然視窗只有微秒、且下一輪自己的新帳本會
# 自癒，但順序本身不該顛倒：先確定證據已經落進 pending，才能刪掉來源）。
# 拆成 `_read_watchdog_ledger`（只讀不刪）＋`_consume_watchdog_ledger`
# （呼叫端在 `set_meta` 成功之後才呼叫）之後，順序在型別層級就對了。
# ---------------------------------------------------------------------------
def test_read_watchdog_ledger_does_not_delete_the_file(pipeline):
    """讀取本身絕不刪檔——刪除只能是呼叫端在 pending 落地後的明確動作。"""
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text(
        '{"exit": 124, "notify_attempted": false, "notify_http": "000", '
        '"notify_curl_exit": 0}',
        encoding="utf-8",
    )

    msg, returned_path = pipeline._read_watchdog_ledger()

    assert msg is not None and "watchdog" in msg
    assert returned_path == ledger_path
    assert ledger_path.exists()  # 讀取不消費


def test_read_watchdog_ledger_none_when_file_absent(pipeline):
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    assert not ledger_path.exists()
    assert pipeline._read_watchdog_ledger() == (None, None)


def test_read_watchdog_ledger_returns_no_path_when_nothing_to_fold(pipeline):
    """exit=0（成功）沒有東西可折——回傳的路徑也要是 None，
    呼叫端才知道不必也不該刪這個檔案。"""
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text('{"exit": 0, "notify_attempted": false}', encoding="utf-8")

    assert pipeline._read_watchdog_ledger() == (None, None)
    assert ledger_path.exists()


def test_read_watchdog_ledger_tolerates_corrupt_json(pipeline):
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text("not valid json{{{", encoding="utf-8")

    assert pipeline._read_watchdog_ledger() == (None, None)  # 不崩潰


def test_consume_watchdog_ledger_deletes_the_file(pipeline):
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text("{}", encoding="utf-8")

    pipeline._consume_watchdog_ledger(ledger_path)

    assert not ledger_path.exists()


def test_consume_watchdog_ledger_tolerates_missing_file(pipeline):
    """刪不掉（例如已經被別的東西刪過）不算例外——下一輪頂多重複折一次，
    比讓 `_update_schedule_state` 整段炸掉輕得多。"""
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    assert not ledger_path.exists()

    pipeline._consume_watchdog_ledger(ledger_path)  # 不崩潰


def test_update_schedule_state_folds_watchdog_ledger_into_pending(pipeline):
    """端到端：帳本檔案 → `_update_schedule_state` → `_schedule_alert` 與
    `PENDING_ALERT_KEY` 都要看得到，而且檔案要被消費掉（不重複折）。"""
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text(
        '{"exit": 124, "notify_attempted": false, "notify_http": "000", '
        '"notify_curl_exit": 0}',
        encoding="utf-8",
    )
    # 沒有排程基準（第一次跑）——這一輪的訊息應該完全來自 watchdog 帳本，
    # 不是排程空窗（沒有 prev_started，schedule_health 回 None）。
    assert pipeline.store.get_meta(RUN_STARTED_KEY) is None

    pipeline._update_schedule_state(dry_run=False, watch_only=False)

    assert pipeline._schedule_alert is not None
    assert "watchdog" in pipeline._schedule_alert
    assert not ledger_path.exists()
    assert pipeline.store.get_meta(PENDING_ALERT_KEY) != ""


def test_update_schedule_state_keeps_ledger_when_pending_write_fails(pipeline, monkeypatch):
    """釘住順序本身：`set_meta(PENDING_ALERT_KEY, …)` 失敗時，帳本檔案
    必須還在——證明刪除確實排在「pending 落地」之後，不是之前。

    包一層 `try/except`（`_update_schedule_state` 本來就有，理由見它的
    docstring：偵測器壞掉不能拖垮真正的掃描）意味著這次呼叫不會拋出，
    但 `_schedule_alert` 也不會被設成新值——這是可接受的降級：寧可這一輪
    沒看到告警文字，也不要在 pending 沒寫成功的情況下先把唯一的證據刪掉。
    """
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text(
        '{"exit": 124, "notify_attempted": false, "notify_http": "000", '
        '"notify_curl_exit": 0}',
        encoding="utf-8",
    )

    real_set_meta = pipeline.store.set_meta

    def _boom(key, value):
        if key == PENDING_ALERT_KEY:
            raise RuntimeError("模擬 store 寫入失敗")
        return real_set_meta(key, value)

    monkeypatch.setattr(pipeline.store, "set_meta", _boom)

    pipeline._update_schedule_state(dry_run=False, watch_only=False)  # 不應該往外拋

    assert ledger_path.exists(), "pending 沒寫成功，帳本不該被刪——這正是這次要釘住的順序"


def test_dry_run_does_not_consume_watchdog_ledger(pipeline):
    """`--dry-run` 不吃邊緣觸發——連 watchdog 帳本也不該被 dry-run 消費掉，
    不然下一次真正的 scan 就讀不到了。"""
    ledger_path = pipeline.cfg.db_path.parent / "last_run_exit"
    ledger_path.write_text('{"exit": 124, "notify_attempted": false}', encoding="utf-8")

    pipeline._update_schedule_state(dry_run=True, watch_only=False)

    assert ledger_path.exists()
    assert pipeline._schedule_alert is None
