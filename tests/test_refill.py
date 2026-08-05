"""需求驅動回補（refill.py）：選卡、節流帳、冪等、入庫過濾。零網路。

守的約束跟廣撒那條線同一組：**請求預算**與**同源過濾**。
- 選卡：缺樣本才選、冷卻中不選、上限截尾端、排序決定性。
- 節流帳：跨行程冪等（落 store）、查無結果也記帳（不然每輪重查零結果的卡）、
  來源全滅**不**記帳（讀不到 ≠ 沒有）。
- 入庫：走既有的 ingest_sold，同一組 parse_card + is_candidate，不另開後門。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeFx

from ygo_sniper.cards import CardIndex
from ygo_sniper.comps import CompsEngine
from ygo_sniper.domain import Currency, Listing, Site
from ygo_sniper.refill import (
    REFILL_PAGES,
    RefillParams,
    comps_count_by_card,
    run_refill,
    select_refill_cards,
)
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store


@pytest.fixture
def index():
    """三張年代內＋一張年代外的小主檔。名字取自真實卡（比對規則是真的）。"""
    return CardIndex(
        [
            {"id": 1, "name_ja": "青眼の白龍", "name_en": "Blue-Eyes White Dragon",
             "ocg_date": "1999-01-01"},
            {"id": 2, "name_ja": "ブラック・マジシャン", "name_en": "Dark Magician",
             "ocg_date": "1999-01-01"},
            {"id": 3, "name_ja": "真紅眼の黒竜", "name_en": "Red-Eyes Black Dragon",
             "ocg_date": "1999-01-01"},
        ],
        out_of_era={"灰流うらら": "2015-04-25"},
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "refill.db")


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


def _engine(cfg, store) -> CompsEngine:
    return CompsEngine(cfg, FakeFx(), store=store)


# ---------------------------------------------------------------------------
# 1. 選卡邏輯（純函式）
# ---------------------------------------------------------------------------
def test_select_only_matched_in_era_below_threshold(index):
    titles = [
        "遊戯王 青眼の白龍 初期 PSA10",     # 比對到、缺樣本 → 選
        "遊戯王 灰流うらら PSA10",          # 比對到但年代外 → 不選
        "遊戯王 初期 まとめ PSA10",          # 比不到卡名 → 不選（沒有可查的關鍵字）
    ]
    selected, cooled = select_refill_cards(
        titles, index, {"青眼の白龍": 0}, min_comps=3, max_cards=10,
    )
    assert [c.card_name for c in selected] == ["青眼の白龍"]
    assert selected[0].comps_n == 0
    assert cooled == []


def test_select_skips_cards_with_enough_comps(index):
    titles = ["遊戯王 青眼の白龍 初期 PSA10", "遊戯王 ブラック・マジシャン 初期 PSA10"]
    selected, _ = select_refill_cards(
        titles, index, {"青眼の白龍": 3, "ブラック・マジシャン": 2},
        min_comps=3, max_cards=10,
    )
    # 3 筆已達門檻 → 不選；2 筆仍缺 → 選
    assert [c.card_name for c in selected] == ["ブラック・マジシャン"]


def test_select_cooldown_excluded_but_reported(index):
    titles = ["遊戯王 青眼の白龍 初期 PSA10", "遊戯王 ブラック・マジシャン 初期 PSA10"]
    selected, cooled = select_refill_cards(
        titles, index, {}, min_comps=3, max_cards=10, cooling={"青眼の白龍"},
    )
    assert [c.card_name for c in selected] == ["ブラック・マジシャン"]
    assert [c.card_name for c in cooled] == ["青眼の白龍"]


def test_select_orders_by_demand_then_scarcity_and_caps(index):
    titles = [
        "遊戯王 青眼の白龍 初期 PSA10",
        "遊戯王 ブラック・マジシャン 初期 PSA10",
        "遊戯王 ブラック・マジシャン 初期 PSA9",   # 2 筆競標等這張 → 最優先
        "遊戯王 真紅眼の黒竜 初期 PSA10",
    ]
    counts = {"青眼の白龍": 2, "真紅眼の黒竜": 0}
    selected, _ = select_refill_cards(titles, index, counts, min_comps=3, max_cards=2)
    # 需求量優先（ブラマジ 2 筆）；同需求量比樣本數（真紅眼 0 < 青眼 2）；截到 2 張
    assert [c.card_name for c in selected] == ["ブラック・マジシャン", "真紅眼の黒竜"]


def test_comps_count_uses_index_match_not_card_name_column(index, store, cfg):
    """計數必須跟估價同一把尺：查詢時用 index.match(title)，不信 card_name 欄。"""
    engine = _engine(cfg, store)
    engine.ingest_sold([_sold_listing("遊戯王 青眼の白龍 初期 PSA10 極美品", external_id="a1")])
    rows = store.comps_by(limit=100)
    assert rows and rows[0].get("card_name") is None  # 欄位是空的（物化快取還沒回填）
    counts = comps_count_by_card(rows, index)
    assert counts == {"青眼の白龍": 1}  # 但計數照樣數得到


# ---------------------------------------------------------------------------
# 2. 節流帳（store）
# ---------------------------------------------------------------------------
def test_ledger_upsert_is_idempotent_across_calls(store):
    store.record_refill("青眼の白龍", found=5, kept=2)
    store.record_refill("青眼の白龍", found=0, kept=0)
    rows = store.refill_ledger()
    assert len(rows) == 1
    assert rows[0]["runs"] == 2
    assert rows[0]["last_found"] == 0 and rows[0]["last_kept"] == 0
    assert store.refill_cooldown_active(7) == {"青眼の白龍"}


def test_cooldown_expires_and_zero_disables(store):
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    store.record_refill("青眼の白龍", found=0, kept=0, now=old)
    store.record_refill("ブラック・マジシャン", found=1, kept=1)
    assert store.refill_cooldown_active(7) == {"ブラック・マジシャン"}  # 8 天前的已過期
    assert store.refill_cooldown_active(0) == set()                     # 0 = 冷卻關閉


# ---------------------------------------------------------------------------
# 3. run_refill（假來源，零網路）
# ---------------------------------------------------------------------------
def test_run_refill_queries_selected_cards_with_sold_and_one_page(index, store, cfg):
    src = _FakeSoldSource("fake_sold", [
        _sold_listing("遊戯王 青眼の白龍 初期 PSA10 極美品", external_id="s1"),
    ])
    report = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=index,
        titles=["遊戯王 青眼の白龍 初期 PSA10"], params=_params(),
    )
    assert [c["keyword"] for c in src.calls] == ["青眼の白龍"]  # 查的是卡名，不是標題
    assert src.calls[0]["sold"] is True
    assert src.calls[0]["pages"] == REFILL_PAGES == 1
    assert report.queries == report.queries_ok == 1
    assert report.kept == 1
    # 真的入庫了，而且下一次計數看得到（同一把尺）
    assert comps_count_by_card(store.comps_by(limit=100), index) == {"青眼の白龍": 1}


def test_run_refill_ingest_goes_through_existing_filter(index, store, cfg):
    """入庫走既有 ingest_sold：垃圾（年代外、無機構、低分）被同一組濾網擋下。"""
    src = _FakeSoldSource("fake_sold", [
        _sold_listing("遊戯王 青眼の白龍 初期 PSA10 極美品", external_id="ok1"),
        _sold_listing("遊戯王 青眼の白龍 25th シークレット PSA10", external_id="bad1"),  # 排除字
        _sold_listing("遊戯王 青眼の白龍 初期 美品", external_id="bad2"),               # 無鑑定機構
        _sold_listing("遊戯王 青眼の白龍 初期 PSA5", external_id="bad3"),               # 低於門檻
    ])
    report = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=index,
        titles=["遊戯王 青眼の白龍 初期 PSA10"], params=_params(),
    )
    assert report.kept == 1
    assert report.rejected == 3
    assert sum(report.reasons.values()) == 3
    rows = store.comps_by(limit=100)
    assert len(rows) == 1 and "PSA10 極美品" in rows[0]["title"]


def test_run_refill_zero_result_still_records_cooldown(index, store, cfg):
    """查無結果（EMPTY_CONFIRMED）也要記帳：第二輪必須整批跳過。"""
    src = _FakeSoldSource("fake_sold", [], health=ParseHealth.EMPTY_CONFIRMED)
    titles = ["遊戯王 青眼の白龍 初期 PSA10"]
    first = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=index,
        titles=titles, params=_params(),
    )
    assert first.per_card["青眼の白龍"] == {"found": 0, "kept": 0, "observed": True}
    assert store.refill_cooldown_active(7) == {"青眼の白龍"}

    second = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=index,
        titles=titles, params=_params(),
    )
    assert second.selected == []
    assert [c.card_name for c in second.skipped_cooldown] == ["青眼の白龍"]
    assert second.queries == 0 and len(src.calls) == 1  # 第二輪一個請求都沒發


def test_run_refill_all_sources_failed_does_not_record(index, store, cfg):
    """來源全滅（被擋）＝什麼都不知道 → 不記帳，下輪重試（讀不到 ≠ 沒有）。"""
    src = _FakeSoldSource("fake_sold", [], health=ParseHealth.BLOCKED)
    titles = ["遊戯王 青眼の白龍 初期 PSA10"]
    report = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=index,
        titles=titles, params=_params(),
    )
    assert report.queries == 1 and report.queries_ok == 0
    assert report.per_card["青眼の白龍"]["observed"] is False
    assert report.errors  # 失敗要外顯，不能安靜
    assert store.refill_cooldown_active(7) == set()

    again = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store), index=index,
        titles=titles, params=_params(),
    )
    assert [c.card_name for c in again.selected] == ["青眼の白龍"]  # 沒被冷卻擋住


def test_run_refill_partial_observation_records(index, store, cfg):
    """一個來源被擋、另一個完成觀測 → 記帳（至少一個來源看過了）。"""
    blocked = _FakeSoldSource("s_blocked", [], health=ParseHealth.BLOCKED)
    empty = _FakeSoldSource("s_empty", [], health=ParseHealth.EMPTY_CONFIRMED)
    report = run_refill(
        store=store, sources={"s_blocked": blocked, "s_empty": empty},
        comps=_engine(cfg, store), index=index,
        titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(sources=("s_blocked", "s_empty")),
    )
    assert report.queries == 2 and report.queries_ok == 1
    assert report.per_card["青眼の白龍"]["observed"] is True
    assert store.refill_cooldown_active(7) == {"青眼の白龍"}


def test_run_refill_dry_run_touches_nothing(index, store, cfg):
    class _Exploding:
        name = "fake_sold"
        site = Site.BUYEE_YAHOO
        supports_sold = True

        def search_detailed(self, *a, **k):
            raise AssertionError("dry-run 不得打任何查詢")

    report = run_refill(
        store=store, sources={"fake_sold": _Exploding()}, comps=_engine(cfg, store),
        index=index, titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(), dry_run=True,
    )
    assert [c.card_name for c in report.selected] == ["青眼の白龍"]
    assert report.dry_run is True and report.queries == 0
    assert store.refill_ledger() == []


def test_run_refill_source_exception_is_isolated(index, store, cfg):
    """來源丟例外（search_detailed 自己炸）→ 該格失敗、其餘照常、不往外拋。"""
    class _Raising:
        name = "s_bad"
        site = Site.BUYEE_YAHOO
        supports_sold = True

        def search_detailed(self, *a, **k):
            raise RuntimeError("source 程式自己炸了")

    good = _FakeSoldSource("s_good", [
        _sold_listing("遊戯王 青眼の白龍 初期 PSA10 極美品", external_id="g1"),
    ])
    report = run_refill(
        store=store, sources={"s_bad": _Raising(), "s_good": good},
        comps=_engine(cfg, store), index=index,
        titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(sources=("s_bad", "s_good")),
    )
    assert report.kept == 1
    assert any("RuntimeError" in e for e in report.errors)
    assert store.refill_cooldown_active(7) == {"青眼の白龍"}


def test_run_refill_missing_or_unsold_source_reported(index, store, cfg):
    """registry 沒有的名字／不支援 sold 的來源 → 跳過並外顯，不炸。"""
    class _NoSold:
        name = "s_nosold"
        site = Site.BUYEE_YAHOO
        supports_sold = False

    report = run_refill(
        store=store, sources={"s_nosold": _NoSold()}, comps=_engine(cfg, store),
        index=index, titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(sources=("ghost", "s_nosold")),
    )
    assert report.queries == 0
    assert len(report.errors) == 2


def test_run_refill_respects_disabled_and_missing_index(store, cfg):
    src = _FakeSoldSource("fake_sold", [])
    off = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store),
        index=CardIndex([]), titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(enabled=False),
    )
    assert off.selected == [] and off.queries == 0

    no_master = run_refill(
        store=store, sources={"fake_sold": src}, comps=_engine(cfg, store),
        index=CardIndex([]), titles=["遊戯王 青眼の白龍 初期 PSA10"],
        params=_params(),
    )
    assert no_master.selected == [] and no_master.errors  # 缺主檔要外顯


# ---------------------------------------------------------------------------
# 4. 參數載入
# ---------------------------------------------------------------------------
def test_params_from_config_defaults_and_overrides(cfg):
    bare = dataclasses.replace(cfg, watchlist={**cfg.watchlist, "refill": None})
    p = RefillParams.from_config(bare)
    assert (p.min_comps, p.max_cards_per_run, p.cooldown_days) == (3, 10, 7.0)
    assert p.enabled is True and "yahoo_closed" in p.sources

    tuned = dataclasses.replace(cfg, watchlist={**cfg.watchlist, "refill": {
        "min_comps": 5, "max_cards_per_run": 2, "cooldown_days": 1,
        "sources": ["yahoo_closed"], "enabled": False,
    }})
    p2 = RefillParams.from_config(tuned)
    assert (p2.min_comps, p2.max_cards_per_run, p2.cooldown_days) == (5, 2, 1.0)
    assert p2.sources == ("yahoo_closed",) and p2.enabled is False


def test_params_invalid_values_fall_back_with_warning(cfg, capsys):
    bad = dataclasses.replace(cfg, watchlist={**cfg.watchlist, "refill": {
        "min_comps": "abc", "max_cards_per_run": -1, "cooldown_days": "xyz",
    }})
    p = RefillParams.from_config(bad)
    assert (p.min_comps, p.max_cards_per_run, p.cooldown_days) == (3, 10, 7.0)
    out = capsys.readouterr().out
    assert "min_comps" in out and "max_cards_per_run" in out and "cooldown_days" in out


# ---------------------------------------------------------------------------
# 5. pipeline 掛鉤（零網路：stub registry、FakeFx、tmp db）
# ---------------------------------------------------------------------------
def test_pipeline_scan_runs_refill_and_uses_new_comps_same_round(monkeypatch, tmp_path, cfg):
    """掃到 L3 競標標的 → 同一輪回補 → 本輪就把新樣本用進估價（comps 視圖刷新）。"""
    import dataclasses as _dc

    import ygo_sniper.pipeline as pipeline_mod

    auction = Listing(
        site=Site.BUYEE_YAHOO, external_id="a1",
        title="遊戯王 青眼の白龍 初期 PSA10 極美品",
        url="https://buyee.jp/item/yahoo/auction/a1",
        price=1000.0, currency=Currency.JPY,
        raw={"price_kind": "current_bid"}, source="stub_sold",
    )

    class _StubSource:
        """在架搜尋回競標標的；sold=True 回同卡成交（refill 那條路）。"""

        name = "stub_sold"
        site = Site.BUYEE_YAHOO
        supports_sold = True
        sold_calls: list[str] = []

        def search_detailed(self, keyword, *, sold=False, pages=None, max_price=None, **kw):
            listings = [auction]
            if sold:
                self.sold_calls.append(keyword)
                listings = [_sold_listing(
                    f"遊戯王 {keyword} 初期 PSA10 美品", external_id=f"s-{len(self.sold_calls)}",
                )]
            return SearchResult(
                source=self.name, site=self.site.value, query=keyword,
                listings=listings, parsed_count=len(listings), pages_fetched=1,
            )

    stub = _StubSource()
    test_cfg = _dc.replace(
        cfg,
        root=tmp_path,  # db/cache 全落 tmp
        watchlist={
            **cfg.watchlist,
            "queries": [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["stub_sold"]}],
            "comps_queries": {},          # 廣撒那條線關掉，only refill 在動
            "refill": {"sources": ["stub_sold"], "max_cards_per_run": 3},
        },
        sources={},                        # 不跑 canary
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _c, _f=None: {"stub_sold": stub})
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _c: FakeFx())

    pipe = pipeline_mod.Pipeline(test_cfg)
    try:
        result = pipe.scan(dry_run=False)
    finally:
        pipe.close()

    # refill 真的跑了：查的是卡名（不是掃描關鍵字），帳也記了
    assert "青眼の白龍" in stub.sold_calls
    ref = result["refill"]
    assert ref is not None and ref["kept"] >= 1
    assert pipe.store.refill_cooldown_active(7) == {"青眼の白龍"}
    # 同一輪的估價已看得到新樣本：這張卡的出價判定應該站在 L1/L2（同卡成交）上。
    # 估價層級落在 payload["bid"]（BidCeiling 帶著 level 出門，ok 與否都帶）。
    row = pipe.store.list_signals(state="all", limit=10)[0]
    import json as _json

    bid = (_json.loads(row["payload"]).get("bid") or {})
    assert bid.get("level") in ("L1", "L2")

    # 第二輪：冷卻擋住，不再發 sold 查詢
    pipe2 = pipeline_mod.Pipeline(test_cfg)
    try:
        result2 = pipe2.scan(dry_run=False)
    finally:
        pipe2.close()
    assert len(stub.sold_calls) == 1
    ref2 = result2["refill"]
    assert ref2 is not None and ref2["queries"] == 0 and ref2["skipped_cooldown"]


def test_yahoo_closed_search_detailed_accepts_sold_kwarg(cfg):
    """refill 統一用 search_detailed(sold=True) 呼叫；yahoo_closed 必須吃得下。"""
    import inspect

    from ygo_sniper.sources.yahoo_closed import YahooClosedSource

    sig = inspect.signature(YahooClosedSource.search_detailed)
    assert "sold" in sig.parameters
