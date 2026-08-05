"""YahooClosedSource（落札相場）的解析與健康判定測試。零網路。

這個檔案釘住的是行情資料最危險的一件事：**流標價不得混進成交價**。

流標（無人出價）商品顯示的是「開始価格」——賣家的期望，不是有人付過的錢。
混進行情表會系統性拉低中位數，然後每一筆折價訊號都變成假的，而且沒有任何
錯誤訊息（PLAN 風險 1 的同型）。所以核心斷言是 `bidCount == 0` 的樣本
一定不會產出 Listing，而且擋掉的筆數要說得出來。

第二危險的是成交時間：closedsearch 視窗 180 天、comps 視窗 90 天。
`sold_at` 若蓋成 now()，冷門查詢翻出的半年前成交就會被當成今天的行情。

fixture 是 2026-08-01 對 `遊戯王 PSA 初期` 實抓的原始頁（50 筆，
25 筆 Yahoo 拍賣 + 25 筆 Yahoo!フリマ，全部 bidCount ≥ 1）。
"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.sources.health import ParseHealth
from ygo_sniper.sources.yahoo_closed import (
    _PAGE_SIZE as PAGE_SIZE,
)
from ygo_sniper.sources.yahoo_closed import (
    YahooClosedSource,
    to_utc_iso,
)

FIXTURES = Path(__file__).parent / "fixtures"
OK_HTML = (FIXTURES / "yahoo_closed_ok.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIXTURES / "yahoo_closed_empty.html").read_text(encoding="utf-8")

KEYWORD = "遊戯王 PSA 初期"

# 實測樣本（見 tests/fixtures/RECON.md §6 對帳表）
AUCTION_ID = "l1238412091"   # Yahoo 拍賣、bidCount 23、落札 20,000（開始価格 1 円）
FLEA_ID = "z646400662"       # Yahoo!フリマ、即決 42,000


@pytest.fixture
def make_source(cfg, tmp_path):
    """網路換成 MockTransport、快取寫在暫存目錄的 closedsearch source。"""
    created: list[CachedFetcher] = []

    def _make(handler) -> YahooClosedSource:
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
        )
        fetcher = CachedFetcher(scoped)
        fetcher._client.close()
        fetcher._client = httpx.Client(transport=httpx.MockTransport(handler))
        created.append(fetcher)
        return YahooClosedSource(scoped, fetcher)

    yield _make
    for f in created:
        f.close()


def serve(status: int, body: str):
    return lambda request: httpx.Response(status, text=body)


def unsold_variant(html: str) -> str:
    """把 fixture 的 `"bidCount": N` 全部改成 0，模擬「Yahoo 開始列出流標」。

    ⚠️ 這是**人工構造**的：closedsearch 實測 200 筆（4 個查詢 × 50 筆）
    沒有任何一筆 bidCount == 0，Yahoo 的落札相場本來就只列落札成功的商品
    （頁面自稱「〈關鍵字〉の落札された商品」）。取不到真的流標樣本，
    但守門條件必須被測到——不能因為「現在不會發生」就不驗，
    那正是它哪天真的發生時會安靜污染行情表的原因。
    """
    import re

    return re.sub(r'"bidCount":\s*\d+', '"bidCount":0', html)


def full_page_html(html: str = OK_HTML, size: int = PAGE_SIZE) -> str:
    """把 fixture 撐成「正好滿一頁」的頁面（auctionId 全部改成唯一值）。

    fixture 是 50 筆的實抓頁，而一頁的判準是 `_PAGE_SIZE`（現在 100）。
    翻頁測試要驗的是「滿頁就續抓」，所以樣本必須真的滿頁——直接把 fixture
    當滿頁用，會讓這條測試在 `_PAGE_SIZE` 改動時**靜默失去意義**
    （測到的是早停，不是翻頁）。id 必須改唯一：`search_detailed` 會用
    `external_id` 去重，重複 id 的第二頁會整頁被丟掉。
    """
    import json

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    payload = json.loads(tag.get_text())
    node = payload["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]
    base = node["items"]
    grown = []
    for i in range(size):
        item = dict(base[i % len(base)])
        item["auctionId"] = f"{item['auctionId']}x{i}"
        grown.append(item)
    node["items"] = grown
    tag.string = json.dumps(payload, ensure_ascii=False)
    return str(soup)


# ---------------------------------------------------------------------------
# 1. build_url
# ---------------------------------------------------------------------------
def test_build_url(make_source):
    src = make_source(serve(200, OK_HTML))
    url = src.build_url(KEYWORD)

    assert url.startswith("https://auctions.yahoo.co.jp/closedsearch/closedsearch?")
    assert "遊戯王" not in url and "%E9%81%8A%E6%88%AF%E7%8E%8B" in url
    assert f"n={PAGE_SIZE}" in url and "b=1" in url
    # comps 不設價格上限：對成交價設上限＝只看便宜的一半，中位數會被系統性壓低
    assert "aucmaxprice" not in url
    # b 是 1-based 商品 offset，步長＝一頁筆數（兩者必須同源）
    assert f"b={PAGE_SIZE + 1}" in src.build_url(KEYWORD, page=2)


# ---------------------------------------------------------------------------
# 2. 正常頁解析（本檔核心：只收有得標者的成交）
# ---------------------------------------------------------------------------
def test_parse_ok_page(make_source):
    src = make_source(serve(200, OK_HTML))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.OK
    assert len(result.listings) == 50          # fixture 50 筆全部 bidCount ≥ 1
    assert "排除無得標者" not in result.detail   # 沒有流標可排除

    by_id = {x.external_id: x for x in result.listings}

    for lst in result.listings:
        assert lst.is_sold is True
        assert lst.source == "yahoo_closed"
        assert lst.raw["price_kind"] == "sold_price"
        assert lst.raw["bid_count"] >= 1
        assert 100 <= lst.price <= 5_000_000
        assert lst.title and lst.external_id
        assert lst.url.startswith("https://buyee.jp/")            # 購買端
        assert lst.origin_url.startswith("https://")              # 發現端原生頁

    # 實測對帳：l1238412091 的落札価格是 20,000（開始価格 1 円、23 次出價）。
    # 若哪天解析改成讀 initPriceNoTax，這條會立刻紅。
    won = by_id[AUCTION_ID]
    assert won.price == 20_000
    assert won.raw["bid_count"] == 23
    assert won.raw["start_price"] == 1
    assert won.url == f"https://buyee.jp/item/yahoo/auction/{AUCTION_ID}"
    assert won.origin_url == f"https://auctions.yahoo.co.jp/jp/auction/{AUCTION_ID}"


def test_fleamarket_items_get_their_own_id_space(make_source):
    """closedsearch 混著 Yahoo 拍賣與 Yahoo!フリマ 兩個 ID 空間（實測各半）。

    全部掛同一個 site 的話，comps 的 site 欄位會說謊，而 `Listing.key`
    會把兩個 ID 空間混在一起——同一組字元在兩邊代表不同商品時就會撞。
    """
    src = make_source(serve(200, OK_HTML))
    by_id = {x.external_id: x for x in src.search_detailed(KEYWORD, pages=1).listings}

    flea = by_id[FLEA_ID]
    assert flea.site.value == "buyee_paypay"
    assert flea.url == f"https://buyee.jp/paypayfleamarket/item/{FLEA_ID}"
    assert flea.key == f"buyee_paypay:{FLEA_ID}"
    assert by_id[AUCTION_ID].site.value == "buyee_yahoo"

    sites = {x.site.value for x in by_id.values()}
    assert sites == {"buyee_yahoo", "buyee_paypay"}


# ---------------------------------------------------------------------------
# 3. ★ 流標守門：bidCount == 0 一律不產出
# ---------------------------------------------------------------------------
def test_unsold_items_are_excluded(make_source):
    src = make_source(serve(200, unsold_variant(OK_HTML)))
    result = src.search_detailed(KEYWORD, pages=1)

    # 一筆都不能出來：顯示的價格是開始価格，不是有人付過的錢
    assert result.listings == []
    # 而且要說得出擋了幾筆——安靜地少收 50 筆與安靜地壞掉，外顯是一樣的
    assert "排除無得標者 50 筆" in result.detail
    # 全是流標**不是**解析壞掉：交叉比對用原始 items 長度，不是過濾後的筆數
    assert result.health is ParseHealth.OK
    # 同一個立場延伸到健康指標：parsed_count 數的是**商業篩選之前**解析到的商品，
    # 所以 50 筆全流標時它仍然是 50（見 health.SearchResult.parsed_count 的事故註記）
    assert result.parsed_count == 50


def test_missing_bid_count_is_excluded(make_source):
    """`bidCount` 欄位不見了 → 一樣擋。

    「不知道有沒有人買」不能當成「有人買」——安全關鍵的預設值要往保守的
    那一邊倒（工程原則 3 的同型：不確定時大聲少收，不要安靜多收）。
    """
    src = make_source(serve(200, OK_HTML.replace('"bidCount":', '"bidKount":')))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.listings == []
    assert "排除無得標者 50 筆" in result.detail


# ---------------------------------------------------------------------------
# 4. 成交時間：用 endTime，不是 now()
# ---------------------------------------------------------------------------
def test_sold_at_comes_from_end_time_in_utc(make_source):
    src = make_source(serve(200, OK_HTML))
    by_id = {x.external_id: x for x in src.search_detailed(KEYWORD, pages=1).listings}

    won = by_id[AUCTION_ID]
    assert won.raw["end_time"] == "2026-08-01T22:17:22+09:00"
    # 必須換算成 UTC：comps 既有的列是 now(UTC).isoformat()，
    # store.load_comps 用字串比大小篩視窗，混著兩種偏移就會篩錯
    assert won.raw["sold_at"] == "2026-08-01T13:17:22+00:00"
    assert won.raw["sold_at"].endswith("+00:00")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-01T22:17:22+09:00", "2026-08-01T13:17:22+00:00"),
        ("2026-08-01T13:17:22+00:00", "2026-08-01T13:17:22+00:00"),
        ("2026-08-01T13:17:22", "2026-08-01T13:17:22+00:00"),  # 無時區 → 當成 UTC
        ("not-a-date", None),
        (None, None),
        ("", None),
    ],
)
def test_to_utc_iso(raw, expected):
    assert to_utc_iso(raw) == expected


def test_sold_at_is_comparable_with_the_comps_window(make_source):
    """真正要守的不變式：sold_at 要能跟 comps 視窗的 cutoff 直接字串比大小。

    `store.load_comps()` 就是這樣篩的（`WHERE sold_at >= ?`），
    所以「格式對」不夠，「跟 cutoff 同基準」才是重點。
    """
    src = make_source(serve(200, OK_HTML))
    listings = src.search_detailed(KEYWORD, pages=1).listings
    cutoff = datetime(2026, 5, 3, tzinfo=UTC).isoformat()

    recent = [x for x in listings if x.raw["sold_at"] and x.raw["sold_at"] >= cutoff]
    # fixture 是 2026-07-19～08-01 的成交，全部落在 90 天視窗內
    assert len(recent) == 50


# ---------------------------------------------------------------------------
# 5. 健康判定三層
# ---------------------------------------------------------------------------
def test_empty_404_page_is_empty_confirmed(make_source):
    """查無成交紀錄回 404 ＋完整頁面（totalResultsAvailable: 0）。"""
    src = make_source(serve(404, EMPTY_HTML))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert result.listings == []


def test_missing_next_data_is_parser_broken(make_source):
    """__NEXT_DATA__ 整個不見（改版／改成純 client render）→ 告警。"""
    src = make_source(serve(200, OK_HTML.replace("__NEXT_DATA__", "__NUXT_DATA__")))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert "__NEXT_DATA__" in result.detail
    assert result.listings == []


def test_hits_crosscheck_catches_dead_item_list(make_source):
    """JSON 還在、命中數還在，但商品陣列空了 → 必定是路徑過期，不是沒貨。"""
    broken = OK_HTML.replace('"listing":{"isFetching":false,"items":[', '"listing":{"isFetching":false,"items":[],"ignored":[', 1)
    src = make_source(serve(200, broken))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert "833" in result.detail  # 判定依據要說得出來：頁面標示 833 件 vs 解析 0 筆


def test_malformed_json_is_parser_broken(make_source):
    src = make_source(serve(200, OK_HTML.replace('{"props"', '{{{"props"', 1)))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert result.listings == []


# ---------------------------------------------------------------------------
# 6. 快取：404 不進快取（「當下沒有成交紀錄」不是穩定內容）
# ---------------------------------------------------------------------------
def test_404_response_is_not_cached(make_source):
    src = make_source(serve(404, EMPTY_HTML))
    src.search_detailed(KEYWORD, pages=1)

    assert list(src.fetcher.cache_dir.glob("*.html")) == []


# ---------------------------------------------------------------------------
# 7. 分頁：不足一頁就停，不多打一個請求
# ---------------------------------------------------------------------------
def test_pages_through_full_pages(make_source):
    """滿頁（_PAGE_SIZE 筆）就續抓下一頁，offset 要對。"""
    seen: list[str] = []
    body = full_page_html()

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=body)

    src = make_source(handler)
    result = src.search_detailed(KEYWORD, pages=3)

    assert len(seen) == 3
    assert f"b={1}" in seen[0]
    assert f"b={PAGE_SIZE + 1}" in seen[1]
    assert f"b={2 * PAGE_SIZE + 1}" in seen[2]
    # 一頁真的收了 _PAGE_SIZE 筆（不然這條測到的是早停，不是翻頁）
    assert result.parsed_count == 3 * PAGE_SIZE


def test_stops_paging_when_page_is_not_full(make_source):
    """回不滿一頁 = 已是最後一頁，不准再打下一個請求。

    這條直接決定整體請求預算：展開後的 78 個查詢裡，實測多數是冷門組合
    （結果 < 50 筆）。少了這個早停，每輪的請求數就是 78×pages 而不是
    「大約 15 個查詢翻 2 頁、其餘 1 頁」——差了將近一倍。
    """
    import json
    import re

    payload = json.loads(
        re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', OK_HTML, re.S).group(1)
    )
    node = payload["props"]["pageProps"]["initialState"]["search"]["items"]["listing"]
    node["items"] = node["items"][:7]        # 只剩 7 筆 = 不滿一頁
    node["totalResultsAvailable"] = 7
    short = f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></body></html>'

    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=short)

    src = make_source(handler)
    result = src.search_detailed(KEYWORD, pages=3)

    assert len(seen) == 1, f"不滿一頁還繼續翻：{seen}"
    assert len(result.listings) == 7
    assert result.health is ParseHealth.OK


def test_search_wrapper_returns_listings(make_source):
    """refresh_comps 走的是舊介面 search()，也要通。"""
    src = make_source(serve(200, OK_HTML))
    listings = src.search(KEYWORD, sold=True, pages=1)

    assert len(listings) == 50
    assert all(x.is_sold for x in listings)
