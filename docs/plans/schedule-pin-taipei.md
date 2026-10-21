# 排程時鐘釘死 Asia/Taipei Implementation Plan

> **For agentic workers:** 主線程逐 task 派 `builder`（@inline），驗收由主線程親跑。Steps 用 `- [ ]` 追蹤。實作者只讀本檔，不依賴規劃對話。先讀 `CLAUDE.md` 第三節（同基準）、第五節（靜默失敗、漏跑視窗帳）、第九節排程時段。

**Goal:** 不論這台 Mac 的系統時區是什麼（使用者 2026-10 在 Dallas、10/28 到 LA、11/9 回台北，macOS 會跟著自動切），掃描排程、排程監督、漏跑視窗帳、補跑回顧全部以**台北牆上時間**為唯一基準；launchd 不再依賴系統時區來對準網格。

**事故背景（2026-10-20 回放 `data/logs/daily-*.log` 得到）：** 自 10-08 起系統時區變成 America/Chicago。launchd 的 `StartCalendarInterval` 仍照 job 載入時（台北）的網格觸發，Python 的 `datetime.now()` 卻跟著系統時區走，兩者**同單位（牆上分鐘）不同基準**。結果 `schedule_health` 連續 12 天每天報「漏跑 9–10 個時段（09:30～20:00）」——那段 Chicago 時間是台北深夜，本來就不排程。使用者 10-19 收到的告警就是這個。這是 CLAUDE.md 第三節的第九個實例。

**Architecture（三層，缺一不可）：**
1. **Python 單一時鐘模組** `src/ygo_sniper/clock.py`：`SCHEDULE_TZ = ZoneInfo("Asia/Taipei")`，`schedule_now()` 回**台北 naive** datetime（與既有 meta 鍵的格式相容：仍是 naive ISO，只是基準從「機器本地」改成「台北」），`schedule_naive_to_utc()`／`utc_to_schedule_str()` 做兩個方向的換算。排程監督、視窗帳、補跑回顧裡所有 `datetime.now()` 一律改走它。`schedule_watch.py` 本身不動（純函式，吃什麼基準就算什麼基準）。
2. **launchd 改成每 15 分鐘觸發、由 Python 判斷「現在是不是台北網格上的一格」**：兩支 plist 的 `StartCalendarInterval` 改成只寫 `Minute` 的四個 dict（0／15／30／45；沒有 `Hour` 鍵＝每小時都觸發）。`run_daily.sh`／`run_high.sh` 取鎖之前先跑 `ygo-sniper slot-check --band std|high`：台北現在落在網格上（±7 分鐘容忍 launchd 漂移）**或**上一輪之後有網格被跳過（筆電睡醒的補觸發）→ exit 0 往下跑；否則 exit 3 **靜默**結束（不寫 log、不碰鎖、不碰 ledger——一天 96 次觸發裡 81 次是這條路，寫 log 會淹掉回放）。網格仍只定義在 `schedule_watch._WINDOWS`／`_HIGH_WINDOWS` 一處。
3. **基準切換的一次性保護**：meta 新鍵 `schedule_clock_tz`。`_update_schedule_state` 讀到它 ≠ `"Asia/Taipei"`（含不存在）時，視為「沒有可用基準」：本輪不做漏跑偵測、不寫視窗帳，只寫新基準與這把鍵。否則部署後第一輪會拿 Chicago 寫的 `prev_started` 對台北的 `now` 算出十幾格假漏跑並推假告警。

**同時做的小事：** 三支 shell wrapper 開頭 `export TZ=Asia/Taipei`，讓 bash `date` 的 log 時間戳、`last_run_exit` 的 `finished_at`、`[req]` log 全部是台北時間（回放 log 才對得上網格）。這**不是**主修法——使用者手動在 Chicago shell 跑 `ygo-sniper daily` 時沒有這個 export，所以 Python 端一定要自己釘。

**不做的事：** 不改 `store._now_iso()`（UTC aware，scan_status／cutoff 用，本來就正確）；不改三個「每日一次」的門（UTC 日界，與排程無關）；不改 `notify._local_end_time`（已是 Asia/Taipei）。

