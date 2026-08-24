"""CachedFetcher 的失敗分類測試。

這個檔案釘住的是一個曾經真的出過事的行為：站台被 WAF 擋下時回的是
2xx（202 + 挑戰頁），`raise_for_status()` 對 2xx 不吭聲，於是「被擋」
被靜默當成「今天沒有好貨」—— 你會連續好幾週以為市場很冷，其實爬蟲早就掛了。

所以下面每一條的重點都不是「函式會不會動」，而是
「失敗有沒有大聲拋出來，以及有沒有被分到正確的類別」。
transient 與 semantic 分錯的代價是不對稱的：前者少重試只是漏一次，
後者多重試會讓 IP 更快被封。

全部請求都走 httpx.MockTransport，這個檔案不會發出任何真實網路請求；
cache_dir 也一律導到 tmp_path，不碰專案的 data/cache。
"""

from dataclasses import replace

import httpx
import pytest

from ygo_sniper.sources.base import BlockedError, CachedFetcher, FetchError

URL = "https://example.test/search?q=blue-eyes"

#: 通過 MIN_BODY_BYTES 門檻的「正常頁面」
GOOD_HTML = "<html><body>" + "listing" * 100 + "</body></html>"


class FakeServer:
    """依序回傳預先排好的回應，並記錄實際被呼叫幾次。

    次數是這組測試的核心斷言 —— 「有沒有重試」沒有別的方法可以觀測，
    只能數實際送出去的請求。回應用完之後重複最後一個，
    方便表達「之後每次都一樣壞」。
    """

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self._responses[min(self.calls - 1, len(self._responses) - 1)]


def waf_challenge() -> httpx.Response:
    """真實觀測到的 Buyee 被擋樣態：202 + WAF header + 一頁有內容的挑戰頁。

    body 刻意做得夠長，這樣萬一 WAF header 那條判斷被拿掉，
    測試不會被 MIN_BODY_BYTES 那條路救回來而假性通過。
    """
    return httpx.Response(
        202,
        headers={"x-amzn-waf-action": "challenge"},
        text="<html><body>" + "js-challenge" * 100 + "</body></html>",
    )


@pytest.fixture
def make_fetcher(cfg, tmp_path):
    """造一個「網路被換掉、不會 sleep、快取寫在暫存目錄」的 fetcher。

    delay/backoff 歸零是必要的：照 config 的 2 秒退避，
    重試測試會真的睡好幾秒，慢到沒有人會想跑它。
    """
    created: list[CachedFetcher] = []

    def _make(server: FakeServer) -> CachedFetcher:
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
        )
        fetcher = CachedFetcher(scoped)
        # __init__ 建的是真 client，先關掉再換成 MockTransport，
        # 確保這個 fetcher 之後不可能連得到外面
        fetcher._client.close()
        fetcher._client = httpx.Client(transport=httpx.MockTransport(server))
        created.append(fetcher)
        return fetcher

    yield _make
    for f in created:
        f.close()


def test_waf_challenge_is_blocked_not_empty_result(make_fetcher):
    """202 + WAF header 必須是 BlockedError，不是「沒有結果」。

    這就是原本那個沉默失敗的現場。status 一起斷言，
    是為了確保未來沒有人把判斷改成「只看 4xx/5xx」。
    """
    server = FakeServer(waf_challenge())
    fetcher = make_fetcher(server)

    with pytest.raises(BlockedError) as exc:
        fetcher.get(URL)

    assert exc.value.status == 202
    assert exc.value.transient is False  # 重試解不了 JS 挑戰，只會加速被封
    assert server.calls == 1


def test_empty_2xx_body_is_blocked(make_fetcher):
    """200 但 body 空白 —— 必須拋，絕不可以回傳空字串。

    回空字串的話 parser 會乖乖 parse 出 0 筆，
    整條 pipeline 從頭到尾都不會有任何人察覺異常。
    """
    server = FakeServer(httpx.Response(200, text=""))
    fetcher = make_fetcher(server)

    with pytest.raises(BlockedError) as exc:
        fetcher.get(URL)

    assert exc.value.status == 200
    assert exc.value.transient is False


