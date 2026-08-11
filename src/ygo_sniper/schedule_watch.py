"""排程空窗偵測：「該跑的時段沒跑」要出聲。

漏跑的三種成因（機器睡眠、鎖被殘留行程卡住、launchd 掉單）外顯完全相同——
log 裡就是少幾行，與「今天沒好貨」無法區分。唯一可靠的偵測點是
**下一次成功跑起來的那一輪**：拿上一輪的開始時間對照排程表，超過預期就報。

邊緣觸發：報完立刻更新基準，同一個空窗只會被報一次，不需要 dedup。
預期間隔刻意抓寬（窗尾直接跳到下一個窗頭），寧可漏報小漂移，
不要每次 launchd 晚 15 分就叫——實測喚醒漂移 ~15 分（08-10 15:45:30）。

這裡只做**純函式**判斷、不碰 store、不碰網路——落基準、送 Telegram 是
呼叫端（pipeline.scan／cli._run_notifications）的事，理由見 CLAUDE.md 第五節
「推論不得直接觸發會動資料的動作」：這個模組只回傳一句話，動不動資料、
發不發送都是呼叫端明確決定的。
"""

from __future__ import annotations

from datetime import datetime

#: store.set_meta / get_meta 用的鍵名。與 comps 節流帳、scan_status 分開放，
#: 免得三本帳的 key 撞名（工程原則 1 的變體：連鍵名都要各管各的）。
RUN_STARTED_KEY = "schedule_run_started_at"
RUN_FINISHED_KEY = "schedule_run_finished_at"

#: 排程表（與 scripts/com.jim.ygosniper.plist 的 StartCalendarInterval 對齊；
#: 改 plist 時段時這裡要一起改——兩處不同步的症狀是空窗告警亂叫或不叫）。
#: (窗起分鐘, 窗迄分鐘, 間隔分鐘)；22:30 之後到隔日 09:30 是刻意的夜間空窗。
_WINDOWS = [(9 * 60 + 30, 17 * 60 + 30, 120), (18 * 60, 22 * 60 + 30, 30)]

#: launchd 喚醒漂移的寬限（實測 ~15 分，見上）。
_SLACK_MINUTES = 20


def expected_next_gap_minutes(prev: datetime) -> int:
    """上一輪在 prev 開始，下一輪「最晚」該在幾分鐘內開始（不含寬限）。"""
    m = prev.hour * 60 + prev.minute
    for lo, hi, step in _WINDOWS:
        if lo <= m < hi and m + step <= hi:
            return step
    starts = sorted(lo for lo, _hi, _step in _WINDOWS)
    for lo in starts:
        if m < lo:
            return lo - m
    return (24 * 60 - m) + starts[0]  # 跨夜到明天第一班


def gap_alert(
    prev_started: str | None, prev_finished: str | None, now: datetime
) -> str | None:
    """回傳要出聲的訊息，或 None（一切正常／沒有基準）。

    刻意對輸入寬容：解析失敗（壞掉的 ISO 字串、naive/aware 混用等）一律當作
    「沒有可用基準」處理而不是往外拋——這是一個純資訊性的偵測器，不是
    安全關鍵動作（不動資料、不下單），讓它的例外炸掉整個 scan()
    比「這一輪沒偵測到空窗」的代價高得多。

    時鐘被往回校正、或基準髒到跑到未來時，actual_min 會是負值，
    天然小於任何 threshold → 不出聲。這是刻意的：一個「未來」的基準值
    本身就不可信，硬要對著它算空窗只會生出更沒意義的訊息；等下一輪
    基準被覆寫回正常值，偵測會自己恢復正常，不需要額外處理這個分支。
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
        expected = expected_next_gap_minutes(prev)
        actual_min = (now - prev).total_seconds() / 60
    except TypeError:
        # naive/aware 混用等無法比較的狀況：資訊性偵測器寧可漏報也不能讓
        # 整輪 scan() 因為 TypeError 而中斷。
        expected = None
        actual_min = None

    if expected is not None and actual_min is not None and actual_min > expected + _SLACK_MINUTES:
        msgs.append(
            f"排程空窗 {actual_min / 60:.1f} 小時（上輪 {prev:%m-%d %H:%M}，"
            f"預期 {expected} 分內接棒）——可能筆電睡眠漏跑或鎖被卡住"
        )

    return "🚨 排程監督：" + "；".join(msgs) if msgs else None
