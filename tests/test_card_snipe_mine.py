"""市場成交檔案挖掘：市場的檔案才是資料庫，我們的庫是它的記憶體。"""
from __future__ import annotations

import pytest

from ygo_sniper.card_snipe import WatchMatcher, mine_sold_archive
from ygo_sniper.domain import Currency, Listing, Site
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store

WATCH_KW = dict(
    grader="ARS", grade=10.0, grade_label="10",
    name_ja="魔法の筒", name_en="Magic Cylinder",
    aliases=["マジック・シリンダー"], code_raw="P4-06", code_norm="P4-6",
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def _sold(ext_id, title, price, sold_at, *, bids=1, fixed=False, seller="S1"):
    return Listing(
        site=Site.BUYEE_YAHOO, external_id=ext_id, title=title,
        url=f"https://buyee.jp/item/yahoo/auction/{ext_id}",
        price=float(price), currency=Currency.JPY, seller_id=seller, is_sold=True,
        source="yahoo_closed",
        origin_url=f"https://page.auctions.yahoo.co.jp/jp/auction/{ext_id}",
        raw={"sold_at": sold_at, "bid_count": bids, "is_fixed_price": fixed,
             "price_kind": "sold_price"},
    )


class FakeSource:
    """`_sold_search` 只需要 search_detailed 同形。"""

    name = "yahoo_closed"
    site = Site.BUYEE_YAHOO
    supports_sold = True

    def __init__(self, listings, health=ParseHealth.OK):
        self.listings = listings
        self.health = health
        self.queries: list[str] = []

    def search_detailed(self, keyword, *, sold=False, pages=1, **kw):
        self.queries.append(keyword)
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=list(self.listings), parsed_count=len(self.listings),
            health=self.health, pages_fetched=pages,
        )


REAL = [
    # 落札檔案的四筆 ARS 命中（2026-08-09 實測原文）
    _sold("n1235105710", "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
          6350, "2026-07-01T13:53:03+00:00", bids=15, seller="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"),
    _sold("l1230920412", "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
          7750, "2026-05-27T13:27:38+00:00", bids=10, seller="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"),
    _sold("x111", "【ARS10】世界に2枚 魔法の筒 Magic cylinder 限定品 プリズマティック 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
          4600, "2026-07-08T12:00:00+00:00", bids=30),
    _sold("x222", "ARS10　遊戯王　ブラックマジシャンガール 25th　魔法の筒　WCS 2023　封筒　鑑定書付き プリズマ プリシク",
          168150, "2026-06-03T12:00:00+00:00", bids=1, fixed=True),
    # 同卡他家鑑定與未鑑定：入帳但不是通知級
    _sold("y1", "PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期", 1900,
          "2026-04-01T12:00:00+00:00", fixed=True),
    # 完全無關：不進帳
    _sold("z1", "遊戯王 青眼の白龍 初期 PSA10", 99999, "2026-04-02T12:00:00+00:00"),
]


def _undated(ext_id, title, price):
    """buyee_mercari／ruten 的搜尋頁**沒有落札時刻**——`raw` 是空 dict
    （2026-08-09 實測：97＋2 筆全都如此）。這裡刻意不塞任何假日期。"""
    return Listing(
        site=Site.BUYEE_YAHOO, external_id=ext_id, title=title,
        url=f"https://buyee.jp/item/mercari/m/{ext_id}",
        price=float(price), currency=Currency.JPY, seller_id="", is_sold=True,
        source="buyee_mercari", raw={},
    )


UNDATED = [
    _undated("m1", "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10", 6800),
    _undated("m2", "ARS10 魔法の筒 初期 ウルトラレア 遊戯王", 7200),
    _undated("m3", "PSA9 遊戯王 魔法の筒 P4-06 第2期", 2100),
]