def test_transient_failure_retries_and_succeeds(make_fetcher):
    """5xx 是暫時性的，GET 又是冪等的，所以重試到成功是對的行為。"""
    server = FakeServer(
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, text=GOOD_HTML),
    )
    fetcher = make_fetcher(server)

    assert fetcher.get(URL) == GOOD_HTML
    assert server.calls == 3  # 兩次失敗 + 一次成功，少一次就代表沒真的重試


def test_semantic_failure_is_not_retried(make_fetcher):
    """404 重試三次不會變成 200，只會讓對方更早把你擋掉。

    calls == 1 是這條的重點；只斷言有拋例外的話，
    「多打了兩次沒必要的請求」這個退步會完全看不出來。
    """
    server = FakeServer(httpx.Response(404))
    fetcher = make_fetcher(server)

    with pytest.raises(FetchError) as exc:
        fetcher.get(URL)

    assert exc.value.status == 404
    assert exc.value.transient is False
    assert server.calls == 1


def test_retry_exhausted_raises_transient_error(make_fetcher):
    """重試用完之後拋出的錯要保留 transient=True。

    分類要一路傳到最外層，上層才能區分「站台掛了，等下次排程」
    與「被擋了，需要人介入」—— 這兩者的處置完全不同。
    """
    server = FakeServer(httpx.Response(500))
    fetcher = make_fetcher(server)

    with pytest.raises(FetchError) as exc:
        fetcher.get(URL)

    assert exc.value.transient is True
    assert server.calls == fetcher.max_attempts


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(waf_challenge(), id="waf_challenge"),
        pytest.param(httpx.Response(200, text=""), id="empty_body"),
    ],
)
def test_blocked_response_is_never_cached(make_fetcher, bad):
    """被擋的回應不准落地。

    挑戰頁一旦進了快取，接下來 12 小時每次讀到的都是它，
    一個瞬間的封鎖會變成半天的靜默失效。
    """
    fetcher = make_fetcher(FakeServer(bad))

    with pytest.raises(BlockedError):
        fetcher.get(URL)

    assert list(fetcher.cache_dir.glob("*.html")) == []


def test_success_is_cached_and_second_call_skips_network(make_fetcher):
    """確認過的成功回應才進快取，而且快取真的擋掉了第二次請求。

    快取失效等於開發時反覆敲人家伺服器，是被擋的主因之一。
    """
    server = FakeServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    first = fetcher.get(URL)
    assert len(list(fetcher.cache_dir.glob("*.html"))) == 1
    assert server.calls == 1

    second = fetcher.get(URL)
    assert second == first
    assert server.calls == 1  # 沒有增加 = 第二次完全沒碰網路


# ---------------------------------------------------------------------------
# [req] 逐請求 log —— 「被節流了嗎」的一手證據（見 CLAUDE.md 常用指令附近
# 對本任務的說明）。這裡只釘住「有沒有記」與「記幾次」，不對格式做脆弱的
# 全字串比對，免得未來調格式時測試變成噪音。
# ---------------------------------------------------------------------------


def _req_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("[req]")]


def test_successful_fetch_emits_one_req_line(make_fetcher, capsys):
    """成功打到網路要留下一行 [req]，帶 host 與耗時，之後才有東西可以拿來比對延遲。"""
    server = FakeServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    fetcher.get(URL)

    lines = _req_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "example.test" in lines[0]
    assert "ms" in lines[0]


def test_cache_hit_emits_no_req_line(make_fetcher, capsys):
    """快取命中不算一次網路請求——記進去的話，log 上的請求量會虛胖，
    「兩個請求間隔多久」這個問題也會被假的間隔污染。
    """
    server = FakeServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    fetcher.get(URL)
    capsys.readouterr()  # 清掉第一次真的出網那行

    fetcher.get(URL)  # 這次應該直接吃快取，完全不碰 server

    assert server.calls == 1
    assert _req_lines(capsys.readouterr().out) == []


def test_retried_transient_failure_logs_one_line_per_attempt(make_fetcher, capsys):
    """重試三次就該有三行 [req]——這是整個功能的存在意義：
    log 上看得到的請求數，必須等於真的打到對方伺服器幾次。
    """
    server = FakeServer(
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, text=GOOD_HTML),
    )
    fetcher = make_fetcher(server)

    fetcher.get(URL)

    lines = _req_lines(capsys.readouterr().out)
    assert len(lines) == 3
    assert server.calls == 3


