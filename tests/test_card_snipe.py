"""指定卡狙擊：store CRUD、比對 tier、pipeline 掛鉤、CLI。"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from ygo_sniper import store as store_mod
from ygo_sniper.ars_census import SEARCH_URL
from ygo_sniper.card_snipe import (
    TIER_EXACT,
    TIER_NEAR,
    TIER_PARTIAL,
    WatchMatcher,
    add_card_watch,
    build_dossier,
    build_notify_context,
    classify,
    load_matchers,
    match_tier,
    observe_listings,
    scan_queries,
)
from ygo_sniper.store import CARD_SNIPE_RULE, Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


WATCH_KW = dict(
    grader="ARS", grade=10.0, grade_label="10",
    name_ja="魔法の筒", name_en="Magic Cylinder",
    aliases=["マジック・シリンダー"], code_raw="P4-06", code_norm="P4-6",
)

WATCH_ROW = {
    "id": 1, "grader": "ARS", "grade": 10.0, "grade_label": "10",
    "name_ja": "魔法の筒", "name_en": "Magic Cylinder",
    "aliases": '["マジック・シリンダー"]', "code_raw": "P4-06", "code_norm": "P4-6",
}


@pytest.fixture
def matcher():
    return WatchMatcher.from_row(WATCH_ROW)


class TestCardWatchStore:
    def test_insert_and_list_roundtrip(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        rows = store.list_card_watch(active_only=True)
        assert len(rows) == 1 and rows[0]["id"] == wid
        w = rows[0]
        assert w["grader"] == "ARS" and w["grade"] == 10.0 and w["grade_label"] == "10"
        assert w["code_norm"] == "P4-6"
        assert json.loads(w["aliases"]) == ["マジック・シリンダー"]
        assert w["active"] == 1 and w["added_at"]

    def test_deactivate_is_soft_delete(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        assert store.deactivate_card_watch(wid) is True
        assert store.deactivate_card_watch(wid) is False          # 已經不在了
        assert store.list_card_watch(active_only=True) == []
        rows = store.list_card_watch(active_only=False)
        assert rows[0]["active"] == 0 and rows[0]["removed_at"]

    def test_census_update(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.update_card_watch_census(
            wid, census_url="https://ars-grading.com/x",
            census_json='{"9": 5, "10": 5, "10+": 1}', census_total=11,
        )
        w = store.get_card_watch(wid)
        assert json.loads(w["census_json"])["10+"] == 1
        assert w["census_total"] == 11 and w["census_fetched_at"]

    def test_census_update_reports_missing_watch(self, store):
        """存到不存在的列必須說出來——靜默無作用與「存好了」外顯相同。"""
        wid = store.insert_card_watch(**WATCH_KW)
        assert store.update_card_watch_census(
            wid, census_url="u", census_json="{}", census_total=1) is True
        assert store.update_card_watch_census(
            wid + 999, census_url="u", census_json="{}", census_total=1) is False

    def test_hit_upsert_is_idempotent_and_updates_last_seen(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        kw = dict(tier="exact", title="t", url="u", site="buyee_yahoo",
                  seller_id="s1", price_native=6350.0, currency="JPY")
        store.upsert_card_watch_hit(wid, "buyee_yahoo:x1", now="2026-08-09T01:00:00+00:00", **kw)
        store.upsert_card_watch_hit(wid, "buyee_yahoo:x1", now="2026-08-09T02:00:00+00:00", **kw)
        hits = store.list_card_watch_hits(watch_id=wid)
        assert len(hits) == 1
        assert hits[0]["first_seen"] == "2026-08-09T01:00:00+00:00"
        assert hits[0]["last_seen"] == "2026-08-09T02:00:00+00:00"
        assert hits[0]["sent_at"] is None                          # notify_log join，還沒送過

    def test_hit_sent_at_comes_from_notify_log(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_hit(wid, "buyee_yahoo:x1", tier="exact", title="t",
                                    url="u", site="buyee_yahoo", seller_id="",
                                    price_native=None, currency="")
        store.mark_rule_notified([(f"{wid}:buyee_yahoo:x1", "card_snipe")])
        hits = store.list_card_watch_hits(watch_id=wid)
        assert hits[0]["sent_at"] is not None

    def test_sent_at_join_uses_the_named_rule_constant(self, store):
        """join 的 rule 名不是 SQL 裡的字面量——漂移時 `sent_at` 會恆為 NULL，
        而「每輪重複推播」與「這筆真的還沒送過」外顯一模一樣（CLAUDE.md 第五節）。"""
        assert CARD_SNIPE_RULE == "card_snipe"
        assert "'card_snipe'" not in store_mod._SCHEMA  # 政策名不埋進 schema
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_hit(wid, "k1", tier="exact", title="t", url="u",
                                    site="s", seller_id="", price_native=None,
                                    currency="")
        store.mark_rule_notified([(f"{wid}:k1", CARD_SNIPE_RULE)])
        assert store.list_card_watch_hits(watch_id=wid)[0]["sent_at"] is not None
        # 別的 rule 落帳不得被誤認成狙擊已送
        store.upsert_card_watch_hit(wid, "k2", tier="exact", title="t", url="u",
                                    site="s", seller_id="", price_native=None,
                                    currency="")
        store.mark_rule_notified([(f"{wid}:k2", "seller_new")])
        by_key = {h["listing_key"]: h for h in store.list_card_watch_hits(watch_id=wid)}
        assert by_key["k2"]["sent_at"] is None

    def test_both_sold_at_columns_declare_the_utc_basis(self):
        """兩張表存的是同一類事實（什麼時候賣掉的），而 `ORDER BY` 是 TEXT
        字典序——混入不同時區偏移就排錯（CLAUDE.md 第三節）。基準必須寫在欄位上。"""
        for table in ("card_watch_sale", "card_watch_evidence"):
            block = store_mod._SCHEMA.split(f"CREATE TABLE IF NOT EXISTS {table}")[1]
            block = block.split(");")[0]
            sold_at_line = next(ln for ln in block.splitlines() if "sold_at" in ln)
            assert "UTC" in sold_at_line, f"{table}.sold_at 沒有宣告時間基準"

    def test_prune_only_touches_old_near(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        old = "2020-01-01T00:00:00+00:00"
        for key, tier, now in (("a:1", "near", old), ("a:2", "exact", old), ("a:3", "near", None)):
            store.upsert_card_watch_hit(wid, key, tier=tier, title="t", url="u",
                                        site="a", seller_id="", price_native=None,
                                        currency="", now=now)
        assert store.prune_card_watch_hits(90, tier="near") == 1   # 只清舊的 near
        left = {h["listing_key"] for h in store.list_card_watch_hits(watch_id=wid)}
        assert left == {"a:2", "a:3"}

    def test_evidence_upsert_and_list(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_evidence(
            wid, "https://example.test/a", status="ok", title="t",
            price_native=6350.0, sold_at="2026-07-01T22:53:03+09:00",
            bids=15, seller_id="S", seller_name="Natural Cards",
        )
        store.upsert_card_watch_evidence(wid, "https://example.test/a", status="ok",
                                         title="t2", price_native=6350.0)
        ev = store.list_card_watch_evidence(wid)
        assert len(ev) == 1 and ev[0]["title"] == "t2"             # 同 URL 更新不重複

    def test_sale_upsert_reports_new_then_not_new(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        kw = dict(tier="exact", title="【ARS10】魔法の筒", url="u", site="buyee_yahoo",
                  seller_id="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx", price_native=6350.0,
                  currency="JPY", sold_at="2026-07-01T13:53:03+00:00",
                  bid_count=15, sale_kind="auction")
        assert store.upsert_card_watch_sale(wid, "buyee_yahoo:n1", **kw) is True
        assert store.upsert_card_watch_sale(wid, "buyee_yahoo:n1", **kw) is False
        sales = store.list_card_watch_sales(wid)
        assert len(sales) == 1 and sales[0]["bid_count"] == 15
        assert sales[0]["sale_kind"] == "auction"

    def test_sale_new_flag_does_not_depend_on_the_timestamp(self, store):
        """呼叫端常帶 run 級的固定 `now=`——用時間戳比較判新舊會兩次都說「新」，
        直接高報新成交數。存在性才是判準（工程原則 1：判準要問到機制那一層）。"""
        wid = store.insert_card_watch(**WATCH_KW)
        kw = dict(tier="exact", title="t", url="u", site="buyee_yahoo",
                  seller_id="S", price_native=1.0, currency="JPY",
                  sold_at="2026-07-01T00:00:00+00:00", bid_count=1,
                  sale_kind="auction", now="2026-08-09T00:00:00+00:00")
        assert store.upsert_card_watch_sale(wid, "s:1", **kw) is True
        assert store.upsert_card_watch_sale(wid, "s:1", **kw) is False

    def test_sale_seller_id_is_filled_never_wiped(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        base = dict(tier="exact", title="t", url="u", site="buyee_yahoo",
                    price_native=1.0, currency="JPY", sold_at="2026-07-01T00:00:00+00:00",
                    bid_count=1, sale_kind="auction")
        store.upsert_card_watch_sale(wid, "s:1", seller_id="SELLER", **base)
        store.upsert_card_watch_sale(wid, "s:1", seller_id="", **base)   # 之後挖到沒賣家
        assert store.list_card_watch_sales(wid)[0]["seller_id"] == "SELLER"

    def test_title_rows_accessors_exist(self, store):
        assert store.comps_title_rows() == []
        assert store.listing_obs_title_rows() == []


class TestMatchTier:
    def test_exact_on_the_real_sold_title(self, matcher):
        # 這一個標題對應**兩筆**真實成交（2026-07-01 ¥6,350 與 2026-05-27 ¥7,750，
        # 同賣家、同刊登模板，標題逐位元組相同），兩筆都沒有卡號 P4-06——
        # 真標的靠的是「卡名＋機構分數」，這正是現代版標記不能降到不推播的理由。
        t = "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_EXACT

    def test_exact_via_code_without_name(self, matcher):
        assert match_tier(matcher, "遊戯王 ARS10 P4-06 ウルトラ") == TIER_EXACT

    def test_exact_via_katakana_alias_without_nakaten(self, matcher):
        # 中点なし也要中：fold 會把中点丟掉、片假名折平假名
        assert match_tier(matcher, "ARS10 マジックシリンダー 初期") == TIER_EXACT

    def test_ars10_plus_is_exact_by_design(self, matcher):
        # parse_grade 把 10+ 折成 10.0（既定行為）；10+ 全球只有 1 張、比 10 更稀，通知是對的
        assert match_tier(matcher, "ARS10+ 魔法の筒 P4-06") == TIER_EXACT

    def test_rush_duel_same_name_is_demoted_to_partial_still_notified(self, matcher):
        # 現代版只降到 👀（照樣推播）——降到不推播的話，詞表寫錯一個字就靜默漏標的
        t = "【ARS10】世界に1枚 魔法の筒 Magic Cylinder ラッシュデュエル 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_PARTIAL

    def test_real_prismatic_false_positive_is_partial(self, matcher):
        # 2026-07-08 ¥4,600 落札檔案實例：ARS10＋魔法の筒，但是現代 プリズマティック
        t = "【ARS10】世界に2枚 魔法の筒 Magic cylinder 限定品 プリズマティック 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_PARTIAL

    def test_real_wcs_bundle_false_positive_is_partial(self, matcher):
        # 2026-06-03 ¥168,150 落札檔案實例：魔法の筒 只是同捆物之一，且是 25th/WCS
        t = "ARS10　遊戯王　ブラックマジシャンガール 25th　魔法の筒　WCS 2023　封筒　鑑定書付き プリズマ プリシク"
        assert match_tier(matcher, t) == TIER_PARTIAL

    def test_code_beats_modern_marker(self, matcher):
        # 卡號是決定性證據：現代版不會印 P4-06，所以標記詞不能推翻它
        assert match_tier(matcher, "ARS10 魔法の筒 P4-06 プリズマティック") == TIER_EXACT

    def test_classify_explains_every_demotion(self, matcher):
        tier, why = classify(matcher, "PSA8 遊戯王　魔法の筒　P4-06　第２期")
        assert tier == TIER_NEAR and "PSA" in why and "ARS" in why
        tier, why = classify(matcher, "【ARS10】魔法の筒 プリズマティック")
        assert tier == TIER_PARTIAL and "現代版" in why

    def test_grader_without_score_is_partial(self, matcher):
        # parse_grade('…ARS鑑定品…') → (ARS, None)：機構對、分數不明 → 👀 通知
        assert match_tier(matcher, "魔法の筒 ARS鑑定品 遊戯王") == TIER_PARTIAL

    def test_same_grader_wrong_grade_is_partial(self, matcher):
        assert match_tier(matcher, "ARS9 魔法の筒 P4-06") == TIER_PARTIAL

    def test_other_code_only_is_partial(self, matcher):
        # 機構分數全符、但標題只明示別張卡號（同捆／別版本）→ 降半級仍通知
        assert match_tier(matcher, "ARS10 魔法の筒 LON-104") == TIER_PARTIAL

    def test_psa_copy_is_near(self, matcher):
        # comps 裡的真實 PSA8 標題（全形空白）
        assert match_tier(matcher, "PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期") == TIER_NEAR

    def test_ungraded_raw_card_is_near(self, matcher):
        assert match_tier(matcher, "遊戯王 魔法の筒 P4-06 ウルトラレア") == TIER_NEAR

    def test_unrelated_title_is_none(self, matcher):
        assert match_tier(matcher, "PSA10 ブラック・マジシャン 初期") is None

    def test_ars_target_with_psa_claim_is_partial_not_near(self, matcher):
        """賣家慣用寫法：ARS 鑑定品，標題再宣稱「相當於 PSA10 以上」。

        `以上` 不在 `parsers/grade.py` 的 `_CLAIM_SUFFIX`，而 PSA 的 pattern 排在
        ARS 前面，所以 `parse_grade` 回 PSA。直接判 near ＝ 靜默漏掉目標卡
        （CLAUDE.md 第一節）。實測 data/sniper.db 3,239 個標題：998 筆自己寫了
        ARS＋分數，其中 79 筆（7.9%）被讀成 PSA。
        """
        tier, why = classify(matcher, "【ARS10】魔法の筒 Magic Cylinder ARS鑑定10 PSA10以上")
        assert tier == TIER_PARTIAL and "ARS" in why and "PSA" in why

    def test_real_corpus_ars_title_read_as_psa_is_not_near(self):
        # data/sniper.db 的真實標題（那 79 筆之一的形狀）
        m = WatchMatcher.from_row({
            **WATCH_ROW, "grade": 7.0, "grade_label": "7",
            "name_ja": "ブラックマジシャン", "name_en": "", "aliases": "[]",
            "code_raw": "", "code_norm": "",
        })
        t = "【ARS7】ブラックマジシャン　初期ウルトラレア　vol.1 PSA7以上"
        assert match_tier(m, t) == TIER_PARTIAL

    def test_psa_only_title_stays_near_even_with_the_claim_guard(self, matcher):
        # 反向守衛：標題完全沒有 ARS token 時，寬容條款不得把 near 拉成 partial
        assert match_tier(matcher, "PSA8 遊戯王　魔法の筒　P4-06　第２期") == TIER_NEAR

    def test_code_hit_does_not_bypass_the_grader_gate(self, matcher):
        # docstring 的順序就是實作的順序：卡號命中**不會**跳過機構閘門。
        # 這筆有 ARS token 所以是 partial；沒有 ARS token 的純 PSA 標題仍是 near。
        assert match_tier(matcher, "【ARS10】魔法の筒 P4-06 遊戯王 PSA10以上") == TIER_PARTIAL

    def test_grader_is_normalized_to_upper(self):
        # 縱深防禦：CLI 會 upper()，但這裡零成本多一道——小寫會讓每一筆都變 near
        m = WatchMatcher.from_row({**WATCH_ROW, "grader": " ars "})
        assert m.grader == "ARS"
        assert match_tier(m, "ARS10 魔法の筒 P4-06") == TIER_EXACT

    def test_aliases_accept_a_decoded_list(self):
        # web/CLI 層可能直接傳已解碼的 list：json.loads 會拋 TypeError，
        # 被吞掉的話別名整組消失，只寫片假名的標的就靜默漏掉
        m = WatchMatcher.from_row({**WATCH_ROW, "aliases": ["マジック・シリンダー"]})
        assert match_tier(m, "ARS10 マジックシリンダー 初期") == TIER_EXACT

    def test_broken_aliases_warn_loudly(self, capsys):
        m = WatchMatcher.from_row({**WATCH_ROW, "aliases": "{壞掉的 json"})
        assert "[warn]" in capsys.readouterr().out
        assert match_tier(m, "ARS10 魔法の筒 P4-06") == TIER_EXACT   # 主名還在


def test_no_modern_marker_folds_to_empty():
    """`"" in folded` 恆為真——一個 fold 後變空的標記詞會讓**每一筆** exact 靜默降級。"""
    from ygo_sniper.card_snipe import _MODERN_FOLDED, _MODERN_MARKERS

    assert all(_MODERN_FOLDED)
    assert len(_MODERN_FOLDED) == len(_MODERN_MARKERS)


class TestObserveAndQueries:
    def test_observe_writes_hits_and_is_idempotent(self, store):
        from ygo_sniper.domain import Currency, Listing, Site

        wid = store.insert_card_watch(**WATCH_KW)
        lst = Listing(site=Site.BUYEE_YAHOO, external_id="x1",
                      title="【ARS10】魔法の筒 P4-06", url="https://example.test/x1",
                      price=50000.0, currency=Currency.JPY, seller_id="s1")
        matchers = load_matchers(store)
        assert observe_listings(store, matchers, [lst]) == 1
        assert observe_listings(store, matchers, [lst]) == 1       # 再跑：冪等
        hits = store.list_card_watch_hits(watch_id=wid)
        assert len(hits) == 1
        h = hits[0]
        assert h["tier"] == "exact" and h["listing_key"] == "buyee_yahoo:x1"
        assert h["site"] == "buyee_yahoo" and h["seller_id"] == "s1"
        assert h["price_native"] == 50000.0 and h["currency"] == "JPY"

    def test_observe_records_near_too(self, store):
        """near 不推播不是丟棄——dashboard 狙擊分頁每一筆都看得到（CLAUDE.md 第一節）。"""
        from ygo_sniper.domain import Currency, Listing, Site

        wid = store.insert_card_watch(**WATCH_KW)
        lst = Listing(site=Site.BUYEE_YAHOO, external_id="p8",
                      title="PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期",
                      url="https://example.test/p8", price=3000.0, currency=Currency.JPY)
        assert observe_listings(store, load_matchers(store), [lst]) == 1
        hits = store.list_card_watch_hits(watch_id=wid)
        assert len(hits) == 1 and hits[0]["tier"] == "near"

    def test_observe_handles_null_price_and_end_time(self, store):
        from datetime import UTC, datetime

        from ygo_sniper.domain import Currency, Listing, Site

        wid = store.insert_card_watch(**WATCH_KW)
        title = "【ARS10】魔法の筒 P4-06"
        no_price = Listing(site=Site.BUYEE_YAHOO, external_id="np", title=title,
                           url="u", price=None, currency=Currency.JPY)
        with_end = Listing(site=Site.BUYEE_YAHOO, external_id="e1", title=title,
                           url="u", price=1.0, currency=Currency.JPY,
                           end_time=datetime(2026, 8, 9, 13, 0, tzinfo=UTC))
        assert observe_listings(store, load_matchers(store), [no_price, with_end]) == 2
        by_key = {h["listing_key"]: h for h in store.list_card_watch_hits(watch_id=wid)}
        assert by_key["buyee_yahoo:np"]["price_native"] is None
        assert by_key["buyee_yahoo:np"]["end_time"] == ""      # 沒有結標時間 ≠ 0
        assert by_key["buyee_yahoo:e1"]["end_time"] == "2026-08-09T13:00:00+00:00"

    def test_one_listing_records_a_row_per_matching_watch(self, store):
        """同一筆標的可能同時是 A 卡的 🎯 與 B 卡的 near——兩本帳各自要有。"""
        from ygo_sniper.domain import Currency, Listing, Site

        a = store.insert_card_watch(**WATCH_KW)
        b = store.insert_card_watch(**{**WATCH_KW, "grader": "PSA"})
        lst = Listing(site=Site.BUYEE_YAHOO, external_id="x9",
                      title="【ARS10】魔法の筒 P4-06", url="u",
                      price=1.0, currency=Currency.JPY)
        assert observe_listings(store, load_matchers(store), [lst]) == 2
        assert [h["tier"] for h in store.list_card_watch_hits(watch_id=a)] == ["exact"]
        assert [h["tier"] for h in store.list_card_watch_hits(watch_id=b)] == ["near"]

    def test_scan_queries_reuse_base_sources(self, store):
        from ygo_sniper.queries import QuerySpec

        store.insert_card_watch(**WATCH_KW)
        base = [QuerySpec(name="q1", keyword="遊戯王 PSA", sources=("a", "b")),
                QuerySpec(name="q2", keyword="遊戯王 ARS", sources=("b", "c"))]
        qs = scan_queries(load_matchers(store), base)
        assert [q.keyword for q in qs] == ["魔法の筒", "Magic Cylinder"]
        assert all(q.sources == ("a", "b", "c") for q in qs)
        assert all(q.category is None for q in qs)
        assert scan_queries(load_matchers(store), []) == []        # base 空就不跑


class PageFetcher:
    """離線 fetcher。**沒有的頁面拋 `FetchError`，不是 KeyError**——生產用的
    `CachedFetcher` 抓不到就是 FetchError，假件也必須長成那樣，否則測試路徑
    與生產路徑分岔（CLAUDE.md 第六節），而分岔處正好是「抓不到要怎麼辦」。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kw):
        from ygo_sniper.sources.base import FetchError

        self.calls.append(url)
        if url not in self.pages:
            raise FetchError("404", url=url, status=404)
        return self.pages[url]


