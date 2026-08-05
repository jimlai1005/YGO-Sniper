"""附圖推播（`sendPhoto`）：讓使用者在手機上看到的是**卡片照片**，不是 Buyee logo。

2026-08-04 實測的根因（這是本檔存在的理由），兩層：
  1. 丟連結讓 Telegram 抓 og:image：Buyee 商品頁對它的 bot UA 回 HTTP 403
     → 抓不到 og:image → 退化成 Buyee 的品牌佔位圖（使用者看到的那張 logo）。
  2. 改成 `sendPhoto` 但只交出網址：**auc-pctr.c.yimg.jp（buyee_paypay／
     buyee_yahoo，我們最大宗的兩個站）回 400 `failed to get HTTP URL content`**，
     而 static.mercdn.net 與 i.ebayimg.com 可以——同一個網址我們自己 GET 是
     200 image/jpeg 15KB。擋的是 Telegram 的抓取端。
所以現行做法是**我們自己抓位元組、以 multipart 上傳**，四站實測全通。

這裡釘四件事：
  1. 有圖就走 `sendPhoto`，而且是上傳**位元組**（不把網址交給第三方去抓）。
  2. caption 只有 1024 字元（`sendMessage` 是 4096）：塞得下就一則解決，
     塞不下切在**行邊界**續送，**永不截斷**——截掉的那半通常正是到手成本與上限。
  3. 兩種退化都不可以讓整則通知消失：圖抓不到（我們這端）→ 純文字；
     Telegram 回 4xx → 純文字。但逾時／5xx **不退回**（不知道有沒有送達，
     退回會給使用者兩則）。
  4. 節流與 429 走的是同一個出口，`sendPhoto` 不另開一條路。

測試不碰網路：httpx.post／httpx.get 全部換成假的，`_sleep` 換成只記錄不真睡。
"""

from __future__ import annotations

import httpx
import pytest

from ygo_sniper import notify as notify_mod
from ygo_sniper.notify import (
    CAPTION_LIMIT,
    TelegramNotifier,
    photo_url_of,
    split_for_caption,
)

_ORIGINAL_SEND = TelegramNotifier.send
IMG = "https://auc-pctr.c.yimg.jp/i/auctions.c.yimg.jp/images.auction/x/y.jpg"
BLOB = b"\xff\xd8\xff\xe0jpeg-bytes"


class Cfg:
    telegram_token = "t"
    telegram_chat_id = "c"

    def __init__(self, **notify):
        self.notify = {"enabled": True, **notify}


def make_response(status: int, json_body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=json_body if json_body is not None else {"ok": status == 200},
        request=httpx.Request("POST", "https://api.telegram.org/bot/sendPhoto"),
    )


def make_image_response(
    status: int = 200, ctype: str = "image/jpeg", body: bytes = BLOB
) -> httpx.Response:
    return httpx.Response(
        status,
        content=body,
        headers={"content-type": ctype},
        request=httpx.Request("GET", IMG),
    )


@pytest.fixture
def rig(monkeypatch):
    """回傳 (build, calls, slept, responses)。

    calls 逐次記 `(method, payload)`；上傳圖片的那種會被攤平成
    `{**data, "photo": <bytes>}`，讓測試用同一種方式檢查兩條路徑。
    """
    calls: list[tuple[str, dict]] = []
    slept: list[float] = []
    responses: list[httpx.Response] = []

    def fake_post(url, json=None, data=None, files=None, timeout=None):
        if files is not None:
            payload = dict(data or {})
            payload["photo"] = files["photo"][1]
            payload["_filename"] = files["photo"][0]
        else:
            payload = json
        calls.append((url.rsplit("/", 1)[-1], payload))
        return responses.pop(0) if responses else make_response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: make_image_response()
    )
    monkeypatch.setattr(notify_mod, "_sleep", slept.append)

    def build(**notify_cfg):
        n = TelegramNotifier(Cfg(send_interval_seconds=0, **notify_cfg))
        n.send = _ORIGINAL_SEND.__get__(n, TelegramNotifier)  # conftest 靜音的還原
        return n

    return build, calls, slept, responses


