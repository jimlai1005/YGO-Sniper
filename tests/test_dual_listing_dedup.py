"""同時出品去重（comps.dup_of_id）。

日本賣家可以把同一件實體商品同時掛在ヤフオク!（buyee_yahoo）與
Yahoo!フリマ／PayPay フリマ（buyee_paypay）——賣掉一邊，另一邊自動下架。
後果：同一筆實體成交在 comps 出現兩次，那張卡的同儕中位數被自己汙染
（同一個價格算了兩票）。

2026-08-05 實測正式庫（2,566 筆 comps）找到的真案例：`青眼の白龍` バンダイ版
ARS鑑定品，buyee_yahoo 與 buyee_paypay 各一筆，日圓價都是 16666（開始價＝
成交價）、標題逐字相同、成交時間差 26.2 分鐘。同一次全語料掃描裡，另外還有
16 組「同價格、24 小時內」的候選，全部是**不同卡剛好同價**——最近的一組
時間差 157.1 分鐘、卡名／稀有度／分數全部對不上。

判準要嚴（CLAUDE.md 第一節）：誤殺（把兩筆真的不同成交當成重複而砍掉一筆）
是靜默的，寧可漏抓幾組真重複，不要誤殺一筆真成交。所以下面的反例比正例更重要。
"""

from __future__ import annotations

import sqlite3

from ygo_sniper.comps import find_dual_listing_duplicates, mark_dual_listing_duplicates
from ygo_sniper.store import Store

REAL_TITLE = "遊戯王 青眼の白龍 バンダイ版 ARS鑑定品 カードダス ブルーアイズホワイトドラゴン プレミア"


