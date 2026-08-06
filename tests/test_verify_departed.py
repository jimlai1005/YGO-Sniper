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
