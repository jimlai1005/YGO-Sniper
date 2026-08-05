"""YahooAuctionSource 的解析與健康判定測試。

這個檔案釘住的是整個 Yahoo 直抓最危險的一件事：**價格語意**。
「現在価格」是目前出價、不是付得出去的價格；把它當可成交價，
成本模型會系統性偏低、產出大量假 FREE_CARD——而且每一筆都看起來像成功。
所以下面最重要的斷言不是「解析出幾筆」，而是「即決與純競標有沒有分流對」
（fixture 兩種都有，樣本 id 出自 tests/fixtures/RECON.md 的實測比對表）。

第二危險的是 Yahoo 的 404 語意：查無結果回 404＋完整頁面。
測試裡的 404 情境必須走真的 MockTransport 回 404，確認 allow_statuses
讓它活著到 parser、判成 EMPTY_CONFIRMED、而且**不進快取**。

全部請求走 httpx.MockTransport，零網路；HTML 是 Phase 0 實抓的 fixture。
"""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ygo_sniper.sources.base import CachedFetcher
from ygo_sniper.sources.buyee import _SITE_SPEC
from ygo_sniper.sources.health import ParseHealth
from ygo_sniper.sources.yahoo import _AUCTION_ID_RE, YahooAuctionSource

FIXTURES = Path(__file__).parent / "fixtures"
OK_HTML = (FIXTURES / "yahoo_search_ok.html").read_text(encoding="utf-8")
EMPTY_HTML = (FIXTURES / "yahoo_search_empty.html").read_text(encoding="utf-8")

KEYWORD = "遊戯王 psa 初期"
BUYEE_PREFIX = "https://buyee.jp/item/yahoo/auction/"

# RECON §2 實測比對表的樣本 id（與 Buyee 商品頁逐円核對過）
BUYOUT_ID = "s1238539612"      # 現在 3,440 / 即決 30,000
PURE_BID_ID = "m1238496717"    # 現在 20,305 / 無即決
NUMERIC_ID = "1239234527"      # 純數字 id、純競標（現在 1円）


@pytest.fixture
def make_source(cfg, tmp_path):
    """造一個「網路換成 MockTransport、快取寫在暫存目錄」的 Yahoo source。"""
    created: list[CachedFetcher] = []

    def _make(handler, *, sources_cfg=None) -> YahooAuctionSource:
        scoped = replace(
            cfg,
            storage={**cfg.storage, "cache_dir": str(tmp_path / "cache")},
            fetch={**cfg.fetch, "delay_seconds": 0.0, "backoff_seconds": 0.0},
            sources=sources_cfg if sources_cfg is not None else cfg.sources,
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


def include_live(cfg_sources: dict) -> dict:
    yd = {**cfg_sources.get("yahoo_direct", {}), "include_live_auctions": True}
    return {**cfg_sources, "yahoo_direct": yd}


def exclude_live(cfg_sources: dict) -> dict:
    """明確關掉純競標。

    2026-08-02 起出貨設定是**開**（competitive 管道打開了），所以「排除純競標」
    這條路徑必須自己把開關關掉才測得到——不能再靠 cfg 的預設值。
    這條路徑仍然要有測試：它是 `price_kind` 分流的另一半，
    而分流壞掉的症狀是大量假 FREE_CARD（見 yahoo.py 模組註解）。
    """
    yd = {**cfg_sources.get("yahoo_direct", {}), "include_live_auctions": False}
    return {**cfg_sources, "yahoo_direct": yd}


def with_sort(cfg_sources: dict, enabled: bool) -> dict:
    yd = {**cfg_sources.get("yahoo_direct", {}), "sort_newest": enabled}
    return {**cfg_sources, "yahoo_direct": yd}


# ---------------------------------------------------------------------------
# 1. build_url
# ---------------------------------------------------------------------------
def test_build_url(make_source):
    src = make_source(serve(200, OK_HTML))

    url = src.build_url(KEYWORD)
    # 關鍵字要 encode（urlencode 會把空白與日文都處理掉）
    assert "遊戯王" not in url and "%E9%81%8A%E6%88%AF%E7%8E%8B" in url
    assert "n=50" in url and "b=1" in url
    assert "aucmaxprice" not in url

    # b 是 1-based 商品 offset，不是頁碼：第 2 頁 = b=51
    assert "b=51" in src.build_url(KEYWORD, page=2)
    assert "aucmaxprice=5000" in src.build_url(KEYWORD, max_price=5000)


# ---------------------------------------------------------------------------
# 1b. 新着排序參數（每小時掃描的核心：新上架優先）
# ---------------------------------------------------------------------------
def test_build_url_sorts_by_newest_by_default(make_source):
    """預設就要帶新着降冪。少了它，每小時跑只會一再看到同一批舊貨。

    值是 2026-08-01 實測出來的：`s1=new&o1=d` 的前 5 筆開始日時全在當天且遞減，
    不加參數那組橫跨六天，兩組重疊 0/5。`s1=start` 實測無效（Yahoo 靜默忽略），
    所以這裡把確切的鍵值釘死——改成別的值必須重新實測才准動。
    """
    src = make_source(serve(200, OK_HTML))
    url = src.build_url(KEYWORD)

    assert "s1=new" in url
    assert "o1=d" in url
    # 排序不可以把既有參數擠掉
    assert "b=1" in url and "n=50" in url


def test_build_url_sort_can_be_disabled(make_source, cfg):
    src = make_source(serve(200, OK_HTML), sources_cfg=with_sort(cfg.sources, False))
    url = src.build_url(KEYWORD)

    assert "s1=" not in url and "o1=" not in url
    assert "b=1" in url and "n=50" in url


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("newest", {"s1": "new", "o1": "d"}),
        ("ending_soon", {"s1": "end", "o1": "a"}),
        ("default", {}),
    ],
)
def test_build_url_sort_modes(make_source, mode, expected):
    """排序模式 → URL 參數的對照表。**這些值是實測出來的，不准憑印象改。**

    `ending_soon`（s1=end&o1=a）是「即將結標」通道的全部技術內容：抓錯排序的
    症狀是回來 50 筆中位 115 小時後才結標的標的——看起來完全正常，
    但那一批在結標前根本不會被看第二眼（競標價在最後幾分鐘才跳）。
    """
    src = make_source(serve(200, OK_HTML))
    url = src.build_url(KEYWORD, sort=mode)

    for k, v in expected.items():
        assert f"{k}={v}" in url
    if not expected:
        assert "s1=" not in url and "o1=" not in url
    # 排序不可以把既有參數擠掉
    assert "b=1" in url and "n=50" in url


