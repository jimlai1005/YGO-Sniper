"""refill 的第二個需求來源：監控賣家掃出來的**定價上架**。零網路。

## 這個來源為什麼存在（2026-08-07 缺口診斷）

逐筆重放 80 筆估不了的監控賣家上架：27 筆的卡在 comps 一筆成交都沒有——
不是被過濾，是從來沒被挖過。根因：refill 佇列只吃 live auction 標題，
而監控賣家的上架 62/80 是定價（paypay 48＋ebay 14），**結構上永遠進不了佇列**。
實測：那 80 筆的 refill-eligible 相異卡 40 張，只有 6 張進過 refill 帳。

## 守的約束

- **共用同一套節流**：合併需求之後才過冷卻濾網與 max_cards 截斷，
  所以新來源不可能繞過任何一道（每輪請求上限不變）。
- **來源可分辨**：selected 的每張卡帶 auction_n / watch_fixed_n，
  report dict 有 selected_origin——之後查「refill 有沒有在吃新來源」不用猜。
- **範圍守住**：一般關鍵字掃描的定價上架**不進**佇列（那會把佇列衝大，另議）。
"""

from __future__ import annotations

import dataclasses

from conftest import FakeFx, make_listing

from ygo_sniper.cards import CardIndex
from ygo_sniper.comps import CompsEngine
from ygo_sniper.domain import Currency, Listing, Site
from ygo_sniper.refill import (
    RefillParams,
    run_refill,
    select_refill_cards,
)
from ygo_sniper.sources.health import ParseHealth, SearchResult


def _index() -> CardIndex:
    """三張年代內的小主檔（名字取自真實卡，比對規則是真的）。"""
    return CardIndex(
        [
            {"id": 1, "name_ja": "青眼の白龍", "name_en": "Blue-Eyes White Dragon",
             "ocg_date": "1999-01-01"},
            {"id": 2, "name_ja": "ブラック・マジシャン", "name_en": "Dark Magician",
             "ocg_date": "1999-01-01"},
            {"id": 3, "name_ja": "真紅眼の黒竜", "name_en": "Red-Eyes Black Dragon",
             "ocg_date": "1999-01-01"},
        ],
    )


def _params(**kw) -> RefillParams:
    base = dict(
        enabled=True, min_comps=3, max_cards_per_run=10,
        cooldown_days=7.0, sources=("fake_sold",),
    )
    base.update(kw)
    return RefillParams(**base)


def _sold_listing(title: str, *, external_id: str, price: float = 20000) -> Listing:
    return Listing(
        site=Site.BUYEE_YAHOO,
        external_id=external_id,
        title=title,
        url=f"https://buyee.jp/item/yahoo/auction/{external_id}",
        price=price,
        currency=Currency.JPY,
        is_sold=True,
        raw={"price_kind": "sold_price", "sold_at": "2026-08-01T00:00:00+00:00"},
        source="fake_sold",
    )


class _FakeSoldSource:
    """回固定 SearchResult 的假已售出來源，並記錄每次呼叫的參數。"""

    def __init__(self, name: str, listings=None, *, health: ParseHealth = ParseHealth.OK):
        self.name = name
        self.site = Site.BUYEE_YAHOO
        self.supports_sold = True
        self.listings = list(listings or [])
        self.health = health
        self.calls: list[dict] = []

    def search_detailed(self, keyword, *, sold=False, pages=None, **kw):
        self.calls.append({"keyword": keyword, "sold": sold, "pages": pages})
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=list(self.listings), health=self.health,
            parsed_count=len(self.listings), pages_fetched=1,
        )


def _store(tmp_path):
    from ygo_sniper.store import Store

    return Store(tmp_path / "refill-watch.db")


def _engine(cfg, store) -> CompsEngine:
    return CompsEngine(cfg, FakeFx(), store=store)


# ---------------------------------------------------------------------------
# 1. 選卡：監控定價需求進得了佇列，且來源標記正確
# ---------------------------------------------------------------------------
def test_watch_fixed_titles_enter_selection_with_origin_marks():
    index = _index()
    selected, cooled = select_refill_cards(
        ["遊戯王 青眼の白龍 初期 PSA10"],            # 競標需求
        index, {}, min_comps=3, max_cards=10,
        watch_titles=[
            "遊戯王 青眼の白龍 初期 PSA9",           # 同卡的監控定價需求
            "遊戯王 ブラック・マジシャン 初期 PSA10",  # 只有監控定價需求的卡
        ],
    )
    assert cooled == []
    by_name = {c.card_name: c for c in selected}
    # 只有監控定價需求的卡也進得了佇列（這正是 2026-08-07 缺口）
    assert "ブラック・マジシャン" in by_name
    assert by_name["ブラック・マジシャン"].auction_n == 0
    assert by_name["ブラック・マジシャン"].watch_fixed_n == 1
    # 兩個來源的需求會合計，且各自的來源計數分開記
    blue = by_name["青眼の白龍"]
    assert (blue.auction_n, blue.watch_fixed_n, blue.listings_n) == (1, 1, 2)


