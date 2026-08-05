"""Yahoo 拍賣賣家頁列舉（`YahooAuctionSource.search_seller`）。

這條管道 2026-08-04 才補上，在那之前監控名單上 6 個 `buyee_yahoo` 賣家
（含分數最高的 79.8／67.5／66.3 三個）**每一輪都被跳過**。

釘住的三件事，照危險程度排序：

1. **價格語意**與搜尋頁必須是同一份分流（`_classify_price`）。賣家頁走
   `__NEXT_DATA__`、搜尋頁走 CSS selector，抽欄位的方式完全不同，但
   「哪個價格是付得出去的」只能有一份答案——分流壞掉的症狀是大量假
   FREE_CARD，而每一筆看起來都像撿到寶。
2. **好評率的單位**。Yahoo 給的 `goodRatio` 是**比例**（0.978），PayPay 給的
   同名欄位是**百分比**（99.7），而 `sellers.feedback_pct` 只有一個欄位。
   不換算的話 Yahoo 賣家會全部被判成「好評率 0.98% → 扣 15 分」。
3. **三層健康判定**：查無在架與解析壞掉必須分得出來（賣家沒貨是常態——
   實測名單上 7 個 Yahoo 賣家有 3 個當下 0 件）。

fixture 是 2026-08-04 用**生產路徑**（`CachedFetcher`＋生產 UA、httpx）抓的
原始 HTML，不是 Playwright 存的（RECON.md 的教訓：Playwright 會把 lazyload
的 JS 跑完，存下來的 DOM 與生產環境拿到的不是同一份東西）。
全部請求走 httpx.MockTransport，零網路。
"""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ygo_sniper.bidding import LIVE_AUCTION_KIND, is_live_auction
from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.sources.health import ParseHealth
from ygo_sniper.sources.yahoo import YahooAuctionSource

FIXTURES = Path(__file__).parent / "fixtures"
#: 賣家 `53dyMh3X…`：5 筆全部有即決価格（且都同時有現在価格＋出價數）
SELLER_OK = (FIXTURES / "yahoo_seller_ok.html").read_text(encoding="utf-8")
#: 賣家 `8m1fe2VP…`（名單第一名，79.8 分）：6 筆全部是純競標（無即決）
SELLER_BID_ONLY = (FIXTURES / "yahoo_seller_bid_only.html").read_text(encoding="utf-8")
#: 賣家 `AfpCqXQp…`：`totalResultsAvailable: 0`，目前沒有在架商品
SELLER_EMPTY = (FIXTURES / "yahoo_seller_empty.html").read_text(encoding="utf-8")

SELLER_ID = "53dyMh3Xwd4Q1vD5ShkF6HM4pNEic"
BID_SELLER_ID = "8m1fe2VPnJdV8xkRDfgL2TymZEJbW"


@pytest.fixture
def make_source(cfg, tmp_path):
    created: list[CachedFetcher] = []

    def _make(handler, *, live: bool | None = None) -> YahooAuctionSource:
        sources = cfg.sources
        if live is not None:
            yd = {**sources.get("yahoo_direct", {}), "include_live_auctions": live}
            sources = {**sources, "yahoo_direct": yd}
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
            sources=sources,
        )
        fetcher = CachedFetcher(scoped)
        fetcher._client.close()
        fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
        created.append(fetcher)
        return YahooAuctionSource(scoped, fetcher)

    yield _make
    for f in created:
        f.close()


def serve(status: int, body: str):
    return lambda request: httpx.Response(status, text=body)


# ---------------------------------------------------------------------------
# 1. URL
# ---------------------------------------------------------------------------
def test_build_seller_url(make_source):
    """`/seller/{混淆ID}`；第 2 頁走 `b`（1-based 商品 offset）＋`n`。

    分頁參數是 2026-08-04 實測的：賣家 `9RdswzR6…`（89 件）第 1 頁 50 筆、
    `?b=51&n=50` 39 筆、兩頁交集 0 筆。這個站對未知參數是**靜默忽略**的，
    所以這些鍵值不准憑印象改。
    """
    src = make_source(serve(200, SELLER_OK))

    assert src.build_seller_url(SELLER_ID) == (
        f"https://auctions.yahoo.co.jp/seller/{SELLER_ID}"
    )
    p2 = src.build_seller_url(SELLER_ID, page=2)
    assert "b=51" in p2 and "n=50" in p2


