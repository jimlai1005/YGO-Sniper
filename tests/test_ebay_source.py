"""eBay Browse API 的健康判定（`search_detailed`）。

2026-08-02 之前 eBay 是唯一只有舊 `search()` 介面的管道：不參與 ParseHealth、
不跑 canary、**壞掉時安靜地回 0 筆**——而 0 筆和「今天美國沒好貨」外顯一模一樣。
這一組測試守的就是那個破口：每一種失敗都必須產出一個**說得出原因**的健康碼。

全部零網路：`httpx.Client.get/post` 一律被換掉，任何漏網的真實請求都會爆。
"""

from __future__ import annotations

import httpx
import pytest

from ygo_sniper.sources.ebay import EbaySource
from ygo_sniper.sources.health import ParseHealth

_ITEM = {
    "itemId": "v1|1234|0",
    "title": "Yugioh PSA 8 Blue-Eyes LOB-001 1st Edition",
    "itemWebUrl": "https://www.ebay.com/itm/1234",
    "price": {"value": "120.00", "currency": "USD"},
    "image": {"imageUrl": "https://i.ebayimg.com/x.jpg"},
    "seller": {"username": "carddealer"},
    "shippingOptions": [{"shippingCost": {"value": "12.50", "currency": "USD"}}],
    "shipToLocations": {"regionIncluded": [{"regionId": "WORLDWIDE"}]},
    "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
    "itemCreationDate": "2026-06-01T00:00:00.000Z",
}


@pytest.fixture
def source(cfg, monkeypatch):
    """憑證已設定的 eBay source，token 預先塞好（不打 OAuth）。"""
    src = EbaySource(cfg)
    src.enabled = True
    src._token = "fake-token"
    src._token_exp = 1e18
    monkeypatch.setattr(
        httpx.Client,
        "post",
        lambda *a, **kw: pytest.fail("測試不得真的打 eBay OAuth"),
    )
    return src


def _respond(monkeypatch, payload: dict | None = None, *, status: int = 200,
             text: str | None = None, exc: Exception | None = None):
    import json as _json

    def fake_get(self, url, **kw):
        if exc is not None:
            raise exc
        body = text if text is not None else _json.dumps(payload or {})
        return httpx.Response(status, text=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", fake_get)


# ---------------------------------------------------------------------------
# 健康判定的三種分支
# ---------------------------------------------------------------------------
def test_ok_run_reports_parsed_count_and_listings(source, monkeypatch):
    _respond(monkeypatch, {"total": 561, "itemSummaries": [_ITEM]})
    res = source.search_detailed("yugioh PSA LOB", pages=1)

    assert res.health is ParseHealth.OK
    assert res.parsed_count == 1
    assert len(res.listings) == 1
    assert res.listings[0].external_id == "v1|1234|0"
    assert res.listings[0].source == "ebay"
    assert res.source == "ebay" and res.site == "ebay"


def test_zero_items_with_positive_total_is_parser_broken(source, monkeypatch):
    """API 說有 561 筆、我們卻一筆都沒解出來 → 不可能是市場問題，是我們壞了。

    `total` 與 `itemSummaries` 出自**同一個回應**（同源同基準），這是這條
    管道唯一不需要依賴外部知識的判準。
    """
    _respond(monkeypatch, {"total": 561, "itemSummaries": []})
    res = source.search_detailed("yugioh PSA LOB", pages=1)

    assert res.health is ParseHealth.PARSER_BROKEN
    assert "561" in res.detail
    assert res.parsed_count == 0


def test_all_items_malformed_is_parser_broken_not_empty(source, monkeypatch):
    """欄位全變了（價格欄改名）＝解析壞掉，不是查無結果。"""
    broken = {**_ITEM, "price": {"amount": "120.00"}}
    _respond(monkeypatch, {"total": 3, "itemSummaries": [broken, broken, broken]})
    res = source.search_detailed("yugioh PSA LOB", pages=1)

    assert res.health is ParseHealth.PARSER_BROKEN
    assert "欄位不全 3 筆" in res.detail


def test_total_zero_is_empty_confirmed(source, monkeypatch):
    """API 自己說 total=0 → 真的沒貨，**不告警**。"""
    _respond(monkeypatch, {"total": 0, "itemSummaries": []})
    res = source.search_detailed("完全不存在的關鍵字", pages=1)

    assert res.health is ParseHealth.EMPTY_CONFIRMED
    assert res.parsed_count == 0


def test_missing_total_field_is_parser_broken(source, monkeypatch):
    """回應連 total 都沒有 = 格式已改版。沒有交叉比對的依據時**不准假設沒貨**。"""
    _respond(monkeypatch, {"itemSummaries": []})
    res = source.search_detailed("yugioh", pages=1)

    assert res.health is ParseHealth.PARSER_BROKEN
    assert "total" in res.detail


# ---------------------------------------------------------------------------
# 抓取層失敗：transient vs 語意（工程原則 2）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs",
    [
        {"exc": httpx.ConnectTimeout("boom")},
        {"status": 503, "text": "Service Unavailable"},
        {"status": 429, "text": "Too Many Requests"},
    ],
)
def test_transient_failures_are_fetch_failed_not_silent_zero(source, monkeypatch, kwargs):
    """這是本檔的核心：API 掛了要回 FETCH_FAILED，**不是安靜的 0 筆**。"""
    _respond(monkeypatch, **kwargs)
    res = source.search_detailed("yugioh PSA LOB", pages=1)

    assert res.health is ParseHealth.FETCH_FAILED
    assert res.listings == []
    assert res.detail  # 一定要說得出為什麼