# ---------------------------------------------------------------------------
# 1. 有圖走 sendPhoto，而且上傳的是我們自己抓下來的位元組
# ---------------------------------------------------------------------------
def test_photo_goes_through_sendphoto_as_uploaded_bytes(rig):
    build, calls, _slept, _ = rig
    n = build()
    assert n.send("到手 NT$1,234", photo=IMG) is True
    assert [m for m, _ in calls] == ["sendPhoto"], "有圖就不該走 sendMessage"
    payload = calls[0][1]
    assert payload["photo"] == BLOB, (
        "必須上傳位元組：交網址給 Telegram 去抓，auc-pctr.c.yimg.jp 會回 400"
    )
    assert payload["caption"] == "到手 NT$1,234"
    assert payload["parse_mode"] == "HTML"
    assert payload["chat_id"] == "c"


def test_image_is_fetched_by_us_from_the_stored_url(rig, monkeypatch):
    got: list[str] = []
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: (got.append(url), make_image_response())[1]
    )
    build, _calls, _slept, _ = rig
    build().send("x", photo=IMG)
    assert got == [IMG], "抓的是 signals.image_url 那個真圖網址"


def test_unfetchable_image_still_delivers_the_message(rig, monkeypatch):
    """我們這端就抓不到圖（404／逾時）→ 純文字照送，通知不可以消失。"""
    monkeypatch.setattr(httpx, "get", lambda url, **kw: make_image_response(404))
    build, calls, _slept, _ = rig
    n = build()
    assert n.send("到手 NT$1,234", photo=IMG) is True
    assert [m for m, _ in calls] == ["sendMessage"]
    assert calls[0][1]["text"] == "到手 NT$1,234"


def test_non_image_content_type_is_not_uploaded(rig, monkeypatch):
    """網址回的是一頁 HTML（賣場的錯誤頁）→ 當成沒有圖，不要上傳一坨 HTML。"""
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: make_image_response(ctype="text/html")
    )
    build, calls, _slept, _ = rig
    assert build().send("x", photo=IMG) is True
    assert [m for m, _ in calls] == ["sendMessage"]


def test_oversized_image_is_not_uploaded(rig, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: make_image_response(
            body=b"x" * (notify_mod.MAX_PHOTO_BYTES + 1)
        ),
    )
    build, calls, _slept, _ = rig
    assert build().send("x", photo=IMG) is True
    assert [m for m, _ in calls] == ["sendMessage"]


def test_fetch_photo_network_error_is_loud_not_silent(monkeypatch, capsys):
    def boom(url, **kw):
        raise httpx.ConnectError("reset")

    monkeypatch.setattr(httpx, "get", boom)
    assert notify_mod.fetch_photo(IMG) is None
    assert "圖片抓取失敗" in capsys.readouterr().out, "安靜降級＝沒有人知道為什麼沒圖"


def test_no_photo_keeps_the_plain_sendmessage_path(rig):
    """沒有圖的訊息（摘要、告警）行為完全不變，連結預覽照舊。"""
    build, calls, _slept, _ = rig
    n = build()
    assert n.send("純文字") is True
    assert calls[0][0] == "sendMessage"
    assert calls[0][1]["text"] == "純文字"
    assert calls[0][1]["disable_web_page_preview"] is False


def test_photo_url_of_only_accepts_http_urls():
    assert photo_url_of({"image_url": IMG}) == IMG
    assert photo_url_of({"image_url": "  " + IMG + " "}) == IMG
    assert photo_url_of({}) is None
    assert photo_url_of({"image_url": ""}) is None
    assert photo_url_of({"image_url": None}) is None
    # 協定相對網址送給 Telegram 只會換來一次 400，當成沒有圖比較快
    assert photo_url_of({"image_url": "//c.yimg.jp/a.jpg"}) is None


# ---------------------------------------------------------------------------
# 2. caption 長度：塞得下一則解決，塞不下切行邊界續送，永不截斷
# ---------------------------------------------------------------------------
def test_real_message_lengths_fit_in_one_caption(rig):
    """實測四條規則最長的一則是 856 字元（規則 3）——所以常態路徑是「一則」。"""
    build, calls, _slept, _ = rig
    n = build()
    text = "\n".join(f"第 {i} 行：到手 NT${i * 1000:,}" for i in range(20))
    assert len(text) < CAPTION_LIMIT
    assert n.send(text, photo=IMG) is True
    assert [m for m, _ in calls] == ["sendPhoto"], "塞得下就不該拆成兩則"