def test_req_log_line_reflects_real_status_even_when_check_rejects(make_fetcher, capsys):
    """被 WAF 擋下的請求也是一次真的網路往返，狀態碼必須留在 log 裡——
    這條診斷的目的就是調查被擋，記錄不能在被擋的那一刻反而消失。
    """
    server = FakeServer(waf_challenge())
    fetcher = make_fetcher(server)

    with pytest.raises(BlockedError):
        fetcher.get(URL)

    lines = _req_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert "202" in lines[0]


def test_req_log_disabled_by_env_var(make_fetcher, capsys, monkeypatch):
    """YGO_REQ_LOG=0 要能整個關掉，給不想要這行雜訊的場合一個退路。"""
    monkeypatch.setenv("YGO_REQ_LOG", "0")
    server = FakeServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    fetcher.get(URL)

    assert _req_lines(capsys.readouterr().out) == []


#: authority 段有不成對的 `[`——`urlsplit()` 對這種寫法真的會拋
#: `ValueError: Invalid IPv6 URL`（已用純 stdlib 重現），但 httpx 自己的
#: URL 解析不介意，一路抓得到內容。這正是診斷旁路可能弄壞主路徑的縫隙：
#: `_log_request` 內部會炸，但抓取本身沒有理由跟著炸。
IPV6_BROKEN_URL = "https://exa[mple.test/search?q=blue-eyes"


def test_log_request_internal_failure_does_not_break_successful_fetch(make_fetcher):
    """`_log_request` 內部真的壞掉（urlsplit 對畸形 authority 拋 ValueError）
    時，抓取結果必須跟沒有這行 log 一模一樣——這是它的契約：純觀察，
    絕不影響呼叫端。
    """
    server = FakeServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    result = fetcher.get(IPV6_BROKEN_URL)

    assert result == GOOD_HTML
    assert server.calls == 1


def test_log_request_internal_failure_does_not_mask_transient_error(make_fetcher):
    """這是這次要補的漏洞本身：`except httpx.HTTPError` 分支裡準備好的
    `FetchError(transient=True)` 不能被 `_log_request` 內部炸出的
    `ValueError` 取代——取代的話重試迴圈會被一個未分類的例外打斷，
    上層再也分不出「可重試」與「log 函式壞了」。
    """
    server = FakeServer(httpx.Response(500))
    fetcher = make_fetcher(server)

    with pytest.raises(FetchError) as exc:
        fetcher.get(IPV6_BROKEN_URL)

    assert exc.value.transient is True
    assert server.calls == fetcher.max_attempts  # 證明重試迴圈真的跑完，沒被中途打斷


class HeaderCapturingServer:
    """只記下這次請求實際帶了哪些 header，不做 FakeServer 的多次排隊行為。"""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.headers: httpx.Headers | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers = request.headers
        return self._response


def test_get_passes_per_request_headers(make_fetcher):
    """get(headers=...) 要把 headers 傳給 httpx client 的這一次請求——
    PSA API 的 `Authorization: bearer <token>` 就是靠這條路送出去，
    絕不能改成 client 預設 header（那會把 token 送給所有主機）。
    """
    server = HeaderCapturingServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    fetcher.get(URL, use_cache=False, headers={"Authorization": "bearer T"})

    assert server.headers is not None
    assert server.headers["Authorization"] == "bearer T"


def test_get_without_headers_sends_no_authorization(make_fetcher):
    """沒給 headers 時不能無中生有塞出 Authorization——維持 client 預設就好。"""
    server = HeaderCapturingServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    fetcher.get(URL, use_cache=False)

    assert server.headers is not None
    assert "authorization" not in server.headers


def test_log_request_swallows_urlsplit_error_via_monkeypatch(make_fetcher, monkeypatch):
    """不依賴 IPv6 這個特定寫法繼續有效——直接讓 `urlsplit` 炸，
    確認 `_log_request` 的 try/except 包住的是整個函式本體，不是只有
    IPv6 這一種輸入形狀。
    """
    import ygo_sniper.sources.base as base_mod

    def _boom(_url: str):
        raise ValueError("模擬 urlsplit 內部炸掉")

    monkeypatch.setattr(base_mod, "urlsplit", _boom)
    server = FakeServer(httpx.Response(200, text=GOOD_HTML))
    fetcher = make_fetcher(server)

    assert fetcher.get(URL) == GOOD_HTML