def test_auth_failure_is_blocked(source, monkeypatch):
    """401/403 是語意失敗——重試一萬次也一樣，要人去換金鑰，所以升成 BLOCKED。"""
    _respond(monkeypatch, status=401, text='{"errors":[{"message":"Invalid token"}]}')
    res = source.search_detailed("yugioh PSA LOB", pages=1)

    assert res.health is ParseHealth.BLOCKED
    assert "401" in res.detail


def test_missing_credentials_is_blocked_not_empty(cfg, monkeypatch):
    """憑證沒設定 = 設定沒做完，要看得見。安靜跳過的話 watchlist 裡那兩條
    eBay 查詢會永遠沒有產出，而沒有任何人會發現。"""
    monkeypatch.setattr(
        httpx.Client, "get", lambda *a, **kw: pytest.fail("停用時不該打任何 API")
    )
    src = EbaySource(cfg)
    src.enabled = False
    res = src.search_detailed("yugioh", pages=1)

    assert res.health is ParseHealth.BLOCKED
    assert "EBAY_CLIENT_ID" in res.detail


# ---------------------------------------------------------------------------
# 介面契約與單位
# ---------------------------------------------------------------------------
def test_source_exposes_registry_contract(cfg):
    src = EbaySource(cfg)
    assert (src.name, src.site.value, src.supports_sold) == ("ebay", "ebay", False)
    assert hasattr(src, "search_detailed"), (
        "沒有 search_detailed 的來源不會參與 ParseHealth 判定，也不會跑 canary"
    )


def test_sold_search_returns_nothing(source, monkeypatch):
    """Browse API 沒有成交紀錄。`supports_sold=False`，這條只是防呆。"""
    monkeypatch.setattr(
        httpx.Client, "get", lambda *a, **kw: pytest.fail("sold 搜尋不該打 API")
    )
    assert source.search("yugioh", sold=True) == []


