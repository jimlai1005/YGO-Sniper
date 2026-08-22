"""排程空窗偵測：「該跑的時段沒跑」要出聲。

漏跑的三種成因（機器睡眠、鎖被殘留行程卡住、launchd 掉單）外顯完全相同——
log 裡就是少幾行，與「今天沒好貨」無法區分。唯一可靠的偵測點是
**下一次成功跑起來的那一輪**：拿上一輪的開始時間對照排程表，超過預期就報。

邊緣觸發：每次偵測都會覆寫「上一輪開始時間」這個基準，所以同一個空窗只會被
新偵測到一次；至於「偵測到了但送不出去」，見下方 `resolve_alert` 的 pending
機制——那是另一條獨立的保險，不是這裡的邊緣觸發負責的事。

**2026-08-12 修正（Fix 1，事故背景）**：舊版拿「now - prev」（實際經過的時間，
基準是上一輪*真正*開始的時刻，帶著 launchd 喚醒漂移）去跟「expected」
（基準是理想排程網格的固定間隔）比大小——兩個數字來自不同的基準，是
CLAUDE.md 第三節／工程原則 1 的同一種錯誤。修法：改成解出「上一輪之後，
排程網格上絕對的下一個時間點」（`next_slot_after`），不再比較「經過了幾分鐘」。

**2026-08-12 修正（Fix B，事故背景）**：Fix 1 把比較基準換成了絕對時間點，
但仍然留了一個 `now - prev > 寬容值` 的判準（`_SLACK_MINUTES=20`）——這個
固定寬容值本身又是一次粗糙近似：實測 10 天真實 log，08-08 那天第一輪在
10:06 才起跑（比 09:30 晚 36 分，純粹是筆電晚開機，當天 15 個時段一個都
沒漏），因為 36 分鐘的「經過時間」把整個隔夜空窗（22:30→09:30，本來就是
刻意的夜間空窗）也算了進去，被報成「排程空窗 11.6 小時」——**假警報**；
另一次是 27 分鐘的正常漂移在鄰近時段又觸發一次重複告警。兩週跑下來假警報
與重複告警各發生一次，而告警訊息的使用者只有一個人，這正是 CLAUDE.md 第一節
「誤放看得見」的反面教材：假警報比沒有告警更快讓人不再看這個頻道。
修法：不再比較「經過了多久」，改成直接列舉「網格上有哪幾格落在上一輪
與這一輪之間、卻沒有任何一輪去代表它」（`missed_slots_between`）——
這一輪自己的存在就代表它所在的那一格，不論它自己漂移了幾分鐘；
漂移因此在結構上不會被誤判成漏跑，**不再需要任何寬容值**，
`_SLACK_MINUTES` 已整個刪除。

**2026-08-12 修正（Fix A，事故背景）**：2026-08-10 那次真正卡死 8.5 小時的
事故，卡點其實在 `pipe.close()`／直譯器結束（Playwright 行程洩漏），
不在 `pipe.scan()` 中途——`ygo-sniper daily` 早就把完整輸出都印完、
`RUN_FINISHED_KEY` 也已經正常寫下，下一輪的 `schedule_health` 因此完全看
不出異狀。唯一還留著證據的地方是 `run_with_timeout.py` 的 watchdog：卡死
超過門檻會被強制殺掉、exit=124，但原本那則失敗通知是 `curl -s ... >
/dev/null` 直接丟掉結果的——筆電剛醒、Wi-Fi 還沒穩時那顆 curl 送不出去，
整個事故就 100% 無聲。修法見 `watchdog_message`：`run_daily.sh` 把每一輪
的結束狀態與通知有沒有確認送達寫進 `data/last_run_exit`，這裡讀出來、
折進同一套 `resolve_alert` pending 帳本，跟排程空窗共用「只有送達才消耗」
的保障。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

#: store.set_meta / get_meta 用的鍵名。與 comps 節流帳、scan_status 分開放，
#: 免得三本帳的 key 撞名（工程原則 1 的變體：連鍵名都要各管各的）。
RUN_STARTED_KEY = "schedule_run_started_at"
RUN_FINISHED_KEY = "schedule_run_finished_at"
#: 「偵測到了但還沒確認送達」的告警。**只有推播真的成功才清**——
#: 這是本模組唯一會被呼叫端「清除」的鍵，其他兩把鍵永遠只被覆寫，不被清空。
PENDING_ALERT_KEY = "schedule_alert_pending"

#: 高價帶（`ygo-sniper daily-high`）自己的三把鍵——與上面低價帶的鍵**刻意
#: 不共用**（2026-08-22，高價帶排程 plan Task 6）：兩條掃描各自跑在自己的
#: launchd 網格上，若共用一把 RUN_STARTED_KEY，任一帶跑一輪就會推進另一帶
#: 的基準，讓另一帶的漏跑偵測失聰——這正是 CLAUDE.md 第三節第八事故
#: （排程空窗判準混源）的同一種錯誤，只是換了「哪兩條輪次共用一本帳」。
RUN_STARTED_KEY_HIGH = "schedule_run_started_at_high"
RUN_FINISHED_KEY_HIGH = "schedule_run_finished_at_high"
PENDING_ALERT_KEY_HIGH = "schedule_alert_pending_high"

#: 排程表（與 scripts/com.jim.ygosniper.plist 的 StartCalendarInterval 對齊；
#: 改 plist 時段時這裡要一起改——`tests/test_schedule_watch.py` 的
#: `test_windows_match_plist` 會在兩邊不同步時當場失敗，不是只能靠這行註解提醒）。
#: (窗起分鐘, 窗迄分鐘, 間隔分鐘)；22:30 之後到隔日 09:30 是刻意的夜間空窗。
_WINDOWS = [(9 * 60 + 30, 17 * 60 + 30, 120), (18 * 60, 22 * 60 + 30, 30)]

#: 高價帶自己的排程表（與 scripts/com.jim.ygosniper.high.plist 對齊；
#: `tests/test_schedule_watch.py::test_high_windows_match_high_plist` 釘死）。
#: 白天偶數整點 10:00/12:00/14:00/16:00、晚間 :15 的 18:15/20:15/22:15——
#: 與 `_WINDOWS` 的所有觸發分鐘零重疊（低價帶佔 :30 與 :00/:30，這裡只落
#: 在 :00 與 :15）。同一組 (起, 迄, 間隔) 表示法就能表達這個不規則時刻表，
#: 不需要另開一種「顯式時刻清單」的資料結構。**不影響 `_WINDOWS` 本身。**
_HIGH_WINDOWS = [(10 * 60, 16 * 60, 120), (18 * 60 + 15, 22 * 60 + 15, 120)]


def _expand_windows(windows: list[tuple[int, int, int]]) -> list[int]:
    """把 (起, 迄, 間隔) 的窗口清單展開成排序去重的分鐘數清單。

    低價帶與高價帶共用同一份展開邏輯——兩條網格的形狀不同，但「怎麼從
    窗口定義變成一串絕對時間點」必須是同一套規則，否則兩邊的排程監督
    互相比不出「誰跟誰同源」。
    """
    return sorted({m for lo, hi, step in windows for m in range(lo, hi + 1, step)})


#: 把 _WINDOWS 展開成當天所有排程時間點（分鐘數，已排序去重）。
#: `next_slot_after`／`_latest_slot_at_or_before` 的唯一資料來源
#: ——不再另外維護一份「間隔」邏輯。
_ALL_SLOTS: list[int] = _expand_windows(_WINDOWS)

#: 高價帶版本的 `_ALL_SLOTS`——`next_slot_after` 等函式的 `slots=` 引數
#: 傳這個，就能在同一套演算法上跑高價帶自己的網格。
_HIGH_ALL_SLOTS: list[int] = _expand_windows(_HIGH_WINDOWS)


def next_slot_after(prev: datetime, slots: list[int] | None = None) -> datetime:
    """`prev` 之後，排程網格上第一個絕對時間點（嚴格晚於 `prev`）。

    刻意回傳**絕對 datetime**而不是分鐘數或間隔——這是 Fix 1 的核心：
    無論 `prev` 本身漂移了幾分鐘，「下一個該接棒的時間點」永遠是網格上
    固定的那一格，不會因為 `prev` 晚起而跟著往後推、也不會非單調地亂跳。

    `slots`：省略時用低價帶的 `_ALL_SLOTS`（既有行為零變化）；高價帶呼叫端
    傳 `_HIGH_ALL_SLOTS`。兩本帳的網格永遠不會被意外混用，因為呼叫端
    （`schedule_health` 及其上游）自己決定要傳哪一份，不會互相 fallback。
    """
    if slots is None:
        slots = _ALL_SLOTS
    today = prev.date()
    for m in slots:
        candidate = datetime(today.year, today.month, today.day, m // 60, m % 60)
        if candidate > prev:
            return candidate
    tomorrow = today + timedelta(days=1)
    first = slots[0]
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, first // 60, first % 60)


def _latest_slot_at_or_before(now: datetime, slots: list[int] | None = None) -> datetime:
    """排程網格上最後一個 `<= now` 的時間點——`now` 這一輪自己代表的那一格。

    不論這一輪實際跑得多準時或多晚，它的存在本身就是「這一格有被執行到」
    的證明；`missed_slots_between` 靠這個值劃出「不算漏跑」的那一格。
    """
    if slots is None:
        slots = _ALL_SLOTS
    today = now.date()
    todays = [
        datetime(today.year, today.month, today.day, m // 60, m % 60) for m in slots
    ]
    at_or_before = [c for c in todays if c <= now]
    if at_or_before:
        return max(at_or_before)
    yesterday = today - timedelta(days=1)
    last = slots[-1]
    return datetime(yesterday.year, yesterday.month, yesterday.day, last // 60, last % 60)


def missed_slots_between(
    prev: datetime, now: datetime, slots: list[int] | None = None
) -> list[datetime]:
    """`prev` 之後、`now` 這一輪自己代表的那一格之前，被跳過的排程時間點。

    見模組開頭 Fix B 的事故背景：這取代了「經過分鐘數 vs 理想間隔 + 寬容值」
    的比較。兩邊都只用來定位網格上的絕對格子，drift 因此在結構上不會被
    誤判成漏跑，不需要任何 magic constant 去容忍它。

    保證終止：`next_slot_after` 每次呼叫都嚴格前進，`_latest_slot_at_or_before`
    是固定值，所以迴圈次數以「兩者之間隔了幾格」為上限——即使停機一整年，
    也只是幾千次迭代，不會失控。`prev > now`（時鐘被往回校正）時第一次
    迭代就會直接超過 attributed，回傳空list，維持舊版「未來基準值不出聲」
    的行為不變。

    `slots`：與 `next_slot_after` 同一個引數，省略時是低價帶網格。
    """
    attributed = _latest_slot_at_or_before(now, slots)
    result: list[datetime] = []
    cursor = prev
    while True:
        nxt = next_slot_after(cursor, slots)
        if nxt >= attributed:
            break
        result.append(nxt)
        cursor = nxt
    return result


def _format_missed(slots: list[datetime]) -> str:
    """漏跑時段列表變成一句話。超過 6 個就只報頭尾＋筆數，避免筆電放了
    一整週沒開機時，Telegram 訊息被幾十個時間戳灌爆。"""
    if len(slots) <= 6:
        listed = "、".join(f"{s:%H:%M}" for s in slots)
        return f"漏跑 {len(slots)} 個時段（{listed}）"
    return f"漏跑 {len(slots)} 個時段（{slots[0]:%m-%d %H:%M} ～ {slots[-1]:%m-%d %H:%M}）"


def expected_next_gap_minutes(prev: datetime, slots: list[int] | None = None) -> int:
    """`next_slot_after(prev) - prev` 的分鐘數。

    **`src/` 與 `web/` 裡沒有任何production呼叫端**——`schedule_health` 的
    告警判準已經改用 `missed_slots_between`（見 Fix B），這個函式純粹是
    給人（REPL／debug）與既有回歸測試用的衍生量，刻意留著沒刪：它是
    `next_slot_after` 最直覺的人類可讀投影，拿掉的話下次要手算「上一輪
    到下一格還有幾分鐘」得重新推導一次。
    """
    delta = next_slot_after(prev, slots) - prev
    return int(delta.total_seconds() // 60)


def _decode_pending(raw: str | None) -> dict | None:
    """安全解出 pending 帳目，格式不對／壞掉一律當「沒有 pending」。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and isinstance(data.get("text"), str)
        and isinstance(data.get("missed"), int)
    ):
        return data
    return None