**預先量好的事實：**
- 台北沒有夏令時間；Chicago／LA 有（11-01 回撥）。釘台北後 DST 問題消失。
- 低價帶網格 15 格、高價帶 7 格，全部是 15 分鐘的倍數（09:30、18:15…）→ 每 15 分鐘觸發打得到每一格。
- launchd `StartCalendarInterval` 的 dict 省略 `Hour` 即為「每小時」（Apple `launchd.plist(5)`：缺的鍵視為萬用）。
- 現有 meta 值：`schedule_run_started_at` 等鍵是 Chicago naive；`schedule_gap_window` 2026-10-20 那筆也是。層 3 處理它們。

---

## File Structure

| 檔案 | 動作 |
|---|---|
| `src/ygo_sniper/clock.py` | 新增：`SCHEDULE_TZ`、`SCHEDULE_TZ_NAME`、`schedule_now()`、`schedule_naive_to_utc()`、`utc_to_schedule_str()` |
| `src/ygo_sniper/pipeline.py` | `_update_schedule_state` 1026／1062、`_finish_schedule_state` 1183 改 `schedule_now()`；加 `schedule_clock_tz` 保護 |
| `src/ygo_sniper/cli.py` | 441／466／469 改 `schedule_now()`；新增 `slot-check` 指令 |
| `src/ygo_sniper/catchup.py` | `window_to_utc` 改用 `SCHEDULE_TZ`；`_fmt_local` 改台北 |
| `src/ygo_sniper/schedule_watch.py` | 新增純函式 `should_run(now, prev_started, slots, tolerance_minutes=7)`；`_format_missed` 加「（台北）」 |
| `scripts/com.jim.ygosniper.plist`、`scripts/com.jim.ygosniper.high.plist` | `StartCalendarInterval` 改四個 Minute-only dict |
| `scripts/run_daily.sh`、`scripts/run_high.sh`、`scripts/run_dashboard.sh` | `export TZ=Asia/Taipei`；前兩支加 slot-check 閘門 |
| `Makefile` | `schedule` 的 echo 文字改正 |
| `web/static/index.html` | 漏跑 pill 文字加「台北」 |
| `tests/test_clock.py`（新）、`tests/test_schedule_watch.py`、`tests/test_pipeline_catchup.py`、`tests/test_catchup.py`、`tests/test_cli_notify.py`、`tests/test_run_daily_script.py` | 見各 task |
| `CLAUDE.md`、`docs/run_daily.md`、`docs/dashboard.md` | 文件 |

---

## Task 1 @inline：`clock.py` ＋ Python 端全部改走台北時鐘 ＋ 基準切換保護

**Files:** `src/ygo_sniper/clock.py`（新）、`pipeline.py`、`cli.py`、`catchup.py`、`tests/test_clock.py`（新）、`tests/test_pipeline_catchup.py`、`tests/test_catchup.py`、`tests/test_cli_notify.py`

- [ ] **Step 1: 失敗測試 `tests/test_clock.py`**（用 `monkeypatch.setenv("TZ", "America/Chicago")`＋`time.tzset()` 把系統時區故意設成 Chicago，證明結果不受影響；teardown 照 `tests/test_cli_notify.py` 的 autouse fixture 寫法還原）

```python
from datetime import UTC, datetime
from ygo_sniper import clock


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
```

- [ ] **Step 2: 實作 `clock.py`**

```python
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
```

- [ ] **Step 3: 改呼叫端**
  - `pipeline.py:1026` `now = datetime.now()` → `now = schedule_now()`；`:1183` 同；`_update_schedule_state` 的 `now` 參數預設維持 `None` → `schedule_now()`。
  - `cli.py:441`、`:466`、`:469` → `schedule_now()`。
  - `catchup.py`：`window_to_utc(local_from, local_to)` 改為 `return schedule_naive_to_utc(local_from), schedule_naive_to_utc(local_to)`；`_fmt_local(dt)` → `utc_to_schedule_str(dt)`；docstring 的「本地」全改「台北」。
  - 全檔 `grep -n "datetime.now()" src/ygo_sniper/pipeline.py src/ygo_sniper/cli.py src/ygo_sniper/catchup.py` 剩下的每一個都要能說出為什麼不是排程用途（例如 `sources/base.py` 的 `[req]` log 不在此 task 範圍）。

