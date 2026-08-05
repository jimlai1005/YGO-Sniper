"""Seller Alpha 資料地基的測試（Phase 0-1，2026-08-03）。

四件事，每件都對應一種「壞了看不出來」的病：

1. **解析端**：三個來源真的從 fixture（生產路徑實抓的 HTML）抽得到 seller_id。
   fixture 的預期值出自 reports/seller-id-availability.md 的實測。
2. **sellers 聚合**：計數是重算不是累加——同一標的每小時被重複掃到，
   listing_count 不准膨脹（膨脹的計數看起來像「這個賣家很活躍」，全是假的）。
3. **回填**：冪等（第二次 0 筆）、dry-run 不落地、不碰已有值的列。
4. **migration**：舊 schema 的 db 開機後欄位補齊、列數不變、重開機零變動。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from ygo_sniper.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def obs_row(key, *, site="buyee_paypay", seller_id=None, fb_score=None, fb_pct=None):
    return {
        "key": key, "source": site, "site": site, "title": f"卡 {key}",
        "url": f"https://example.test/{key}", "price_native": 5000.0,
        "currency": "JPY", "price_twd": 1000.0, "landed_twd": 1300.0,
        "rarity": "ultra", "grader": "PSA", "grade": 9.0,
        "card_name": None, "era_evidence": "初期", "price_kind": "fixed",
        "seller_id": seller_id,
        "seller_feedback_score": fb_score, "seller_feedback_pct": fb_pct,
    }


def batch(rows, *, site="buyee_paypay", healthy=True):
    return {"source": site, "site": site, "healthy": healthy, "rows": rows}


# ---------------------------------------------------------------------------
# 1. 解析端：fixture（生產路徑實抓）→ seller_id
# ---------------------------------------------------------------------------
def test_yahoo_search_cards_carry_the_seller_attr():
    """搜尋頁 53/53 張卡都有 data-auction-auc-seller-id（Phase 0 實測）。"""
    from ygo_sniper.sources.yahoo import YahooAuctionSource

    html = (FIXTURES / "yahoo_search_ok.html").read_text(encoding="utf-8")
    products = BeautifulSoup(html, "html.parser").select("li.Product")
    sellers = [YahooAuctionSource._parse_seller(li) for li in products]
    assert len(sellers) == 53
    assert all(sellers), "每張卡片都該有賣家 ID——掉到 0 代表 Yahoo 改版了"
    # 混淆 ID 的形狀：28-29 字英數（不是舊式帳號名）
    assert {len(s) for s in sellers} <= {28, 29}
    # RECON §2 的比對樣本 s1238539612 屬於 fixture 裡的已知賣家
    by_id = {}
    for li in products:
        a = li.select_one("a.Product__titleLink")
        node = li.select_one(".Product__bonus")
        if a and node:
            by_id[node.get("data-auction-id")] = node.get("data-auction-auc-seller-id")
    assert by_id["s1238539612"] == "13unCMsQyTixBUEkMisEyFbiSjw9t"


def test_yahoo_search_listing_has_seller_id(cfg):
    """走完整 `_extract_listings`，Listing.seller_id 真的被填上。"""
    from dataclasses import replace

    from ygo_sniper.sources.yahoo import YahooAuctionSource

    scoped = replace(
        cfg,
        sources={**cfg.sources, "yahoo_direct": {"include_live_auctions": True}},
    )
    src = YahooAuctionSource(scoped, fetcher=object())  # 不打網路，直接餵 soup
    html = (FIXTURES / "yahoo_search_ok.html").read_text(encoding="utf-8")
    products = BeautifulSoup(html, "html.parser").select("li.Product")
    listings, _excluded, parsed = src._extract_listings(products)
    assert parsed > 0
    assert all(lst.seller_id for lst in listings)


def test_yahoo_closed_items_carry_seller_and_rating():
    """落札相場 50/50 帶 seller.userId；フリマ與拍賣兩個 ID 空間都收得到。

    這個來源特別重要：**成交資料帶賣家**，折價歷史才有主人。
    """
    from ygo_sniper.config import load_config
    from ygo_sniper.sources.yahoo_closed import YahooClosedSource

    src = YahooClosedSource(load_config(), fetcher=object())
    html = (FIXTURES / "yahoo_closed_ok.html").read_text(encoding="utf-8")
    node = src._listing_node(html)
    listings, _unsold, _bad = src._extract_listings(node["items"])
    assert listings, "fixture 該解析出成交列"
    assert all(lst.seller_id for lst in listings)
    flea = [x for x in listings if x.site.value == "buyee_paypay"]
    auction = [x for x in listings if x.site.value == "buyee_yahoo"]
    assert flea and auction
    assert all(x.seller_id.startswith("p") for x in flea)      # p\d+ 空間
    assert all(len(x.seller_id) >= 28 for x in auction)        # 混淆 ID 空間
    # 額外欄位（評價字串）放 raw，評分模組之後用
    assert any(x.raw.get("seller_rating") for x in listings)


def test_paypay_items_carry_seller_id_and_seller_obj(cfg):
    """paypay_direct：sellerId 直接採收、seller 物件（goodRatio/numRating）進 raw。"""
    from ygo_sniper.sources.paypay import PayPayDirectSource

    src = PayPayDirectSource(cfg, fetcher=object())
    html = (FIXTURES / "paypay_search_ok.html").read_text(encoding="utf-8")
    node = src._result_node(html)
    listings, _stats = src._extract_listings(node["items"], set(), sold=False)
    assert listings
    assert all(lst.seller_id and lst.seller_id.startswith("p") for lst in listings)
    assert all(isinstance(lst.raw.get("seller"), dict) for lst in listings)


def test_listing_row_carries_seller_and_normalized_feedback(fx):
    """listing_row 帶 seller_id 進帳本、feedback 兩種鍵名（eBay/PayPay）都吃。"""
    from conftest import make_listing

    from ygo_sniper.domain import CardInfo
    from ygo_sniper.venue_study import listing_row

    ebay_like = make_listing(
        seller_id="psa",
        raw={"seller": {"username": "psa", "feedbackScore": 747,
                        "feedbackPercentage": "100.0"}},
    )
    row = listing_row(ebay_like, CardInfo(), source="ebay", fx=fx, price_kind="fixed")
    assert row["seller_id"] == "psa"
    assert row["seller_feedback_score"] == 747
    assert row["seller_feedback_pct"] == 100.0

    paypay_like = make_listing(
        seller_id="p245246",
        raw={"seller": {"id": "p245246", "goodRatio": 99.7, "numRating": 307}},
    )
    row = listing_row(paypay_like, CardInfo(), source="paypay_direct", fx=fx,
                      price_kind="fixed")
    assert row["seller_feedback_score"] == 307
    assert row["seller_feedback_pct"] == 99.7

    none_like = make_listing()  # 沒有賣家的來源（buyee_mercari）
    row = listing_row(none_like, CardInfo(), source="buyee_mercari", fx=fx,
                      price_kind="fixed")
    assert row["seller_id"] is None
    assert row["seller_feedback_score"] is None


# ---------------------------------------------------------------------------
# 2. sellers 聚合：重算不累加
# ---------------------------------------------------------------------------
def test_scan_writes_seller_ledger(store):
    rep = store.record_listing_scan(
        [batch([obs_row("a", seller_id="p1"), obs_row("b", seller_id="p1"),
                obs_row("c", seller_id="p2")])],
        now="2026-08-03T00:00:00+00:00",
    )
    assert rep["sellers"] == 2
    rows = {r["seller_key"]: r for r in store.list_sellers()}
    assert rows["buyee_paypay:p1"]["listing_count"] == 2
    assert rows["buyee_paypay:p2"]["listing_count"] == 1
    assert rows["buyee_paypay:p1"]["first_seen"] == "2026-08-03T00:00:00+00:00"


def test_rescanning_the_same_listing_does_not_inflate_counts(store):
    """膨脹的計數看起來像「很活躍的賣家」——那正是評分模組要吃的數字。"""
    for hour in (0, 1, 2):
        store.record_listing_scan(
            [batch([obs_row("a", seller_id="p1")])],
            now=f"2026-08-03T0{hour}:00:00+00:00",
        )
    row = store.list_sellers()[0]
    assert row["listing_count"] == 1          # 同一標的，重掃三次仍是 1
    assert row["first_seen"] == "2026-08-03T00:00:00+00:00"
    assert row["last_seen"] == "2026-08-03T02:00:00+00:00"


def test_cross_site_same_name_is_never_merged(store):
    store.record_listing_scan(
        [
            batch([obs_row("a", seller_id="psa")], site="ebay"),
            batch([obs_row("b", site="buyee_yahoo", seller_id="psa")],
                  site="buyee_yahoo"),
        ],
        now="2026-08-03T00:00:00+00:00",
    )
    keys = {r["seller_key"] for r in store.list_sellers()}
    assert keys == {"ebay:psa", "buyee_yahoo:psa"}


def test_feedback_is_stored_and_survives_scans_without_it(store):
    store.record_listing_scan(
        [batch([obs_row("a", seller_id="psa", fb_score=747, fb_pct=100.0)],
               site="ebay")],
        now="2026-08-03T00:00:00+00:00",
    )
    row = store.list_sellers()[0]
    assert (row["feedback_score"], row["feedback_pct"]) == (747, 100.0)
    # 下一輪沒帶 feedback（欄位 None）不可以把上次的評價洗掉
    store.record_listing_scan(
        [batch([obs_row("a", seller_id="psa")], site="ebay")],
        now="2026-08-03T01:00:00+00:00",
    )
    row = store.list_sellers()[0]
    assert (row["feedback_score"], row["feedback_pct"]) == (747, 100.0)


def test_save_comps_updates_sold_count(store):
    store.save_comps({
        "SIG|PSA10": [
            {"title": "t", "price_twd": 1000, "price_native": 5000,
             "currency": "JPY", "url": "https://example.test/s1", "site": "buyee_yahoo",
             "sold_at": "2026-06-01T00:00:00+00:00", "confidence": "high",
             "sold_at_is_ingest": 0, "seller_id": "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"},
        ]
    })
    row = store.list_sellers()[0]
    assert row["seller_key"] == "buyee_yahoo:AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
    assert row["sold_count"] == 1
    assert row["listing_count"] == 0
    # 同一筆重複 ingest（UNIQUE(signature,url) 擋掉）不膨脹
    store.save_comps({
        "SIG|PSA10": [
            {"title": "t", "price_twd": 1000, "price_native": 5000,
             "currency": "JPY", "url": "https://example.test/s1", "site": "buyee_yahoo",
             "sold_at": "2026-06-01T00:00:00+00:00", "confidence": "high",
             "sold_at_is_ingest": 0, "seller_id": "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"},
        ]
    })
    assert store.list_sellers()[0]["sold_count"] == 1


def test_reingesting_a_known_comp_enriches_its_null_seller_id(store):
    """seller 欄位上線前入庫的成交列，重掃（同 signature+url）要補上賣家。

    INSERT OR IGNORE 會整列跳過舊列；沒有這條補寫，yahoo_closed 180 天視窗
    裡的歷史成交永遠無主。已有值的列一律不碰。
    """
    base = {"title": "t", "price_twd": 1000, "price_native": 5000,
            "currency": "JPY", "url": "https://example.test/s1",
            "site": "buyee_yahoo", "sold_at": "2026-06-01T00:00:00+00:00",
            "confidence": "high", "sold_at_is_ingest": 0}
    store.save_comps({"SIG|PSA10": [dict(base)]})               # 舊時代：無 seller
    store.save_comps({"SIG|PSA10": [{**base, "seller_id": "A" * 28}]})  # 重掃帶 seller
    with store._conn() as c:
        row = c.execute("SELECT seller_id FROM comps WHERE url = ?",
                        (base["url"],)).fetchone()
    assert row["seller_id"] == "A" * 28
    assert store.list_sellers()[0]["sold_count"] == 1
    # 再來一個帶不同 seller 的重複列：已有值不被改寫
    store.save_comps({"SIG|PSA10": [{**base, "seller_id": "B" * 28}]})
    with store._conn() as c:
        row = c.execute("SELECT seller_id FROM comps WHERE url = ?",
                        (base["url"],)).fetchone()
    assert row["seller_id"] == "A" * 28


# ---------------------------------------------------------------------------
# 3. 回填：冪等、dry-run、不碰已有值
# ---------------------------------------------------------------------------
def _seed_signal_with_payload(store, key, seller_id):
    payload = json.dumps({"listing": {"seller_id": seller_id}})
    with store._conn() as c:
        c.execute(
            "INSERT INTO signals (key, site, external_id, title, url, payload) "
            "VALUES (?, 'ebay', ?, 't', 'u', ?)",
            (key, key.split(":", 1)[1], payload),
        )


def test_backfill_fills_null_seller_ids_and_is_idempotent(store):
    store.record_listing_scan(
        [batch([obs_row("ebay:1", site="ebay"), obs_row("ebay:2", site="ebay")],
               site="ebay")],
        now="2026-08-01T00:00:00+00:00",
    )
    _seed_signal_with_payload(store, "ebay:1", "psa")
    _seed_signal_with_payload(store, "ebay:2", "trunksociety")
    _seed_signal_with_payload(store, "ebay:3", "no-obs-row")  # 沒有觀測列

    r1 = store.backfill_seller_ids()
    assert r1 == {"payload_with_seller": 3, "updated": 2,
                  "already_set": 0, "no_obs_row": 1}
    obs = {r["key"]: r["seller_id"] for r in store.listing_obs()}
    assert obs == {"ebay:1": "psa", "ebay:2": "trunksociety"}
    # 聚合帳跟著到位，且回填**不是**新觀測——last_seen 停在原觀測時間
    sellers = {r["seller_key"]: r for r in store.list_sellers()}
    assert sellers["ebay:psa"]["listing_count"] == 1
    assert sellers["ebay:psa"]["last_seen"] == "2026-08-01T00:00:00+00:00"

    r2 = store.backfill_seller_ids()
    assert r2["updated"] == 0 and r2["already_set"] == 2  # 冪等


def test_backfill_dry_run_writes_nothing(store):
    store.record_listing_scan(
        [batch([obs_row("ebay:1", site="ebay")], site="ebay")], now="2026-08-01T00:00:00+00:00"
    )
    _seed_signal_with_payload(store, "ebay:1", "psa")
    r = store.backfill_seller_ids(dry_run=True)
    assert r["updated"] == 1                      # 會回報「將回填幾筆」
    assert store.listing_obs()[0]["seller_id"] is None
    assert store.list_sellers() == []


def test_backfill_never_overwrites_an_existing_seller_id(store):
    store.record_listing_scan(
        [batch([obs_row("ebay:1", site="ebay", seller_id="scan-truth")], site="ebay")],
        now="2026-08-01T00:00:00+00:00",
    )
    _seed_signal_with_payload(store, "ebay:1", "stale-payload-value")
    r = store.backfill_seller_ids()
    assert r["updated"] == 0 and r["already_set"] == 1
    assert store.listing_obs()[0]["seller_id"] == "scan-truth"


# ---------------------------------------------------------------------------
# 4. migration：舊 db 升級後資料原封不動、冪等
# ---------------------------------------------------------------------------
_OLD_SCHEMA = """
CREATE TABLE signals (key TEXT PRIMARY KEY, site TEXT, external_id TEXT,
    title TEXT, url TEXT, image_url TEXT, price_native REAL, currency TEXT,
    landed_twd REAL, route TEXT, grader TEXT, grade REAL, set_code TEXT,
    comps_n INTEGER, comps_median REAL, discount_pct REAL, score REAL,
    flags TEXT, reason TEXT, payload TEXT, state TEXT DEFAULT 'new',
    note TEXT DEFAULT '', first_seen TEXT, last_seen TEXT, notified_at TEXT);