def resolve_alert(pending_raw: str | None, detected: str | None) -> tuple[str | None, str]:
    """合併「上一輪還沒送達的告警」與「這一輪新算出來的告警」。

    回傳 `(這一輪要印出／嘗試送出的訊息, 要寫回 PENDING_ALERT_KEY 的新值)`。
    這個函式只負責合併，**不清帳**——清 pending 是「推播真的成功」才能做的事，
    那個判斷只有呼叫端（打完 Telegram API 之後）知道，見 CLAUDE.md 第五節
    「推論不得直接觸發會動資料的動作」的同一條精神：這裡只回傳結果。

    有界（Fix 2 的要求）：`missed` 只是一個計數器，`text` 永遠只留**最新**
    一句話——連續好幾天送不出去，存的字串也不會無限串接；遺失的是舊訊息的
    確切文字，不會遺失的是「有東西一直沒送達」這個事實（`missed` 計數）。
    """
    prev = _decode_pending(pending_raw)
    if detected is not None:
        missed = (prev["missed"] if prev else 0) + 1
        record = {"text": detected, "missed": missed}
    elif prev is not None:
        record = prev
    else:
        return None, ""

    text = record["text"]
    if record["missed"] > 1:
        text = f"{text}（外加先前 {record['missed'] - 1} 次未送達的同類告警，一併重送）"
    return text, json.dumps(record, ensure_ascii=False)