- [ ] **Step 4: 基準切換保護（層 3）**

`schedule_watch.py` 加常數 `CLOCK_TZ_KEY = "schedule_clock_tz"`。`pipeline._update_schedule_state`，在讀 `prev_started` 之前：

```python
            clock_tz = self.store.get_meta(CLOCK_TZ_KEY)
            if clock_tz != SCHEDULE_TZ_NAME:
                # 排程帳的基準換了（第一次部署台北時鐘，或未來再換）：舊的 prev_started
                # 是另一個時區的牆上時間，拿來對帳只會生出假漏跑。本輪視為「沒有基準」，
                # 只寫新基準與標記，從下一輪開始正常偵測。
                print(f"[schedule] 排程時鐘基準 {clock_tz!r} → {SCHEDULE_TZ_NAME}，本輪不做漏跑偵測")
                self.store.set_meta(CLOCK_TZ_KEY, SCHEDULE_TZ_NAME)
                self.store.set_meta(started_key, now.isoformat())
                self._schedule_alert = None
                return
```

（高價帶與低價帶共用同一把 `CLOCK_TZ_KEY`——基準是全域的；但 `started_key` 要寫自己那一帶的。注意 pending 告警帳不清：若上一輪有未送達的告警，照舊由 `resolve_alert` 在下一輪處理。）

測試（`tests/test_pipeline_catchup.py`）：
```python
def test_first_run_after_clock_change_resets_baseline_without_alert(pipe_factory):
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T09:00:01")   # 舊基準（Chicago）
    pipe.store.set_meta(sw.RUN_FINISHED_KEY, "2026-10-19T10:15:23")
    assert pipe.store.get_meta(sw.CLOCK_TZ_KEY) is None
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 21, 13, 30))
    assert pipe._schedule_alert is None
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None
    assert pipe.store.get_meta(sw.CLOCK_TZ_KEY) == "Asia/Taipei"
    assert pipe.store.get_meta(sw.RUN_STARTED_KEY) == "2026-10-21T13:30:00"
    # 第二輪開始正常偵測：13:30 → 18:00 之間漏了 15:30、17:30
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 21, 18, 0, 30))
    assert pipe._schedule_alert and "漏跑 2 個時段" in pipe._schedule_alert
```

- [ ] **Step 4b（2026-10-21 主線程裁決）: 既有 8 個測試補寫基準鍵**
  Step 4 的保護對「全新 store、直接寫舊 `RUN_STARTED_KEY` 再期待本輪偵測」的測試是預期內的紅燈：它們的前提少了「排程帳已經是台北基準」這件事。修法：在這 8 個測試寫入 `RUN_STARTED_KEY`／`RUN_STARTED_KEY_HIGH` 的那一行旁補 `store.set_meta(sw.CLOCK_TZ_KEY, "Asia/Taipei")`（或對應的 `pipe.store`），**斷言一字不改**。清單：`tests/test_pipeline_catchup.py::test_gap_record_is_written_by_update_schedule_state`、`::test_gap_record_failure_does_not_block_baseline_or_scan`；`tests/test_schedule_watch.py::test_manual_scan_leaves_pending_for_next_daily_to_deliver`、`::test_crashed_run_alert_survives_and_is_delivered_next_time`、`::test_failed_push_keeps_pending_for_retry`、`::test_high_band_alert_delivery_clears_high_key_not_low_key`、`::test_low_band_alert_delivery_clears_low_key_not_high_key`、`::test_update_schedule_state_folds_watchdog_ledger_into_pending`。
  不採用 autouse fixture 自動預寫：那會讓「保護分支」在測試裡變成看不見的例外路徑，新的保護測試還要 opt-out。

- [ ] **Step 5: 既有測試改成「系統時區是 Chicago 也要過」**
  `tests/test_catchup.py:20-24` 與 `tests/test_cli_notify.py:324-331, 370-371` 目前把 `TZ` 設成 `Asia/Taipei` 才讓錨值成立——改成 `America/Chicago`。改完這些測試仍必須全綠，這就是「釘死台北」的證據。

