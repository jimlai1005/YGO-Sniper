"""Yahoo!フリマ（PayPay）直抓的解析與健康判定。**零網路**：全部吃 fixture。

fixture 取得方式（RECON 的硬規則）：**httpx 直抓的原始 HTML**，不是 Playwright
渲染後的 DOM。這個專案已經吃過一次虧——Playwright 把 lazyload 跑完，於是
fixture 是綠的、線上全是轉圈圈的佔位圖。這條管道沒有 lazyload 問題（資料在
`__NEXT_DATA__` 裡），但取得方式的規矩一樣要守，否則下一個人會照抄錯的做法。

| fixture | 內容 | HTTP |
|---|---|---|
| `paypay_search_ok.html` | `遊戯王 PSA 初期`＋`maxPrice=5000&sort=openTime`，100 筆 | 200 |
| `paypay_search_empty.html` | 亂數關鍵字，`totalResultsAvailable: 0` | **404** |

ok fixture 刻意挑「已售出佔一半」的那一份（47 OPEN / 53 SOLD ＋ 2 筆混進來的
ヤフオク!標的）：三條分流（在架／已售出／非フリマ ID）在同一份資料上都測得到。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ygo_sniper.domain import Site
from ygo_sniper.sources.health import ParseHealth
from ygo_sniper.sources.paypay import PayPayDirectSource

FIXTURES = Path(__file__).parent / "fixtures"
OK_HTML = (FIXTURES / "paypay_search_ok.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIXTURES / "paypay_search_empty.html").read_text(encoding="utf-8")


class _StubFetcher:
    """固定回傳一份 HTML 的 fetcher。第 2 頁一律回同一份——測試只看第 1 頁的判定。"""

    def __init__(self, html: str | Exception):
        self.html = html
        self.urls: list[str] = []

    def get(self, url: str, **kw):
        self.urls.append(url)
        if isinstance(self.html, Exception):
            raise self.html
        return self.html

    def close(self) -> None:
        pass


@pytest.fixture
def make_source(cfg):
    def _make(html: str | Exception) -> PayPayDirectSource:
        return PayPayDirectSource(cfg, _StubFetcher(html))

    return _make


# ---------------------------------------------------------------------------
# 1. URL 組法（實測出來的形狀，憑印象改一定錯）
# ---------------------------------------------------------------------------
def test_keyword_goes_in_the_path_not_the_query(make_source):
    """`/search/{關鍵字}` 是路徑形式。query 參數形式（`?query=`）實測回 404 空頁。"""
    url = make_source(OK_HTML).build_url("遊戯王 PSA")
    assert url.startswith("https://paypayfleamarket.yahoo.co.jp/search/%E9%81%8A")
    assert "query=" not in url


def test_pagination_and_price_filter(make_source):
    src = make_source(OK_HTML)
    assert "page=2" in src.build_url("遊戯王", page=2)
    assert "page=" not in src.build_url("遊戯王", page=1)  # 第 1 頁不必帶
    assert "maxPrice=5000" in src.build_url("遊戯王", max_price=5000)


def test_sort_uses_the_value_the_server_actually_accepts(make_source):
    """`sort=openTime&order=DESC`。⚠️ 選單顯示的 `newer` 是前端 enum，
    送進 URL 會被**靜默忽略**——兩者不可互換（這正是 Yahoo 系來源反覆踩的坑）。"""
    url = make_source(OK_HTML).build_url("遊戯王")
    assert "sort=openTime" in url and "order=DESC" in url
    assert "newer" not in url


# ---------------------------------------------------------------------------
# 2. 解析：三條分流
# ---------------------------------------------------------------------------
def test_in_stock_search_keeps_only_open_flea_items(make_source):
    src = make_source(OK_HTML)
    res = src.search_detailed("遊戯王 PSA 初期", pages=1)

    assert res.health is ParseHealth.OK
    # parsed_count = 商業篩選前解析器認得的商品數（健康指標的分子）
    assert res.parsed_count == 100
    assert len(res.listings) == 45
    assert all(not lst.is_sold for lst in res.listings)
    assert all(lst.external_id.startswith("z") for lst in res.listings)
    assert "排除已售出 53 筆" in res.detail
    assert "排除非フリマ ID 2 筆" in res.detail


def test_listing_url_is_the_buyee_one_and_origin_is_the_native_page(make_source):
    """購買路徑沒變（走 Buyee），發現端才是原站——與 yahoo_direct 同一個模式。"""
    lst = make_source(OK_HTML).search_detailed("遊戯王", pages=1).listings[0]

    assert lst.site is Site.BUYEE_PAYPAY
    assert lst.url == f"https://buyee.jp/paypayfleamarket/item/{lst.external_id}"
    assert lst.origin_url == (
        f"https://paypayfleamarket.yahoo.co.jp/item/{lst.external_id}"
    )
    assert lst.source == "paypay_direct"
    assert lst.key == f"buyee_paypay:{lst.external_id}"
    assert lst.currency.value == "JPY" and lst.price > 0
    assert lst.ships_to_tw is True


def test_foreign_auction_ids_are_excluded_but_still_counted_as_parsed(make_source):
    """混進來的ヤフオク!標的（id 不是 `z`+數字）購買路徑完全不同。

    照單全收會產出買不到的 Buyee 連結，還會把兩個 ID 空間混進同一個
    `Listing.key`。但它們**是解析成功的**——計入 parsed_count，只是排除出
    listings，否則健康判定會把「今天混進來比較多」誤報成解析壞掉。
    """
    res = make_source(OK_HTML).search_detailed("遊戯王", pages=1)
    assert res.parsed_count > len(res.listings)
    assert not any(
        lst.external_id.startswith(("h", "u", "e", "f", "r")) for lst in res.listings
    )


def test_sold_search_returns_sold_items_with_real_sale_time(make_source):
    """已售出模式的**整個價值**就是那個真實成交時間。

    在架商品的 `endTime` 是「上架期限」（openTime + 1 年），已售出的才是
    真的結束時刻。抓不到時間的一律不收——收了就得蓋上入庫時間，那正是
    `sold_at_is_ingest` 要修的病。
    """
    res = make_source(OK_HTML).search_detailed("遊戯王", sold=True, pages=1)

    assert res.health is ParseHealth.OK
    assert res.parsed_count == 100          # 解析器看到的還是同樣 100 筆
    assert len(res.listings) == 53
    assert all(lst.is_sold for lst in res.listings)
    for lst in res.listings:
        sold_at = lst.raw["sold_at"]
        assert sold_at.endswith("+00:00"), "必須是 UTC，才能跟 comps 視窗字串比大小"
        assert lst.raw["price_kind"] == "sold_price"
    # 成交時間必須落在過去，而且不是「一年後的上架期限」
    assert max(lst.raw["sold_at"] for lst in res.listings) < "2026-08-03"


def test_in_stock_listings_never_carry_a_sold_at(make_source):
    """在架標的的 endTime 是上架期限，不是成交時間——絕不可以進 raw['sold_at']，
    不然 ingest_sold 會把一個一年後的「成交時間」寫進行情表。"""
    res = make_source(OK_HTML).search_detailed("遊戯王", pages=1)
    assert all("sold_at" not in (lst.raw or {}) for lst in res.listings)


# ---------------------------------------------------------------------------
# 3. 健康判定
# ---------------------------------------------------------------------------
def test_zero_match_page_is_empty_confirmed(make_source):
    """查無結果實測是 **HTTP 404 ＋完整頁面**（`totalResultsAvailable: 0`）。
    fetch 必須帶 allow_statuses=(404,)，否則「今天沒貨」會被當成抓取失敗。"""
    res = make_source(EMPTY_HTML).search_detailed("亂數關鍵字", pages=1)

    assert res.health is ParseHealth.EMPTY_CONFIRMED
    assert res.parsed_count == 0
    assert res.listings == []


def test_fetch_allows_404(make_source):
    src = PayPayDirectSource.__new__(PayPayDirectSource)
    captured: dict = {}

    class _F:
        def get(self, url, **kw):
            captured.update(kw)
            return EMPTY_HTML

    src.cfg = make_source(OK_HTML).cfg
    src.fetcher = _F()
    src.sort_newest = True
    src.search_detailed("x", pages=1)
    assert captured.get("allow_statuses") == (404,)


def test_missing_next_data_is_parser_broken(make_source):
    res = make_source("<html><body>完全不同的頁面</body></html>").search_detailed("遊戯王", pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "__NEXT_DATA__" in res.detail


def test_hits_without_items_is_parser_broken(make_source):
    """命中數交叉比對：頁面說有 2677 件、items 卻是空的 → selector/路徑過期。"""
    broken = OK_HTML.replace('"totalResultsReturned":100', '"totalResultsReturned":0')
    # 把 items 陣列清空（保留 total），模擬「JSON 結構改了一層」
    import json
    import re

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(broken, "html.parser")
    payload = json.loads(soup.find("script", id="__NEXT_DATA__").get_text())
    payload["props"]["initialState"]["searchState"]["search"]["result"]["items"] = []
    html = re.sub(
        r'(<script id="__NEXT_DATA__"[^>]*>).*?(</script>)',
        lambda m: m.group(1) + json.dumps(payload) + m.group(2),
        broken,
        count=1,
        flags=re.S,
    )
    res = make_source(html).search_detailed("遊戯王", pages=1)
    assert res.health is ParseHealth.PARSER_BROKEN
    assert "但解析出 0 筆" in res.detail


def test_blocked_and_fetch_failed_are_separate(make_source):
    from ygo_sniper.sources.base import BlockedError, FetchError

    blocked = make_source(BlockedError("waf", url="u")).search_detailed("遊戯王", pages=1)
    assert blocked.health is ParseHealth.BLOCKED

    failed = make_source(
        FetchError("timeout", url="u", transient=True)
    ).search_detailed("遊戯王", pages=1)
    assert failed.health is ParseHealth.FETCH_FAILED
    assert failed.parsed_count == 0


def test_search_wrapper_raises_on_fetch_layer_failure(make_source):
    """`refresh_comps` 靠例外印警告——靜默回空清單的話，被擋三週你只會看到
    三週的「comps 沒有新資料」。"""
    from ygo_sniper.sources.base import BlockedError

    with pytest.raises(BlockedError):
        make_source(BlockedError("waf", url="u")).search("遊戯王", sold=True)


# ---------------------------------------------------------------------------
# 4. registry 契約
# ---------------------------------------------------------------------------
def test_registry_uses_direct_paypay_not_the_buyee_mirror(cfg):
    from ygo_sniper.sources import build_sources

    reg = build_sources(cfg)
    assert "paypay_direct" in reg
    assert "buyee_paypay" not in reg, (
        "registry 預設要走直抓（100 筆/頁、純 httpx），"
        "Buyee 鏡像是 40 筆/頁而且要開瀏覽器解 WAF"
    )
    src = reg["paypay_direct"]
    assert (src.name, src.site.value, src.supports_sold) == (
        "paypay_direct", "buyee_paypay", True
    )


def test_importing_sources_does_not_load_playwright():
    """直抓這條路完全不需要瀏覽器。這條測試同時守住 `waf.py` 的延遲 import
    ——registry 建起來就載入 playwright 的話，每次掃描都要付那個成本。"""
    import subprocess
    import sys

    code = (
        "import sys; import ygo_sniper.sources as s; "
        "s.build_sources(__import__('ygo_sniper.config', fromlist=['x']).load_config()); "
        "print('playwright' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False", out.stdout