def test_price_ceiling_is_converted_from_jpy_to_usd(source, monkeypatch, cfg):
    """pipeline 給的上限是**日圓**，eBay 的過濾器吃美元。

    直接把日圓數字當美元用會把上限放大約 140 倍——過濾器形同不存在，
    而且沒有任何錯誤訊息（工程原則 1：被比較的兩個值必須同單位）。
    """
    seen: dict = {}

    def fake_get(self, url, **kw):
        seen.update(kw.get("params") or {})
        return httpx.Response(
            200, json={"total": 0, "itemSummaries": []}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    source.search_detailed("yugioh", max_price=14000, pages=1)

    expected = 14000 * float(cfg.fx["jpy_twd"]) / float(cfg.fx["usd_twd"])
    assert f"price:[..{expected:.2f}]" in seen["filter"]
    assert expected < 200, "換算後應該是幾十美元的量級，不是上萬"


# ---------------------------------------------------------------------------
# 價格語意（2026-08-03）——本檔的第二條紅線，與 test_yahoo_source.py 同一件事
#
# eBay 的**純競標標的在搜尋端 `price` 是 null**，價格在 `currentBidPrice`。
# 舊的 `_to_listing` 是「取不到 price['value'] 就回 None」，所以那一整批被當成
# 「欄位不全」**靜默丟掉**——外顯與「今天美國沒好貨」一模一樣。
# fixture 是實抓的 API 回應（tests/fixtures/ebay_api_items.json）。
# ---------------------------------------------------------------------------
import json  # noqa: E402
from pathlib import Path  # noqa: E402

from ygo_sniper.bidding import LIVE_AUCTION_KIND, is_live_auction  # noqa: E402

_FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "ebay_api_items.json").read_text(encoding="utf-8")
)
_AUCTION = _FIXTURES["summary_auction"]          # price 是 null，currentBidPrice 有值
_AUCTION_BIN = _FIXTURES["summary_auction_bin"]  # AUCTION+FIXED_PRICE：price 是 BIN 價


def _auction_source(cfg, monkeypatch, *, include: bool):
    src = EbaySource(cfg)
    src.enabled = True
    src.include_live_auctions = include
    src._token = "fake-token"
    src._token_exp = 1e18
    monkeypatch.setattr(
        httpx.Client, "post", lambda *a, **kw: pytest.fail("測試不得真的打 eBay OAuth")
    )
    return src


def test_pure_auction_summary_has_no_price_field(source):
    """前提事實：這是實抓的回應，`price` 真的不存在。判準壞了要在這裡先爆。"""
    assert _AUCTION.get("price") is None
    assert _AUCTION["currentBidPrice"]["value"] == "70.75"
    assert "AUCTION" in _AUCTION["buyingOptions"]


def test_auction_is_excluded_by_default_but_counted_not_silently_dropped(
    cfg, monkeypatch
):
    """預設不收競標，但它是**商業篩選**：parsed_count 要照算、detail 要說出來。

    混在一起的話，「今天上架的全是競標」會被誤報成「解析器壞了」，
    而真的解析壞掉時又會被排除數蓋過去。
    """
    src = _auction_source(cfg, monkeypatch, include=False)
    _respond(monkeypatch, {"total": 2, "itemSummaries": [_AUCTION, _ITEM]})
    res = src.search_detailed("yugioh", pages=1)

    assert res.health is ParseHealth.OK
    assert res.parsed_count == 2            # 解析器兩筆都認得
    assert len(res.listings) == 1           # 但競標那筆不進清單
    assert "排除純競標 1 筆" in res.detail
    assert "欄位不全" not in res.detail     # 它不是「壞掉」，是被規則擋下


def test_auction_is_kept_when_enabled_and_carries_bid_semantics(cfg, monkeypatch):
    """打開之後：價格用 currentBidPrice，並帶 price_kind／結標時間／出價數。"""
    src = _auction_source(cfg, monkeypatch, include=True)
    _respond(monkeypatch, {"total": 1, "itemSummaries": [_AUCTION]})
    res = src.search_detailed("yugioh", pages=1)

    assert res.parsed_count == 1 and len(res.listings) == 1
    lst = res.listings[0]
    assert lst.price == 70.75                       # currentBidPrice，不是 0、不是 None
    assert lst.currency.value == "TWD"
    assert lst.raw["price_kind"] == LIVE_AUCTION_KIND
    assert is_live_auction(lst) is True             # 下游分流的唯一判準
    assert lst.bids == 0
    assert lst.end_time is not None and lst.end_time.tzinfo is not None