- [ ] **Step 6: 驗收 ＋ commit**
  `TZ=America/Chicago .venv/bin/pytest tests/test_clock.py tests/test_catchup.py tests/test_cli_notify.py tests/test_pipeline_catchup.py tests/test_schedule_watch.py` 全綠；`TZ=Asia/Taipei` 同一組也全綠；`make test` 全綠（前景）。
  commit：`feat(clock): 排程時鐘釘死 Asia/Taipei——schedule_now()、視窗/回顧換算改台北基準、基準切換一次性保護`

---

## Task 2 @inline：launchd 每 15 分觸發 ＋ `slot-check` 閘門 ＋ wrapper TZ

**Files:** `schedule_watch.py`（`should_run`）、`cli.py`（`slot-check`）、兩支 plist、三支 wrapper、`Makefile`、`tests/test_schedule_watch.py`、`tests/test_run_daily_script.py`

- [ ] **Step 1: 失敗測試——`should_run` 純函式**（`tests/test_schedule_watch.py`）

```python
def test_should_run_on_grid_slot_with_tolerance():
    prev = "2026-10-21T11:30:00"
    assert sw.should_run(datetime(2026, 10, 21, 13, 30, 0), prev) is True
    assert sw.should_run(datetime(2026, 10, 21, 13, 36, 59), prev) is True    # launchd 漂移 +7 內
    assert sw.should_run(datetime(2026, 10, 21, 13, 45, 0), prev) is False    # 不在格上、也沒漏格
    assert sw.should_run(datetime(2026, 10, 21, 12, 0, 0), prev) is False


def test_should_run_catches_up_after_sleep():
    """睡醒時 launchd 補觸發一次；即使不在格上，只要上一輪之後有格被跳過就要跑。"""
    prev = "2026-10-21T09:30:00"
    assert sw.should_run(datetime(2026, 10, 21, 14, 7, 0), prev) is True      # 漏了 11:30、13:30


def test_should_run_without_baseline_runs_only_on_slot():
    assert sw.should_run(datetime(2026, 10, 21, 13, 30, 0), None) is True
    assert sw.should_run(datetime(2026, 10, 21, 13, 45, 0), None) is False


def test_should_run_high_band_uses_high_grid():
    assert sw.should_run(datetime(2026, 10, 21, 18, 15, 0), None, slots=sw._HIGH_ALL_SLOTS) is True
    assert sw.should_run(datetime(2026, 10, 21, 18, 0, 0), None, slots=sw._HIGH_ALL_SLOTS) is False


def test_all_slots_are_multiples_of_15_minutes():
    """plist 改成每 15 分觸發後，網格每一格都必須打得到。"""
    assert all(m % 15 == 0 for m in sw._ALL_SLOTS)
    assert all(m % 15 == 0 for m in sw._HIGH_ALL_SLOTS)


def test_plists_fire_every_15_minutes_all_day():
    """取代舊的 test_windows_match_plist／test_high_windows_match_high_plist：
    網格不再寫在 plist 裡（會跟著系統時區跑），plist 只負責每 15 分叫醒 slot-check。"""
    import plistlib
    for path in (PLIST, HIGH_PLIST):
        with open(path, "rb") as f:
            intervals = plistlib.load(f)["StartCalendarInterval"]
        assert [d.get("Minute") for d in intervals] == [0, 15, 30, 45]
        assert all("Hour" not in d for d in intervals)
```

舊的 `test_windows_match_plist`／`test_high_windows_match_high_plist` **刪除**（它們釘的是舊設計），commit message 要說明。

- [ ] **Step 2: 實作 `should_run`**（`schedule_watch.py`）

```python
def should_run(now: datetime, prev_started: str | None, slots: list[int] | None = None,
               *, tolerance_minutes: int = 7) -> bool:
    """launchd 每 15 分鐘叫一次，這裡決定這一次要不要真的掃。

    兩個條件任一成立就跑：
    1. `now` 落在網格某一格的 [slot, slot + tolerance) 內——tolerance 吃掉 launchd 的喚醒漂移；
    2. 上一輪（`prev_started`）之後有格被跳過（`missed_slots_between`）——筆電睡醒時 launchd
       只補觸發一次、時間不在格上，不靠這條就永遠補不到。
    `now` 必須是台北 naive（`clock.schedule_now()`）；`prev_started` 是同基準的 ISO 或 None。
    """
    if slots is None:
        slots = _ALL_SLOTS
    minute_of_day = now.hour * 60 + now.minute
    on_slot = any(0 <= minute_of_day - s < tolerance_minutes for s in slots)
    if on_slot:
        return True
    return bool(detect_missed(prev_started, now, slots))
```

