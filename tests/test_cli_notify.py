"""`daily` 的推播決策：沒好貨就靜默，但告警永遠照送。

改成每小時跑之後，一天有 24 輪，其中絕大多數必然沒有新貨。如果每輪都送一則
「掃完了、0 筆」，推播會被訓練成雜訊，真的撿到漏的那一次反而會被滑過去。
所以 `notify.silent_when_empty`（預設 true）在沒有新訊號時連摘要都不送。

**本檔最重要的一條是 `test_alerts_are_sent_even_when_silent`。**
「今天沒好貨」與「爬蟲瞎了」的外顯行為一模一樣（都是 0 筆），差別只在後者
需要你去修。如果為了避免洗版把告警一起靜音，就會退回這個專案整套健康判定
最初要解的那個病：連續三週以為市場很冷，其實 parser 早就掛了。
靜默只准吞前者，永遠不准吞後者。

這裡用假的 Pipeline（不碰網路、不碰資料庫、不碰 Telegram），只驗決策邏輯：
誰被呼叫、誰沒被呼叫。
"""

from __future__ import annotations

import pytest

from ygo_sniper.cli import _run_notifications
from ygo_sniper.notify import TelegramNotifier as _TN

#: 在 conftest 的 autouse fixture 把 send 換掉**之前**抓住原始函式（模組載入時機）。
#: 沒有這一手就沒辦法測「真正的 send 在停用時會不會擋下來」。
_ORIGINAL_SEND = _TN.send


def make_outcome(sent: int = 0, *, urgent: int = 0, high_p: int = 0):
    """一個 `notify_rules.Outcome`，只填 `_run_notifications` 會讀到的欄位。

    `sent` 是**真的送出去幾則**，與命中數是兩個數字：命中了卻沒送（去重、
    超量）時，CLI 必須兩個都講——不然使用者分不出「沒好貨」與「有好貨沒說」。
    """
    from ygo_sniper.notify_rules import RULE_AUCTION_URGENT, RULE_HIGH_P, Outcome

    out = Outcome()
    out.urgent = [object()] * (urgent or sent)
    out.high_p = [object()] * high_p
    out.sent = [(f"k{i}", RULE_AUCTION_URGENT if i % 2 else RULE_HIGH_P) for i in range(sent)]
    return out


class FakeNotifier:
    def __init__(self, alert_ok: bool = True):
        self.summaries: list[tuple] = []
        self.alerts: list[str] = []
        self.recoveries: list[str] = []
        self._alert_ok = alert_ok

    def send_summary(self, stats, scanned, signals, sources=None):
        self.summaries.append((stats, scanned, signals, sources))

    def send_alert(self, text) -> bool:
        self.alerts.append(str(text))
        return self._alert_ok

    def send_recovery(self, text) -> bool:
        self.recoveries.append(str(text))
        return self._alert_ok


class FakeStore:
    @staticmethod
    def stats() -> dict:
        return {"by_state": {}, "comps": 0}


class FakeAlertEngine:
    def __init__(self):
        self.marked: list = []

    def mark_sent(self, sent):
        self.marked.extend(sent)


class FakeCfg:
    def __init__(self, silent: bool | None = True, enabled: bool | None = None):
        self.notify = {"max_items_per_run": 15}
        if silent is not None:
            self.notify["silent_when_empty"] = silent
        if enabled is not None:
            self.notify["enabled"] = enabled


class FakePipeline:
    """只有 _run_notifications 會碰到的那幾個面。"""

    def __init__(self, *, notified: int = 0, silent: bool | None = True,
                 alert_ok: bool = True, enabled: bool | None = None,
                 pending: int = 0):
        self.cfg = FakeCfg(silent, enabled)
        self.notifier = FakeNotifier(alert_ok=alert_ok)
        self.store = FakeStore()
        self.alerts = FakeAlertEngine()
        self._notified = notified
        #: 停用時 `_report_notify_disabled` 會問「這一輪命中幾筆」
        self._pending = pending
        self.notify_calls = 0
        self.preview_calls = 0

    def notify(self):
        self.notify_calls += 1
        return make_outcome(self._notified)

    def notification_outcome(self):
        self.preview_calls += 1
        return make_outcome(0, urgent=self._pending)