def _row(id, site, price, sold_at, title, **extra):
    base = {
        "id": id,
        "site": site,
        "price_native": price,
        "currency": "JPY",
        "sold_at": sold_at,
        "title": title,
        "card_name": None,
        "set_code": None,
        "rarity": None,
        "grader": None,
        "grade": None,
        "url": f"https://x.test/{id}",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. 純函式：find_dual_listing_duplicates
# ---------------------------------------------------------------------------
class TestFindDualListingDuplicates:
    def test_real_case_is_detected(self):
        """實測案例：同商品同時出品，26 分鐘內同價格成交 → 判定為重複，留 yahoo。"""
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE,
                 grader="ARS"),
            _row(2, "buyee_paypay", 16666.0, "2026-03-31T14:48:13+00:00", REAL_TITLE,
                 grader="ARS"),
        ]
        matches = find_dual_listing_duplicates(rows)
        assert len(matches) == 1
        m = matches[0]
        assert (m["keep_id"], m["dup_id"]) == (1, 2)
        assert (m["keep_site"], m["dup_site"]) == ("buyee_yahoo", "buyee_paypay")
        assert m["delta_minutes"] == 26.2

    # -- 反例：比正例重要 --------------------------------------------------
    def test_different_cards_at_the_same_price_is_not_a_duplicate(self):
        """同一天同價格，但卡不一樣——這是全語料裡最常見的假候選型態
        （實測 17 組同價候選裡有 16 組長這樣）。"""
        rows = [
            _row(1, "buyee_yahoo", 30000.0, "2026-05-31T12:39:57+00:00",
                 "PSA10 真紅眼の黒竜 東映版", grader="PSA", grade=10.0),
            _row(2, "buyee_paypay", 30000.0, "2026-05-31T15:17:04+00:00",
                 "ARS10 銀幕の鏡壁 スーパーレア", grader="ARS", grade=10.0),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_cross_company_is_never_a_duplicate(self):
        """跨公司（Mercari／eBay）一律不判重複——同時出品不可能跨公司。"""
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "buyee_mercari", 16666.0, "2026-03-31T15:10:00+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_ebay_is_never_a_duplicate_partner(self):
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "ebay", 16666.0, "2026-03-31T15:10:00+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_both_sides_on_yahoo_is_not_cross_family(self):
        """同一站台兩筆不算「同時出品」——那是另一件事（補刊或真的兩件實物）。"""
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "buyee_yahoo", 16666.0, "2026-03-31T14:48:13+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_one_yen_difference_breaks_the_match(self):
        """原幣金額必須完全相同——差一元就不是同一筆（不是換算後湊巧撞上）。"""
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "buyee_paypay", 16667.0, "2026-03-31T14:48:13+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_different_currency_is_not_a_duplicate(self):
        rows = [
            _row(1, "buyee_yahoo", 100.0, "2026-03-31T15:14:26+00:00", REAL_TITLE,
                 currency="JPY"),
            _row(2, "buyee_paypay", 100.0, "2026-03-31T15:20:00+00:00", REAL_TITLE,
                 currency="TWD"),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_outside_the_time_window_is_not_a_duplicate(self):
        """真案例 26.2 分鐘、視窗訂 60 分鐘；超出視窗一律不判重（寧可漏抓）。"""
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "buyee_paypay", 16666.0, "2026-03-31T12:00:00+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_just_outside_the_window_is_rejected_at_the_boundary(self):
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:00:00+00:00", REAL_TITLE),
            _row(2, "buyee_paypay", 16666.0, "2026-03-31T16:00:01+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_different_titles_are_not_a_duplicate(self):
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "buyee_paypay", 16666.0, "2026-03-31T14:48:13+00:00", REAL_TITLE + " 美品"),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_conflicting_parsed_attributes_block_the_match(self):
        """標題正規化相同，但解析出的鑑定機構衝突——防禦性第二道檢查，不判重。"""
        rows = [
            _row(1, "buyee_yahoo", 16666.0, "2026-03-31T15:14:26+00:00", REAL_TITLE,
                 grader="ARS"),
            _row(2, "buyee_paypay", 16666.0, "2026-03-31T14:48:13+00:00", REAL_TITLE,
                 grader="PSA"),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_missing_price_or_sold_at_is_not_a_duplicate(self):
        rows = [
            _row(1, "buyee_yahoo", None, "2026-03-31T15:14:26+00:00", REAL_TITLE),
            _row(2, "buyee_paypay", 16666.0, "2026-03-31T14:48:13+00:00", REAL_TITLE),
            _row(3, "buyee_yahoo", 16666.0, None, REAL_TITLE),
            _row(4, "buyee_paypay", 16666.0, "2026-03-31T14:48:13+00:00", REAL_TITLE),
        ]
        assert find_dual_listing_duplicates(rows) == []

    def test_real_corpus_false_candidates_stay_unmatched(self):
        """2026-08-05 全語料掃描的其餘真實候選（同價、24 小時內，但卡不同）——
        釘住這幾組永遠不會被判重，防止日後有人放寬判準時悄悄回歸。"""
        rows = [
            _row(257014, "buyee_yahoo", 30000.0, "2026-05-31T12:39:57+00:00",
                 "【PSA10】真紅眼の黒竜　トランプコレクション　初期　東映版　バンダイ版　遊戯王　極美品",
                 grader="PSA", grade=10.0),
            _row(509661, "buyee_paypay", 30000.0, "2026-05-31T15:17:04+00:00",
                 "【ARS10】 銀幕の鏡壁 2期 スーパーレア 遊戯王 ARS鑑定",
                 rarity="super", grader="ARS", grade=10.0),
            _row(12880, "buyee_yahoo", 20000.0, "2026-08-01T13:17:22+00:00",
                 "遊戯王 マジシャン・オブ・ブラックカオス 初期 ウルトラレア PSA8 鑑定品",
                 card_name="マジシャン・オブ・ブラックカオス", rarity="ultra",
                 grader="PSA", grade=8.0),
            _row(265684, "buyee_paypay", 20000.0, "2026-08-01T16:33:05+00:00",
                 "BGS8.5 バスターブレイダー レリーフ 303-054 アルティメットレア UL 遊戯王 極美品　PSA8.5相当",
                 rarity="ultimate", grader="PSA", grade=8.0),
        ]
        assert find_dual_listing_duplicates(rows) == []


# ---------------------------------------------------------------------------
# 2. Store 整合：mark_dual_listing_duplicates
# ---------------------------------------------------------------------------
def _seed_real_pair(tmp_path):
    db = tmp_path / "t.db"
    store = Store(db)
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at,"
            " price_native, currency, grader) VALUES"
            " ('S1', ?, 'u1', 'buyee_yahoo', '2026-03-31T15:14:26+00:00',"
            " 16666, 'JPY', 'ARS')",
            (REAL_TITLE,),
        )
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at,"
            " price_native, currency, grader) VALUES"
            " ('S2', ?, 'u2', 'buyee_paypay', '2026-03-31T14:48:13+00:00',"
            " 16666, 'JPY', 'ARS')",
            (REAL_TITLE,),
        )
    return store


class TestMarkDualListingDuplicates:
    def test_marks_the_paypay_side_and_keeps_yahoo_untouched(self, tmp_path):
        store = _seed_real_pair(tmp_path)
        rep = mark_dual_listing_duplicates(store)
        assert len(rep["matches"]) == 1

        rows = {r["url"]: r for r in store.comps_by(limit=10)}
        assert rows["u1"]["dup_of_id"] is None
        assert rows["u2"]["dup_of_id"] == rows["u1"]["id"]

    def test_never_deletes_a_row(self, tmp_path):
        store = _seed_real_pair(tmp_path)
        mark_dual_listing_duplicates(store)
        assert len(store.comps_by(limit=10)) == 2

    def test_is_idempotent(self, tmp_path):
        store = _seed_real_pair(tmp_path)
        first = mark_dual_listing_duplicates(store)
        second = mark_dual_listing_duplicates(store)
        third = mark_dual_listing_duplicates(store)
        assert len(first["matches"]) == 1
        assert len(second["matches"]) == 0, "已標記的列不再參與偵測"
        assert len(third["matches"]) == 0

    def test_dry_run_writes_nothing(self, tmp_path):
        store = _seed_real_pair(tmp_path)
        rep = mark_dual_listing_duplicates(store, dry_run=True)
        assert len(rep["matches"]) == 1
        assert all(r["dup_of_id"] is None for r in store.comps_by(limit=10))

    def test_no_false_positives_on_a_bigger_synthetic_corpus(self, tmp_path):
        """混一批不相關的 comps 進去，判重數量不能變多——語料變大不該製造誤判。"""
        db = tmp_path / "noise.db"
        store = Store(db)
        with sqlite3.connect(db) as c:
            c.execute(
                "INSERT INTO comps (signature, title, url, site, sold_at,"
                " price_native, currency, grader) VALUES"
                " ('S1', ?, 'u1', 'buyee_yahoo', '2026-03-31T15:14:26+00:00',"
                " 16666, 'JPY', 'ARS')",
                (REAL_TITLE,),
            )
            c.execute(
                "INSERT INTO comps (signature, title, url, site, sold_at,"
                " price_native, currency, grader) VALUES"
                " ('S2', ?, 'u2', 'buyee_paypay', '2026-03-31T14:48:13+00:00',"
                " 16666, 'JPY', 'ARS')",
                (REAL_TITLE,),
            )
            for i in range(20):
                c.execute(
                    "INSERT INTO comps (signature, title, url, site, sold_at,"
                    " price_native, currency) VALUES"
                    " (?, ?, ?, 'buyee_yahoo', '2026-03-31T15:14:26+00:00', 16666, 'JPY')",
                    (f"NOISE{i}", f"不相關的卡 {i}", f"noise{i}"),
                )
        rep = mark_dual_listing_duplicates(store)
        assert len(rep["matches"]) == 1


# ---------------------------------------------------------------------------
# 3. Migration：additive、冪等
# ---------------------------------------------------------------------------
def _legacy_db(path, rows):
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE comps ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " signature TEXT NOT NULL, title TEXT, price_twd REAL, price_native REAL,"
            " currency TEXT, url TEXT, site TEXT, sold_at TEXT, confidence TEXT,"
            " UNIQUE(signature, url))"
        )
        c.executemany(
            "INSERT INTO comps (signature, title, url, site, sold_at)"
            " VALUES (?,?,?,?,'2026-03-15T13:40:41+00:00')",
            rows,
        )