def test_unknown_sort_mode_falls_back_loudly(make_source, capsys):
    """未知模式**印警告**並退回預設，不靜默。

    Yahoo 對未知排序鍵是靜默忽略的（`s1=start` 實測就是這樣死的），所以打錯
    模式名的症狀會是「抓回來的還是新着、但你以為抓的是即將結標」——
    沒有錯誤訊息，而整個功能的價值就在那個差別上。
    """
    src = make_source(serve(200, OK_HTML))
    url = src.build_url(KEYWORD, sort="ending_soonish")

    assert "s1=new" in url and "o1=d" in url          # 退回這個 source 的預設
    assert "未知排序模式" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["newest", "ending_soon"])
def test_scan_pass_sort_reaches_the_wire(make_source, mode):
    """每一趟真正送出去的請求都要帶到自己的排序參數（不是只有 build_url 對）。"""
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=OK_HTML)

    make_source(handler).search_detailed(KEYWORD, pages=1, sort=mode)

    expected = {"newest": ("s1=new", "o1=d"), "ending_soon": ("s1=end", "o1=a")}[mode]
    assert seen, "沒有發出任何請求"
    assert all(all(p in u for p in expected) for u in seen)


# ---------------------------------------------------------------------------
# 1c. 抓取通道（scan_passes）：新着＋即將結標兩趟
# ---------------------------------------------------------------------------
def with_passes(cfg_sources: dict, passes) -> dict:
    yd = {**cfg_sources.get("yahoo_direct", {}), "scan_passes": passes}
    return {**cfg_sources, "yahoo_direct": yd}


def test_scan_passes_from_config(make_source, cfg):
    src = make_source(
        serve(200, OK_HTML),
        sources_cfg=with_passes(cfg.sources, {
            "newest": {"enabled": True, "pages": 1},
            "ending_soon": {"enabled": True, "pages": 2},
        }),
    )
    assert [(p.mode, p.pages) for p in src.scan_passes()] == [
        ("newest", 1), ("ending_soon", 2)
    ]


def test_scan_passes_can_disable_one_channel(make_source, cfg):
    src = make_source(
        serve(200, OK_HTML),
        sources_cfg=with_passes(cfg.sources, {
            "newest": {"enabled": False, "pages": 1},
            "ending_soon": {"enabled": True},
        }),
    )
    passes = src.scan_passes()
    # 沒寫 pages 就用 Config.max_pages_for（全域 1）
    assert [(p.mode, p.pages) for p in passes] == [("ending_soon", 1)]


