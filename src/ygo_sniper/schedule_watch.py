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
CLAUDE.md 第三節／工程原則 1 的same錯誤。後果：只要上一輪本身晚起
（哪怕只晚 1-15 分鐘，而 15 分正是本模組自己文件裡承認的正常喚醒漂移），
「expected」就會因為算法非單調而膨脹（例如 15:30→120 但 15:31→145），
吃掉緊接著被漏掉的那個時段——實測整個 18:00-22:30 晚間結標高峰，只要有
15 分鐘的正常漂移疊上任何一個被跳過的時段，就完全不出聲。

修法：不比較「經過了幾分鐘」，改成解出「上一輪之後，排程網格上絕對的
下一個時間點」（`next_slot_after`），拿它直接跟現在的絕對時鐘時間比。
兩邊都是絕對時間，drift 不會再汙染判準，也不會有非單調的問題。
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

#: 排程表（與 scripts/com.jim.ygosniper.plist 的 StartCalendarInterval 對齊；
#: 改 plist 時段時這裡要一起改——`tests/test_schedule_watch.py` 的
#: `test_windows_match_plist` 會在兩邊不同步時當場失敗，不是只能靠這行註解提醒）。
#: (窗起分鐘, 窗迄分鐘, 間隔分鐘)；22:30 之後到隔日 09:30 是刻意的夜間空窗。
_WINDOWS = [(9 * 60 + 30, 17 * 60 + 30, 120), (18 * 60, 22 * 60 + 30, 30)]

#: 把 _WINDOWS 展開成當天所有排程時間點（分鐘數，已排序去重）。
#: `next_slot_after` 的唯一資料來源——不再另外維護一份「間隔」邏輯。
_ALL_SLOTS: list[int] = sorted({m for lo, hi, step in _WINDOWS for m in range(lo, hi + 1, step)})

#: launchd 喚醒漂移的寬限（實測 ~15 分，見上）。
_SLACK_MINUTES = 20


def next_slot_after(prev: datetime) -> datetime:
    """`prev` 之後，排程網格上第一個絕對時間點（嚴格晚於 `prev`）。

    刻意回傳**絕對 datetime**而不是分鐘數或間隔——這是 Fix 1 的核心：
    無論 `prev` 本身漂移了幾分鐘，「下一個該接棒的時間點」永遠是網格上
    固定的那一格，不會因為 `prev` 晚起而跟著往後推、也不會非單調地亂跳。
    """
    today = prev.date()
    for m in _ALL_SLOTS:
        candidate = datetime(today.year, today.month, today.day, m // 60, m % 60)
        if candidate > prev:
            return candidate
    tomorrow = today + timedelta(days=1)
    first = _ALL_SLOTS[0]
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, first // 60, first % 60)


def expected_next_gap_minutes(prev: datetime) -> int:
    """`next_slot_after(prev) - prev` 的分鐘數——純粹是給人看／給舊測試相容用的
    衍生量，**不是**告警判準本身（告警判準見 `schedule_health` 內部直接用
    `next_slot_after` 與絕對時鐘比較，理由見模組開頭的 Fix 1 說明）。
    """
    delta = next_slot_after(prev) - prev
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
    prev_started: str | None, prev_finished: str | None, now: datetime
) -> str | None:
    """回傳要出聲的訊息，或 None（一切正常／沒有基準）。

    兩件事分開檢查、可以同時成立：
    1. 上一輪有開始沒收尾（當機／被 kill／watchdog 終止）。
    2. 現在的時鐘已經超過「上一輪之後的下一個排程時間點 + 寬限」——
       這是 Fix 1 改過的比較：`next_slot_after(prev)` 是絕對時間，
       跟 `now` 這個絕對時間直接比，drift 不會汙染判準。

    刻意對輸入寬容：解析失敗（壞掉的 ISO 字串、naive/aware 混用等）一律當作
    「沒有可用基準」處理而不是往外拋——這是一個純資訊性的偵測器，不是
    安全關鍵動作（不動資料、不下單），讓它的例外炸掉整個 scan()
    比「這一輪沒偵測到空窗」的代價高得多。

    時鐘被往回校正、或基準髒到跑到未來時，`now > due + slack` 天然為假
    → 不出聲。這是刻意的：一個「未來」的基準值本身就不可信，硬要對著它算
    空窗只會生出更沒意義的訊息；等下一輪基準被覆寫回正常值，偵測會自己
    恢復正常，不需要額外處理這個分支。
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
        due = next_slot_after(prev)
        late = now > due + timedelta(minutes=_SLACK_MINUTES)
    except TypeError:
        # naive/aware 混用等無法比較的狀況：資訊性偵測器寧可漏報也不能讓
        # 整輪 scan() 因為 TypeError 而中斷。
        due = None
        late = False

    if late and due is not None:
        gap_hours = (now - prev).total_seconds() / 3600
        msgs.append(
            f"排程空窗 {gap_hours:.1f} 小時（上輪 {prev:%m-%d %H:%M} 開始，"
            f"預期 {due:%m-%d %H:%M} 前接棒）——可能筆電睡眠漏跑或鎖被卡住"
        )

    return "🚨 排程監督：" + "；".join(msgs) if msgs else None
