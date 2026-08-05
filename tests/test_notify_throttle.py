"""送出節流與 429：一輪送幾十則不能把自己送進速率限制。

`notify.max_items_per_run` 從 15 改成「不限」之後，一輪就可能連送數十則，
而 Telegram 的限制是 30 則/秒（全域）與同一個 chat 約 20 則/分鐘。沒有節流
的症狀不是「錯誤訊息」而是**沉默**：Telegram 回 429、那幾則永遠沒到，
使用者只看到「今天沒收到通知」。

這裡釘三件事：
  1. 節流擋在**唯一的出口** `send()`，所以五個呼叫點（訊號、摘要、告警、
     復原、test-telegram）一起受惠——速率限制是整個 bot 對整個 chat 的，
     不分訊息種類。
  2. 429 **可以**重送一次（Telegram 在處理前就退回 → 那一則確定沒送達），
     其他失敗一律不重送（sendMessage 不是冪等的，重送會給使用者兩則）。
  3. 送失敗**不落已通知帳**——這是本專案原有的紅線，節流不准改變它。

測試不碰網路：httpx.post 全部換成假的，`_sleep` 換成只記錄不真睡。
"""

from __future__ import annotations

import httpx
import pytest

from ygo_sniper import notify as notify_mod
from ygo_sniper.notify import TelegramNotifier

_ORIGINAL_SEND = TelegramNotifier.send


class Cfg:
    telegram_token = "t"
    telegram_chat_id = "c"

    def __init__(self, **notify):
        self.notify = {"enabled": True, **notify}


def make_response(status: int, json_body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=json_body if json_body is not None else {"ok": status == 200},
        request=httpx.Request("POST", "https://api.telegram.org/bot/sendMessage"),
    )


@pytest.fixture
def rig(monkeypatch):
    """回傳 (notifier, posts, slept)。posts 是每次真的打 API 的訊息文字。"""
    posts: list[str] = []
    slept: list[float] = []
    responses: list[httpx.Response] = []

    def fake_post(url, json=None, timeout=None):
        posts.append(json["text"])
        return responses.pop(0) if responses else make_response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(notify_mod, "_sleep", slept.append)

    def build(**notify_cfg):
        n = TelegramNotifier(Cfg(**notify_cfg))
        n.send = _ORIGINAL_SEND.__get__(n, TelegramNotifier)  # conftest 靜音的還原
        return n

    return build, posts, slept, responses


# ---------------------------------------------------------------------------
# 1. 節流
# ---------------------------------------------------------------------------
def test_first_message_never_waits(rig):
    build, posts, slept, _ = rig
    n = build(send_interval_seconds=3.0)
    assert n.send("hi") is True
    assert posts == ["hi"] and slept == []


def test_second_message_waits_the_interval(rig):
    """連送時每一則之間至少隔 send_interval_seconds。"""
    build, posts, slept, _ = rig
    n = build(send_interval_seconds=3.0)
    n.send("a")
    n.send("b")
    n.send("c")
    assert posts == ["a", "b", "c"]
    assert len(slept) == 2
    assert all(0 < w <= 3.0 for w in slept), slept


def test_interval_zero_disables_throttle(rig):
    build, _posts, slept, _ = rig
    n = build(send_interval_seconds=0)
    n.send("a")
    n.send("b")
    assert slept == []


def test_bad_interval_falls_back_loudly(rig, capsys):
    build, _posts, _slept, _ = rig
    n = build(send_interval_seconds="每三秒")
    assert n.send_interval_seconds == notify_mod.DEFAULT_SEND_INTERVAL_SECONDS
    assert "send_interval_seconds" in capsys.readouterr().out


def test_disabled_notifier_does_not_burn_the_throttle_budget(rig):
    """開關擋下來的訊息沒有打到 API，不該讓下一則多等。"""
    build, posts, slept, _ = rig
    n = build(send_interval_seconds=3.0)
    n.config_enabled = False
    n.enabled = False
    assert n.send("blocked") is False
    assert posts == [] and slept == []


# ---------------------------------------------------------------------------
# 2. 429
# ---------------------------------------------------------------------------
def test_429_is_retried_once_after_retry_after(rig):
    build, posts, slept, responses = rig
    responses.extend([
        make_response(429, {"ok": False, "parameters": {"retry_after": 5}}),
        make_response(200),
    ])
    n = build(send_interval_seconds=0)
    assert n.send("hi") is True
    assert posts == ["hi", "hi"], "429 之後要重送同一則"
    assert 5.0 in slept


def test_429_twice_gives_up_without_claiming_success(rig):
    build, _posts, _slept, responses = rig
    responses.extend([
        make_response(429, {"ok": False, "parameters": {"retry_after": 5}}),
        make_response(429, {"ok": False, "parameters": {"retry_after": 5}}),
    ])
    n = build(send_interval_seconds=0)
    assert n.send("hi") is False, "沒送成功就必須回 False（呼叫端才不會落已通知帳）"


def test_429_without_retry_after_is_not_retried(rig):
    build, posts, _slept, responses = rig
    responses.append(make_response(429, {"ok": False}))
    n = build(send_interval_seconds=0)
    assert n.send("hi") is False
    assert posts == ["hi"], "不知道要等多久就不要猜著重送"


def test_other_failures_are_never_retried(rig):
    """500／逾時**不重送**：sendMessage 不是冪等的，回應遺失時訊息可能已經送達。"""
    build, posts, _slept, responses = rig
    responses.append(make_response(500))
    n = build(send_interval_seconds=0)
    assert n.send("hi") is False
    assert posts == ["hi"]


# ---------------------------------------------------------------------------
# 3. 失敗不落帳（既有紅線，節流不准改變它）
# ---------------------------------------------------------------------------
def test_failed_sends_are_not_recorded_as_notified(rig):
    from ygo_sniper.notify_rules import RULE_HIGH_P, Match, Outcome

    build, _posts, _slept, responses = rig
    n = build(send_interval_seconds=0)
    n.render = lambda m: "msg"
    out = Outcome()
    out.to_send = [
        Match(key="k1", rule=RULE_HIGH_P, row={}),
        Match(key="k2", rule=RULE_HIGH_P, row={}),
    ]
    responses.extend([make_response(200), make_response(500)])
    sent = n.send_rule_matches(out)
    assert sent == [("k1", RULE_HIGH_P)], "送失敗的 k2 不得進已通知帳"