def test_overlong_message_is_split_at_line_boundary_and_nothing_is_lost(rig):
    build, calls, _slept, _ = rig
    n = build()
    lines = [f"第 {i} 行：到手 NT${i * 1000:,}｜公允價 NT${i * 1100:,}" for i in range(60)]
    text = "\n".join(lines)
    assert len(text) > CAPTION_LIMIT

    assert n.send(text, photo=IMG) is True
    assert [m for m, _ in calls] == ["sendPhoto", "sendMessage"]
    caption, rest = calls[0][1]["caption"], calls[1][1]["text"]
    assert len(caption) <= CAPTION_LIMIT
    assert caption + "\n" + rest == text, "切開之後不准少任何一個字"
    for chunk in (caption, rest):
        assert chunk.startswith("第 ") and "｜公允價" in chunk, "只准切在行邊界"
    assert calls[1][1]["disable_web_page_preview"] is True


def test_split_helper_keeps_short_text_whole():
    assert split_for_caption("abc") == ("abc", "")


def test_split_helper_never_truncates():
    text = "\n".join("x" * 100 for _ in range(30))
    head, rest = split_for_caption(text)
    assert len(head) <= CAPTION_LIMIT
    assert head + "\n" + rest == text


def test_split_helper_survives_a_single_overlong_line():
    """第一行就超過上限：圖照送（caption 空），全文走 sendMessage，不掉字。"""
    text = "y" * (CAPTION_LIMIT + 50)
    head, rest = split_for_caption(text)
    assert head == "" and rest == text


def test_text_over_the_sendmessage_limit_is_flagged_loudly(rig, capsys):
    """4096 以上 Telegram 一定回 400——日誌上不可以只有一個沒頭沒尾的 400。"""
    build, _calls, _slept, _ = rig
    build().send("q" * (notify_mod.TEXT_LIMIT + 1))
    assert "超過 sendMessage 上限" in capsys.readouterr().out


def test_partial_delivery_is_not_reported_as_success(rig):
    """圖送出了、餘文沒送出 → 回 False（不落已通知帳，下輪重送整則）。"""
    build, _calls, _slept, responses = rig
    responses.extend([make_response(200), make_response(500)])
    n = build()
    text = "\n".join("z" * 80 for _ in range(30))
    assert n.send(text, photo=IMG) is False


# ---------------------------------------------------------------------------
# 3. 退化：圖抓不到不可以讓整則通知消失
# ---------------------------------------------------------------------------
def test_broken_image_falls_back_to_plain_text(rig):
    """Telegram 回 400（wrong file identifier / failed to get HTTP URL content）
    ＝ 確定沒送達 → 改送純文字，內容一個字都不少。"""
    build, calls, _slept, responses = rig
    responses.extend([make_response(400, {"ok": False, "description": "wrong file"})])
    n = build()
    assert n.send("到手 NT$1,234", photo="https://example.test/dead.jpg") is True
    assert [m for m, _ in calls] == ["sendPhoto", "sendMessage"]
    assert calls[1][1]["text"] == "到手 NT$1,234", "退回純文字時全文照送"
    # 賣場連結的預覽正是那張 Buyee 佔位 logo，退化路徑上留著它只是再看一次 logo
    assert calls[1][1]["disable_web_page_preview"] is True


def test_transient_photo_failure_is_not_resent_as_text(rig):
    """5xx／逾時是「不知道有沒有送達」——退回純文字就可能給使用者兩則。"""
    build, calls, _slept, responses = rig
    responses.append(make_response(500))
    n = build()
    assert n.send("到手 NT$1,234", photo=IMG) is False
    assert [m for m, _ in calls] == ["sendPhoto"], "曖昧的失敗不准補送第二種形式"


def test_network_error_on_photo_is_not_resent_as_text(rig, monkeypatch):
    calls: list[str] = []

    def boom(url, json=None, data=None, files=None, timeout=None):
        calls.append(url.rsplit("/", 1)[-1])
        raise httpx.ConnectError("reset")

    monkeypatch.setattr(httpx, "post", boom)
    build, _calls, _slept, _ = rig
    n = build()
    assert n.send("x", photo=IMG) is False
    assert calls == ["sendPhoto"]