`_format_missed` 的兩種文字各加「台北」：`漏跑 N 個時段（台北 …）`（既有測試若斷言完整字串要同步改；只改這個字）。

- [ ] **Step 3: `ygo-sniper slot-check --band std|high`**（`cli.py`）

```python
@app.command("slot-check")
def slot_check(band: str = typer.Option("std", help="std（低價帶）或 high（高價帶）")):
    """launchd 每 15 分鐘叫一次 run_daily.sh／run_high.sh，腳本先問這裡「現在該不該掃」。
    exit 0 = 該掃；exit 3 = 不在台北網格上、也沒有漏格，靜默結束。
    只讀 meta，不寫任何東西、不印任何東西（一天 96 次呼叫，印了會淹掉 log）。
    """
    from .clock import schedule_now
    from .schedule_watch import RUN_STARTED_KEY, RUN_STARTED_KEY_HIGH, _HIGH_ALL_SLOTS, should_run
    store = Store(load_config().db_path)
    try:
        if band == "high":
            ok = should_run(schedule_now(), store.get_meta(RUN_STARTED_KEY_HIGH), _HIGH_ALL_SLOTS)
        elif band == "std":
            ok = should_run(schedule_now(), store.get_meta(RUN_STARTED_KEY))
        else:
            raise typer.BadParameter("band 只能是 std 或 high")
    finally:
        store.close()
    raise typer.Exit(code=0 if ok else 3)
```

（`Store` 的建構與關閉方式照 `web/app.py` 或其他 CLI 指令既有寫法；若 `Store` 沒有 `close`，就不要呼叫。）

- [ ] **Step 4: wrapper**
  - `run_daily.sh`、`run_high.sh`、`run_dashboard.sh` 在 `set -uo pipefail` 之後加：
    ```bash
    # 排程時鐘釘台北（docs/plans/schedule-pin-taipei.md）：bash 的 date、ledger 的 finished_at、
    # Python 內所有未指定時區的 datetime.now() 都跟著變成台北。Python 端排程邏輯另有
    # clock.schedule_now() 自己釘，這行只是讓 log 與帳本看起來一致。
    export TZ=Asia/Taipei
    ```
  - `run_daily.sh`／`run_high.sh` 在 `cd "$PROJECT_DIR"` 之後、`acquire_lock` 之前加閘門：
    ```bash
    # 每 15 分鐘被 launchd 叫醒一次；不在台北網格上、也沒有漏格就靜默結束。
    # exit 0：這條路一天走 81 次，不寫 log、不碰鎖、不碰 ledger。
    if [ -x .venv/bin/ygo-sniper ]; then
        .venv/bin/ygo-sniper slot-check --band std >/dev/null 2>&1
        rc=$?
        if [ "$rc" -eq 3 ]; then exit 0; fi
        # rc 非 0 非 3（例如 db 壞掉）：放行往下跑，讓既有的失敗路徑大聲報錯，不要在這裡吞掉
    fi
    ```
    （`run_high.sh` 用 `--band high`。）`.venv` 不存在的情況留給既有的檢查（它會送失敗通知）。
  - `Makefile:49` echo 改成「每 15 分鐘叫醒一次，由 ygo-sniper slot-check 依台北網格決定是否掃描」。