def without_passes(cfg_sources: dict, *, sort_newest: bool) -> dict:
    """回到「還沒有 scan_passes 這個鍵」的舊設定形狀。"""
    yd = {k: v for k, v in cfg_sources.get("yahoo_direct", {}).items() if k != "scan_passes"}
    yd["sort_newest"] = sort_newest
    return {**cfg_sources, "yahoo_direct": yd}


def test_scan_passes_defaults_to_single_legacy_pass(make_source, cfg):
    """沒設 scan_passes → 單趟，模式由舊鍵 sort_newest 決定（相容出口）。"""
    src = make_source(
        serve(200, OK_HTML), sources_cfg=without_passes(cfg.sources, sort_newest=True)
    )
    assert [(p.mode, p.pages) for p in src.scan_passes()] == [("newest", 1)]

    off = make_source(
        serve(200, OK_HTML), sources_cfg=without_passes(cfg.sources, sort_newest=False)
    )
    assert [p.mode for p in off.scan_passes()] == ["default"]


def test_bad_scan_passes_never_yields_zero_passes(make_source, cfg, capsys):
    """設定壞掉時寧可多抓一趟，也不要安靜地變成 0 趟。

    回空清單的話畫面上會是「Yahoo 0 筆」——與「今天沒貨」外顯一模一樣，
    而只有前者需要你去改設定。
    """
    src = make_source(
        serve(200, OK_HTML),
        sources_cfg=with_passes(cfg.sources, {
            "endig_soon": {"enabled": True},          # 打錯字的模式名
            "newest": {"enabled": False},
        }),
    )
    passes = src.scan_passes()
    out = capsys.readouterr().out

    assert [p.mode for p in passes] == ["newest"]     # 退回單趟預設，不是 0 趟
    assert "未知排序模式" in out and "沒有任何啟用的通道" in out


def test_scan_pass_bad_pages_falls_back(make_source, cfg, capsys):
    src = make_source(
        serve(200, OK_HTML),
        sources_cfg=with_passes(cfg.sources, {"ending_soon": {"pages": 0}}),
    )
    assert [(p.mode, p.pages) for p in src.scan_passes()] == [("ending_soon", 1)]
    assert "pages=0" in capsys.readouterr().out


def test_sort_param_actually_reaches_the_wire(make_source, cfg):
    """光是 build_url 對還不夠——真正送出去的請求也要帶到。

    build_url 與 search_detailed 各自組 URL 的話，很容易只改到一邊：
    測試會綠、實際抓的卻還是預設排序，而且完全看不出來。
    """
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, text=OK_HTML)

    src = make_source(handler, sources_cfg=with_sort(cfg.sources, True))
    src.search_detailed(KEYWORD, pages=1)

    assert seen, "沒有發出任何請求"
    assert all("s1=new" in u and "o1=d" in u for u in seen)


# ---------------------------------------------------------------------------
# 2. 正常頁解析＋即決/純競標分流（本檔核心）
# ---------------------------------------------------------------------------
def test_parse_ok_page_buyout_only(make_source, cfg):
    src = make_source(serve(200, OK_HTML), sources_cfg=exclude_live(cfg.sources))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.OK
    # fixture：50 個唯一 id = 13 有即決 + 37 純競標；關掉開關時只收即決
    assert len(result.listings) == 13
    assert "排除純競標 37 筆" in result.detail

    by_id = {x.external_id: x for x in result.listings}
    for lst in result.listings:
        assert lst.url.startswith(BUYEE_PREFIX)                       # 購買端
        assert lst.origin_url.startswith("https://auctions.yahoo.co.jp/")  # 發現端
        assert 300 <= lst.price <= 5_000_000
        assert lst.external_id and lst.title
        assert lst.source == "yahoo_direct"
        assert lst.raw["price_kind"] == "buyout"

    # RECON 樣本：s1238539612 的可成交價是即決 30,000，不是現在 3,440
    assert by_id[BUYOUT_ID].price == 30_000
    assert by_id[BUYOUT_ID].raw["current_bid"] == 3_440
    # 純競標不得產出——現在価格不是付得出去的價格
    assert PURE_BID_ID not in by_id
    assert NUMERIC_ID not in by_id


# ---------------------------------------------------------------------------
# 3. 404 無結果頁 → EMPTY_CONFIRMED（不是 FETCH_FAILED）
# ---------------------------------------------------------------------------
def test_empty_404_page_is_empty_confirmed(make_source):
    src = make_source(serve(404, EMPTY_HTML))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.EMPTY_CONFIRMED
    assert result.listings == []


