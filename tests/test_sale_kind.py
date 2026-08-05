"""成交型態（`comps.sale_kind`）：競標結標 vs 定價成交，**不可混池**。

## 為什麼要分（工程原則 1：同源、同基準）

`sold` 這一桶原本把兩種語意完全不同的成交放在一起：

- **競標結標價**（ヤフオク落札）＝ 買家搶到多高。那是市場需求的結果，
  **不是賣家的定價決定**（賣家只設了開始価格，最終價是別人喊出來的）。
- **定價成交**（Yahoo!フリマ／Mercari／Yahoo 一口價即決）＝ 賣家開多少。

Seller Alpha 問的是「這個賣家開的價比同儕低多少」。拿別人搶出來的落札價
當同儕，量到的是熱度不是定價行為——而且錯誤方向是「這個賣家好便宜」，
正是使用者的直覺攔不下來的那個方向。

2026-08-06 實測（正式庫 2,566 筆 comps）：468 個計分點裡 251 筆是
`sold × buyee_yahoo × 競標結標`（53.6%）；13 筆 Yahoo 一口價的標的，
同儕 24 筆裡有 22 筆是競標結標——**現況已經在混池**。

## 判定規則只有一處，而且只吃證據

平台事實（Mercari／Yahoo!フリマ 根本沒有競標機制）> 來源逐筆旗標
（`isFixedPrice`）> 其他一律 `unknown`。**`unknown` 不准被當成 `fixed`**：
那會讓一筆沒有證據的成交安靜地變成同儕，正是本專案第五節那類靜默失敗。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from conftest import FakeFx, make_listing

from ygo_sniper.comps import CompsEngine, backfill_sale_kind, sale_kind_for, sale_kind_of
from ygo_sniper.domain import SaleKind, Site
from ygo_sniper.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
_TITLE = "PSA10 遊戯王 初期 青眼の白龍 シークレット"


@pytest.fixture
def engine(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    return CompsEngine(cfg, FakeFx(), store)


def _sold(ext: str, *, site=Site.BUYEE_YAHOO, raw: dict | None = None):
    lst = make_listing(price=9000, site=site, title=_TITLE, external_id=ext)
    lst = dataclasses.replace(lst, url=f"https://buyee.jp/item/yahoo/auction/{ext}", is_sold=True)
    lst.raw = dict(raw or {})
    return lst


# ---------------------------------------------------------------------------
# 1. 判定規則：逐站，依平台事實與來源旗標，不猜
# ---------------------------------------------------------------------------
def test_yahoo_auction_close_is_an_auction():
    """`isFixedPrice=False` 的 ヤフオク 落札＝有人喊價喊上去的。"""
    assert sale_kind_for("buyee_yahoo", {"is_fixed_price": False}) is SaleKind.AUCTION


def test_yahoo_buy_it_now_is_a_fixed_price_sale():
    """即決（一口價）是賣家開的價，即使它出現在落札相場裡。"""
    assert sale_kind_for("buyee_yahoo", {"is_fixed_price": True}) is SaleKind.FIXED


def test_yahoo_without_the_flag_is_unknown_not_a_guess():
    """拿不到旗標＝不知道。**不准因為「多數是競標」就猜競標**——
    那是拿統計當個案證據，錯了完全看不出來。"""
    assert sale_kind_for("buyee_yahoo", {}) is SaleKind.UNKNOWN
    assert sale_kind_for("buyee_yahoo", None) is SaleKind.UNKNOWN
    assert sale_kind_for("buyee_yahoo", {"is_fixed_price": None}) is SaleKind.UNKNOWN


@pytest.mark.parametrize("site", ["buyee_paypay", "buyee_mercari", "mercari_tw"])
def test_flea_market_sites_have_no_auction_mechanism(site):
    """Yahoo!フリマ 與 Mercari **平台上沒有競標**，每一筆成交都是賣家開的價。

    這是平台事實，比逐筆旗標更硬，所以它先判。2026-08-06 對帳過沒有衝突：
    快取裡 2,553 筆 `isFleamarketItem=True` 的標的 `isFixedPrice` 全部是 True。
    """
    assert sale_kind_for(site, {}) is SaleKind.FIXED
    assert sale_kind_for(site, {"is_fixed_price": True}) is SaleKind.FIXED


def test_ebay_needs_evidence_and_never_defaults_to_fixed():
    """eBay 兩種都有（拍賣＋Buy It Now）。沒有 `buyingOptions` 就是不知道。"""
    assert sale_kind_for("ebay", {}) is SaleKind.UNKNOWN
    assert sale_kind_for("ebay", {"buyingOptions": ["AUCTION"]}) is SaleKind.AUCTION
    assert sale_kind_for("ebay", {"buyingOptions": ["FIXED_PRICE"]}) is SaleKind.FIXED
    # 競標帶 BIN：`price` 取的是 BIN 價（見 sources/ebay.read_price）→ 定價
    assert sale_kind_for(
        "ebay", {"buyingOptions": ["AUCTION", "FIXED_PRICE"]}
    ) is SaleKind.FIXED


def test_unknown_site_without_evidence_is_unknown():
    assert sale_kind_for("ruten", {}) is SaleKind.UNKNOWN
    assert sale_kind_for("", None) is SaleKind.UNKNOWN


def test_sale_kind_of_reads_the_listing_raw_flags():
    assert sale_kind_of(_sold("a1", raw={"is_fixed_price": False})) is SaleKind.AUCTION
    assert sale_kind_of(_sold("a2", raw={"is_fixed_price": True})) is SaleKind.FIXED
    assert sale_kind_of(_sold("a3")) is SaleKind.UNKNOWN
    assert sale_kind_of(_sold("a4", site=Site.BUYEE_MERCARI)) is SaleKind.FIXED


# ---------------------------------------------------------------------------
# 2. 入庫時就落欄位（新資料不需要回填）
# ---------------------------------------------------------------------------
def test_ingest_sold_persists_the_sale_kind(engine):
    engine.ingest_sold([
        _sold("i1", raw={"is_fixed_price": False}),
        _sold("i2", raw={"is_fixed_price": True}),
        _sold("i3"),
    ])
    kinds = {r["url"].rsplit("/", 1)[-1]: r["sale_kind"] for r in engine.store.comps_by()}
    assert kinds == {"i1": "auction", "i2": "fixed", "i3": "unknown"}


def test_yahoo_closed_listings_carry_the_flag_end_to_end(cfg, engine):
    """生產路徑：closedsearch 的實抓頁 → Listing → comps.sale_kind。

    fixture 混著 ヤフオク（競標）與 Yahoo!フリマ（定價）兩個 ID 空間，
    所以這一條同時證明「兩種型態都被正確標出來」。
    """
    from ygo_sniper.sources.yahoo_closed import YahooClosedSource

    html = (FIXTURES / "yahoo_closed_ok.html").read_text(encoding="utf-8")

    class _F:
        def get(self, url, **kw):
            return html

    listings = YahooClosedSource(cfg, _F()).search_detailed("遊戯王", pages=1).listings
    assert listings
    engine.ingest_sold(listings)

    rows = engine.store.comps_by(limit=1000)
    assert rows, "fixture 應該至少收得到一筆"
    kinds = {r["sale_kind"] for r in rows}
    assert kinds <= {"auction", "fixed"}, "實抓頁的每一筆都帶得出型態"
    assert "unknown" not in kinds
    # フリマ（buyee_paypay）一律定價；ヤフオク 競標一律 auction
    for r in rows:
        if r["site"] == "buyee_paypay":
            assert r["sale_kind"] == "fixed"


# ---------------------------------------------------------------------------
# 3. Migration：additive、冪等
# ---------------------------------------------------------------------------
def _legacy_db(path: Path, rows: list[tuple[str, str]]) -> None:
    """舊 schema 的 comps（沒有 sale_kind 欄位），塞幾列進去。"""
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
            [(f"SIG{i}", _TITLE, url, site) for i, (site, url) in enumerate(rows)],
        )


def test_migration_adds_the_column_without_touching_existing_rows(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db, [("buyee_yahoo", "https://buyee.jp/item/yahoo/auction/p1")])

    Store(db)
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(comps)")}
        assert "sale_kind" in cols
        # 既有列原封不動：migration 不猜型態（回填是另一支明確的指令）
        assert c.execute("SELECT sale_kind FROM comps").fetchone()[0] is None
        assert c.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 1


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "idem.db"
    _legacy_db(db, [("buyee_yahoo", "https://buyee.jp/item/yahoo/auction/p1")])
    Store(db)
    Store(db)          # 第二次開機：不得拋 duplicate column
    Store(db)
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM comps").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# 4. 回填：只吃證據、冪等、不增不減列
# ---------------------------------------------------------------------------
def _seeded_store(tmp_path) -> Store:
    db = tmp_path / "backfill.db"
    _legacy_db(db, [
        ("buyee_yahoo", "https://buyee.jp/item/yahoo/auction/auc_bid"),    # 有證據：競標
        ("buyee_yahoo", "https://buyee.jp/item/yahoo/auction/auc_fixed"),  # 有證據：即決
        ("buyee_yahoo", "https://buyee.jp/item/yahoo/auction/auc_none"),   # 無證據
        ("buyee_paypay", "https://buyee.jp/paypayfleamarket/item/z1"),     # 平台事實
        ("buyee_mercari", "https://buyee.jp/mercari/item/m1"),             # 平台事實
    ])
    return Store(db)


_EVIDENCE = {"auc_bid": False, "auc_fixed": True}


def test_backfill_uses_evidence_and_platform_facts_only(tmp_path):
    store = _seeded_store(tmp_path)
    rep = backfill_sale_kind(store, _EVIDENCE)

    kinds = {r["url"].rsplit("/", 1)[-1]: r["sale_kind"] for r in store.comps_by(limit=100)}
    assert kinds == {
        "auc_bid": "auction",
        "auc_fixed": "fixed",
        "auc_none": "unknown",      # 沒有證據就是不知道
        "z1": "fixed",
        "m1": "fixed",
    }
    assert rep["updated"] == 5
    assert rep["by_site_kind"][("buyee_yahoo", "auction")] == 1
    assert rep["by_site_kind"][("buyee_yahoo", "unknown")] == 1


def test_backfill_is_idempotent(tmp_path):
    store = _seeded_store(tmp_path)
    first = backfill_sale_kind(store, _EVIDENCE)
    second = backfill_sale_kind(store, _EVIDENCE)
    third = backfill_sale_kind(store, _EVIDENCE)

    assert first["updated"] == 5
    assert (second["updated"], third["updated"]) == (0, 0)
    assert store.comps_by(limit=100).__len__() == 5   # 回填不增不減列


def test_backfill_never_downgrades_a_known_kind_to_unknown(tmp_path):
    """證據消失（快取被清）不該把已知的型態抹成 unknown——那是資料遺失。"""
    store = _seeded_store(tmp_path)
    backfill_sale_kind(store, _EVIDENCE)
    backfill_sale_kind(store, {})     # 沒有任何證據的一次重跑

    kinds = {r["url"].rsplit("/", 1)[-1]: r["sale_kind"] for r in store.comps_by(limit=100)}
    assert kinds["auc_bid"] == "auction"
    assert kinds["auc_fixed"] == "fixed"


def test_backfill_upgrades_unknown_once_evidence_shows_up(tmp_path):
    """先跑（沒證據）→ unknown；證據補齊後再跑 → 升級成真實型態。

    只寫 NULL 的話這批會永遠卡在 unknown，等於「第一次跑的時候快取剛好被清」
    就永久少掉一批可比樣本——而且完全看不出來。
    """
    store = _seeded_store(tmp_path)
    backfill_sale_kind(store, {})
    assert store.comps_by(limit=100)[0]["sale_kind"] is not None

    rep = backfill_sale_kind(store, _EVIDENCE)
    kinds = {r["url"].rsplit("/", 1)[-1]: r["sale_kind"] for r in store.comps_by(limit=100)}
    assert kinds["auc_bid"] == "auction"
    assert kinds["auc_fixed"] == "fixed"
    assert rep["updated"] == 2


def test_backfill_dry_run_writes_nothing(tmp_path):
    store = _seeded_store(tmp_path)
    rep = backfill_sale_kind(store, _EVIDENCE, dry_run=True)

    assert rep["updated"] == 5
    assert all(r["sale_kind"] is None for r in store.comps_by(limit=100))


# ---------------------------------------------------------------------------
# 5. 證據來源：快取的 closedsearch 快照（走生產解析路徑）
# ---------------------------------------------------------------------------
def test_cache_evidence_comes_from_the_production_parser(tmp_path):
    """快取證據用 `_listing_node`（＝生產路徑）解，不另寫一套 regex。

    另寫一套的下場是它跟生產解析器漂移，而漂移後回填出來的型態是錯的、
    看起來卻很正常（本專案第六節：測試路徑必須等於生產路徑）。
    """
    from ygo_sniper.sources.yahoo_closed import sale_flags_from_cache

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "page.html").write_text(
        (FIXTURES / "yahoo_closed_ok.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (cache / "noise.html").write_text("<html>不是 closedsearch</html>", encoding="utf-8")

    flags = sale_flags_from_cache(cache)
    assert len(flags) == 50, "fixture 是 50 筆實抓樣本"
    assert set(flags.values()) == {True, False}, "兩種型態都在"


def test_rescanning_upgrades_an_unknown_row_but_never_overwrites_a_known_one(engine):
    """`INSERT OR IGNORE` 會整列跳過既有的成交，所以型態必須**另外補**。

    yahoo_closed 的視窗有 180 天，重掃一定會一直碰到同一批列——不補的話
    「當時查不到證據」的那些會永遠卡在 unknown（＝永遠不進同儕比較）。
    """
    engine.ingest_sold([_sold("u1")])                                   # 無旗標 → unknown
    engine.ingest_sold([_sold("u1", raw={"is_fixed_price": False})])    # 重掃：這次有證據
    rows = {r["url"].rsplit("/", 1)[-1]: r for r in engine.store.comps_by()}
    assert len(rows) == 1, "同一筆成交不得變成兩列"
    assert rows["u1"]["sale_kind"] == "auction"

    engine.ingest_sold([_sold("u1")])          # 再一次重掃，這次又沒有旗標
    rows = {r["url"].rsplit("/", 1)[-1]: r for r in engine.store.comps_by()}
    assert rows["u1"]["sale_kind"] == "auction", "已知的型態不得被 unknown 蓋掉"