- [ ] **Step 4b（2026-10-21 主線程裁決，builder 抓到 plan 的靜默失敗）: 閘門只在 launchd 觸發時生效**
  原 Step 4 的閘門無條件跑在 `acquire_lock` 之前，會把 dashboard 的 `run_daily.sh --catchup`（`web/app.py` 的補跑按鈕）與使用者手動執行一起靜默吞掉——dashboard 回「已啟動」但什麼都沒跑，正是 CLAUDE.md 第五節的病。修法：閘門包在 `if [ "${YGO_SLOT_GATE:-0}" = "1" ]; then … fi` 裡，`YGO_SLOT_GATE=1` **只**由兩支 plist 的 `EnvironmentVariables` 設定；dashboard subprocess 與手動 shell 沒有這個變數，永遠直接往下跑。`tests/test_run_daily_script.py` 既有兩條（watchdog 124、重入跳過）因此不受影響、不改；另加兩條：(a) `YGO_SLOT_GATE=1` 且 stub 的 `slot-check` 回 3 → wrapper exit 0、log 無新行、無鎖目錄、ledger 未寫；(b) 未設變數時即使 stub 回 3 也照常往下跑。
  附帶：`plutil -lint` 對 XML 註解內的 `--` 放行但 `plistlib` 會拒絕，plist 的驗收以 pytest（`plistlib.load`）為準；`Makefile` 的 `schedule-high` echo 一併改正（plan 漏列）。

- [ ] **Step 5: plist**
  兩支 plist 的 `StartCalendarInterval` 陣列改成：
  ```xml
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Minute</key><integer>0</integer></dict>
    <dict><key>Minute</key><integer>15</integer></dict>
    <dict><key>Minute</key><integer>30</integer></dict>
    <dict><key>Minute</key><integer>45</integer></dict>
  </array>
  ```
  並在 plist 的註解（既有的 `<!-- -->`）寫明「網格在 schedule_watch._WINDOWS（台北時間），這裡只負責每 15 分叫醒」。`EnvironmentVariables` 加 `<key>TZ</key><string>Asia/Taipei</string>`（與 wrapper 的 export 雙保險）。

- [ ] **Step 6: `tests/test_run_daily_script.py`**
  看它怎麼測 wrapper（可能用 `bash -n` 或字串斷言）；加一條：腳本含 `export TZ=Asia/Taipei` 與 `slot-check --band std`，且 slot-check 出現在 `acquire_lock` 之前（用字串位置比較）。

- [ ] **Step 7: 驗收 ＋ commit**
  `bash -n scripts/run_daily.sh scripts/run_high.sh scripts/run_dashboard.sh`；`plutil -lint scripts/*.plist`；`.venv/bin/ygo-sniper slot-check --band std; echo $?`（主線程跑，印 0 或 3，不印其他字）；`.venv/bin/pytest tests/test_schedule_watch.py tests/test_run_daily_script.py tests/test_cli_notify.py` 全綠；`make test` 全綠。
  commit：`feat(schedule): launchd 每 15 分叫醒、slot-check 依台北網格決定是否掃描；wrapper 與 plist 釘 TZ=Asia/Taipei`

---

## Task 3 @inline：文件

- `CLAUDE.md` 第三節表格加第九列「排程時區｜launchd 台北網格 vs Python 機器時區｜連續 12 天假漏跑告警」＋表後一段（症狀、為什麼 12 天沒人抓到：告警文字看起來合理、每天都一樣；修法三層；判準：**時鐘也是基準**——兩個牆上時間要比，先問「哪個時區的牆」）。第九節排程時段段落改寫：明寫「台北時間」、launchd 每 15 分叫醒、`slot-check`、`_WINDOWS` 是唯一網格來源、`make schedule` 後 launchd 不再依賴系統時區。第五節漏跑視窗帳那段的「本地 naive」全改「台北 naive」。
- `docs/run_daily.md`：新增「排程時鐘」一節（三層＋部署步驟＋`slot-check` 的 exit code）；80-81 行「本機時間 naive」改「台北時間 naive」。
- `docs/dashboard.md`：漏跑 pill 的時間標「台北時間」。
- `web/static/index.html` `pollGap`：區間文字前加「台北 」。
- commit：`docs: 排程時鐘釘台北——第三節第九事故、第九節排程說明、run_daily 部署步驟`

---

## Task 5 @inline：wrapper 腳本防「執行中被改寫」（2026-10-21 事故，部署前做）