def test_watch_titles_default_empty_keeps_existing_behavior():
    """不帶 watch_titles 時行為與從前完全相同（既有呼叫端不受影響）。"""
    index = _index()
    selected, _ = select_refill_cards(
        ["遊戯王 青眼の白龍 初期 PSA10"], index, {}, min_comps=3, max_cards=10,
    )
    assert [c.card_name for c in selected] == ["青眼の白龍"]
    assert selected[0].auction_n == 1 and selected[0].watch_fixed_n == 0


# ---------------------------------------------------------------------------
# 2. 節流共用：冷卻與每輪卡數上限對新來源一樣生效
# ---------------------------------------------------------------------------
def test_watch_demand_respects_shared_cooldown():
    index = _index()
    selected, cooled = select_refill_cards(
        [], index, {}, min_comps=3, max_cards=10,
        cooling={"青眼の白龍"},
        watch_titles=["遊戯王 青眼の白龍 初期 PSA10"],
    )
    assert selected == []
    assert [c.card_name for c in cooled] == ["青眼の白龍"]


def test_watch_demand_shares_max_cards_cap_and_request_budget(tmp_path, cfg):
    """合併需求後才截斷：新來源塞不爆每輪請求預算（max_cards × 來源數 × 1 頁）。"""
    store = _store(tmp_path)
    src = _FakeSoldSource("fake_sold", [])
    report = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=_index(),
        titles=["遊戯王 青眼の白龍 初期 PSA10"],
        watch_titles=[
            "遊戯王 ブラック・マジシャン 初期 PSA10",
            "遊戯王 真紅眼の黒竜 初期 PSA10",
        ],
        params=_params(max_cards_per_run=2),
    )
    # 三張缺樣本的卡、上限 2 → 只發 2 張卡 × 1 來源 × 1 頁 = 2 個查詢
    assert len(report.selected) == 2
    assert report.queries == len(src.calls) == 2
    assert report.requests <= 2


def test_watch_refilled_card_not_requeried_within_cooldown(tmp_path, cfg):
    """同一張卡（監控定價需求）在 cooldown 內不重複進佇列——與競標需求同一本帳。"""
    store = _store(tmp_path)
    src = _FakeSoldSource("fake_sold", [], health=ParseHealth.EMPTY_CONFIRMED)
    kw = dict(
        store=store, sources={"fake_sold": src}, comps=None, index=_index(),
        titles=[], watch_titles=["遊戯王 青眼の白龍 初期 PSA10"], params=_params(),
    )
    kw["comps"] = _engine(cfg, store)
    first = run_refill(**kw)
    assert [c.card_name for c in first.selected] == ["青眼の白龍"]
    assert store.refill_cooldown_active(7) == {"青眼の白龍"}

    second = run_refill(**kw)
    assert second.selected == []
    assert [c.card_name for c in second.skipped_cooldown] == ["青眼の白龍"]
    assert second.queries == 0 and len(src.calls) == 1  # 第二輪一個請求都沒發


# ---------------------------------------------------------------------------
# 3. run_refill 帳面：來源分辨得出來（report dict＋summary）
# ---------------------------------------------------------------------------
def test_run_refill_report_carries_origin_breakdown(tmp_path, cfg):
    store = _store(tmp_path)
    src = _FakeSoldSource("fake_sold", [
        _sold_listing("遊戯王 青眼の白龍 初期 PSA10 極美品", external_id="w1"),
    ])
    report = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=_index(),
        titles=[],
        watch_titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(),
    )
    assert [c["keyword"] for c in src.calls] == ["青眼の白龍"]  # 查的是卡名
    d = report.to_dict()
    assert d["selected_origin"] == {"青眼の白龍": {"auction": 0, "watch_fixed": 1}}
    assert "監控定價" in report.summary()  # log 那條線也看得出新來源在動
    assert store.refill_cooldown_active(7) == {"青眼の白龍"}


# ---------------------------------------------------------------------------
# 4. pipeline 掛鉤（零網路：stub registry、FakeFx、tmp db）
# ---------------------------------------------------------------------------
class _WatchSellerSource:
    """賣家頁列舉回定價上架；sold=True 回同卡成交（refill 那條路）。"""

    def __init__(self, name: str, seller_listings, *, site=Site.BUYEE_YAHOO):
        self.name = name
        self.site = site
        self.supports_sold = True
        self.seller_listings = list(seller_listings)
        self.seller_calls: list[str] = []
        self.sold_calls: list[str] = []

    def search_seller(self, seller_id, *, pages=1, sold=False):
        self.seller_calls.append(str(seller_id))
        return SearchResult(
            source=self.name, site=self.site.value, query=f"seller:{seller_id}",
            listings=list(self.seller_listings),
            parsed_count=len(self.seller_listings), pages_fetched=1,
        )

    def search_detailed(self, keyword, *, sold=False, pages=None, **kw):
        if sold:
            self.sold_calls.append(keyword)
            listings = [_sold_listing(
                f"遊戯王 {keyword} 初期 PSA10 美品",
                external_id=f"s-{len(self.sold_calls)}",
            )]
        else:
            listings = []
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=listings, parsed_count=len(listings), pages_fetched=1,
        )