def _fixture_pages():
    return {
        SEARCH_URL.format(name=quote("魔法の筒")):
            (FIXTURES / "ars_search_magic_cylinder.html").read_text(encoding="utf-8"),
        "https://ars-grading.com/grading/searchNameDetail?id=001202208090020007":
            (FIXTURES / "ars_census_p4_06.html").read_text(encoding="utf-8"),
        "https://auctions.yahoo.co.jp/jp/auction/n1235105710":
            (FIXTURES / "yahoo_closed_n1235105710.html").read_text(encoding="utf-8"),
    }


class TestAddCardWatch:
    def test_full_flow_offline(self, store):
        res = add_card_watch(
            store, PageFetcher(_fixture_pages()),
            grader="ars", grade_input="10", name_ja="魔法の筒", code="P4-06",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/n1235105710"],
        )
        w = store.get_card_watch(res.watch_id)
        assert w["grader"] == "ARS" and w["grade"] == 10.0 and w["grade_label"] == "10"
        assert w["code_norm"] == "P4-6"
        assert "マジック・シリンダー" in w["aliases"]          # 主檔 enrich
        assert w["name_en"] == "Magic Cylinder"                 # 主檔補英文名
        assert json.loads(w["census_json"])["10"] == 5          # census 自動搜到＋抓到
        assert w["census_total"] == 11
        ev = store.list_card_watch_evidence(res.watch_id)
        assert len(ev) == 1 and ev[0]["status"] == "ok"
        assert ev[0]["price_native"] == 6350.0
        assert ev[0]["seller_id"] == "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
        # sources 沒給就不挖市場檔案，而且要講出來（靜默跳過＝之後查不出為什麼是空的）
        assert any("跳過市場成交檔案挖掘" in m for m in res.messages)

    def test_evidence_sold_at_shares_the_utc_basis_of_sales(self, store):
        """證據頁原文是 `+09:00`，`card_watch_sale.sold_at` 是 UTC。

        兩張表存的是同一類事實（什麼時候賣掉的），而排序是 TEXT 字典序——
        存進去的偏移不統一就會排錯，而且錯得很安靜（CLAUDE.md 第三節）。
        欄位註解已經宣告 UTC，這裡釘住**寫入端真的照做**。
        """
        res = add_card_watch(
            store, PageFetcher(_fixture_pages()),
            grader="ars", grade_input="10", name_ja="魔法の筒", code="P4-06",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/n1235105710"],
        )
        ev = store.list_card_watch_evidence(res.watch_id)[0]
        assert ev["sold_at"].endswith("+00:00"), "證據頁的 +09:00 沒被換算成 UTC"
        assert ev["sold_at"] == "2026-07-01T13:53:03+00:00"   # 頁面原文 22:53:03+09:00

    def test_bad_grader_raises(self, store):
        with pytest.raises(ValueError):
            add_card_watch(store, PageFetcher({}), grader="CGC",
                           grade_input="10", name_ja="x")

    def test_unfetchable_evidence_is_kept_loudly(self, store):
        class BoomFetcher:
            def get(self, url, **kw):
                from ygo_sniper.sources.base import FetchError
                raise FetchError("404", url=url, status=404)

        res = add_card_watch(
            store, BoomFetcher(), grader="PSA", grade_input="10", name_ja="魔法の筒",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/dead1"],
        )
        ev = store.list_card_watch_evidence(res.watch_id)
        assert ev[0]["status"] == "unverifiable"                # 讀不到 ≠ 不存在
        assert any("unverifiable" in m or "抓不到" in m for m in res.messages)

    def test_non_yahoo_evidence_is_stored_as_unsupported(self, store):
        res = add_card_watch(
            store, PageFetcher({}), grader="PSA", grade_input="10", name_ja="魔法の筒",
            evidence_urls=["https://www.ebay.com/itm/12345"],
        )
        ev = store.list_card_watch_evidence(res.watch_id)
        assert ev[0]["status"] == "unsupported"