def test_migration_adds_dup_of_id_without_touching_existing_rows(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [("SIG1", REAL_TITLE, "u1", "buyee_yahoo")])

    Store(db)

    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(comps)")}
        assert "dup_of_id" in cols
        assert c.execute("SELECT dup_of_id FROM comps").fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 1


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "idem.db"
    _legacy_db(db, [("SIG1", REAL_TITLE, "u1", "buyee_yahoo")])
    Store(db)
    Store(db)
    Store(db)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 4. seller_alpha 整合：被標記的列必須從同儕池與計分中消失
# ---------------------------------------------------------------------------
def test_market_rows_from_store_skips_rows_marked_as_duplicates(tmp_path):
    from ygo_sniper.seller_alpha import market_rows_from_store

    db = tmp_path / "dedup_rows.db"
    store = Store(db)
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at, price_twd,"
            " price_native, currency) VALUES"
            " ('S1','t','u1','buyee_yahoo','2026-03-01T00:00:00+00:00',100,16666,'JPY')"
        )
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at, price_twd,"
            " price_native, currency, dup_of_id) VALUES"
            " ('S2','t','u2','buyee_paypay','2026-03-01T00:00:00+00:00',100,16666,'JPY',1)"
        )
    rows = market_rows_from_store(store, None)
    urls = {r.url for r in rows}
    assert "u1" in urls, "留下的那一筆（未被標記）必須照常參與同儕池"
    assert "u2" not in urls, "被標記為重複的那一筆必須從同儕池消失"