def make_result(alerts=None) -> dict:
    return {
        "scanned": 120,
        "signals": 0,
        "sources": {"yahoo_direct": {"health": "ok", "count": 120, "detail": ""}},
        "alerts": alerts or [],
    }


# ---------------------------------------------------------------------------
# 1. 沒有新訊號 → 完全靜默
# ---------------------------------------------------------------------------
def test_silent_when_no_signals(capsys):
    pipe = FakePipeline(notified=0, silent=True)

    n = _run_notifications(pipe, make_result())

    assert n == 0
    # 沒有摘要 = 這一輪 Telegram 上一則訊息都不會出現
    assert pipe.notifier.summaries == []
    assert pipe.notifier.alerts == []
    assert pipe.notifier.recoveries == []


def test_summary_sent_when_signals_exist():
    pipe = FakePipeline(notified=3, silent=True)

    n = _run_notifications(pipe, make_result())

    assert n == 3
    # 摘要是好貨的附註：有好貨才附，且要帶各來源筆數讓使用者看得見管道健康
    assert len(pipe.notifier.summaries) == 1
    assert pipe.notifier.summaries[0][3] == {
        "yahoo_direct": {"health": "ok", "count": 120, "detail": ""}
    }


def test_silent_false_restores_always_on_summary():
    """開關要能關掉——不然「可調」只是嘴上說說。"""
    pipe = FakePipeline(notified=0, silent=False)

    _run_notifications(pipe, make_result())

    assert len(pipe.notifier.summaries) == 1


def test_silent_defaults_to_true_when_key_missing():
    """舊的 settings.yaml 沒有這個 key 時，要走新行為而不是炸掉。"""
    pipe = FakePipeline(notified=0, silent=None)

    _run_notifications(pipe, make_result())

    assert pipe.notifier.summaries == []


# ---------------------------------------------------------------------------
# 2. ⚠️ 本檔最重要的一條：靜默不准吞掉健康告警
# ---------------------------------------------------------------------------
def test_alerts_are_sent_even_when_silent():
    """沒有任何新訊號、靜默模式全開，來源告警仍然必須送出去。

    這條測試如果紅了，代表「爬蟲壞了」會跟「今天沒好貨」一起被靜音——
    那是這個專案最不能出的錯，不要靠放寬斷言讓它變綠。
    """
    pipe = FakePipeline(notified=0, silent=True)
    result = make_result(alerts=["🚨 yahoo_direct 疑似解析失效：頁面標示 142 件但解析出 0 筆"])

    n = _run_notifications(pipe, result)

    assert n == 0
    assert pipe.notifier.summaries == [], "靜默模式不該送摘要"
    # 告警照送，而且是完整內容
    assert len(pipe.notifier.alerts) == 1
    assert "yahoo_direct" in pipe.notifier.alerts[0]
    # 送成功才落冷卻帳
    assert pipe.alerts.marked == result["alerts"]


def test_recovery_alerts_also_sent_when_silent():
    """復原通知走的是另一個分支（send_recovery），一起釘住。"""
    class Recovered(str):
        recovery_source = "buyee_mercari"

    pipe = FakePipeline(notified=0, silent=True)
    result = make_result(alerts=[Recovered("✅ buyee_mercari 已恢復")])

    _run_notifications(pipe, result)

    assert pipe.notifier.recoveries == ["✅ buyee_mercari 已恢復"]
    assert pipe.notifier.alerts == []
    assert pipe.notifier.summaries == []


def test_failed_alert_is_not_marked_sent():
    """送失敗不落冷卻帳，下一輪要再吵一次（工程原則 3：不准靜默吞掉）。"""
    pipe = FakePipeline(notified=0, silent=True, alert_ok=False)
    result = make_result(alerts=["🚨 buyee_paypay 被擋"])

    _run_notifications(pipe, result)

    assert len(pipe.notifier.alerts) == 1
    assert pipe.alerts.marked == [], "送失敗卻記成已通知：這則告警就永遠消失了"


# ---------------------------------------------------------------------------
# 3. 靜默不影響訊號本身的推播路徑
# ---------------------------------------------------------------------------
def test_notify_always_attempted():
    """靜默是「沒有結果就別出聲」，不是「不要去看有沒有結果」。"""
    for silent in (True, False):
        pipe = FakePipeline(notified=0, silent=silent)
        _run_notifications(pipe, make_result())
        assert pipe.notify_calls == 1


