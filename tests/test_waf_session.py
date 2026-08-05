"""WafSession 的 TTL 狀態機測試（零網路、零瀏覽器）。

這個檔案釘住的是一件很容易寫成「看起來會動、實際上一直重開瀏覽器」
或者更糟「token 過期了還一直裸奔」的東西：token 的生命週期。

兩個注入點讓整台狀態機可以在毫秒內測完：
- `session._clock`：換成假時鐘，token 年齡由測試指定，不用真的等 240 秒。
- `session._acquire`：換成計數器，**完全不啟動 Playwright**——
  這也是刻意的設計約束（PLAN Q4），沒裝 playwright 的機器要跑得動這個檔案。

斷言的重點一律是「開了幾次瀏覽器、送了幾次請求」，不是「有沒有回字串」：
重取次數是這裡唯一能觀測到的行為，多一次就是多 3-5 秒與一次被封的機會，
少一次就是拿過期 token 去撞牆。
"""

from __future__ import annotations

import sys
from dataclasses import replace

import httpx
import pytest

from ygo_sniper.sources.base import BlockedError
from ygo_sniper.sources.waf import MAX_REFRESHES_PER_RUN, TTL_BUDGET_SECONDS, WafSession

URL = "https://buyee.jp/mercari/search?keyword=test&lang=ja"
OTHER_URL = "https://buyee.jp/paypayfleamarket/search?keyword=test&lang=ja"

#: 過得了 MIN_BODY_BYTES 的正常頁面
GOOD_HTML = "<html><body>" + "item" * 300 + "</body></html>"
#: 短於 MIN_BODY_BYTES：_acquire 回這個時 seed html 不重用，
#: 測試因此能觀測到「重取之後真的還是走了 fetcher」這條路徑
SHORT_SEED = "<html>challenge</html>"


class FakeClock:
    """可手動推進的 monotonic 替身。"""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAcquire:
    """`_acquire` 的替身：只數次數，不開瀏覽器。"""

    def __init__(self, *, html: str = SHORT_SEED, error: BaseException | None = None) -> None:
        self.calls = 0
        self.html = html
        self.error = error

    def __call__(self, seed_url: str) -> tuple[str, str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return f"token-{self.calls}", self.html


class FakeServer:
    """依序回傳排好的回應並計數；用完之後重複最後一個。"""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def waf_challenge() -> httpx.Response:
    """Buyee 被擋的真實樣態：202 + WAF header + 有內容的挑戰頁。"""
    return httpx.Response(
        202,
        headers={"x-amzn-waf-action": "challenge"},
        text="<html><body>" + "js-challenge" * 100 + "</body></html>",
    )


def ok() -> httpx.Response:
    return httpx.Response(200, text=GOOD_HTML)


@pytest.fixture
def make_session(cfg, tmp_path):
    """造一個「網路換成 MockTransport、時鐘是假的、_acquire 是假的」的 session。"""
    created: list[WafSession] = []

    def _make(server: FakeServer, acquire: FakeAcquire) -> tuple[WafSession, FakeClock]:
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
        )
        session = WafSession(scoped)
        # 真 client 先關掉再換掉，確保這個 session 之後不可能連得到外面
        session.fetcher._client.close()
        session.fetcher._client = httpx.Client(transport=httpx.MockTransport(server))
        clock = FakeClock()
        session._clock = clock
        session._acquire = acquire  # type: ignore[method-assign]
        created.append(session)
        return session, clock

    yield _make
    for s in created:
        s.close()


# ---------------------------------------------------------------------------
# 0. lazy import：這個模組不准把 playwright 拉進來
# ---------------------------------------------------------------------------
def test_importing_waf_does_not_load_playwright():
    """沒裝 playwright 的機器 `import waf` 必須零成本成功（PLAN Q4 保證 1）。"""
    assert "playwright" not in sys.modules


# ---------------------------------------------------------------------------
# 1. token 還在預算內 → 不重取
# ---------------------------------------------------------------------------
def test_fresh_token_is_reused_without_refresh(make_session):
    acquire = FakeAcquire()
    server = FakeServer(ok())
    session, clock = make_session(server, acquire)

    session.get(URL, use_cache=False)
    assert acquire.calls == 1  # 第一次沒有 token，必然要取一次

    clock.advance(200)  # < TTL_BUDGET_SECONDS(240)
    session.get(URL, use_cache=False)

    assert acquire.calls == 1, "token 還在預算內卻又開了一次瀏覽器"
    assert server.calls == 2


# ---------------------------------------------------------------------------
# 2. 超過預算 → 事前重取（不等被擋才反應）
# ---------------------------------------------------------------------------
def test_expired_token_is_refreshed_before_request(make_session):
    acquire = FakeAcquire()
    server = FakeServer(ok())
    session, clock = make_session(server, acquire)

    session.get(URL, use_cache=False)
    clock.advance(TTL_BUDGET_SECONDS + 10)  # 250s：實測 282s 才失效，但預算就是預算
    session.get(URL, use_cache=False)

    assert acquire.calls == 2, "token 已超過 TTL 預算卻沒有事前重取"
    assert server.calls == 2  # 事前重取省掉的是「注定被擋的那一次請求」，不是正常請求