def test_seller_id_is_url_quoted(make_source):
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=SELLER_EMPTY)

    make_source(handler).search_seller("a b/c")
    assert "a%20b%2Fc" in seen[0]


# ---------------------------------------------------------------------------
# 2. 價格語意（本檔最重要的斷言）
# ---------------------------------------------------------------------------
def test_buyout_wins_over_current_price(make_source):
    """有即決 → `price` 是即決価格、`price_kind="buyout"`，現在価格另存 raw。

    fixture 這 5 筆同時有現在価格與即決価格（例：現在 81,000／即決 298,000），
    正是最容易做錯的形狀：抓錯欄位會讓成本模型系統性偏低 3.7 倍。
    """
    result = make_source(serve(200, SELLER_OK)).search_seller(SELLER_ID)

    assert result.health is ParseHealth.OK
    assert len(result.listings) == 5
    assert {x.raw["price_kind"] for x in result.listings} == {"buyout"}

    first = next(x for x in result.listings if x.external_id == "e1239514318")
    assert first.price == 298000.0          # 即決価格
    assert first.raw["current_bid"] == 81000.0  # 現在価格只當參考，不當可成交價
    assert not is_live_auction(first)


def test_pure_auction_is_current_bid(make_source):
    """無即決 → `price_kind` 必須是 `bidding.LIVE_AUCTION_KIND`。

    這個字串是下游 `is_live_auction()` 的唯一依據：scoring 據以改用
    「出價上限 vs 目前出價」而不是「到手成本 < 鑑定費」。
    """
    result = make_source(serve(200, SELLER_BID_ONLY), live=True).search_seller(
        BID_SELLER_ID
    )

    assert len(result.listings) == 6
    assert {x.raw["price_kind"] for x in result.listings} == {LIVE_AUCTION_KIND}
    assert all(is_live_auction(x) for x in result.listings)
    # 競標是時間敏感的：結標時間與出價數都要抓到
    assert all(x.end_time is not None for x in result.listings)
    assert all(x.bids is not None and x.bids > 0 for x in result.listings)


def test_pure_auction_excluded_when_channel_closed(make_source):
    """關掉競標通道 → 純競標整批排除，但**仍然計入 parsed_count**。

    這是商業篩選不是解析失敗。混在一起的話，selector 過期時 parsed_count
    會被排除數撐住，健康判定就瞎了。
    """
    result = make_source(serve(200, SELLER_BID_ONLY), live=False).search_seller(
        BID_SELLER_ID
    )

    assert result.listings == []
    assert result.parsed_count == 6
    assert result.health is ParseHealth.OK
    assert "排除純競標 6 筆" in result.detail


def test_price_split_shares_one_implementation(make_source):
    """賣家頁與搜尋頁必須是同一份分流——這裡直接對 `_classify_price` 下手。"""
    src = make_source(serve(200, SELLER_OK))
    src.include_live_auctions = True
    assert src._classify_price(100.0, 900.0) == (900.0, "buyout")
    assert src._classify_price(100.0, None) == (100.0, LIVE_AUCTION_KIND)
    src.include_live_auctions = False
    assert src._classify_price(100.0, None)[0] is None


# ---------------------------------------------------------------------------
# 3. 購買路徑與去重鍵
# ---------------------------------------------------------------------------
def test_buy_url_is_buyee_and_origin_is_yahoo(make_source):
    """`url` 一律放買得到的那一端（Buyee），`origin_url` 放發現端。"""
    result = make_source(serve(200, SELLER_OK)).search_seller(SELLER_ID)
    lst = result.listings[0]

    assert lst.url == f"https://buyee.jp/item/yahoo/auction/{lst.external_id}"
    assert lst.origin_url == (
        f"https://auctions.yahoo.co.jp/jp/auction/{lst.external_id}"
    )
    assert lst.site.value == "buyee_yahoo"
    assert lst.source == "yahoo_direct"


def test_seller_id_is_on_every_listing(make_source):
    """賣家 ID 必須逐筆帶著——沒有它，賣家帳本聚合不到這一輪的觀測。"""
    result = make_source(serve(200, SELLER_OK)).search_seller(SELLER_ID)
    assert {x.seller_id for x in result.listings} == {SELLER_ID}