CREATE TABLE comps (id INTEGER PRIMARY KEY AUTOINCREMENT, signature TEXT NOT NULL,
    title TEXT, price_twd REAL, price_native REAL, currency TEXT, url TEXT,
    site TEXT, sold_at TEXT, confidence TEXT, UNIQUE(signature, url));
CREATE TABLE listing_obs (key TEXT PRIMARY KEY, source TEXT, site TEXT,
    title TEXT, url TEXT, price_native REAL, currency TEXT, price_twd REAL,
    landed_twd REAL, rarity TEXT, grader TEXT, grade REAL, card_name TEXT,
    era_evidence TEXT, price_kind TEXT, first_seen TEXT, last_seen TEXT,
    seen_count INTEGER DEFAULT 1, disappeared_at TEXT, window_exit_at TEXT,
    revived_count INTEGER DEFAULT 0);
"""


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO comps (signature, title, price_twd, url, site, sold_at) "
        "VALUES ('S|PSA9', 'old', 900, 'https://example.test/c1', 'buyee_yahoo', "
        "'2026-07-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO listing_obs (key, site, title, first_seen, last_seen) "
        "VALUES ('buyee_yahoo:x1', 'buyee_yahoo', 'old obs', "
        "'2026-07-01T00:00:00+00:00', '2026-07-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO signals (key, site, external_id, title, url) "
        "VALUES ('buyee_yahoo:x1', 'buyee_yahoo', 'x1', 't', 'u')"
    )
    conn.commit()
    conn.close()


def _table_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    out = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("signals", "comps", "listing_obs")
    }
    conn.close()
    return out


def test_old_db_migrates_additively_and_idempotently(tmp_path):
    db = tmp_path / "old.db"
    _make_old_db(db)
    before = _table_counts(db)

    Store(db)  # 開機 = migration
    after = _table_counts(db)
    assert after == before, "migration 不准增刪任何一列"

    conn = sqlite3.connect(db)
    obs_cols = {r[1] for r in conn.execute("PRAGMA table_info(listing_obs)")}
    comps_cols = {r[1] for r in conn.execute("PRAGMA table_info(comps)")}
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    schema_dump = "\n".join(
        r[0] or "" for r in conn.execute("SELECT sql FROM sqlite_master"))
    conn.close()
    assert "seller_id" in obs_cols
    assert "seller_id" in comps_cols
    assert "sellers" in tables

    Store(db)  # 第二次開機：冪等（schema 與資料都零變動）
    conn = sqlite3.connect(db)
    schema_dump2 = "\n".join(
        r[0] or "" for r in conn.execute("SELECT sql FROM sqlite_master"))
    conn.close()
    assert _table_counts(db) == before
    assert schema_dump2 == schema_dump


def test_fresh_db_and_migrated_db_have_the_same_listing_obs_columns(tmp_path):
    """新建 db 與升級 db 的欄位集合必須相同——兩條路徑分岔就是下一個坑。"""
    fresh = tmp_path / "fresh.db"
    Store(fresh)
    old = tmp_path / "old.db"
    _make_old_db(old)
    Store(old)

    def cols(path, table):
        conn = sqlite3.connect(path)
        out = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        conn.close()
        return out

    assert cols(fresh, "listing_obs") == cols(old, "listing_obs")
    assert cols(fresh, "comps") == cols(old, "comps")
    assert cols(fresh, "sellers") == cols(old, "sellers")


# ---------------------------------------------------------------------------
# 5. 重掃不得把賣家抹掉（2026-08-04 事故）
# ---------------------------------------------------------------------------
def test_rescan_without_a_seller_does_not_erase_a_known_one(store):
    """「這次不知道」≠「這筆沒有賣家」。

    事故：從商品頁挖回 25 個 Mercari 賣家，一小時後例行掃描把 22 個抹成 NULL
    ——因為 Mercari 的**搜尋頁**根本不帶 seller_id，而更新路徑無條件覆寫。
    外顯與「這條管道本來就沒有賣家」一模一樣，沒有任何錯誤訊息。
    """
    store.record_listing_scan(
        [batch([obs_row("buyee_mercari:m1", site="buyee_mercari")], site="buyee_mercari")]
    )
    assert store.set_listing_obs_seller("buyee_mercari:m1", "901019808") is True

    # 例行掃描再看到同一筆（搜尋頁沒有賣家）
    store.record_listing_scan(
        [batch([obs_row("buyee_mercari:m1", site="buyee_mercari")], site="buyee_mercari")]
    )
    row = store.listing_obs(limit=10)[0]
    assert row["seller_id"] == "901019808"
    assert row["seen_count"] == 2, "其餘帳務欄位照常前進"


def test_rescan_with_a_seller_updates_the_value(store):
    """來源帶了新值就用新值——sticky 是「不被 NULL 蓋掉」，不是「永不更新」。"""
    store.record_listing_scan(
        [batch([obs_row("ebay:1", site="ebay", seller_id="old")], site="ebay")]
    )
    store.record_listing_scan(
        [batch([obs_row("ebay:1", site="ebay", seller_id="new")], site="ebay")]
    )
    assert store.listing_obs(limit=10)[0]["seller_id"] == "new"


def test_set_listing_obs_seller_is_idempotent_and_only_fills_nulls(store):
    store.record_listing_scan(
        [batch([obs_row("ebay:2", site="ebay")], site="ebay")]
    )
    assert store.set_listing_obs_seller("ebay:2", "A") is True
    assert store.set_listing_obs_seller("ebay:2", "B") is False, "只補不改"
    assert store.listing_obs(limit=10)[0]["seller_id"] == "A"
    assert store.set_listing_obs_seller("ebay:missing", "A") is False


# ---------------------------------------------------------------------------
# 6. `sellers --supply` 的 Alpha 欄：0 分與證據不足不可混為一談（Task 6a）
# ---------------------------------------------------------------------------
def test_fmt_alpha_total_distinguishes_zero_from_insufficient_evidence():
    """`_fmt_alpha_total` 是供給契合度排行榜 Alpha 欄的格式化函式。

    0.0 分（`ok=True`）＝「算出來就是比同儕貴」；`ok=False`＝「湊不到同儕，
    不知道」。兩者的語意相反，顯示成同一個東西會讓使用者把「不知道」
    讀成「比同儕貴」。CLAUDE.md 第四節：不要拿自己的模型當尺——這裡的
    對應教訓是不要把「沒有證據」讀成「證據顯示很差」。
    """
    from ygo_sniper.cli import _fmt_alpha_total
    from ygo_sniper.seller_alpha import SellerScore

    zero_score = SellerScore(seller_key="ebay:x", ok=True, reason="算出來", total=0.0)
    assert _fmt_alpha_total(zero_score) == "0.0"

    no_evidence_score = SellerScore(
        seller_key="ebay:y", ok=False, reason="可比不足", total=None
    )
    assert _fmt_alpha_total(no_evidence_score) == "證據不足"

    # 賣家在 Alpha 那邊完全沒有分數紀錄（例如只出現在 Supply Fit 側）
    assert _fmt_alpha_total(None) == "證據不足"


def test_supply_dim_raw_reads_available_dimension_and_none_when_missing():
    from ygo_sniper.cli import _supply_dim_raw
    from ygo_sniper.seller_supply import SupplyDimension, SupplyFit

    fit = SupplyFit(
        seller_key="ebay:x", site="ebay", ok=True, reason="ok", total=90.0,
        dimensions=[
            SupplyDimension.of("supply_depth", raw=12.0, detail="累積觀測 12 列"),
            SupplyDimension.unavailable("persistence", "只觀測到單一時間點"),
        ],
    )
    assert _supply_dim_raw(fit, "supply_depth") == 12.0
    assert _supply_dim_raw(fit, "persistence") is None
    assert _supply_dim_raw(fit, "series_focus") is None  # 根本不在 dimensions 裡