def schedule_health(
    prev_started: str | None,
    prev_finished: str | None,
    now: datetime,
    slots: list[int] | None = None,
) -> str | None:
    """回傳要出聲的訊息，或 None（一切正常／沒有基準）。

    兩件事分開檢查、可以同時成立：
    1. 上一輪有開始沒收尾（當機／被 kill／watchdog 終止）。
    2. `prev` 與這一輪之間，排程網格上有沒有格子被跳過（`missed_slots_between`，
       見模組開頭 Fix B 的事故背景——不再是「經過分鐘數 vs 寬容值」）。

    刻意對輸入寬容：解析失敗（壞掉的 ISO 字串、naive/aware 混用等）一律當作
    「沒有可用基準」處理而不是往外拋——這是一個純資訊性的偵測器，不是
    安全關鍵動作（不動資料、不下單），讓它的例外炸掉整個 scan()
    比「這一輪沒偵測到空窗」的代價高得多。

    時鐘被往回校正、或基準髒到跑到未來時，`missed_slots_between` 天然回傳
    空 list → 不出聲。這是刻意的：一個「未來」的基準值本身就不可信，
    硬要對著它算漏了幾格只會生出更沒意義的訊息；等下一輪基準被覆寫回
    正常值，偵測會自己恢復正常，不需要額外處理這個分支。

    `slots`：省略時是低價帶的 `_ALL_SLOTS`；高價帶呼叫端傳 `_HIGH_ALL_SLOTS`
    ——兩帶各自比對自己的網格，不共用基準也不共用網格（同源條款）。
    """
    if not prev_started:
        return None
    try:
        prev = datetime.fromisoformat(prev_started)
    except (TypeError, ValueError):
        return None

    msgs: list[str] = []

    finished_ok = False
    if prev_finished:
        try:
            finished_ok = datetime.fromisoformat(prev_finished) >= prev
        except (TypeError, ValueError):
            finished_ok = False
    if not finished_ok:
        msgs.append(
            f"上一輪（{prev:%m-%d %H:%M} 開始）沒有正常收尾——"
            "當機、被 kill 或 watchdog 終止，請看 data/logs/"
        )

    try:
        missed = missed_slots_between(prev, now, slots)
    except TypeError:
        # naive/aware 混用等無法比較的狀況：資訊性偵測器寧可漏報也不能讓
        # 整輪 scan() 因為 TypeError 而中斷。
        missed = []

    if missed:
        msgs.append(f"{_format_missed(missed)}——可能筆電睡眠漏跑或鎖被卡住")

    return "🚨 排程監督：" + "；".join(msgs) if msgs else None