# ---------------------------------------------------------------------------
# 4. 好評率（Seller Alpha 的風險維度）
# ---------------------------------------------------------------------------
def test_feedback_ratio_is_converted_to_percent(make_source):
    """Yahoo 的 `goodRatio` 是比例（0.993），存進去必須是百分比（99.3）。

    PayPay 的同名欄位是百分比，而 `sellers.feedback_pct` 只有一個欄位、
    `seller_alpha` 的門檻寫的是 `< 95`／`< 98`。不換算就是每個 Yahoo 賣家
    都被判成「好評率 0.99%」——扣 15 分，而且完全沒有外顯症狀。
    """
    from ygo_sniper.venue_study import _seller_feedback

    result = make_source(serve(200, SELLER_OK)).search_seller(SELLER_ID)
    raw_seller = result.listings[0].raw["seller"]

    assert raw_seller == {"numRating": 448, "goodRatio": 99.3}
    # 走 venue_study 的共用抽取器（sellers 表真正吃到的那一份）
    assert _seller_feedback(result.listings[0].raw) == (448, 99.3)


def test_feedback_absent_is_none_not_guessed(make_source):
    """抽不到評價一律不寫——`risk_known=False` 比一個編出來的 100% 誠實。"""
    assert YahooAuctionSource.seller_feedback(None) is None
    assert YahooAuctionSource.seller_feedback({"rating": "壞掉了"}) is None
    assert YahooAuctionSource.seller_feedback({"rating": {}}) is None


# ---------------------------------------------------------------------------
# 5. 三層健康判定
# ---------------------------------------------------------------------------
def test_seller_with_no_stock_is_empty_confirmed(make_source):
    """`totalResultsAvailable: 0` ＝ 這個賣家現在沒貨，不是解析壞了。

    實測名單上 7 個 Yahoo 賣家有 3 個當下就是這個狀態——把它判成
    PARSER_BROKEN 會讓告警每小時叫一次然後被無視。
    """
    result = make_source(serve(200, SELLER_EMPTY)).search_seller("whoever")

    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert result.listings == []
    assert result.parsed_count == 0


def test_missing_next_data_is_parser_broken(make_source):
    broken = SELLER_OK.replace('id="__NEXT_DATA__"', 'id="__NEXT_DATA_X__"')
    result = make_source(serve(200, broken)).search_seller(SELLER_ID)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert "改版" in result.detail


def test_hits_crosscheck_catches_dead_path(make_source):
    """節點路徑改名 → 件數對得上但解析 0 筆，第 1 層交叉比對必須抓到。"""
    broken = SELLER_OK.replace('"listing":', '"listingX":')
    result = make_source(serve(200, broken)).search_seller(SELLER_ID)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert result.parsed_count == 0


def test_fetch_failure_on_first_page_is_not_empty(make_source):
    """第一頁抓不到 ＝ 對這個賣家「什麼都不知道」，跟「他沒貨」是兩件事。"""
    result = make_source(serve(500, "boom")).search_seller(SELLER_ID)

    assert result.health is ParseHealth.FETCH_FAILED
    assert result.listings == []


def test_404_page_still_reaches_the_parser(make_source):
    """Yahoo 查無結果回 404＋完整頁面（allow_statuses 的老陷阱）。"""
    result = make_source(serve(404, SELLER_EMPTY)).search_seller("whoever")
    assert result.health is ParseHealth.EMPTY_CONFIRMED


# ---------------------------------------------------------------------------
# 6. 已售出：這一站沒有
# ---------------------------------------------------------------------------
def test_sold_mode_refuses_loudly(make_source, capsys):
    """賣家頁沒有已售出清單。**靜默回在架 = 把開價當成交價寫進行情表。**"""
    result = make_source(serve(200, SELLER_OK)).search_seller(SELLER_ID, sold=True)

    assert result.listings == []
    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert "[warn]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 7. 分頁
# ---------------------------------------------------------------------------
def test_single_page_stops_when_not_full(make_source):
    """一頁不滿 50 筆 ＝ 已經是最後一頁，不再多打一個請求。"""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=SELLER_OK)

    result = make_source(handler).search_seller(SELLER_ID, pages=3)
    assert len(calls) == 1
    assert result.pages_fetched == 1
