"""verify_departed：清除前的商品頁實證分類。

背景（2026-08-07 事故）：`disappeared_at` 由觀測窗推論產生，一鍵清除 43 筆
的誤殺率是 100%。修法是**驗證取代推論**——清除當下真的打開商品頁，只有
拿到實證（已售出／連結失效）才准清。分類表是本模組的全部價值，所以逐格測：

- SOLD / DELISTED 才是離場實證（可清）
- STILL_LIVE 是 `disappeared_at` 的誤判（不清，還要修帳）
- UNVERIFIABLE（被擋／逾時／不支援的站）**絕不當成已離場**——
  讀不到 ≠ 賣光（全域工程原則事故 #4 的同構）

所有 IO 都以注入的 `fetch_page` 假件在**呼叫層**擋掉，測試不出網
（工程原則 4；conftest 的防外呼 fixture 只是最後一道兜底，不是依賴）。
key 一律用生產形狀 `site:external_id`（正式庫實測 `buyee_yahoo:n1238185137`）。
"""

import pytest

from ygo_sniper.appraise import ItemPage, Target
from ygo_sniper.domain import Currency
from ygo_sniper.sources.base import BlockedError, FetchError
from ygo_sniper.sources.ebay import EbayItemNotFound
from ygo_sniper.verify_departed import (
    CLEARABLE_VERDICTS,
    VerifyResult,
    _NoCacheGetter,
    build_page_verifier,
    verify_listing,
)

YAHOO_URL = "https://buyee.jp/item/yahoo/auction/n1238185137"
YAHOO_KEY = "buyee_yahoo:n1238185137"
MERCARI_TW_URL = (
    "https://tw.mercari.com/zh-hant/items/12345678-90ab-cdef-1234-567890abcdef"
)
MERCARI_TW_KEY = "mercari_tw:12345678-90ab-cdef-1234-567890abcdef"
EBAY_URL = "https://www.ebay.com/itm/407031244912"
EBAY_KEY = "ebay:407031244912"


def _page(**kw) -> ItemPage:
    base = dict(
        title="遊戯王 青眼の白龍 初期 PSA10",
        price=10000.0,
        currency=Currency.JPY,
        price_kind="fixed",
        price_note="",
    )
    base.update(kw)
    return ItemPage(**base)


class _Fetch:
    """記錄呼叫的 fetch_page 假件：回固定頁或拋指定例外。

    真正的生產 wrapper 是包住 `appraise.fetch_item_page` 的 callable；
    這裡只需要同一個介面形狀（吃 Target、回 ItemPage 或拋例外）。
    """

    def __init__(self, page: ItemPage | None = None, exc: Exception | None = None):
        self.page = page
        self.exc = exc
        self.calls: list[Target] = []

    def __call__(self, target: Target) -> ItemPage:
        self.calls.append(target)
        if self.exc is not None:
            raise self.exc
        assert self.page is not None
        return self.page


# ---------------------------------------------------------------------------
# 可清的兩格：SOLD 與 DELISTED
# ---------------------------------------------------------------------------
def test_sold_page_is_sold():
    fetch = _Fetch(page=_page(is_sold=True, status="closed"))
    res = verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)
    assert res.verdict == "SOLD"
    assert res.key == YAHOO_KEY
    assert res.clears is True
    assert len(fetch.calls) == 1


def test_404_is_delisted():
    fetch = _Fetch(
        exc=FetchError("HTTP 404", url=YAHOO_URL, status=404, transient=False)
    )
    res = verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)
    assert res.verdict == "DELISTED"
    assert res.clears is True


def test_ebay_not_found_is_delisted():
    fetch = _Fetch(exc=EbayItemNotFound("404: 商品不存在或已下架"))
    res = verify_listing(EBAY_KEY, EBAY_URL, fetch_page=fetch)
    assert res.verdict == "DELISTED"
    assert res.clears is True


# ---------------------------------------------------------------------------
# 不清的兩格：STILL_LIVE 與 UNVERIFIABLE
# ---------------------------------------------------------------------------
def test_live_page_is_still_live():
    fetch = _Fetch(page=_page(is_sold=False))
    res = verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)
    assert res.verdict == "STILL_LIVE"
    assert res.clears is False


def test_blocked_is_unverifiable():
    fetch = _Fetch(exc=BlockedError("WAF 驗證頁", url=YAHOO_URL, status=202))
    res = verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)
    assert res.verdict == "UNVERIFIABLE"
    assert res.clears is False


