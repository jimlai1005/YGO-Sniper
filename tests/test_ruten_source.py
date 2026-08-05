"""露天拍賣直抓的解析、健康判定、幣別語意與成本路徑。**零網路**：全部吃 fixture。

fixture 取得方式（RECON 的硬規則）：**用生產路徑抓的原始 JSON**
（`RutenSource` 自己的 `CachedFetcher`），不是瀏覽器渲染後的版本。
這條管道連 HTML 都不碰（是公開 JSON API），但規矩一樣要守。

| fixture | 內容 | HTTP |
|---|---|---|
| `ruten_search_ok.json` | 搜尋 API `遊戲王 PSA`＋`sort=new/dc`，100 個 id，`TotalRows` 數千 | 200 |
| `ruten_prod_ok.json` | 詳情 API，上面那 100 個 id 的商品欄位（含 6 筆 USD 標價） | 200 |
| `ruten_search_empty.json` | 亂數關鍵字，`TotalRows: 0`，**全長只有 49 bytes** | 200 |

兩份 ok fixture 是**配對**的（同一次抓取的同一批 id）：這條管道一頁要打兩個
請求，用不成對的資料測會漏掉兩段之間的錯位。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ygo_sniper.domain import Currency, Site
from ygo_sniper.sources.base import BlockedError, FetchError
from ygo_sniper.sources.health import ParseHealth
from ygo_sniper.sources.ruten import RutenSource

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_OK = (FIXTURES / "ruten_search_ok.json").read_text(encoding="utf-8")
PROD_OK = (FIXTURES / "ruten_prod_ok.json").read_text(encoding="utf-8")
SEARCH_EMPTY = (FIXTURES / "ruten_search_empty.json").read_text(encoding="utf-8")


class _StubFetcher:
    """依 URL 分流的 fetcher：搜尋 API 一份、詳情 API 一份。

    分流是必要的——這條管道一頁打兩個請求，用「不管什麼 URL 都回同一份」
    的 stub 會讓「詳情 API 壞了」這個分支測不出來。
    """

    def __init__(self, search: str | Exception, detail: str | Exception | None = None):
        self.search = search
        self.detail = detail if detail is not None else PROD_OK
        self.urls: list[str] = []
        self.min_bytes: list[int | None] = []

    def get(self, url: str, **kw):
        self.urls.append(url)
        self.min_bytes.append(kw.get("min_bytes"))
        payload = self.detail if "/prod/v2/" in url else self.search
        if isinstance(payload, Exception):
            raise payload
        return payload

    def close(self) -> None:
        pass


@pytest.fixture
def make_source(cfg):
    def _make(search=SEARCH_OK, detail=None) -> RutenSource:
        return RutenSource(cfg, _StubFetcher(search, detail))

    return _make


# ---------------------------------------------------------------------------
# 1. URL 組法（實測出來的形狀）
# ---------------------------------------------------------------------------
def test_url_shape_and_paging(make_source):
    """`offset` 是 1-based 商品 offset（第 2 頁 = 101），不是頁碼。"""
    src = make_source()
    assert src.build_url("遊戲王 PSA").startswith(
        "https://rtapi.ruten.com.tw/api/search/v3/index.php/core/prod?"
    )
    assert "offset=1&" in src.build_url("遊戲王", page=1) + "&"
    assert "offset=101" in src.build_url("遊戲王", page=2)
    assert "limit=100" in src.build_url("遊戲王")
    assert "type=direct" in src.build_url("遊戲王")


def test_max_price_is_never_sent_to_the_platform(make_source):
    """實測 `prc.now`／`priceRange`／`min_price` 三種寫法都被**靜默忽略**
    （TotalRows 與第 1 筆完全不變）。塞一個假裝有效的參數比不塞更危險：
    下游會以為結果已經篩過了。過濾一律在本地做。"""
    url = make_source().build_url("遊戲王", max_price=5000)
    for bogus in ("prc", "priceRange", "min_price", "max_price", "5000"):
        assert bogus not in url


def test_sort_newest_uses_the_value_the_api_accepts(cfg):
    """`sort=new/dc`。未知排序鍵實測回 HTTP 400（不是靜默忽略）。"""
    assert "sort=new%2Fdc" in RutenSource(cfg, _StubFetcher(SEARCH_OK)).build_url("x")
    src = RutenSource(cfg, _StubFetcher(SEARCH_OK))
    src.sort_newest = False
    assert "sort=rnk%2Fdc" in src.build_url("x")


def test_json_api_uses_a_lower_body_floor(make_source):
    """查無結果的合法回應只有 49 bytes；用 HTML 的 512 判它會誤診成「被擋」。

    所以每一次外呼都必須明講 `min_bytes`——這是呼叫點強制宣告，
    不是「記得要傳」（工程原則 5）。
    """
    src = make_source()
    src.search_detailed("遊戲王 PSA", pages=1)
    assert src.fetcher.min_bytes, "沒有任何外呼？"
    assert all(m is not None and m < 512 for m in src.fetcher.min_bytes)


# ---------------------------------------------------------------------------
# 2. 解析（兩段式：搜尋拿 id、詳情拿欄位）
# ---------------------------------------------------------------------------
def test_in_stock_search_parses_the_fixture(make_source):
    src = make_source()
    res = src.search_detailed("遊戲王 PSA", pages=1)

    assert res.health is ParseHealth.OK
    assert res.parsed_count == 100          # 詳情 API 認得的商品數
    assert res.listings
    assert res.pages_fetched == 1
    # 一頁 = 兩個請求（搜尋 + 詳情），而且詳情帶著搜尋回來的 id
    assert len(src.fetcher.urls) == 2
    assert "/prod/v2/" in src.fetcher.urls[1]

    for lst in res.listings:
        assert lst.site is Site.RUTEN
        assert lst.source == "ruten"
        assert lst.url == f"https://www.ruten.com.tw/item/show?{lst.external_id}"
        assert lst.external_id.isdigit()
        assert lst.price > 0
        assert lst.ships_to_tw is True       # 貨就在台灣
        assert lst.raw["price_kind"] == "fixed"
        assert lst.is_sold is False


def test_currency_comes_from_the_row_never_assumed(make_source):
    """實測 100 筆裡有 6 筆是海外賣家的 USD 標價。

    硬當台幣會把 US$79.8 算成 NT$79.8（低估 31 倍），而且方向是
    「看起來超便宜」——正是會讓人按下去買的方向。
    """
    res = make_source().search_detailed("遊戲王 PSA", pages=1)
    by_ccy = {}
    for lst in res.listings:
        by_ccy.setdefault(lst.currency, []).append(lst)

    assert Currency.TWD in by_ccy and Currency.USD in by_ccy, (
        "fixture 應該同時含 TWD 與 USD 標價；只剩一種的話這條防線就測不到了"
    )
    # USD 那批的原始數字量級明顯不同（幾十），台幣那批是幾百到幾千
    assert max(x.price for x in by_ccy[Currency.USD]) < 1000


def test_unknown_currency_is_malformed_not_guessed(make_source):
    """認不得的幣別整筆丟掉，**不計入 parsed_count**——猜幣別等於猜匯率。"""
    rows = json.loads(PROD_OK)
    for r in rows:
        r["Currency"] = "XYZ"
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1)

    assert res.listings == []
    # 一筆都解不出來 → 第 1 層交叉比對判定為解析器壞了（不是「沒貨」）
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "0 筆" in res.detail


def test_price_range_takes_the_low_end_and_keeps_the_high(make_source):
    """多規格商品的 `PriceRange` 是區間。取下緣＝「現在最少要付多少」，
    與其他平台「可立即成交價」同一個口徑；上緣留在 raw 讓下游看得見。"""
    rows = json.loads(PROD_OK)
    rows[0]["PriceRange"] = [3912, 4169]
    rows[0]["StockQty"] = 1
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1)

    hit = next(x for x in res.listings if x.external_id == str(rows[0]["ProdId"]))
    assert hit.price == 3912
    assert hit.raw["price_max"] == 4169
    # 單點價格不留 price_max（不然每一筆都多一個沒資訊的欄位）
    single = next(x for x in res.listings if "price_max" not in x.raw)
    assert single.price > 0


def test_post_time_is_taipei_time_converted_to_utc(make_source):
    rows = json.loads(PROD_OK)
    rows[0]["PostTime"] = "2023/08/12 01:31:13"
    rows[0]["StockQty"] = 1
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1)
    hit = next(x for x in res.listings if x.external_id == str(rows[0]["ProdId"]))
    assert hit.listed_at is not None
    assert hit.listed_at.isoformat() == "2023-08-11T17:31:13+00:00"   # 01:31 +08:00


def test_unparseable_post_time_is_none_never_now(make_source):
    """抽不到就是 None。塞 now() 會讓每一筆看起來都是剛上架的。"""
    rows = json.loads(PROD_OK)
    for r in rows:
        r["PostTime"] = "不是時間"
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1)
    assert res.listings
    assert all(x.listed_at is None for x in res.listings)


def test_max_price_filters_locally_in_the_listed_currency(make_source):
    res = make_source().search_detailed("遊戲王 PSA", pages=1, max_price=1000)
    assert res.listings
    assert all(x.price <= 1000 for x in res.listings)
    # 被價格擋掉的是**商業篩選**，解析器照樣認得它們
    assert res.parsed_count == 100
    assert "價格上限" in res.detail


# ---------------------------------------------------------------------------
# 3. 已售出模式（SoldQty，**沒有成交時間**）
# ---------------------------------------------------------------------------
def test_sold_search_keeps_only_items_that_actually_sold(make_source):
    rows = json.loads(PROD_OK)
    rows[0]["SoldQty"] = 3
    rows[1]["SoldQty"] = 0
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1, sold=True)

    ids = {x.external_id for x in res.listings}
    assert str(rows[0]["ProdId"]) in ids
    assert str(rows[1]["ProdId"]) not in ids
    hit = next(x for x in res.listings if x.external_id == str(rows[0]["ProdId"]))
    assert hit.is_sold is True
    assert hit.raw["price_kind"] == "sold_price"
    assert hit.raw["sold_qty"] == 3


def test_sold_listings_never_carry_a_fabricated_sold_at(make_source):
    """露天沒有成交時間欄位。**絕不放 `raw["sold_at"]`**——放了就等於憑空
    造一個成交日期，而 comps 的 90 天視窗會照單全收。沒有時間就讓
    `comps.ingest_sold` 標 `sold_at_is_ingest=1`，讓假時間看得見。"""
    rows = json.loads(PROD_OK)
    for r in rows:
        r["SoldQty"] = 2
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1, sold=True)

    assert res.listings
    assert all("sold_at" not in x.raw for x in res.listings)
    assert all("PostTime" not in str(x.raw.get("sold_at", "")) for x in res.listings)


def test_in_stock_mode_drops_out_of_stock_rows(make_source):
    rows = json.loads(PROD_OK)
    for r in rows:
        r["StockQty"] = 0
    res = make_source(detail=json.dumps(rows)).search_detailed("x", pages=1)
    assert res.listings == []
    assert res.parsed_count == 100          # 解析器活著，只是沒貨可買
    assert res.health is ParseHealth.OK


# ---------------------------------------------------------------------------
# 4. 健康判定三分支
# ---------------------------------------------------------------------------
def test_zero_total_rows_is_empty_confirmed(make_source):
    """49 bytes 的合法回應。這條測試守的是「確認沒貨」不會被誤診成「被擋」。"""
    assert len(SEARCH_EMPTY) < 512
    src = make_source(search=SEARCH_EMPTY)
    res = src.search_detailed("亂數", pages=1)
    assert res.health is ParseHealth.EMPTY_CONFIRMED
    assert res.listings == []
    # 沒有 id 就不該再打詳情 API（多打一個請求，換不到任何資訊）
    assert len(src.fetcher.urls) == 1
    assert not any("/prod/v2/" in u for u in src.fetcher.urls)


def test_hits_without_ids_is_parser_broken(make_source):
    """API 說有 8392 件、我們解出 0 個 id → 不可能是市場問題，必定是格式改了。"""
    res = make_source(search='{"TotalRows": 8392, "Rows": []}').search_detailed("x", pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "8392" in res.detail


def test_non_json_response_is_parser_broken(make_source):
    res = make_source(search="<html>我們改版了</html>").search_detailed("x", pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "JSON" in res.detail


def test_detail_api_returning_nothing_is_parser_broken(make_source):
    """搜尋給了 100 個 id、詳情一筆也解不出來 → 詳情 API 改版（同源同基準）。

    這一條是兩段式管道特有的破口：只看搜尋 API 的話這種壞法完全隱形，
    外顯就是「台灣今天沒貨」。
    """
    res = make_source(detail="[]").search_detailed("遊戲王", pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "詳情" in res.detail and "100" in res.detail


def test_blocked_and_fetch_failed_are_separate(make_source):
    blocked = make_source(search=BlockedError("WAF", url="u")).search_detailed("x", pages=1)
    assert blocked.health is ParseHealth.BLOCKED

    failed = make_source(
        search=FetchError("timeout", url="u", transient=True)
    ).search_detailed("x", pages=1)
    assert failed.health is ParseHealth.FETCH_FAILED


def test_detail_fetch_failure_is_not_reported_as_empty(make_source):
    """第二段請求掛掉 = 「不知道」，不是「沒貨」。"""
    res = make_source(
        detail=FetchError("timeout", url="u", transient=True)
    ).search_detailed("x", pages=1)
    assert res.health is ParseHealth.FETCH_FAILED
    assert res.listings == []


def test_search_wrapper_raises_on_fetch_layer_failure(make_source):
    """comps 靠例外印警告；靜默回空清單的話，被擋三週你只會看到三週的「沒行情」。"""
    with pytest.raises(BlockedError):
        make_source(search=BlockedError("WAF", url="u")).search("x")
    with pytest.raises(FetchError):
        make_source(search=FetchError("t", url="u", transient=True)).search("x")
    # EMPTY_CONFIRMED 不是失敗，不可以拋
    assert make_source(search=SEARCH_EMPTY).search("x") == []


# ---------------------------------------------------------------------------
# 5. 幣別語意：台幣標價絕不可以再套一次匯率（Mercari 台灣那條教訓的同型防線）
# ---------------------------------------------------------------------------
def test_twd_listing_is_not_converted_again(cfg, fx):
    """到手成本 = 台幣標價 + route 費用，**不再乘一次匯率、不加海外刷卡加成**。

    把 NT$5,751 當日圓的話 item_twd 會掉到 1,200 上下（低估 4.7 倍），
    而且方向是「看起來很便宜」。
    """
    from ygo_sniper.costs import quote_all_routes
    from ygo_sniper.domain import Listing

    lst = Listing(
        site=Site.RUTEN, external_id="22332436825897", title="遊戲王 PSA 8",
        url="https://www.ruten.com.tw/item/show?22332436825897",
        price=5751, currency=Currency.TWD,
    )
    quotes = quote_all_routes(lst, cfg, fx)

    assert quotes, "Site.RUTEN 沒有對應 route，標的會被靜默丟棄"
    assert [q.route for q in quotes] == ["ruten_local"]   # 走不了任何日本路徑
    assert quotes[0].item_twd == 5751                    # 一比一，沒有 markup
    assert quotes[0].landed_twd == pytest.approx(
        5751 + quotes[0].fee_twd + quotes[0].shipping_twd
    )


def test_usd_listing_on_ruten_is_still_converted(cfg, fx):
    """幣別分流的判準是「這筆錢會不會以外幣請款」，不是「站台在不在台灣」。

    露天上的 USD 標價（實測 6/100 筆）照樣要換匯——只放行 TWD 那一條，
    才不會讓「露天 = 台幣」這個錯誤假設從註解偷偷變成程式行為。
    """
    from ygo_sniper.costs import quote_all_routes
    from ygo_sniper.domain import Listing

    lst = Listing(
        site=Site.RUTEN, external_id="30263240038203", title="Yu-Gi-Oh PSA9",
        url="https://www.ruten.com.tw/item/show?30263240038203",
        price=79.8, currency=Currency.USD,
    )
    q = quote_all_routes(lst, cfg, fx)[0]
    assert q.item_twd > 2500          # 79.8 × 31.5 ≈ 2,513（含 markup 更高）
    assert q.item_twd != pytest.approx(79.8)


# ---------------------------------------------------------------------------
# 6. 成本路徑：在台灣買台灣貨沒有國際運費與代購費
# ---------------------------------------------------------------------------
def test_ruten_route_has_no_international_leg(cfg):
    route = cfg.routes["ruten_local"]
    assert route.sites == ["ruten"]
    assert route.intl_ship_jpy == 0        # 貨已經在台灣
    assert route.consolidation_fee_jpy == 0
    assert route.purchase_fee_jpy == 0     # 自己就是買家，沒有代購
    assert route.plan_fee_jpy == 0
    assert route.bundle_size == 1          # 各賣家各自出貨，湊單不省運費


def test_ruten_is_much_cheaper_overhead_than_the_japan_routes(cfg, fx):
    """同樣一張 NT$1,000 的卡，台灣境內取貨的雜費必須明顯低於日本任一路徑。

    這條測的是**結構**不是精確數字：日本路徑一定含代購費＋國際運費，
    台灣路徑只有島內運費。哪天有人把日本費率照抄過來，這裡會紅。
    """
    from ygo_sniper.costs import quote_route
    from ygo_sniper.domain import Listing

    tw = Listing(site=Site.RUTEN, external_id="1", title="t",
                 url="u", price=1000, currency=Currency.TWD)
    q_tw = quote_route(tw, cfg.routes["ruten_local"], fx)
    for name in ("buyee_consolidated", "buyee_single", "mercari_tw"):
        jp = cfg.routes[name]
        overhead_jp = fx.to_twd(
            jp.per_order_fee_jpy + jp.amortizable_jpy / jp.bundle_size, Currency.JPY
        )
        assert q_tw.overhead_twd < overhead_jp / 2, f"ruten_local 的雜費不該接近 {name}"


def test_ruten_listings_cannot_take_a_japan_route(cfg):
    """反向防呆：露天的貨走不了 Buyee 集運（那條路徑的前提是貨在日本）。"""
    for name in ("buyee_consolidated", "buyee_single", "mercari_tw", "ebay_direct"):
        assert "ruten" not in cfg.routes[name].sites


# ---------------------------------------------------------------------------
# 7. registry 與「刻意不進 comps」的約定
# ---------------------------------------------------------------------------
def test_registry_exposes_ruten(cfg):
    from ygo_sniper.sources import build_sources

    src = build_sources(cfg)["ruten"]
    assert src.site is Site.RUTEN
    assert src.supports_sold is True


def test_ruten_is_deliberately_absent_from_the_japan_comps_pipeline(cfg):
    """台幣成交價混進日本 comps 索引 = 混源比較（工程原則 1）。

    `valuation.venue_premium_prior` 裡沒有 `ruten` 的係數，所以在量出它之前，
    這條管道不可以出現在 watchlist 的 queries 或 comps_queries。
    要啟用時**同時**改這條測試與 venue 先驗——不會有人手滑加進去。
    """
    wl = cfg.watchlist
    for q in wl.get("queries", []):
        assert "ruten" not in (q.get("sources") or []), q.get("name")
    assert "ruten" not in ((wl.get("comps_queries") or {}).get("sources") or [])
    assert "ruten" not in ((wl.get("refill") or {}).get("sources") or [])