def test_enabling_auctions_widens_the_api_filter(cfg, monkeypatch):
    """不放 AUCTION 進 filter 的話，只會撈到「剛好也開放議價」的競標。"""
    seen: dict = {}

    def fake_get(self, url, **kw):
        seen.update(kw.get("params") or {})
        return httpx.Response(
            200, json={"total": 0, "itemSummaries": []}, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    _auction_source(cfg, monkeypatch, include=False).search_detailed("yugioh", pages=1)
    assert "buyingOptions:{FIXED_PRICE|BEST_OFFER}" in seen["filter"]

    _auction_source(cfg, monkeypatch, include=True).search_detailed("yugioh", pages=1)
    assert "buyingOptions:{FIXED_PRICE|BEST_OFFER|AUCTION}" in seen["filter"]


def test_auction_with_buy_it_now_is_a_buyout_not_a_live_auction(cfg, monkeypatch):
    """實測釐清：「buyingOptions 含 AUCTION 卻有 price」＝**競標帶 BIN**。

    那個 `price` 是 Buy It Now 價（可立即成交），`currentBidPrice` 才是目前出價
    ——所以它是 buyout，預設設定下照樣進清單。
    """
    assert _AUCTION_BIN["buyingOptions"] == ["FIXED_PRICE", "AUCTION"]
    src = _auction_source(cfg, monkeypatch, include=False)
    _respond(monkeypatch, {"total": 1, "itemSummaries": [_AUCTION_BIN]})
    res = src.search_detailed("yugioh", pages=1)

    [lst] = res.listings
    assert lst.price == 32661.37                    # BIN 價
    assert lst.raw["price_kind"] == "buyout"
    assert lst.raw["current_bid"] == 12922.4        # 目前出價保留下來，但不算成本
    assert is_live_auction(lst) is False


def test_ship_to_locations_excluding_tw_is_not_shippable(source, monkeypatch):
    """`regionIncluded=[WORLDWIDE]` ＋ `regionExcluded=[TW]` 實測存在。

    只看 included 的話，一個明確排除台灣的賣家會被判成寄得到——
    然後成本模型會為一筆不可能發生的交易報價。
    """
    blocked = {
        **_ITEM,
        "shipToLocations": {
            "regionIncluded": [{"regionId": "WORLDWIDE"}],
            "regionExcluded": [{"regionId": "TW"}],
        },
    }
    _respond(monkeypatch, {"total": 1, "itemSummaries": [blocked]})
    [lst] = source.search_detailed("yugioh", pages=1).listings
    assert lst.ships_to_tw is False


def test_item_endpoint_url_shape(cfg):
    """單品端點吃的是 `v1|{id}|0`，不是網址上的純數字。"""
    from ygo_sniper.sources.ebay import item_api_url, item_id_v1

    assert item_id_v1("407031244912") == "v1|407031244912|0"
    assert item_id_v1("v1|407031244912|0") == "v1|407031244912|0"
    assert item_api_url("407031244912").endswith("/item/v1|407031244912|0")


def test_get_item_without_credentials_is_an_auth_error_not_a_request(cfg, monkeypatch):
    from ygo_sniper.sources.ebay import EbayAuthError

    monkeypatch.setattr(
        httpx.Client, "get", lambda *a, **kw: pytest.fail("停用時不該打任何 API")
    )
    src = EbaySource(cfg)
    src.enabled = False
    with pytest.raises(EbayAuthError):
        src.get_item("407031244912")


def test_get_item_404_is_semantic_not_transient(source, monkeypatch):
    """404 ＝ 商品號不對／已下架。重試沒有意義，要跟連線失敗分開。"""
    from ygo_sniper.sources.ebay import EbayItemNotFound

    _respond(monkeypatch, status=404, text='{"errors":[{"message":"not found"}]}')
    with pytest.raises(EbayItemNotFound):
        source.get_item("407031244912")


def test_get_item_returns_the_blob_with_tw_context_header(source, monkeypatch):
    """帶 contextualLocation=country=TW 才會拿到寄台灣的運費與台幣換算價。"""
    seen: dict = {}

    def fake_get(self, url, **kw):
        seen["url"] = str(url)
        seen["headers"] = kw.get("headers") or {}
        return httpx.Response(
            200, json=_FIXTURES["item_fixed"], request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    blob = source.get_item("407031244912")

    assert blob["itemId"] == "v1|407031244912|0"
    assert seen["headers"]["X-EBAY-C-ENDUSERCTX"] == "contextualLocation=country=TW"
    assert "item/v1" in seen["url"]
