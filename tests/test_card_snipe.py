"""指定卡狙擊：store CRUD、比對 tier、pipeline 掛鉤、CLI。"""
from __future__ import annotations

import json

import pytest

from ygo_sniper import store as store_mod
from ygo_sniper.card_snipe import (
    TIER_EXACT,
    TIER_NEAR,
    TIER_PARTIAL,
    WatchMatcher,
    load_matchers,
    match_tier,
    observe_listings,
    scan_queries,
)
from ygo_sniper.store import CARD_SNIPE_RULE, Store


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
        # 2026-07-01 ¥6,350 真實結標標題
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

    def test_the_other_real_sold_title_is_exact(self, matcher):
        # 2026-05-27 ¥7,750 真實結標標題（與上一筆同賣家、同樣沒有卡號）
        t = "【ARS10】魔法の筒 Magic Cylinder ウルトラ 鑑定書付 遊戯王 ARS鑑定10 PSA 芸術品"
        assert match_tier(matcher, t) == TIER_EXACT

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
        from ygo_sniper.card_snipe import classify

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