def test_transient_fetch_error_is_unverifiable():
    fetch = _Fetch(exc=FetchError("逾時", url=YAHOO_URL, transient=True))
    res = verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)
    assert res.verdict == "UNVERIFIABLE"


def test_non_404_semantic_fetch_error_is_unverifiable():
    """403／410 之類的語意失敗**不是**下架證據——只有 404 是連結失效。"""
    fetch = _Fetch(
        exc=FetchError("HTTP 403", url=YAHOO_URL, status=403, transient=False)
    )
    res = verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)
    assert res.verdict == "UNVERIFIABLE"


def test_unsupported_url_is_unverifiable_and_never_fetches():
    """不認得的網址：驗不了就說驗不了，而且**不准出網亂抓**。"""
    fetch = _Fetch(page=_page())
    res = verify_listing(
        "unknown:x", "https://example.com/item/123", fetch_page=fetch
    )
    assert res.verdict == "UNVERIFIABLE"
    assert fetch.calls == []


def test_mercari_tw_page_is_unverifiable_not_still_live():
    """Mercari 台灣頁沒有可靠的售出標記（appraise.py:552-555 明講**不判斷**，
    `is_sold` 恆 False）——頁面開得起來也回答不了「還買得到嗎」，
    所以是 UNVERIFIABLE，不是 STILL_LIVE。當成 STILL_LIVE 會把
    真的賣掉的標的判成誤殺、還去修 `disappeared_at` 的帳。"""
    fetch = _Fetch(page=_page(is_sold=False, currency=Currency.TWD))
    res = verify_listing(MERCARI_TW_KEY, MERCARI_TW_URL, fetch_page=fetch)
    assert res.verdict == "UNVERIFIABLE"
    assert res.clears is False


def test_mercari_tw_404_is_still_delisted():
    """連結失效與站別無關：Mercari 台灣的 404 一樣是下架實證。"""
    fetch = _Fetch(
        exc=FetchError("HTTP 404", url=MERCARI_TW_URL, status=404, transient=False)
    )
    res = verify_listing(MERCARI_TW_KEY, MERCARI_TW_URL, fetch_page=fetch)
    assert res.verdict == "DELISTED"


# ---------------------------------------------------------------------------
# 結構性強制：分類表寫死在 VerifyResult，不靠呼叫端記得
# ---------------------------------------------------------------------------
def test_clearable_verdicts_is_exactly_sold_and_delisted():
    assert set(CLEARABLE_VERDICTS) == {"SOLD", "DELISTED"}
    assert VerifyResult("k", "SOLD", "").clears is True
    assert VerifyResult("k", "DELISTED", "").clears is True
    assert VerifyResult("k", "STILL_LIVE", "").clears is False
    assert VerifyResult("k", "UNVERIFIABLE", "").clears is False


def test_invalid_verdict_is_rejected_at_construction():
    """打錯字的 verdict 要在**建構當下**爆，不是清除跑到一半才 KeyError。"""
    with pytest.raises(ValueError, match="verdict"):
        VerifyResult("k", "GONE", "")