class TestMineSoldArchive:
    def test_mines_and_classifies_the_real_archive(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = FakeSource(REAL)
        res = mine_sold_archive(store, {"yahoo_closed": src}, m)

        assert res.ok is True
        assert res.new_sales == 5           # 6 筆裡「青眼の白龍」不相關，不入帳
        assert res.undated_sales == 0       # REAL 這批全都有真實落札時刻，不得誤報
        # ⚠️ 這條同時釘住「跨關鍵字去重」：兩個關鍵字（日文名＋英文名）各跑一次查詢、
        #    各回同一份清單，沒有去重的話每個數字都會變兩倍。實測真實檔案：
        #    未去重 exact 4／partial 3，去重後 exact 2／partial 2。
        assert res.tier_counts == {"exact": 2, "partial": 2, "near": 1}
        assert len(res.queries) == 2        # 確實打了兩次查詢（不是靠少查來避免重複）
        sales = store.list_card_watch_sales(wid)
        assert len(sales) == 5
        exact = [s for s in sales if s["tier"] == "exact"]
        assert {s["price_native"] for s in exact} == {6350.0, 7750.0}
        assert all(s["seller_id"] == "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx" for s in exact)
        assert all(s["sale_kind"] == "auction" for s in exact)
        assert {s["sold_at"][:10] for s in exact} == {"2026-07-01", "2026-05-27"}

    def test_fixed_price_sale_kind_is_from_the_flag_not_bid_count(self, store):
        """フリマ 定價成交的 bidCount 也是 1（佔位值）——型態只看 is_fixed_price。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        mine_sold_archive(store, {"yahoo_closed": FakeSource(REAL)}, m)
        wcs = [s for s in store.list_card_watch_sales(wid)
               if s["price_native"] == 168150.0][0]
        assert wcs["sale_kind"] == "fixed" and wcs["bid_count"] == 1

    def test_queries_card_names_only_never_grader_terms(self, store):
        """伺服器端多一個鑑定詞 ＝ AND 過濾 ＝ 靜默誤殺（只寫 ARS鑑定10 的賣家就消失）。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = FakeSource(REAL)
        mine_sold_archive(store, {"yahoo_closed": src}, m)
        assert src.queries == ["魔法の筒", "Magic Cylinder"]
        assert all("ARS" not in q and "PSA" not in q for q in src.queries)

    def test_remining_is_idempotent_and_counts_only_new(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = {"yahoo_closed": FakeSource(REAL)}
        first = mine_sold_archive(store, src, m)
        again = mine_sold_archive(store, src, m)
        assert first.new_sales == 5 and again.new_sales == 0
        assert again.total_sales == 5
        assert len(store.list_card_watch_sales(wid)) == 5

    def test_blocked_source_is_loud_and_not_reported_as_zero(self, store):
        """0 筆有兩種讀法：真的沒賣過／被擋。分不出來就是靜默失敗。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = FakeSource([], health=ParseHealth.BLOCKED)
        res = mine_sold_archive(store, {"yahoo_closed": src}, m)
        assert res.ok is False
        assert res.new_sales == 0
        assert any("BLOCKED" in p or "被擋" in p for p in res.problems)

    def test_undated_sales_are_counted_and_surfaced(self, store):
        """Mercari／露天的搜尋頁給不出落札時刻。**留著它們**（賣過、但不知何時
        仍是有用的價格資訊），但缺口必須看得見——否則無日期的列會混進
        `ORDER BY sold_at` 的清單與「多久出現一次」的計數，變成拿兩種基準
        合成一個數字（CLAUDE.md 第三節；comps 的 sold_at_is_ingest 同一個坑）。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        res = mine_sold_archive(store, {"yahoo_closed": FakeSource(UNDATED)}, m)

        assert res.undated_sales == 3          # 三筆都沒有真實成交時刻
        assert res.total_sales == 3            # 但一筆都沒被丟掉
        assert all(s["sold_at"] == "" for s in store.list_card_watch_sales(wid))
        text = res.summary()
        assert "3" in text
        assert "成交時刻" in text or "不知何時" in text

    def test_undated_sales_never_leak_into_the_date_span(self, store):
        """混一批有日期、一批沒日期：`oldest`／`newest` 只能由**有日期的那批**決定。
        少了這道守衛，空字串會排到最前面成為 `oldest`，「涵蓋」區間就變成
        兩種基準合出來的數字——而且錯得很安靜（TEXT 字典序，'' < 任何日期）。"""
        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        res = mine_sold_archive(store, {"yahoo_closed": FakeSource(REAL + UNDATED)}, m)

        assert res.undated_sales == 3
        assert res.total_sales == 8                 # 5 筆有日期 ＋ 3 筆無日期
        assert res.oldest[:10] == "2026-04-01"      # REAL 裡最舊的那筆
        assert res.newest[:10] == "2026-07-08"      # REAL 裡最新的那筆

    def test_source_without_sold_support_is_skipped(self, store):
        class NoSold(FakeSource):
            supports_sold = False

        wid = store.insert_card_watch(**WATCH_KW)
        m = WatchMatcher.from_row(store.get_card_watch(wid))
        src = NoSold(REAL)
        res = mine_sold_archive(store, {"nosold": src}, m)
        assert src.queries == [] and res.total_sales == 0