@pytest.mark.parametrize("notified,expect_summary", [(0, 0), (1, 1), (15, 1)])
def test_summary_count_matches_signal_presence(notified, expect_summary):
    pipe = FakePipeline(notified=notified, silent=True)
    _run_notifications(pipe, make_result())
    assert len(pipe.notifier.summaries) == expect_summary


# ---------------------------------------------------------------------------
# 4. notify.enabled=false：整條推播管道關掉（與 silent_when_empty 是兩件事）
#
# silent 是「這一輪沒東西講」；enabled=false 是「我現在不想被 Telegram 吵」。
# 關掉時掃描、告警判定、落庫全部照跑，只是不送出——而且**要印出來說我沒送**，
# 靜默的停用跟壞掉長得一模一樣。
# ---------------------------------------------------------------------------
def test_disabled_sends_absolutely_nothing():
    """有訊號、有告警、silent 關掉——三個「本來一定會送」的條件全開，
    enabled=false 仍然一則都不送。"""
    pipe = FakePipeline(notified=5, silent=False, enabled=False, pending=5,
                        alert_ok=True)
    result = make_result(alerts=["🚨 yahoo_direct 疑似解析失效"])

    n = _run_notifications(pipe, result)

    assert n == 0
    assert pipe.notifier.summaries == []
    assert pipe.notifier.alerts == []
    assert pipe.notifier.recoveries == []
    assert pipe.notify_calls == 0, "連 notify() 都不該跑——跑了就會把訊號標成已通知"


def test_disabled_prints_explicit_message(capsys):
    """停用要出聲。使用者看不到任何訊息時，分不出『我關掉了』與『它壞了』。"""
    pipe = FakePipeline(notified=0, enabled=False, pending=7)
    _run_notifications(pipe, make_result(alerts=["🚨 buyee_paypay 被擋"]))

    out = capsys.readouterr().out
    assert "notify.enabled=false" in out
    assert "7" in out, "要講清楚有幾筆只落庫不推播"
    assert "告警" in out, "被壓下來的健康告警也要交代"


def test_disabled_does_not_burn_alert_cooldown():
    """告警不落冷卻帳 → 恢復推播的那一輪會補送，不會有斷層。"""
    pipe = FakePipeline(notified=0, enabled=False, pending=0)
    _run_notifications(pipe, make_result(alerts=["🚨 buyee_paypay 被擋"]))
    assert pipe.alerts.marked == []


def test_enabled_true_and_missing_key_keep_old_behaviour():
    """開關預設值：沒有這個 key 的舊 settings.yaml 要維持原本的行為。"""
    for enabled in (True, None):
        pipe = FakePipeline(notified=2, silent=True, enabled=enabled)
        n = _run_notifications(pipe, make_result())
        assert n == 2
        assert len(pipe.notifier.summaries) == 1


def test_notifier_send_is_blocked_at_the_boundary(monkeypatch):
    """CLI 之外還有一道：唯一真的打 HTTP 的 send() 自己也擋。

    推播的呼叫點有五個，靠每個呼叫點記得檢查開關，漏一個就會在使用者
    以為關掉的時候送出去（工程原則 5：擋在唯一的出口才是結構性的保證）。
    """
    import httpx

    from ygo_sniper.notify import TelegramNotifier

    class Cfg:
        notify = {"enabled": False}
        telegram_token = "t"      # 設定完整——所以擋下來的一定是開關，不是缺 token
        telegram_chat_id = "c"

    def _boom(*a, **k):
        raise AssertionError("notify.enabled=false 卻真的打了 Telegram API")

    monkeypatch.setattr(httpx, "post", _boom)
    n = TelegramNotifier(Cfg())
    assert n.configured is True, "token 是齊的——所以擋下來的一定是開關本身"
    assert n.config_enabled is False and n.enabled is False

    # conftest 的 autouse fixture 把 send 換成會爆的 stub，所以這裡叫的是
    # 模組載入時就抓住的**原始**函式：它必須自己回 False 且完全不碰 httpx。
    assert _ORIGINAL_SEND(n, "hi") is False