def test_unexpected_exception_propagates():
    """分類表之外的例外不准吞成 UNVERIFIABLE——那是 bug，要大聲炸
    （CLAUDE.md 第五節：靜默失敗是頭號敵人）。"""
    fetch = _Fetch(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        verify_listing(YAHOO_KEY, YAHOO_URL, fetch_page=fetch)


# ---------------------------------------------------------------------------
# build_page_verifier：把 verify_listing 接上真實抓取管線的可重用接線。
# 單元測試只驗「組出來的 callable 會把例外分類對」與資源接線的形狀
# （注入假 fetch_page／mock fetch_item_page），**絕不出網、絕不開瀏覽器**。
# ---------------------------------------------------------------------------
BUYEE_MERCARI_URL = "https://buyee.jp/mercari/item/m93414631870"
BUYEE_MERCARI_KEY = "buyee_mercari:m93414631870"


def test_build_page_verifier_classifies_injected_exceptions(cfg):
    """注入假 fetch_page：組出來的 callable 是 (key, url) → VerifyResult，
    例外分類與 verify_listing 同一份（它就是應該直接委給 verify_listing）。"""
    with build_page_verifier(
        cfg, fetch_page=_Fetch(exc=BlockedError("WAF", url=YAHOO_URL, status=202))
    ) as verifier:
        res = verifier(YAHOO_KEY, YAHOO_URL)
    assert res.key == YAHOO_KEY
    assert res.verdict == "UNVERIFIABLE"


def test_build_page_verifier_classifies_injected_sold_page(cfg):
    with build_page_verifier(
        cfg, fetch_page=_Fetch(page=_page(is_sold=True, status="closed"))
    ) as verifier:
        res = verifier(YAHOO_KEY, YAHOO_URL)
    assert res.verdict == "SOLD"


def test_build_page_verifier_close_before_any_fetch_is_safe(cfg):
    """一筆都沒驗就 close：懶初始化的資源根本沒開，不准爆。"""
    build_page_verifier(cfg).close()


def test_no_cache_getter_pins_use_cache_false():
    """12 小時快取會回舊頁——驗證要看**現在**，use_cache=False 是釘死的，
    連呼叫端明說 use_cache=True 都要被否決。"""

    class _Inner:
        def __init__(self):
            self.calls = []
            self.closed = False

        def get(self, url, **kw):
            self.calls.append((url, kw))
            return "<html>ok</html>"

        def close(self):
            self.closed = True

    inner = _Inner()
    getter = _NoCacheGetter(inner)
    getter.get("https://x.test/a")
    getter.get("https://x.test/b", use_cache=True, min_bytes=10)
    assert inner.calls[0] == ("https://x.test/a", {"use_cache": False})
    assert inner.calls[1] == (
        "https://x.test/b", {"use_cache": False, "min_bytes": 10}
    )
    getter.close()
    assert inner.closed is True


def _wiring_probe(monkeypatch):
    """mock 掉 appraise.fetch_item_page，記錄接線收到的資源。"""
    import ygo_sniper.appraise as appraise_mod

    calls = []

    def fake_fetch_item_page(_cfg, target, *, fetcher=None, waf=None, ebay=None):
        calls.append({"mode": target.fetch_mode, "fetcher": fetcher,
                      "waf": waf, "ebay": ebay})
        return _page(is_sold=True, status="closed")

    monkeypatch.setattr(appraise_mod, "fetch_item_page", fake_fetch_item_page)
    return calls


def test_page_verifier_wiring_reuses_fetcher_and_skips_waf_for_yahoo(
    cfg, monkeypatch
):
    """yahoo_native 走一般 fetcher（no-cache 釘死），**不開** WafSession；
    第二筆重用同一個 fetcher（節流與連線池共用）。"""
    calls = _wiring_probe(monkeypatch)
    with build_page_verifier(cfg) as verifier:
        verifier(YAHOO_KEY, YAHOO_URL)
        verifier(YAHOO_KEY, YAHOO_URL)
    assert [c["mode"] for c in calls] == ["yahoo_native", "yahoo_native"]
    assert all(c["waf"] is None for c in calls)
    assert isinstance(calls[0]["fetcher"], _NoCacheGetter)
    assert calls[0]["fetcher"] is calls[1]["fetcher"]


def test_page_verifier_wiring_opens_waf_lazily_and_reuses_it(cfg, monkeypatch):
    """WafSession 只在遇到 buyee_waf 標的時建立（token TTL 只有約 5 分鐘，
    先開好再慢慢驗等於開一顆就過期），之後整批重用同一顆；
    而且它也要包 no-cache——驗證看的是現在的頁面。"""
    calls = _wiring_probe(monkeypatch)
    with build_page_verifier(cfg) as verifier:
        verifier(YAHOO_KEY, YAHOO_URL)                       # 不該觸發 waf
        assert calls[-1]["waf"] is None
        verifier(BUYEE_MERCARI_KEY, BUYEE_MERCARI_URL)
        verifier(BUYEE_MERCARI_KEY, BUYEE_MERCARI_URL)
    waf_calls = [c for c in calls if c["mode"] == "buyee_waf"]
    assert len(waf_calls) == 2
    assert all(isinstance(c["waf"], _NoCacheGetter) for c in waf_calls)
    assert waf_calls[0]["waf"] is waf_calls[1]["waf"]


def test_page_verifier_wiring_shares_one_ebay_source(cfg, monkeypatch):
    """eBay 走 API：共用同一顆 EbaySource（同一顆 OAuth token），不逐筆重建。"""
    calls = _wiring_probe(monkeypatch)
    with build_page_verifier(cfg) as verifier:
        verifier(EBAY_KEY, EBAY_URL)
        verifier(EBAY_KEY, EBAY_URL)
    assert [c["mode"] for c in calls] == ["ebay_api", "ebay_api"]
    assert calls[0]["ebay"] is not None
    assert calls[0]["ebay"] is calls[1]["ebay"]