**事故：** Task 2 的 builder 在 00:36 CDT 原地改寫 `scripts/run_daily.sh`，當時 00:30 那輪正在跑（bash 卡在等 python）。bash 是**邊執行邊依位元組位移讀檔**的：python 回來後 bash 從舊位移 8752 繼續讀，新檔案在那裡是 `write_last_run_ledger` 函式中間 → 接著讀到「開始」echo 與 python 那行 → **同一輪又跑了一次 daily**（log 00:37:08 第二個「開始」），而第一輪沒有「結束」行、ledger 沒寫（`data/last_run_exit` 停在 10-20 22:34）。外顯是「多推一次、監督帳本漏一筆」，沒有任何錯誤訊息——靜默失敗。

**修法（結構性，不靠「記得別改」）：** 三支 wrapper 把 `set -uo pipefail` 之後的**全部內容**包進 `main() { … }`，檔案最後一行 `main "$@"; exit $?`。bash 必須讀完整個 `main` 的定義（到閉合大括號）才會開始執行，之後檔案怎麼改都與這次執行無關。注意：
- `trap 'rm -rf "$LOCK_DIR"' EXIT` 留在 `main` 內原位即可（trap 是行程層級，不受函式作用域影響）。
- `exit 0`／`exit 1`／`exit $STATUS` 在函式內照樣結束整個腳本，不用改成 `return`。
- 函式內的 `local` 不要亂加到原本的全域變數上（`NOTIFY_ATTEMPTED` 等要被 `write_last_run_ledger` 讀到）。
- `main "$@"` 必須是檔案**最後一個非空行**；測試用正規式釘住：`tests/test_run_daily_script.py` 加一條，對三支 wrapper 斷言（a）存在 `^main\(\) \{` 行，（b）最後一個非空行是 `main "$@"; exit $?`，（c）`set -uo pipefail` 之後、`main() {` 之前沒有任何非註解的可執行行（只允許註解與空行）。
- 既有的 run_daily 沙盒測試全部要照常綠（行為零變化）。

**附帶規則（寫進 CLAUDE.md 第五節，一行）：** 改 `scripts/*.sh` 之前先 `ls data/run_daily.lock data/run_high.lock`，有鎖就等；就算有 `main()` 保護，也不要在正式輪次跑到一半時部署 plist。

驗收：`bash -n` 三支；`.venv/bin/pytest tests/test_run_daily_script.py` 全綠（含新測試先紅後綠）；`make test` 全綠；commit：`fix(scripts): wrapper 全部包進 main()，杜絕執行中被改寫導致 bash 讀錯位移（2026-10-21 重跑事故）`。

## Task 4（主線程）：部署與驗收

- [ ] `make unschedule && make schedule && make unschedule-high && make schedule-high`（載入新 plist；這會動使用者的 launchd 服務，使用者已在 2026-10-20 要求修這個問題）。
- [ ] `launchctl kickstart -k gui/$(id -u)/com.jim.ygosniper.dashboard`（dashboard 行程吃到 clock.py）。
- [ ] 等下一個 :00／:15／:30／:45：`tail -3 data/logs/daily-$(TZ=Asia/Taipei date +%Y%m%d).log`——若台北時間不在格上，log **不該**多任何一行；若在格上，應出現「開始」行且時間戳是台北。`launchctl print gui/$(id -u)/com.jim.ygosniper | grep "last exit"` 應是 0。
- [ ] 第一輪真正掃描的 log 要有 `[schedule] 排程時鐘基準 None → Asia/Taipei，本輪不做漏跑偵測`，且**沒有**排程監督告警。
- [ ] `sqlite3 data/sniper.db "select key,value from meta where key like 'schedule_%'"`：`schedule_clock_tz = Asia/Taipei`，`schedule_run_started_at` 是台北時間。
- [ ] 隔天（2026-10-22 台北）回放：`grep -h "開始 =====" data/logs/daily-20261022.log` 的時刻應全部落在 09:30／11:30／…／22:30 ±7 分；`grep -c "排程監督" ` 應為 0（除非真的睡眠漏跑）。這是預註冊的驗收，寫進 memory 提醒下一個 session 看。
- [ ] 更新 memory `machine-timezone-vs-launchd-grid.md`：加「已修（日期、方案、驗收結果）」。