def watchdog_message(ledger: dict | None) -> str | None:
    """把 `run_daily.sh` 寫的「上一輪結束帳本」（`data/last_run_exit`）翻成一句話。

    純函式：只吃一個已經解析好的 dict，不碰檔案系統——讀檔／刪檔是呼叫端
    （pipeline.py）的事，跟本模組其他函式的純／不純分工一致（見模組開頭）。

    見模組開頭 Fix A 的事故背景：只有「上一輪真的失敗（`exit != 0`），
    而且 `run_daily.sh` 自己那則失敗通知也沒確認送達」才出聲——已經成功
    送達的失敗通知不重複講一次，重複講就是 Fix B 想解決的那種雜訊
    （兩套告警管道疊在一起，比只有一套更容易被使用者關掉）。

    對輸入寬容：ledger 格式不對、缺欄位、`exit` 不是 int，一律當作
    「沒有可用資訊」回傳 None，不往外拋——理由與 `schedule_health` 相同。
    """
    if not isinstance(ledger, dict):
        return None
    exit_code = ledger.get("exit")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code == 0:
        return None
    delivered = (
        ledger.get("notify_attempted") is True
        and ledger.get("notify_http") == "200"
        and ledger.get("notify_curl_exit") == 0
    )
    if delivered:
        return None
    if exit_code == 124:
        return (
            "上一輪被 watchdog 強制終止（exit=124），"
            "run_daily.sh 的失敗通知本身也沒確認送達"
        )
    return (
        f"上一輪掃描失敗（exit={exit_code}），"
        "run_daily.sh 的失敗通知本身也沒確認送達"
    )
