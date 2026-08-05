"""來源隔離：任一發現管道壞掉（含拋例外），其他管道照常產出。

這是本專案的核心約束（PLAN Phase 3）。測試全程零網路：
registry 用 monkeypatch 換成假 source，FxRates 換成 conftest 的 FakeFx，
db/cache 落在 tmp_path。
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import FakeFx, make_listing

import ygo_sniper.pipeline as pipeline_mod
from ygo_sniper.comps import CompsEngine
from ygo_sniper.domain import Site
from ygo_sniper.pipeline import Pipeline
from ygo_sniper.sources.base import BlockedError


class _RaisingSource:
    """search 一被呼叫就拋指定例外的假來源。"""

    def __init__(self, name: str, exc: Exception, *, site: Site = Site.BUYEE_MERCARI) -> None:
        self.name = name
        self.site = site
        self.supports_sold = True
        self.exc = exc

    def search(self, keyword, **_kw):
        raise self.exc


class _GoodSource:
    """回固定 listings 的假來源（走舊 search() 介面，驗證包裝路徑）。"""

    def __init__(self, name: str, listings, *, site: Site = Site.BUYEE_YAHOO) -> None:
        self.name = name
        self.site = site
        self.supports_sold = False
        self.listings = listings

    def search(self, keyword, **_kw):
        return list(self.listings)


def _make_pipeline(monkeypatch, tmp_path, cfg, registry, queries):
    """建一條零網路的 Pipeline：假 registry、FakeFx、tmp db/cache。"""
    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,  # db_path / cache_dir 都在 root 底下 → 全部落到 tmp
        watchlist={**cfg.watchlist, "queries": queries},
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    return Pipeline(test_cfg)


@pytest.fixture
def good_listings():
    return [
        make_listing(price=1500, site=Site.BUYEE_YAHOO, external_id=f"b{i}", source="src_b")
        for i in range(3)
    ]


def test_blocked_source_does_not_break_others(monkeypatch, tmp_path, cfg, good_listings):
    """核心：A 拋 BlockedError，B 的 3 筆照常進結果，整個 scan 不拋例外。"""
    registry = {
        "src_a": _RaisingSource("src_a", BlockedError("stub 擋掉", url="stub://a")),
        "src_b": _GoodSource("src_b", good_listings),
    }
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["src_a", "src_b"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, registry, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)  # 不能 raise
    finally:
        pipe.close()

    assert result["scanned"] == 3  # B 的三筆都在，沒有被 A 拖下水
    assert result["sources"]["src_a"] == {"health": "blocked", "count": 0, "detail": "stub 擋掉"}
    assert result["sources"]["src_b"]["health"] == "ok"
    assert result["sources"]["src_b"]["count"] == 3
    # SearchResult 物件要完整帶回（Phase 4 告警要吃）
    assert {r.source for r in result["search_results"]} == {"src_a", "src_b"}


def test_unexpected_exception_maps_to_parser_broken(monkeypatch, tmp_path, cfg, good_listings):
    """A 拋普通 RuntimeError → 摘要記 parser_broken（含例外型別），B 照常。"""
    registry = {
        "src_a": _RaisingSource("src_a", RuntimeError("source 程式自己炸了")),
        "src_b": _GoodSource("src_b", good_listings),
    }
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["src_a", "src_b"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, registry, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    a = result["sources"]["src_a"]
    assert a["health"] == "parser_broken"
    assert "RuntimeError" in a["detail"]  # 歸因要看得到例外型別
    assert result["sources"]["src_b"]["count"] == 3
    assert result["scanned"] == 3


def test_unknown_source_name_is_skipped(monkeypatch, tmp_path, cfg, good_listings, capsys):
    """watchlist 出現 registry 沒有的名字 → warn 跳過，不炸、不進摘要。"""
    registry = {"src_b": _GoodSource("src_b", good_listings)}
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["ghost_source", "src_b"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, registry, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    assert result["scanned"] == 3
    assert "ghost_source" not in result["sources"]
    assert "ghost_source" in capsys.readouterr().out  # 有 warn，不是靜默吞掉


def test_sold_queries_respect_supports_sold(cfg):
    """comps 的「已售出」查詢只含 supports_sold=True 的來源（不看名稱前綴）。"""
    registry = {
        "yahoo_like": _GoodSource("yahoo_like", []),          # supports_sold=False
        "buyee_like": _RaisingSource("buyee_like", RuntimeError()),  # supports_sold=True
    }
    test_cfg = dataclasses.replace(
        cfg,
        watchlist={
            **cfg.watchlist,
            "queries": [
                {"name": "t", "keyword": "遊戯王 PSA", "sources": ["yahoo_like", "buyee_like"]},
                {"name": "u", "keyword": "遊戯王 ARS", "sources": ["yahoo_like"]},
            ],
        },
    )
    engine = CompsEngine(test_cfg, FakeFx(), store=None)
    assert engine.sold_queries(registry) == [("buyee_like", "遊戯王 PSA")]


# ---------------------------------------------------------------------------
# 賣家頁輪替：Yahoo 拍賣（2026-08-04 補上解析器之前，這一站每輪都被跳過）
# ---------------------------------------------------------------------------
class _SellerPageSource:
    """有 `search_seller` 的假來源（賣家頁列舉的介面）。"""

    def __init__(self, name, listings, *, site=Site.BUYEE_YAHOO):
        self.name = name
        self.site = site
        self.supports_sold = False
        self.listings = listings
        self.calls: list[str] = []

    def search_seller(self, seller_id, *, pages=1, sold=False):
        from ygo_sniper.sources.health import SearchResult

        self.calls.append(str(seller_id))
        return SearchResult(
            source=self.name, site=self.site.value, query=f"seller:{seller_id}",
            url=f"https://auctions.yahoo.co.jp/seller/{seller_id}",
            listings=list(self.listings), parsed_count=len(self.listings),
            pages_fetched=1,
        )


def test_watched_yahoo_seller_is_actually_scanned(monkeypatch, tmp_path, cfg):
    """`buyee_yahoo` 的監控賣家必須真的被掃到並落 `listing_obs`。

    這是 2026-08-04 之前最大的覆蓋缺口：名單上 16 個賣家有 6 個是這一站
    （含分數最高的三個），全部被記成「來源尚未支援」跳過。
    """
    from ygo_sniper.seller_watch import SOURCE_MANUAL, WatchParams, add_watch

    seller = "8m1fe2VPnJdV8xkRDfgL2TymZEJbW"
    listings = [
        make_listing(
            price=3000, site=Site.BUYEE_YAHOO, external_id=f"y{i}",
            source="yahoo_direct", title="遊戯王 初期 青眼の白龍 PSA10 ウルトラ",
            seller_id=seller,
        )
        for i in range(2)
    ]
    src = _SellerPageSource("yahoo_direct", listings)
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"yahoo_direct": src}, [])
    try:
        add_watch(
            pipe.store, f"buyee_yahoo:{seller}", source=SOURCE_MANUAL,
            reason="測試", params=WatchParams(),
        )
        result = pipe.scan(watch_only=True, watch_force=True)
        report = result["seller_watch"]
        # 這一批的批次可能不是這一輪輪到的，force 也只認領一批 → 最多輪四次
        for _ in range(4):
            if src.calls:
                break
            result = pipe.scan(watch_only=True, watch_force=True)
            report = result["seller_watch"]
        assert src.calls == [seller], "Yahoo 監控賣家沒有被賣家頁列舉掃到"
        assert not any(
            "尚未" in s["reason"] or "沒有賣家頁列舉" in s["reason"]
            for s in report["skipped"]
        )
        obs = {r["key"] for r in pipe.store.listing_obs()}
        assert {lst.key for lst in listings} <= obs
    finally:
        pipe.close()


def test_seller_page_batch_never_drives_exit_scope(monkeypatch, tmp_path, cfg):
    """賣家頁批次一律 `exit_scope=False`——只看得到一個賣家的貨。"""
    from ygo_sniper.seller_watch import SOURCE_MANUAL, WatchParams, add_watch

    seller = "AiUkMq1pEUfNxvPeCv5PnfGpsFLrx"
    src = _SellerPageSource("yahoo_direct", [
        make_listing(
            price=3000, site=Site.BUYEE_YAHOO, external_id="y9",
            source="yahoo_direct", title="遊戯王 初期 青眼の白龍 PSA10 ウルトラ",
            seller_id=seller,
        )
    ])
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"yahoo_direct": src}, [])
    try:
        add_watch(
            pipe.store, f"buyee_yahoo:{seller}", source=SOURCE_MANUAL,
            reason="測試", params=WatchParams(),
        )
        for _ in range(4):
            batches, _report = pipe._scan_watched_sellers([], force=True)
            if batches:
                break
        assert batches, "沒有掃到任何賣家，這個測試就沒有在驗東西"
        assert all(b["exit_scope"] is False for b in batches)
    finally:
        pipe.close()