# ---------------------------------------------------------------------------
# 4. 節流與 429：sendPhoto 吃的是同一組限制（速率限制不分訊息種類）
# ---------------------------------------------------------------------------
def test_photo_sends_are_throttled_like_text(rig):
    build, calls, slept, _ = rig
    n = build()
    n.send_interval_seconds = 3.0
    n.send("a", photo=IMG)
    n.send("b", photo=IMG)
    n.send("c")  # 純文字也共用同一個預算
    assert [m for m, _ in calls] == ["sendPhoto", "sendPhoto", "sendMessage"]
    assert len(slept) == 2 and all(0 < w <= 3.0 for w in slept), slept


def test_photo_429_is_retried_once(rig):
    build, calls, slept, responses = rig
    responses.extend([
        make_response(429, {"ok": False, "parameters": {"retry_after": 5}}),
        make_response(200),
    ])
    n = build()
    assert n.send("hi", photo=IMG) is True
    assert [m for m, _ in calls] == ["sendPhoto", "sendPhoto"]
    assert 5.0 in slept


def test_photo_429_twice_is_not_downgraded_to_text(rig):
    """429 不是「圖片壞了」，是「太快了」——不可以誤判成退化條件而改送純文字。"""
    build, calls, _slept, responses = rig
    responses.extend([
        make_response(429, {"ok": False, "parameters": {"retry_after": 5}}),
        make_response(429, {"ok": False, "parameters": {"retry_after": 5}}),
    ])
    n = build()
    assert n.send("hi", photo=IMG) is False
    assert [m for m, _ in calls] == ["sendPhoto", "sendPhoto"]


def test_disabled_notifier_never_posts_a_photo(rig):
    build, calls, _slept, _ = rig
    n = build()
    n.config_enabled = False
    n.enabled = False
    assert n.send("hi", photo=IMG) is False
    assert calls == []


# ---------------------------------------------------------------------------
# 5. 四條規則都要帶圖（送出點）
# ---------------------------------------------------------------------------
def test_every_rule_sends_its_image(rig, monkeypatch):
    fetched: list[str] = []
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: (fetched.append(url), make_image_response())[1],
    )
    from ygo_sniper.notify_rules import (
        RULE_AUCTION_URGENT,
        RULE_HIGH_P,
        RULE_SELLER_NEW,
        RULE_SELLER_UNPRICED,
        Match,
        Outcome,
    )

    build, calls, _slept, _ = rig
    n = build()
    n.render = lambda m: f"msg {m.key}"
    out = Outcome()
    out.to_send = [
        Match(key=f"k{i}", rule=rule, row={"image_url": f"{IMG}?{i}"})
        for i, rule in enumerate(
            (RULE_AUCTION_URGENT, RULE_HIGH_P, RULE_SELLER_NEW, RULE_SELLER_UNPRICED)
        )
    ]
    sent = n.send_rule_matches(out)
    assert len(sent) == 4
    assert [m for m, _ in calls] == ["sendPhoto"] * 4, "四條規則都要帶圖"
    assert fetched == [f"{IMG}?{i}" for i in range(4)], "每則抓的是自己那張圖"
    assert [p["caption"] for _, p in calls] == [f"msg k{i}" for i in range(4)]


def test_match_without_image_still_gets_sent_as_text(rig):
    from ygo_sniper.notify_rules import RULE_HIGH_P, Match, Outcome

    build, calls, _slept, _ = rig
    n = build()
    n.render = lambda m: "msg"
    out = Outcome()
    out.to_send = [Match(key="k1", rule=RULE_HIGH_P, row={})]
    assert n.send_rule_matches(out) == [("k1", RULE_HIGH_P)]
    assert [m for m, _ in calls] == ["sendMessage"]


def test_failed_photo_send_is_not_recorded_as_notified(rig):
    """既有紅線：送失敗不落已通知帳。附圖路徑不准改變它。"""
    from ygo_sniper.notify_rules import RULE_SELLER_UNPRICED, Match, Outcome

    build, _calls, _slept, responses = rig
    responses.extend([make_response(200), make_response(500)])
    n = build()
    n.render = lambda m: "msg"
    out = Outcome()
    out.to_send = [
        Match(key="k1", rule=RULE_SELLER_UNPRICED, row={"image_url": IMG}),
        Match(key="k2", rule=RULE_SELLER_UNPRICED, row={"image_url": IMG}),
    ]
    assert n.send_rule_matches(out) == [("k1", RULE_SELLER_UNPRICED)]
