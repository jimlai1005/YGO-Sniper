"""關鍵字搜尋（market_search.py）的零網路測試。

這個檔案釘住四件「錯了會看起來像成功」的事：

1. **漏斗數字**：搜到 N 筆卻只列 K 筆時，丟掉的原因必須留下來。
   沒有漏斗，使用者只會覺得工具很爛；有漏斗，他才知道是篩選器在工作。
2. **排序規則**：預算內優先 → P(值得買) 降冪 → 到手成本升冪。
   超預算的**不丟掉**（使用者要看預算外有什麼），但一律排後面。
3. **兩個入口同一套判準**：同一個標的走 /api/appraise 與 /api/search
   必須得到同一個判決。判準分岔的話兩邊的數字就都不能信。
4. **來源隔離**：一條管道炸掉只污染自己那格，其他來源照常出結果。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FakeFx

from ygo_sniper import appraise as appraise_mod
from ygo_sniper import market_search as ms
from ygo_sniper.appraise import appraise
from ygo_sniper.cards import CardIndex
from ygo_sniper.domain import Currency, Listing, Site
from ygo_sniper.market_search import VIEW_MIXED, VIEW_VENUE, search_market, sort_key
from ygo_sniper.sources.base import BlockedError
from ygo_sniper.sources.health import ParseHealth, SearchResult

FIXTURES = Path(__file__).parent / "fixtures"
YAHOO_BUYOUT = (FIXTURES / "yahoo_item_buyout.html").read_text(encoding="utf-8")
BUYOUT_ID = "s1238539612"
BUYOUT_TITLE = "ARS10 遊戯王 はにわ 初期 Vol.1 PSA10相当"  # 與 fixture 商品頁逐字相同
BUYOUT_PRICE_JPY = 30_000.0


# ---------------------------------------------------------------------------
# 測試替身
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def comps_by(self, **_kw):
        return list(self._rows)


class FakeSource:
    """回一份固定 SearchResult 的假來源（或直接炸掉，測隔離）。"""

    def __init__(self, name, site, listings=(), *, health=ParseHealth.OK,
                 parsed=None, boom: Exception | None = None):
        self.name, self.site = name, site
        self._listings = list(listings)
        self._health = health
        self._parsed = len(self._listings) if parsed is None else parsed
        self._boom = boom
        self.calls: list[dict] = []

    def search_detailed(self, keyword, *, max_price=None, pages=None):
        self.calls.append({"keyword": keyword, "max_price": max_price, "pages": pages})
        if self._boom:
            raise self._boom
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=list(self._listings), health=self._health,
            parsed_count=self._parsed, url=f"https://example.test/{self.name}",
        )


def listing(site, ext_id, title, price, **kw) -> Listing:
    return Listing(
        site=site,
        external_id=ext_id,
        title=title,
        url=f"https://buyee.jp/x/{ext_id}",
        price=price,
        currency=Currency.JPY,
        source=kw.pop("source", "test"),
        raw=kw.pop("raw", {}),
        **kw,
    )


#: 卡表只有一張「はにわ」（1999 首發），比對得到才有 L1/L2 分層
INDEX = CardIndex(
    [{"id": 1, "name_ja": "はにわ", "name_en": "Hane-Hane", "ocg_date": "1999-04-01"}], {}
)


def comps_rows(n=200, price=900.0, site="buyee_yahoo"):
    """同卡同稀有度同分數的成交列。200 筆才過得了 min_calibration=50，
    區間與 P(值得買) 才會真的算出來（否則判決永遠停在資料不足）。"""
    return [
        {
            "id": i,
            "title": f"ARS10 遊戯王 はにわ 初期 Vol.1 #{i}",
            "price_twd": price + (i % 11) * 20,
            "rarity": None,
            "grade": 10.0,
            "card_name": "はにわ",
            "site": site,
            "grader": "ARS",
            "url": f"https://buyee.jp/mercari/item/m{i}",
            "sold_at": f"2026-07-{i % 28 + 1:02d}",
            "era_evidence": "jp_kw:初期",
        }
        for i in range(n)
    ]


def run(cfg, registry, *, rows=None, budget=1200.0, sources=None):
    return search_market(
        cfg,
        "遊戯王",
        registry=registry,
        store=FakeStore(comps_rows() if rows is None else rows),
        fx=FakeFx(),
        index=INDEX,
        budget_twd=budget,
        sources=tuple(sources or registry.keys()),
    )


# ---------------------------------------------------------------------------
# 1. 漏斗
# ---------------------------------------------------------------------------
def test_funnel_counts_and_reasons(cfg):
    """抓 4 筆 → 只有 1 筆符合，其餘三筆的丟棄原因逐條統計。"""
    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [
        listing(Site.BUYEE_YAHOO, "a1", "ARS10 遊戯王 はにわ 初期 Vol.1", 900),   # 收
        listing(Site.BUYEE_YAHOO, "a2", "遊戯王 25th プリシク PSA10", 900),      # 排除字
        listing(Site.BUYEE_YAHOO, "a3", "遊戯王 現代カード PSA10", 900),         # 無年代證據
        listing(Site.BUYEE_YAHOO, "a4", "遊戯王 初期 ブラマジ 美品", 900),        # 無鑑定機構
    ], parsed=50)
    res = run(cfg, {"yahoo_direct": src})

    assert res["funnel"]["fetched"] == 4
    assert res["funnel"]["parsed"] == 50      # 解析器解出 50 個，商業篩選後才剩 4 筆
    assert res["funnel"]["candidates"] == 1
    assert res["funnel"]["listed"] == 1
    reasons = res["funnel"]["rejected"]
    assert reasons["排除字 25th"] == 1
    assert reasons["無 1998-2004 年代證據"] == 1
    assert reasons["未偵測到鑑定機構"] == 1
    assert sum(reasons.values()) == 3, "每一筆丟掉的都要有原因，不能憑空消失"


def test_source_report_shows_timing_and_health(cfg):
    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [], parsed=50)
    rep = run(cfg, {"yahoo_direct": src})["sources"][0]

    assert rep["health"] == "ok" and rep["ok"] is True
    assert rep["parsed_count"] == 50 and rep["listings"] == 0
    assert rep["elapsed_seconds"] >= 0
    assert rep["site_label"]


def test_search_never_applies_a_price_ceiling(cfg):
    """預算是使用者的判斷，不在平台側先砍——砍掉的東西再也救不回來，
    而使用者明說要看得到預算外有什麼。"""
    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [])
    run(cfg, {"yahoo_direct": src}, budget=300)

    assert src.calls == [{"keyword": "遊戯王", "max_price": None, "pages": 1}]


# ---------------------------------------------------------------------------
# 2. 來源隔離
# ---------------------------------------------------------------------------
def test_one_broken_source_does_not_kill_the_others(cfg):
    ok = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [
        listing(Site.BUYEE_YAHOO, "a1", "ARS10 遊戯王 はにわ 初期 Vol.1", 900),
    ])
    dead = FakeSource(
        "buyee_mercari", Site.BUYEE_MERCARI,
        boom=BlockedError("WAF", url="https://buyee.jp/mercari/search"),
    )
    res = run(cfg, {"yahoo_direct": ok, "buyee_mercari": dead})

    health = {r["source"]: r["health"] for r in res["sources"]}
    assert health == {"yahoo_direct": "ok", "buyee_mercari": "blocked"}
    assert len(res["items"]) == 1, "一條管道被擋不該讓另一條的結果消失"


# ---------------------------------------------------------------------------
# 3. 排序與預算
# ---------------------------------------------------------------------------
def _item(key, p, landed, over):
    return {"key": key, "landed_twd": landed, "over_budget": over,
            "views": {VIEW_VENUE: {"p_worth_buying": p}}}


def test_sort_key_orders_by_p_then_cost():
    items = [
        _item("cheap_bad", 0.10, 500, False),
        _item("good", 0.90, 900, False),
        _item("good_cheaper", 0.90, 700, False),
        _item("over_budget_best", 0.99, 400, True),
        _item("no_interval", None, 100, False),
    ]
    order = [i["key"] for i in sorted(items, key=lambda x: sort_key(x, VIEW_VENUE))]

    # P 高的在前；同 P 比到手成本；沒有 P 的（模型不給區間）排在有 P 的後面；
    # 超預算的無論多好都在最後。
    assert order == ["good_cheaper", "good", "cheap_bad", "no_interval", "over_budget_best"]


def test_over_budget_is_marked_not_dropped(cfg):
    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [
        listing(Site.BUYEE_YAHOO, "cheap", "ARS10 遊戯王 はにわ 初期 Vol.1", 500),
        listing(Site.BUYEE_YAHOO, "pricey", "ARS10 遊戯王 はにわ 初期 Vol.1 極美", 30_000),
    ])
    res = run(cfg, {"yahoo_direct": src}, budget=1200)

    flags = {i["key"]: i["over_budget"] for i in res["items"]}
    assert len(flags) == 2, "超預算的標的不可以被丟掉"
    assert flags["buyee_yahoo:pricey"] is True
    assert flags["buyee_yahoo:cheap"] is False
    assert res["order"][VIEW_VENUE][-1] == "buyee_yahoo:pricey"


# ---------------------------------------------------------------------------
# 4. 兩種視角（平台校正開／關）
# ---------------------------------------------------------------------------
def test_both_views_are_computed_in_one_pass(cfg):
    """切換平台校正不重抓：兩份估價一起回。

    校正與否會改變**公允價本身**（不只是排序），所以前端不可能只靠重排做到，
    必須兩份都算好。
    """
    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [
        listing(Site.BUYEE_YAHOO, "a1", "ARS10 遊戯王 はにわ 初期 Vol.1", 900),
    ])
    res = run(cfg, {"yahoo_direct": src})
    views = res["items"][0]["views"]

    assert set(views) == {VIEW_VENUE, VIEW_MIXED}
    assert views[VIEW_VENUE]["estimate"]["venue_adjusted"] is True
    assert views[VIEW_VENUE]["estimate"]["venue"] == "buyee_yahoo"
    assert views[VIEW_MIXED]["estimate"]["venue_adjusted"] is False
    assert views[VIEW_MIXED]["estimate"]["venue"] is None
    assert set(res["order"]) == {VIEW_VENUE, VIEW_MIXED}


# ---------------------------------------------------------------------------
# 5. 與 appraise 共用判決（最重要的一組）
# ---------------------------------------------------------------------------
def test_verdict_ladder_is_literally_the_appraise_one():
    """結構性保證：不是「照著寫一份一樣的」，是同一支函式。"""
    assert ms.decide_verdict is appraise_mod.decide_verdict
    assert ms.collect_comparables is appraise_mod.collect_comparables


def test_same_item_gets_the_same_verdict_from_both_entrypoints(cfg):
    """同一個標的（同 id、同標題、同即決価格）走兩個入口 → 判決與到手成本一致。

    這是使用者最容易踩到的不信任點：清單說「值得看」、貼網址說「不要買」，
    兩個數字就都不能信了。
    """
    rows = comps_rows()

    class FakeFetcher:
        def get(self, url, **_kw):
            return YAHOO_BUYOUT

        def close(self):
            pass

    report = appraise(
        cfg,
        f"https://auctions.yahoo.co.jp/jp/auction/{BUYOUT_ID}",
        store=FakeStore(rows), fetcher=FakeFetcher(), fx=FakeFx(), index=INDEX,
    )

    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [
        listing(Site.BUYEE_YAHOO, BUYOUT_ID, BUYOUT_TITLE, BUYOUT_PRICE_JPY,
                raw={"price_kind": "buyout", "current_bid": 3440}),
    ])
    res = run(cfg, {"yahoo_direct": src}, rows=rows)
    item = res["items"][0]
    view = item["views"][VIEW_VENUE]   # appraise 一律用標的自己的平台校正

    assert item["landed_twd"] == report.best_route["landed_twd"]
    assert view["verdict"] == report.verdict
    assert view["verdict_reasons"] == report.verdict_reasons
    assert view["verdict_numbers"] == report.verdict_numbers
    assert view["comparable_n"] == report.comparable_stats["n"]


def test_price_semantics_survive_into_the_list(cfg):
    """競標中的標的必須在清單上就看得出來——現在価格不是付得出去的價格。"""
    src = FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [
        listing(Site.BUYEE_YAHOO, "bid", "ARS10 遊戯王 はにわ 初期 Vol.1", 900,
                raw={"price_kind": "current_bid", "current_bid": 900}),
    ])
    item = run(cfg, {"yahoo_direct": src})["items"][0]

    assert item["price_kind"] == "current_bid"
    assert "競標" in item["price_kind_label"]
    assert item["currency"] == "JPY"


@pytest.mark.parametrize("missing", ["not_a_source"])
def test_unknown_source_is_reported_not_crashed(cfg, missing):
    res = run(cfg, {"yahoo_direct": FakeSource("yahoo_direct", Site.BUYEE_YAHOO, [])},
              sources=["yahoo_direct", missing])
    assert [r["health"] for r in res["sources"]] == ["ok", "missing"]
