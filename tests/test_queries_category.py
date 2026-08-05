"""查詢設定（`queries.py`）與分類參數的端到端接線。

**這一組守的是一種已經發生過、而且完全無聲的失敗**（2026-08-03）：
`categories:` 在 watchlist 裡設好了、`pipeline._scan_source` 也查出來了，
然後在 `run_source_search` 的 `**kwargs` 那一行被丟掉——因為舊版只把
`category_id` 傳給「沒有 `search_detailed` 的舊介面」，而 Buyee 有
`search_detailed`。症狀是零：分類沒生效與「分類生效了但今天貨就長這樣」
外顯一模一樣，沒有錯誤、沒有告警、沒有數字變化。

所以這裡的斷言一路釘到**送出去的那個 URL**：只驗「函式收到參數」擋不住
這個 bug（舊版的 `_scan_source` 也確實算出了 1152）。
"""

from __future__ import annotations

import pytest

from ygo_sniper.domain import Site
from ygo_sniper.pipeline import _optional_kwargs, run_source_search
from ygo_sniper.queries import QuerySpec, load_queries, resolve_category
from ygo_sniper.sources.buyee import BuyeeSource
from ygo_sniper.sources.ebay import EbaySource
from ygo_sniper.sources.paypay import PayPayDirectSource
from ygo_sniper.sources.yahoo import YahooAuctionSource

WATCHLIST = {
    "queries": [
        {"name": "帶分類", "keyword": "PSA 初期", "category": "yugioh",
         "sources": ["yahoo_direct", "buyee_mercari"]},
        {"name": "不帶分類", "keyword": "遊戯王 PSA 初期", "sources": ["buyee_mercari"]},
    ],
    "categories": {
        "buyee_mercari": {"yugioh": 1152, "trading_cards": 82},
        "yahoo_direct": {"yugioh": 2084005059},
        "paypay_direct": {"yugioh": "brand:167476", "trading_cards": "category:2511,2420"},
        "ebay": {"yugioh": "183454"},
    },
}


# ---------------------------------------------------------------------------
# 1. watchlist → QuerySpec
# ---------------------------------------------------------------------------
def test_load_queries_reads_category_alias():
    qs = load_queries(WATCHLIST)
    assert [q.name for q in qs] == ["帶分類", "不帶分類"]
    assert qs[0].category == "yugioh"
    assert qs[0].sources == ("yahoo_direct", "buyee_mercari")
    assert qs[1].category is None


def test_empty_keyword_is_a_legal_query():
    """「純分類、不帶關鍵字」是要被**量測**的設定之一，不是設定錯誤。

    （實測結論是不採用——分類第 1 頁的候選率趨近 0——但那是資料說的，
    不該由解析層先把它變成不可表達。）
    """
    qs = load_queries({"queries": [{"name": "純分類", "keyword": "", "sources": ["x"]}]})
    assert qs[0].keyword == ""


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "沒有 sources", "keyword": "x"},
        {"name": "沒有 keyword", "sources": ["x"]},
        "根本不是對映表",
    ],
)
def test_broken_query_is_skipped_not_fatal(raw):
    """一條寫壞的 query 不該讓整輪掃描沒有產出（但會印警告）。"""
    assert load_queries({"queries": [raw]}) == []


def test_resolve_category_is_per_source():
    """同一個別名在不同來源解出**完全不同**的值——各站是兩棵不同的樹。"""
    assert resolve_category(WATCHLIST, "buyee_mercari", "yugioh") == "1152"
    assert resolve_category(WATCHLIST, "yahoo_direct", "yugioh") == "2084005059"
    assert resolve_category(WATCHLIST, "paypay_direct", "yugioh") == "brand:167476"


def test_resolve_category_missing_source_is_not_an_error():
    """某來源沒有這個別名 → 退化成純關鍵字搜尋（各站分類樹不對齊是事實）。"""
    assert resolve_category(WATCHLIST, "ruten", "yugioh") is None
    assert resolve_category(WATCHLIST, "buyee_mercari", None) is None


# ---------------------------------------------------------------------------
# 2. 分類值真的進到 URL（逐來源，各自的參數名）
# ---------------------------------------------------------------------------
def test_buyee_url_carries_category_id(cfg):
    src = BuyeeSource(cfg, Site.BUYEE_MERCARI, fetcher=object())
    assert "category_id=1152" in src.build_url("PSA 初期", category="1152")
    assert "category_id" not in src.build_url("PSA 初期")


def test_yahoo_url_carries_auccat(cfg):
    src = YahooAuctionSource(cfg, fetcher=object())
    assert "auccat=2084005059" in src.build_url("PSA 初期", category="2084005059")
    assert "auccat" not in src.build_url("PSA 初期")


