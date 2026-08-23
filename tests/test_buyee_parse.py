"""BuyeeSource 的解析與健康判定測試（Mercari + PayPay，零網路）。

這個檔案釘住三件在 Phase 0 實勘（tests/fixtures/RECON.md）才看得出來的事：

**1. Mercari 商品 id 有兩種形狀。** 舊的 `m\\d+` 之外，新版是 22 字 base62
（如 `2JUaqAqRfVZv8txc7wBYEY`）。ok fixture 92 筆裡只有 4 筆是新版，但 sold
搜尋頁已經是 98 筆佔 82——比例只會往上。regex 寫死 `m\\d+` 的話，新上架的貨
會**整批安靜地消失**，而 health 依然是 OK、筆數依然「有幾十筆」，沒有任何人會發現。
所以下面不只斷言「解析出很多筆」，還單獨釘住長 id 有被抓到、且 `Listing.key` 穩定。

**2. 查無結果沒有任何「無結果」文字。** Buyee 無結果頁的主內容區就是空白；
側欄 `div.sideFilterItemBody__noResults` 的「検索結果 0件」是**品牌 facet**，
ok 頁也有——拿它當判定依據會讓有貨的日子全部被判成沒貨。唯一可靠的是結構性判準
（`ul.item-lists` 在、0 個直接 li）。這裡用 empty fixture 正向驗，也用 ok fixture
反向驗（ok 頁同樣含那段側欄文字，卻絕不可以被判成 EMPTY）。

**3. PayPay 與 Mercari 同構但不同 ID 空間。** 同一份 parser 兩組 spec，
所以要釘住 PayPay 的 Listing 掛的是 `Site.BUYEE_PAYPAY`——掛錯會走到錯的
運送路徑報價，而且一樣不會報錯。

HTML 全部是 Phase 0 實抓的 fixture，請求走 httpx.MockTransport，零網路。
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ygo_sniper.domain import Site
from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.sources.buyee import BuyeeSource
from ygo_sniper.sources.health import ParseHealth

FIXTURES = Path(__file__).parent / "fixtures"
MERCARI_OK = (FIXTURES / "buyee_mercari_ok.html").read_text(encoding="utf-8")
MERCARI_EMPTY = (FIXTURES / "buyee_mercari_empty.html").read_text(encoding="utf-8")
PAYPAY_OK = (FIXTURES / "buyee_paypay_ok.html").read_text(encoding="utf-8")

KEYWORD = "遊戯王 PSA"

#: RECON §3 實測的新版 id（22 字 base62），存在於 ok fixture（實測 96 筆中佔 11 筆）
LONG_ID = "2JMDvD9qoEqyoqPcWu3LuK"
LEGACY_ID_RE = re.compile(r"^m\d+$")
PAYPAY_ID_RE = re.compile(r"^z\d{9,10}$")


@pytest.fixture
def make_source(cfg, tmp_path):
    """造一個「網路換成 MockTransport、快取寫在暫存目錄」的 BuyeeSource。"""
    created: list[CachedFetcher] = []

    def _make(site: Site, status: int, body: str, *, sources_cfg=None) -> BuyeeSource:
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
            sources=sources_cfg if sources_cfg is not None else cfg.sources,
        )
        fetcher = CachedFetcher(scoped)
        fetcher._client.close()
        fetcher._client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body))
        )
        created.append(fetcher)
        return BuyeeSource(scoped, site, fetcher)

    yield _make
    for f in created:
        f.close()


# ---------------------------------------------------------------------------
# 1. Mercari 正常頁：解析出貨，而且新版長 id 沒有被漏掉
# ---------------------------------------------------------------------------
def test_mercari_ok_page_parses_both_id_shapes(make_source):
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.OK
    assert len(result.listings) > 50  # fixture 實測 96 筆

    by_id = {x.external_id: x for x in result.listings}
    # 新版 22 字 base62 id 必須在——這是 `(m\d+)` 那版 regex 會整批漏掉的部分
    assert LONG_ID in by_id, "新版 22 字 base62 id 沒被抓到：新上架的貨會安靜消失"
    long_ids = [i for i in by_id if len(i) == 22]
    assert len(long_ids) >= 4
    # 舊版也還在（不是把舊的換成新的）
    assert any(LEGACY_ID_RE.match(i) for i in by_id)

    for lst in result.listings:
        assert lst.site is Site.BUYEE_MERCARI
        assert lst.source == "buyee_mercari"       # 歸因；沒設會讓 alerts 認不出來源
        assert lst.url == f"https://buyee.jp/mercari/item/{lst.external_id}"
        assert lst.price and 100 <= lst.price <= 5_000_000
        assert lst.title
        assert lst.ships_to_tw is True


def test_long_id_listing_key_is_stable(make_source):
    """`Listing.key` 是去重與 store 主鍵；長 id 被截斷的話同一標的會裂成兩列。"""
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    by_id = {x.external_id: x for x in src.search_detailed(KEYWORD, pages=1).listings}

    lst = by_id[LONG_ID]
    assert lst.key == f"buyee_mercari:{LONG_ID}"
    # round-trip：產出的 URL 必須能原樣還原回同一個 external_id
    m = re.search(r"/mercari/item/([A-Za-z0-9]+)", lst.url)
    assert m and m.group(1) == LONG_ID


# ---------------------------------------------------------------------------
# 2. 查無結果頁 → EMPTY_CONFIRMED（結構性判定，沒有字串可比對）
# ---------------------------------------------------------------------------
def test_mercari_empty_page_is_empty_confirmed(make_source):
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_EMPTY)
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert result.listings == []


def test_sidebar_zero_hits_text_does_not_fake_empty(make_source):
    """側欄「検索結果 0件」是品牌 facet，ok 頁也有——它絕不可以蓋掉有貨的日子。"""
    assert "検索結果" in MERCARI_OK  # 前提：ok fixture 真的含那段側欄文字
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)

    assert src.search_detailed(KEYWORD, pages=1).health is ParseHealth.OK


# ---------------------------------------------------------------------------
# 3. 商品連結路徑改版 → PARSER_BROKEN（不可以靜靜回 0 筆）
# ---------------------------------------------------------------------------
def test_renamed_item_path_is_parser_broken(make_source):
    """把商品連結路徑改掉＝對方改版。頁面骨架與「1〜100 件目」都還在，

    卻解析出 0 筆——這不可能是市場問題，必須判成壞掉並告警。
    """
    broken = MERCARI_OK.replace("/mercari/item/", "/mercari/produkt/")
    src = make_source(Site.BUYEE_MERCARI, 200, broken)
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert result.listings == []
    assert "件目" in result.detail  # 判定依據要說得出來


def test_paypay_renamed_item_path_is_parser_broken(make_source):
    broken = PAYPAY_OK.replace("/paypayfleamarket/item/", "/paypayfleamarket/produkt/")
    src = make_source(Site.BUYEE_PAYPAY, 200, broken)
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert result.listings == []


# ---------------------------------------------------------------------------
# 4. PayPay：同一份 parser、不同 ID 空間與購買路徑
# ---------------------------------------------------------------------------
def test_paypay_ok_page_parses_with_own_site(make_source):
    src = make_source(Site.BUYEE_PAYPAY, 200, PAYPAY_OK)
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.OK
    assert len(result.listings) > 20  # fixture 實測 40 筆

    for lst in result.listings:
        # 掛錯 site 會走到錯的運送路徑報價，而且完全不會報錯
        assert lst.site is Site.BUYEE_PAYPAY
        assert lst.source == "buyee_paypay"
        assert PAYPAY_ID_RE.match(lst.external_id), f"非預期的 PayPay id: {lst.external_id}"
        assert lst.url == f"https://buyee.jp/paypayfleamarket/item/{lst.external_id}"
        assert lst.key == f"buyee_paypay:{lst.external_id}"

    assert src.supports_sold is True  # RECON §4：status=sold_out 實測 40/40 SOLD


def test_paypay_url_and_flags(make_source):
    src = make_source(Site.BUYEE_PAYPAY, 200, PAYPAY_OK)

    url = src.build_url(KEYWORD, page=1, max_price=5000, sold=True)
    assert url.startswith("https://buyee.jp/paypayfleamarket/search?")
    assert "lang=ja" in url and "price_max=5000" in url and "status=sold_out" in url
    assert src.name == "buyee_paypay"


def test_min_price_reaches_url_as_price_min(make_source):
    """高價帶掃描（2026-08-22）：`min_price` 對照組實測 Buyee Mercari 的
    `price_min` 生效且閉區間（plan `docs/superpowers/plans/2026-08-22-high-band-scan.md`）。
    寫法比照 `price_max`：`min_price=None` 完全不帶這個鍵，不留一個空參數。
    """
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)

    url = src.build_url(KEYWORD, min_price=15000)
    assert "price_min=15000" in url

    assert "price_min" not in src.build_url(KEYWORD)


# ---------------------------------------------------------------------------
# 4b. 新着排序：兩站的值不同，而且**不可互換**
# ---------------------------------------------------------------------------
def no_sort(cfg_sources: dict, name: str) -> dict:
    return {**cfg_sources, name: {**(cfg_sources.get(name) or {}), "sort_newest": False}}


def test_build_url_sorts_by_newest_by_default(make_source):
    """參數名 `order-sort`（實測：排序選單選「新着順」後導向的 URL 就是它）。"""
    mercari = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    paypay = make_source(Site.BUYEE_PAYPAY, 200, PAYPAY_OK)

    assert "order-sort=desc-created_time" in mercari.build_url(KEYWORD)
    assert "order-sort=created_time" in paypay.build_url(KEYWORD)


def test_paypay_sort_value_is_not_mercaris(make_source):
    """這條單獨存在，因為兩站的值只差一個 `desc-` 前綴，抄錯太容易。

    2026-08-01 實測：把 Mercari 的 `desc-created_time` 送給 PayPay，回來的
    **不是**退回預設排序的正常頁，而是一份 89KB、連 `ul.item-lists` 骨架都
    沒有的壞頁 —— 我們的健康判定會判成 PARSER_BROKEN，於是每小時發一則
    假告警，而真正的錯只是參數抄錯了。
    """
    paypay = make_source(Site.BUYEE_PAYPAY, 200, PAYPAY_OK)
    url = paypay.build_url(KEYWORD)

    assert "desc-created_time" not in url, (
        "PayPay 被塞了 Mercari 的排序值：實測會回傳沒有 ul.item-lists 的壞頁，"
        "健康判定會誤報 PARSER_BROKEN"
    )
    assert "order-sort=created_time" in url


def test_sort_survives_sold_and_price_filters(make_source):
    """排序不可以把 comps 靠的 sold 篩選或價格上限擠掉（實測三者併用不衝突）。"""
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    url = src.build_url(KEYWORD, page=2, max_price=5000, sold=True)

    assert "order-sort=desc-created_time" in url
    assert "status=sold_out" in url and "price_max=5000" in url and "page=2" in url


def test_build_url_sort_can_be_disabled(make_source, cfg):
    src = make_source(
        Site.BUYEE_MERCARI, 200, MERCARI_OK,
        sources_cfg=no_sort(cfg.sources, "buyee_mercari"),
    )
    url = src.build_url(KEYWORD)

    assert "order-sort" not in url
    assert "lang=ja" in url  # 其他參數不受影響


def test_sort_param_actually_reaches_the_wire(make_source):
    """真正送出去的請求要帶到排序，不是只有 build_url 對。"""
    seen: list[str] = []
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=MERCARI_OK)

    src.fetcher._client.close()
    src.fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
    src.search_detailed(KEYWORD, pages=1)

    assert seen, "沒有發出任何請求"
    assert all("order-sort=desc-created_time" in u for u in seen)


# ---------------------------------------------------------------------------
# 5. 抓取層失敗必須與「0 筆」嚴格分開（PLAN Q5）
# ---------------------------------------------------------------------------
def test_waf_challenge_is_blocked_not_empty(make_source):
    """被擋 = 什麼都不知道；判成 EMPTY 的話 WAF 擋三週你只會看到三週沒貨。"""
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    src.fetcher._client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                202,
                headers={"x-amzn-waf-action": "challenge"},
                text="<html>" + "challenge" * 100 + "</html>",
            )
        )
    )
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.BLOCKED
    assert result.listings == []


def test_server_error_is_fetch_failed(make_source):
    src = make_source(Site.BUYEE_MERCARI, 500, "boom")
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.FETCH_FAILED
    assert result.listings == []


# ---------------------------------------------------------------------------
# 6. parsed_count：Buyee 沒有 Yahoo 那種商業篩選，兩個數字相等
#
# 相等**不代表可以省略**：canary 判定只認 parsed_count，這裡明確填上，
# 哪天 Buyee 這邊加了任何篩選（例如排除 sold），健康判定不會跟著位移。
# ---------------------------------------------------------------------------
def test_parsed_count_matches_listings_for_buyee(make_source):
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.parsed_count == len(result.listings) > 0


def test_parsed_count_zero_when_blocked(make_source):
    """被擋 → 解析器根本沒跑到，parsed_count 必須是 0（不是「沒填」的巧合）。"""
    src = make_source(Site.BUYEE_MERCARI, 500, "boom")
    assert src.search_detailed(KEYWORD, pages=1).parsed_count == 0


# ---------------------------------------------------------------------------
# 7. 縮圖：lazyload 的真圖藏在 data-bind，src 永遠是佔位動畫
#
# 2026-08-02 事故：fixture 是 Playwright 抓的（JS 跑完，真圖已被塞進 src），
# 生產環境是 httpx 抓原始 HTML（JS 不跑）——於是測試永遠綠、線上 24/28 筆
# Buyee 訊號的 image_url 全是 loading-spinner.gif。fixture 已換成 httpx 版，
# 下面這幾條就是那個盲點的守門員：**抓到佔位圖 = 沒抓到**。
# ---------------------------------------------------------------------------
def test_mercari_thumbnail_comes_from_lazyload_not_placeholder(make_source):
    src = make_source(Site.BUYEE_MERCARI, 200, MERCARI_OK)
    listings = src.search_detailed(KEYWORD, pages=1).listings

    assert listings
    for lst in listings:
        assert lst.image_url, f"{lst.external_id} 沒有縮圖"
        assert "loading-spinner" not in lst.image_url
        assert lst.image_url.startswith("https://")
    # 真圖在 Mercari 的 CDN 上，不在 buyee 的靜態資源目錄
    assert all("static.mercdn.net" in x.image_url for x in listings[:10])


def test_paypay_thumbnail_comes_from_lazyload_not_placeholder(make_source):
    src = make_source(Site.BUYEE_PAYPAY, 200, PAYPAY_OK)
    listings = src.search_detailed(KEYWORD, pages=1).listings

    assert listings
    for lst in listings:
        assert lst.image_url and "loading-spinner" not in lst.image_url
        assert lst.image_url.startswith("https://")


def test_placeholder_only_image_is_none_not_the_spinner():
    """整張卡只有佔位圖時要回 None——存佔位圖進 db，前端就再也分不出真假。"""
    norm = BuyeeSource.normalize_image_url
    assert norm("https://cdn.buyee.jp/mercari/images/common/loading-spinner.gif") is None
    assert norm("https://x.test/placeholder.png") is None
    assert norm("https://x.test/blank.gif") is None
    assert norm("https://x.test/no_image.jpg") is None
    assert norm("") is None and norm(None) is None


def test_protocol_relative_image_gets_https():
    """lazyload 的 imagePath 是 `//static.mercdn.net/...`，少了 scheme 前端會破圖。"""
    assert (
        BuyeeSource.normalize_image_url("//static.mercdn.net/thumb/item/jpeg/m1_1.jpg")
        == "https://static.mercdn.net/thumb/item/jpeg/m1_1.jpg"
    )


def test_lazy_regex_does_not_grab_onerror_placeholder():
    """`data-bind` 裡 imagePath 後面緊跟著 onError，而 onError 的值就是佔位圖。

    regex 放寬成任意 `'…'` 的話會抓到 onError，等於什麼都沒修（而且測試會綠）。
    """
    from bs4 import BeautifulSoup

    html = """<div><img class="thumbnail" alt="t"
      src="https://cdn.buyee.jp/mercari/images/common/loading-spinner.gif"
      data-bind="lazyload: {
                   imagePath: '//static.mercdn.net/thumb/item/jpeg/m9_1.jpg?1',
                   onError: 'https://cdn.buyee.jp/mercari/images/common/no-image.png' }"></div>"""
    container = BeautifulSoup(html, "html.parser").div

    assert (
        BuyeeSource._parse_image(container)
        == "https://static.mercdn.net/thumb/item/jpeg/m9_1.jpg?1"
    )


def test_fixtures_are_the_html_production_actually_sees():
    """fixture 必須是 **httpx 抓的原始 HTML**，不是 Playwright 渲染後的。

    這條看起來很怪的斷言是本次事故的結構性防線：Playwright 版的 fixture 裡
    lazyload 已經被 JS 換成真圖，`src` 不含 loading-spinner——於是「從 src
    讀圖」的 parser 在測試裡永遠是綠的，線上卻永遠拿到佔位圖。
    fixture 一旦被人用瀏覽器重抓，這裡就會紅。
    """
    for name, html in (("mercari", MERCARI_OK), ("paypay", PAYPAY_OK)):
        assert "loading-spinner" in html, (
            f"{name} fixture 沒有佔位圖 src：它八成是 Playwright 渲染後的頁面，"
            "測不到生產環境真正會遇到的 lazyload"
        )
        assert "imagePath:" in html, f"{name} fixture 缺 lazyload 的 imagePath"