# ---------------------------------------------------------------------------
# 4. Product class 全滅 → PARSER_BROKEN
# ---------------------------------------------------------------------------
def test_renamed_product_classes_is_parser_broken(make_source):
    broken = OK_HTML.replace("Product", "Produkt")  # 模擬對方全面改版 class 名
    src = make_source(serve(200, broken))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert result.listings == []


# ---------------------------------------------------------------------------
# 5. 命中數交叉比對：頁面說 142 件、商品卻解析 0 筆 → 必定 PARSER_BROKEN
# ---------------------------------------------------------------------------
def test_hits_crosscheck_catches_dead_selector(make_source):
    # 只改掉商品容器 class，命中數元素（Tab__subText: 142件）原封不動
    broken = OK_HTML.replace('li class="Product"', 'li class="ProductRenamed"')
    src = make_source(serve(200, broken))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.PARSER_BROKEN
    assert "142" in result.detail  # 判定依據要說得出來：頁面標示 vs 解析數


# ---------------------------------------------------------------------------
# 6. id 正規化：與 buyee.py 同一個 ID 空間、key 穩定
# ---------------------------------------------------------------------------
def test_external_id_matches_buyee_extraction(make_source, cfg):
    # 開 include_live_auctions 才能拿到純數字 id 那筆（它是純競標）
    src = make_source(serve(200, OK_HTML), sources_cfg=include_live(cfg.sources))
    by_id = {x.external_id: x for x in src.search_detailed(KEYWORD, pages=1).listings}

    # buyee.py 的 _SITE_SPEC 已不再有 BUYEE_YAHOO 的搜尋 spec（發現管道由
    # yahoo_direct 取代），所以這裡改用 yahoo.py 自己的抽取式做 round-trip：
    # 「產出的購買端 URL 必須能原樣還原回同一個 external_id」。
    # 這才是真正要守的不變式——它決定 Listing.key 穩不穩定，
    # 而 key 分岔的後果是同一標的兩列、推播兩次、comps 算兩次。
    assert _SITE_SPEC.get(src.site) is None, (
        "buyee.py 又長回 BUYEE_YAHOO 的搜尋 spec 了——"
        "兩條發現管道並存會讓同一標的被抓兩次，請確認是有意為之"
    )

    # 字母開頭 id
    lst = by_id[BUYOUT_ID]
    m = _AUCTION_ID_RE.search(lst.url)
    assert m and m.group(1) == lst.external_id == BUYOUT_ID
    assert lst.key == f"buyee_yahoo:{BUYOUT_ID}"

    # 純數字 id（Yahoo 實際存在這種，RECON §1 實測）：一樣要 round-trip 得回來
    numeric = by_id[NUMERIC_ID]
    m2 = _AUCTION_ID_RE.search(numeric.url)
    assert m2 and m2.group(1) == numeric.external_id == NUMERIC_ID
    assert numeric.key == f"buyee_yahoo:{NUMERIC_ID}"
    assert numeric.url == f"{BUYEE_PREFIX}{NUMERIC_ID}"


# ---------------------------------------------------------------------------
# 7. 404 頁不進快取（「當下沒結果」不是穩定內容）
# ---------------------------------------------------------------------------
def test_404_response_is_not_cached(make_source):
    src = make_source(serve(404, EMPTY_HTML))
    src.search_detailed(KEYWORD, pages=1)

    assert list(src.fetcher.cache_dir.glob("*.html")) == []


def test_200_response_is_cached(make_source):
    """對照組：200 正常頁照常進快取，確認 allow_statuses 沒有把快取整個關掉。"""
    src = make_source(serve(200, OK_HTML))
    src.search_detailed(KEYWORD, pages=1)

    assert len(list(src.fetcher.cache_dir.glob("*.html"))) == 1


# ---------------------------------------------------------------------------
# 8. include_live_auctions=true：純競標也產出，price 用現在価格
# ---------------------------------------------------------------------------
def test_include_live_auctions_emits_current_bid(make_source, cfg):
    src = make_source(serve(200, OK_HTML), sources_cfg=include_live(cfg.sources))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.health is ParseHealth.OK
    assert len(result.listings) == 50  # 13 即決 + 37 純競標，全收
    assert "排除純競標" not in result.detail

    by_id = {x.external_id: x for x in result.listings}
    # RECON 樣本：m1238496717 無即決，price 必須是現在価格 20,305
    assert by_id[PURE_BID_ID].price == 20_305
    assert by_id[PURE_BID_ID].raw["price_kind"] == "current_bid"
    # 有即決的照舊用即決価格，不受開關影響
    assert by_id[BUYOUT_ID].price == 30_000
    assert by_id[BUYOUT_ID].raw["price_kind"] == "buyout"