class TestDossier:
    def test_three_buckets_stay_separate_and_recommendation_names_the_seller(self, store):
        res = add_card_watch(
            store, PageFetcher(_fixture_pages()),
            grader="ars", grade_input="10", name_ja="魔法の筒", code="P4-06",
            evidence_urls=["https://auctions.yahoo.co.jp/jp/auction/n1235105710"],
        )
        # 市場成交檔案桶：直接寫一筆（挖掘本身在 test_card_snipe_mine.py 測）
        store.upsert_card_watch_sale(
            res.watch_id, "buyee_yahoo:l1230920412", tier="exact",
            title="【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品",
            url="https://buyee.jp/item/yahoo/auction/l1230920412", site="buyee_yahoo",
            seller_id="AiUkMq1pEUfNxvPeCv5PnfGpsFLrx", price_native=7750.0,
            currency="JPY", sold_at="2026-05-27T13:27:38+00:00", bid_count=10,
            sale_kind="auction", source="yahoo_closed",
        )
        # 本地補充桶：一筆 comps（PSA8 真實標題）
        with store._conn() as c:
            c.execute(
                "INSERT INTO comps (signature, title, price_native, currency, url,"
                " site, sold_at, sale_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("sig", "PSA8 遊戯王　魔法の筒　ウルトラレア！　P4-06　第２期",
                 1900.0, "JPY", "https://example.test/c1", "buyee_mercari",
                 "2026-08-03T10:01:36Z", "fixed"),
            )
        w = store.get_card_watch(res.watch_id)
        d = build_dossier(store, w)

        assert d.census["10"] == 5 and d.census_total == 11
        # 三個桶各自獨立，沒有被合併成一個數字
        assert len(d.sales) == 1 and d.sales[0]["tier"] == "exact"
        assert d.sales[0]["sale_kind"] == "auction"
        assert len(d.evidence) == 1 and d.evidence[0]["status"] == "ok"
        assert len(d.local_history) == 1
        assert d.local_history[0]["tier"] == "near"             # PSA8 是同卡他家鑑定
        assert d.local_history[0]["ledger"] == "comps"

        joined = "\n".join(d.recommendation)
        # 賣家歸因要指名道姓、給可執行指令，並講出成交價
        assert "watch-seller pin buyee_yahoo:AiUkMq1pEUfNxvPeCv5PnfGpsFLrx" in joined
        assert "6,350" in joined and "7,750" in joined
        assert "全世界" in joined                                # census 稀缺度有講
        # 檔案期間的極限要誠實標註（不能讓使用者以為那是全部歷史）
        assert "不是全部歷史" in joined
        assert "競標" in joined                                  # 成交型態的等待策略

    def test_undated_sales_never_inflate_the_frequency_claim(self, store):
        """來源給不出落札時間的成交（Mercari／露天）**不得**進入「幾次／期間」。

        實測一次挖掘 206 筆裡有 77 筆沒有成交時刻。把它們算進次數，就是拿兩種
        基準的東西合成一個數字（CLAUDE.md 第三節；comps 的 sold_at_is_ingest
        是同一個立場）。它們照樣入帳、照樣顯示，只是不進日期類宣稱。
        """
        res = add_card_watch(
            store, PageFetcher({}), grader="ars", grade_input="10",
            name_ja="魔法の筒", code="P4-06",
        )
        common = dict(tier="exact", title="【ARS10】魔法の筒", url="u",
                      site="buyee_yahoo", seller_id="S", price_native=7000.0,
                      currency="JPY", bid_count=None, sale_kind="unknown")
        store.upsert_card_watch_sale(res.watch_id, "y:dated",
                                     sold_at="2026-05-27T13:27:38+00:00", **common)
        for i in range(3):                       # 三筆無日期（Mercari 形態）
            store.upsert_card_watch_sale(res.watch_id, f"m:{i}", sold_at="", **common)

        d = build_dossier(store, store.get_card_watch(res.watch_id))
        assert len(d.sales) == 4                 # 四筆都留著，一筆都沒丟
        joined = "\n".join(d.recommendation)
        assert "成交檔案裡 1 次" in joined        # 次數只算有日期的那一筆
        assert "3 筆" in joined and "沒給成交時刻" in joined   # 缺口要說出來
        assert "4 次" not in joined              # 絕不把無日期的算進次數

    def test_listed_hits_are_never_counted_as_sales(self, store):
        """**在架 ≠ 賣掉**，兩種基準永遠不相加（CLAUDE.md 第三節，這個專案列了六次）。

        混起來的錯誤方向照例是「看起來很划算」：使用者會把「同一件貨掛在架上很久
        沒賣掉」讀成「這個賣家供給穩定」，於是決定不必急著出手——而這個數字正是
        「我該去哪等、要不要現在動手」的依據。
        """
        res = add_card_watch(store, PageFetcher({}), grader="ars", grade_input="10",
                             name_ja="魔法の筒", code="P4-06")
        store.upsert_card_watch_sale(
            res.watch_id, "buyee_yahoo:sold1", tier="exact",
            title="【ARS10】魔法の筒", url="u", site="buyee_yahoo", seller_id="S",
            price_native=6350.0, currency="JPY",
            sold_at="2026-07-01T13:53:03+00:00", bid_count=15, sale_kind="auction",
        )
        for i in range(2):                       # 同一個賣家目前還掛著兩筆
            store.upsert_card_watch_hit(
                res.watch_id, f"buyee_yahoo:live{i}", tier="exact",
                title="【ARS10】魔法の筒", url="u", site="buyee_yahoo",
                seller_id="S", price_native=9000.0, currency="JPY")

        d = build_dossier(store, store.get_card_watch(res.watch_id))
        joined = "\n".join(d.recommendation)
        assert "賣掉過這張卡 1 次" in joined      # 賣掉的只有那一筆
        assert "3 次" not in joined              # 1 賣掉 ＋ 2 在架 ≠ 3 次成交
        assert "在架" in joined and "目前在架 2 筆" in joined   # 在架筆數看得到，不是被丟掉


class TestNotifyContext:
    def test_pending_only_contains_unsent_exact_and_partial(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        for key, tier in (("a:1", "exact"), ("a:2", "partial"), ("a:3", "near")):
            store.upsert_card_watch_hit(wid, key, tier=tier, title="t", url="u",
                                        site="a", seller_id="", price_native=None,
                                        currency="")
        store.mark_rule_notified([(f"{wid}:a:1", "card_snipe")])   # a:1 已送過
        ctx = build_notify_context(store)
        keys = [h["listing_key"] for h in ctx.pending]
        assert keys == ["a:2"]                                   # near 不進、已送不進
        assert ctx.pending[0]["watch"]["id"] == wid              # watch 附在 hit 上

    def test_inactive_watch_hits_are_excluded(self, store):
        wid = store.insert_card_watch(**WATCH_KW)
        store.upsert_card_watch_hit(wid, "a:1", tier="exact", title="t", url="u",
                                    site="a", seller_id="", price_native=None,
                                    currency="")
        store.deactivate_card_watch(wid)
        assert build_notify_context(store).pending == []