def test_paypay_url_uses_categoryids_or_brandids(cfg):
    """PayPay 的兩種過濾器：`categoryIds`（分類）與 `brandIds`（品牌）。

    ⚠️ 這兩個名字都是實測出來的：`catId`／`categoryId`／`brandId`（單數）
    送出去會被**靜默忽略**，頁面照常回結果、過濾根本沒生效。
    """
    src = PayPayDirectSource(cfg, fetcher=object())
    assert "brandIds=167476" in src.build_url("PSA", category="brand:167476")
    assert "categoryIds=2511%2C2420" in src.build_url("PSA", category="category:2511,2420")
    # 無前綴 = 分類（向後相容）
    assert "categoryIds=2420" in src.build_url("PSA", category="2420")
    assert "categoryIds" not in src.build_url("PSA")


def test_ebay_defaults_to_yugioh_category(cfg, monkeypatch):
    """eBay **不帶分類就是搜整個 eBay**，所以預設值不可以是 None。"""
    captured: dict = {}

    def fake_get_page(client, headers, params):
        captured.update(params)
        return {"total": 0, "itemSummaries": []}, 0

    monkeypatch.setattr(EbaySource, "_get_token", lambda self: "tok")
    monkeypatch.setattr(EbaySource, "_get_page", staticmethod(fake_get_page))
    src = EbaySource(cfg)
    src.enabled = True

    src.search_detailed("yugioh PSA", pages=1)
    assert captured["category_ids"] == "183454"

    src.search_detailed("yugioh PSA", pages=1, category="1234,5678")
    assert captured["category_ids"] == "1234,5678"


# ---------------------------------------------------------------------------
# 3. pipeline 的接線：**這是原 bug 的所在地**
# ---------------------------------------------------------------------------
class _SpySource:
    """有 `search_detailed` 的 source（Buyee/Yahoo/PayPay/eBay 都是這一類）。"""

    site = Site.BUYEE_MERCARI

    def __init__(self, supports_category: bool = True) -> None:
        self.supports_category = supports_category
        self.calls: list[dict] = []

    def search_detailed(self, keyword, **kw):
        from ygo_sniper.sources.health import SearchResult

        self.calls.append({"keyword": keyword, **kw})
        return SearchResult(source="spy", site=self.site.value, query=keyword)


def test_category_reaches_search_detailed():
    """原 bug：category 只傳給舊介面，有 `search_detailed` 的來源整個收不到。"""
    src = _SpySource()
    run_source_search("spy", src, "PSA 初期", pages=1, category="1152")
    assert src.calls[0]["category"] == "1152"


def test_category_is_dropped_loudly_when_source_cannot_use_it(capsys):
    """不支援的來源不該 TypeError（會被隔離成假的 parser_broken），但要印警告。"""
    src = _SpySource(supports_category=False)
    res = run_source_search("spy", src, "PSA 初期", pages=1, category="1152")
    assert "category" not in src.calls[0]
    assert res.health.value == "ok"
    assert "不支援分類參數" in capsys.readouterr().out


def test_optional_kwargs_never_passes_empty_category():
    """空字串／None 不該變成 `category=''`——那會讓 URL 多一個空參數。"""
    src = _SpySource()
    assert _optional_kwargs("spy", src, category=None, sort=None) == {}
    assert _optional_kwargs("spy", src, category="", sort=None) == {}
    assert _optional_kwargs("spy", src, category="1152", sort="newest") == {
        "category": "1152", "sort": "newest",
    }


# ---------------------------------------------------------------------------
# 4. 真的那份 watchlist：分類別名一定解得出值
# ---------------------------------------------------------------------------
def test_real_watchlist_categories_all_resolve(cfg):
    """設定檔裡寫了 `category:` 卻解不出值 = 那條 query 靜默退化成純關鍵字。"""
    wl = cfg.watchlist
    for q in load_queries(wl):
        if not q.category:
            continue
        resolved = {s: resolve_category(wl, s, q.category) for s in q.sources}
        assert all(resolved.values()), (
            f"query {q.name!r} 的分類別名 {q.category!r} 在 {resolved} 裡有解不出的來源"
        )


def test_real_watchlist_stays_within_request_budget(cfg):
    """請求預算是硬約束：條目 ≤ 12、每輪 (query, source) 請求數 ≤ 21。

    排程一天跑很多輪，每條 (query, source) 每輪都是一個對外請求（另外還有
    canary 與 Yahoo 的第二趟，那兩個不在這裡算——它們不隨 watchlist 變動）。
    這道測試不是品味問題：它是「加查詢之前先跑 `ygo-sniper recall-study`」
    那條紀律的守衛。要調高上限，請先附一份研究報告說明多的請求換到了什麼。
    """
    qs = load_queries(cfg.watchlist)
    requests = sum(len(q.sources) for q in qs)
    assert len(qs) <= 12, f"query 條目 {len(qs)} 超過上限 12"
    assert requests <= 21, f"每輪請求數 {requests} 超過上限 21"


def test_query_spec_label_is_readable():
    assert QuerySpec("PSA 初期", "PSA 初期", ("x",), "yugioh").label == "PSA 初期+yugioh"
    assert QuerySpec("PSA 初期", "PSA 初期", ("x",)).label == "PSA 初期"