# ---------------------------------------------------------------------------
# 3. 自認新鮮卻被擋 → 反應式重取一次，而且只重試一次
# ---------------------------------------------------------------------------
def test_reactive_refresh_retries_exactly_once(make_session):
    acquire = FakeAcquire()
    server = FakeServer(waf_challenge())  # 之後每次都被擋
    session, _clock = make_session(server, acquire)

    with pytest.raises(BlockedError):
        session.get(URL, use_cache=False)

    # 1 次事前（沒 token）+ 1 次反應式；再擋就往上拋，不准迴圈
    assert acquire.calls == 2
    assert server.calls == 2, "反應式補救之後又多打了請求：這是無界重試"


def test_reactive_refresh_recovers_when_new_token_works(make_session):
    """對照組：換了 token 就通的話，呼叫端根本不該看到錯誤。"""
    acquire = FakeAcquire()
    server = FakeServer(waf_challenge(), ok())
    session, _clock = make_session(server, acquire)

    assert session.get(URL, use_cache=False) == GOOD_HTML
    assert acquire.calls == 2
    assert server.calls == 2


# ---------------------------------------------------------------------------
# 4. 重取次數上限 → 大聲拋 BlockedError（不是靜默回空）
# ---------------------------------------------------------------------------
def test_refresh_limit_raises_blocked_error(make_session):
    acquire = FakeAcquire()
    server = FakeServer(waf_challenge())
    session, _clock = make_session(server, acquire)

    # 每次呼叫吃掉重取次數：第 1 次吃 2 次（事前 + 反應式），之後每次吃 1 次
    for _ in range(3):
        with pytest.raises(BlockedError):
            session.get(URL, use_cache=False)
    assert acquire.calls == MAX_REFRESHES_PER_RUN

    with pytest.raises(BlockedError) as exc:
        session.get(URL, use_cache=False)

    assert str(MAX_REFRESHES_PER_RUN) in str(exc.value)
    assert "上限" in str(exc.value)
    # 上限之後不准再開瀏覽器——那只是燒時間並加速被封
    assert acquire.calls == MAX_REFRESHES_PER_RUN


# ---------------------------------------------------------------------------
# 5. 沒裝 playwright → BlockedError（不是 ImportError）
# ---------------------------------------------------------------------------
def test_missing_playwright_becomes_blocked_error_with_install_hint(make_session):
    """ImportError 沒有人接得住，會炸穿整條 pipeline 讓 Yahoo 也一起死。

    轉成 BlockedError，Buyee 這兩條管道才會乖乖被 alerts 標記成 blocked，
    而訊息裡必須有安裝指引——否則使用者只會看到「被擋」，去查 IP 與 UA。
    """
    acquire = FakeAcquire(error=ImportError("No module named 'playwright'"))
    server = FakeServer(ok())
    session, _clock = make_session(server, acquire)

    with pytest.raises(BlockedError) as exc:
        session.get(URL, use_cache=False)

    msg = str(exc.value)
    assert "playwright" in msg
    assert "browser" in msg and "install chromium" in msg  # 照著打就能修好
    assert server.calls == 0  # 連 token 都沒有，不該裸著去打網路


# ---------------------------------------------------------------------------
# 6. seed 頁 HTML 重用：瀏覽器已經渲染好第一頁，不再打一次
# ---------------------------------------------------------------------------
def test_seed_html_is_reused_once(make_session):
    acquire = FakeAcquire(html=GOOD_HTML)
    server = FakeServer(ok())
    session, clock = make_session(server, acquire)

    assert session.get(URL, use_cache=False) == GOOD_HTML
    assert server.calls == 0, "seed 頁已經在手上還去打網路，等於白開一次瀏覽器"

    # 只重用一次：同一顆 seed 不能無限期冒充後續請求的結果
    clock.advance(10)
    session.get(URL, use_cache=False)
    assert server.calls == 1


# ---------------------------------------------------------------------------
# 7. 快取命中時完全不碰 token（第二次跑掃描必須數秒完成、不開瀏覽器）
# ---------------------------------------------------------------------------
def test_cache_hit_never_opens_browser(make_session):
    acquire = FakeAcquire()
    server = FakeServer(ok())
    session, clock = make_session(server, acquire)

    session.get(URL)  # 這次會取 token 並寫快取
    assert acquire.calls == 1

    clock.advance(TTL_BUDGET_SECONDS + 100)  # token 早就過期了，但快取還新鮮
    assert session.get(URL) == GOOD_HTML

    assert acquire.calls == 1, "快取命中卻還是開了瀏覽器"
    assert server.calls == 1


# ---------------------------------------------------------------------------
# 8. 兩條 Buyee 管道共用一顆 token（RECON §4：一顆 token 解鎖全部路徑）
# ---------------------------------------------------------------------------
def test_one_token_serves_both_buyee_paths(make_session):
    acquire = FakeAcquire()
    server = FakeServer(ok())
    session, _clock = make_session(server, acquire)

    session.get(URL, use_cache=False)          # mercari
    session.get(OTHER_URL, use_cache=False)    # paypay

    assert acquire.calls == 1, "換一條 Buyee 路徑就重開瀏覽器：token 是通用的，不必重取"