# ---------------------------------------------------------------------------
# 9. parsed_count：解析器解出幾個商品（**商業篩選之前**）
#
# 這一組是 2026-08-01 假警報的迴歸測試。canary 原本數 len(listings)，
# 而 listings 是 include_live_auctions 篩選**之後**的結果——同一份頁面、
# 同一個健康的解析器，只要今天新上架的都是純競標，listings 就會塌到接近 0。
# 兩個數字必須分得開，健康判定才有得挑。
# ---------------------------------------------------------------------------
def test_parsed_count_is_before_commercial_filter(make_source, cfg):
    """同一份 fixture：解析 50 個商品、商業篩選後只剩 13 筆。"""
    src = make_source(serve(200, OK_HTML), sources_cfg=exclude_live(cfg.sources))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.parsed_count == 50           # 解析器：50 個商品都認得
    assert len(result.listings) == 13          # 商業篩選：只有 13 筆有即決価格
    assert result.parsed_count != len(result.listings), "兩個數字混成一個就沒有意義了"
    assert "解析 50 筆" in result.detail        # 排錯線索要說得出兩個數字


def test_parsed_count_unchanged_when_filter_opens(make_source, cfg):
    """開了 include_live_auctions（純商業決定）→ listings 跳到 50，parsed_count 不動。

    這正是要守的不變式：健康指標不可以隨商業設定漂移，否則調一個 config
    就會讓 canary 的門檻語意整個換掉。
    """
    src = make_source(serve(200, OK_HTML), sources_cfg=include_live(cfg.sources))
    result = src.search_detailed(KEYWORD, pages=1)

    assert result.parsed_count == 50
    assert len(result.listings) == 50


# ---------------------------------------------------------------------------
# 10. 結標時間與出價數（競標視圖的地基）
#
# 競標是時間敏感的：沒有結標時間的競標標的在清單上毫無用處。
# 來源必須是 `data-auction-endtime`（epoch 秒），**不是**看得見的
# 「残り 1日／10時間」文字——那是四捨五入過的相對時間，反推會差好幾小時，
# 而競標最後五分鐘才是決勝點。
# ---------------------------------------------------------------------------
def test_end_time_and_bids_are_parsed(make_source, cfg):
    from datetime import UTC, datetime

    src = make_source(serve(200, OK_HTML), sources_cfg=include_live(cfg.sources))
    by_id = {x.external_id: x for x in src.search_detailed(KEYWORD, pages=1).listings}

    # fixture 實測值：data-auction-endtime=1785679082、Product__bid=11
    lst = by_id[BUYOUT_ID]
    assert lst.end_time == datetime.fromtimestamp(1785679082, UTC)
    assert lst.end_time.tzinfo is not None, "沒有時區的結標時間會讓倒數差 9 小時"
    assert lst.bids == 11

    # 每一筆都要有——缺一筆就代表 selector 只對某些版型有效
    assert all(x.end_time is not None for x in by_id.values())
    assert all(x.bids is not None for x in by_id.values())
    # 出價數 0 是真的 0（剛開標、還沒有人出價），不是「抽不到」
    assert min(x.bids for x in by_id.values()) == 0


def test_missing_end_time_is_none_not_guessed(make_source, cfg):
    """抽不到 epoch 就回 None——**絕不用「現在＋残り文字」猜一個絕對時間**。"""
    stripped = OK_HTML.replace("data-auction-endtime", "data-auction-endtime-x")
    src = make_source(serve(200, stripped), sources_cfg=include_live(cfg.sources))
    listings = src.search_detailed(KEYWORD, pages=1).listings

    assert listings, "拿掉結標時間不該讓整批標的消失"
    assert all(x.end_time is None for x in listings)
    assert all(x.bids is not None for x in listings)  # 出價數是另一個 selector，不該連坐


def test_shipped_config_includes_live_auctions(cfg):
    """出貨設定：競標管道是開的（2026-08-02 起）。

    關著等於只看即決＝賣家開的溢價，而實測競標出清價只有即決的 0.19 倍。
    """
    assert cfg.sources["yahoo_direct"]["include_live_auctions"] is True


def test_parsed_count_is_zero_when_parser_dies(make_source):
    """對照組：真的壞掉時 parsed_count 必須是 0（否則 canary 永遠不會叫）。"""
    broken = OK_HTML.replace("Product", "Produkt")
    result = make_source(serve(200, broken)).search_detailed(KEYWORD, pages=1)

    assert result.parsed_count == 0
    assert result.health is ParseHealth.PARSER_BROKEN