def _make_pipeline(monkeypatch, tmp_path, cfg, registry, *, queries, refill_sources):
    import ygo_sniper.pipeline as pipeline_mod

    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,  # db/cache 全落 tmp
        watchlist={
            **cfg.watchlist,
            "queries": queries,
            "comps_queries": {},  # 廣撒那條線關掉
            "refill": {"sources": refill_sources, "max_cards_per_run": 5},
        },
        sources={},  # 不跑 canary
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _c, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _c: FakeFx())
    return pipeline_mod.Pipeline(test_cfg)


def test_pipeline_watch_seller_fixed_price_feeds_refill(monkeypatch, tmp_path, cfg):
    """監控賣家的定價上架 → 同一輪 scan 的 refill 佇列（來源標記 watch_fixed）。"""
    from ygo_sniper.seller_watch import SOURCE_MANUAL, WatchParams, add_watch

    seller = "8m1fe2VPnJdV8xkRDfgL2TymZEJbW"
    fixed = [
        make_listing(
            price=3000, site=Site.BUYEE_YAHOO, external_id=f"f{i}",
            source="yahoo_direct", title="遊戯王 初期 青眼の白龍 PSA10 ウルトラ",
            seller_id=seller, raw={"price_kind": "fixed"},
        )
        for i in range(2)
    ]
    src = _WatchSellerSource("yahoo_direct", fixed)
    pipe = _make_pipeline(
        monkeypatch, tmp_path, cfg, {"yahoo_direct": src},
        queries=[], refill_sources=["yahoo_direct"],
    )
    try:
        add_watch(
            pipe.store, f"buyee_yahoo:{seller}", source=SOURCE_MANUAL,
            reason="測試", params=WatchParams(),
        )
        # 賣家的批次由鍵的雜湊決定，force 一次只認領一批 → 最多輪四次
        result = pipe.scan(watch_force=True)
        for _ in range(4):
            if src.seller_calls:
                break
            result = pipe.scan(watch_force=True)
        assert src.seller_calls == [seller], "監控賣家沒有被賣家頁列舉掃到"

        # 定價上架的卡進了 refill：查的是卡名、帳記了、來源標記分得出來
        assert src.sold_calls == ["青眼の白龍"]
        ref = result["refill"]
        assert ref is not None
        assert ref["selected_origin"]["青眼の白龍"] == {"auction": 0, "watch_fixed": 2}
        assert pipe.store.refill_cooldown_active(7) == {"青眼の白龍"}
    finally:
        pipe.close()


def test_pipeline_general_scan_fixed_price_stays_out_of_refill(monkeypatch, tmp_path, cfg):
    """範圍守衛：一般關鍵字掃描的定價上架**不進**佇列（競標照舊進）。"""
    auction = make_listing(
        price=1000, site=Site.BUYEE_YAHOO, external_id="a1",
        source="stub_sold", title="遊戯王 青眼の白龍 初期 PSA10 極美品",
        raw={"price_kind": "current_bid"},
    )
    fixed = make_listing(
        price=3000, site=Site.BUYEE_YAHOO, external_id="f1",
        source="stub_sold", title="遊戯王 ブラック・マジシャン 初期 PSA10 極美品",
        raw={"price_kind": "fixed"},
    )

    class _KeywordSource(_WatchSellerSource):
        def search_detailed(self, keyword, *, sold=False, pages=None, **kw):
            if sold:
                return super().search_detailed(keyword, sold=True, pages=pages, **kw)
            listings = [auction, fixed]
            return SearchResult(
                source=self.name, site=self.site.value, query=keyword,
                listings=listings, parsed_count=len(listings), pages_fetched=1,
            )

    src = _KeywordSource("stub_sold", [])
    pipe = _make_pipeline(
        monkeypatch, tmp_path, cfg, {"stub_sold": src},
        queries=[{"name": "t", "keyword": "遊戯王 PSA", "sources": ["stub_sold"]}],
        refill_sources=["stub_sold"],
    )
    try:
        result = pipe.scan()
        # 競標那張照舊進佇列；一般掃描的定價那張不准進
        assert "青眼の白龍" in src.sold_calls
        assert "ブラック・マジシャン" not in src.sold_calls
        ref = result["refill"]
        assert ref is not None
        assert "ブラック・マジシャン" not in ref["selected"]
        assert ref["selected_origin"]["青眼の白龍"]["watch_fixed"] == 0
    finally:
        pipe.close()
