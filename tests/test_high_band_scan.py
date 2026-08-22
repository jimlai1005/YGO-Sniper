"""高價帶掃描（¥8,624～50,000，只掛 buyee_mercari）：`Pipeline` 的 `high_band`
旗與 `signals.band` 欄位。見
`docs/superpowers/plans/2026-08-22-high-band-scan.md` Task 3。

五類要驗（各自對應 plan Task 3／Task 10 的紅燈測試）：

0. 守衛（2026-08-22 修正回合 W1）——`_price_ceiling_jpy` 算不出下限
   （回 None 或 ≤0）時，該來源**拒絕掃描**、回 PARSER_BROKEN 級大聲失敗，
   不准塌成「無下限、只有 ≤¥50,000 上限」（那會讓大量低價標的被寫成
   `band='high'` 並被規則 1/2/3 永久排除）。與 `load_high_band_queries`
   對上限缺失的既有立場（拒跑＋大聲）對齊。
1. 同源＋無縫無重疊（2026-08-22 修正回合 S3 更新）——高價帶輪的
   `min_price` ＝ 低價帶輪的 `max_price`（同一個 `_price_ceiling_jpy`
   呼叫）**+ 1**：Buyee 的 `price_min`／`price_max` 都是閉區間，不 +1
   的話恰好等於上限的商品會同時落在兩帶內，band 隨掃描順序翻面。
   高價帶上限是 watchlist 設定的 `max_price_jpy`。
2. 隔離——`high_band=True` 不跑 comps refresh／canary／賣家輪替監控／
   需求驅動回補，那些是低價帶完整輪的事。
3. band 欄位——高價帶輪落庫 `band='high'`；一般輪 `band='std'`；舊庫
   （沒有 band 欄位）遷移後既有列補 `'std'`（additive migration，重跑安全）。
4. 地平線分帶——**2026-08-22 reviewer Critical 1 修法後**：高價帶批次帶
   `exit_scope=True`、`band='high'`；`store.record_listing_scan` 的地平線
   判定分組鍵是 (site, band)，所以高價帶批次能為自己的 band 建地平線，
   同時不會誤判帶外（band='std'）、這一輪天生缺席的低價帶標的離場。
   （store 層的完整紅燈測試在 `tests/test_venue_study.py`，含高價帶輪
   自己能正常判 disappeared/window_exit 的情境，見高價帶掃描 plan Task 8。）
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest
from conftest import FakeFx, make_listing

import ygo_sniper.pipeline as pipeline_mod
from ygo_sniper.domain import Site
from ygo_sniper.pipeline import Pipeline
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.store import Store

_TITLE = "遊戯王 初期 ウルトラ PSA9 青眼の白龍"


class _RecordingSource:
    """假 buyee_mercari 來源：記錄每次 `search_detailed` 收到的價格參數，
    回傳可控的固定 listings（預設空）。"""

    def __init__(self, name: str = "hb_src", *, site: Site = Site.BUYEE_MERCARI,
                 listings=None) -> None:
        self.name = name
        self.site = site
        self.supports_sold = True
        self.supports_min_price = True
        self.supports_category = False
        self.listings = list(listings or [])
        self.calls: list[dict] = []

    def search_detailed(
        self, keyword, *, max_price=None, min_price=None, pages=None,
        category=None, sort=None,
    ) -> SearchResult:
        self.calls.append({"min_price": min_price, "max_price": max_price})
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=list(self.listings), parsed_count=len(self.listings),
        )


def _make_pipeline(monkeypatch, tmp_path, cfg, registry, watchlist):
    test_cfg = dataclasses.replace(cfg, root=tmp_path, watchlist=watchlist)
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    return Pipeline(test_cfg)


def _watchlist(cfg, *, std_queries=None, high_band=None):
    """疊在**真實** `cfg.watchlist` 之上（比照 `test_pipeline_isolation.py` 的
    `_make_pipeline`）——只換 `queries`／`high_band`，年代／分級／排除字規則
    照舊，否則 `is_candidate` 會因為缺規則而整批槓龜（不是這裡要測的東西）。
    """
    wl: dict = {**cfg.watchlist, "queries": std_queries or []}
    if high_band is not None:
        wl["high_band"] = high_band
    else:
        wl.pop("high_band", None)
    return wl


_HIGH_BAND_BLOCK = {
    "max_price_jpy": 50000,
    "queries": [{"name": "high", "keyword": "PSA 初期", "sources": ["hb_src"]}],
}


# ---------------------------------------------------------------------------
# 0. 守衛：ceiling 算不出來 → 拒絕掃描，不發任何請求（修正回合 W1）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_ceiling", [None, 0.0], ids=["none", "zero"])
def test_high_band_refuses_to_scan_when_ceiling_unavailable(
    monkeypatch, tmp_path, cfg, bad_ceiling,
):
    src = _RecordingSource()
    watchlist = _watchlist(cfg, high_band=_HIGH_BAND_BLOCK)
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"hb_src": src}, watchlist)
    monkeypatch.setattr(Pipeline, "_price_ceiling_jpy", lambda self, site: bad_ceiling)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True, high_band=True)
    finally:
        pipe.close()

    assert src.calls == [], (
        f"下限算不出來（ceiling={bad_ceiling!r}）時不准發任何請求：{src.calls}"
    )
    assert result["search_results"], "應該有一筆大聲失敗的 SearchResult，不是空手而回"
    res = result["search_results"][0]
    assert res.health == ParseHealth.PARSER_BROKEN
    assert "下限" in res.detail, res.detail


# ---------------------------------------------------------------------------
# 1. 同源＋無縫無重疊：高價帶下限 == 低價帶上限 + 1（修正回合 S3）
# ---------------------------------------------------------------------------
def test_high_band_min_price_is_low_band_max_price_plus_one(monkeypatch, tmp_path, cfg):
    src = _RecordingSource()
    watchlist = _watchlist(
        cfg,
        std_queries=[{"name": "std", "keyword": "遊戯王 PSA", "sources": ["hb_src"]}],
        high_band=_HIGH_BAND_BLOCK,
    )
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"hb_src": src}, watchlist)
    monkeypatch.setattr(Pipeline, "_price_ceiling_jpy", lambda self, site: 8624.0)
    try:
        pipe.scan(skip_comps=True, dry_run=True)                  # 低價帶輪
        pipe.scan(skip_comps=True, dry_run=True, high_band=True)  # 高價帶輪
    finally:
        pipe.close()

    assert len(src.calls) == 2, src.calls
    low_call, high_call = src.calls
    assert low_call == {"min_price": None, "max_price": 8624.0}
    # Buyee 的 price_min/price_max 都是閉區間：+1 才不會讓恰好等於上限的
    # 商品同時落在兩帶（band 隨掃描順序翻面）。
    assert high_call == {"min_price": 8625.0, "max_price": 50000.0}


# ---------------------------------------------------------------------------
# 2. 隔離：high_band=True 不跑 comps refresh／canary／賣家輪替監控／回補
# ---------------------------------------------------------------------------
def test_high_band_scan_skips_comps_canary_seller_watch_and_refill(
    monkeypatch, tmp_path, cfg,
):
    calls = {"refresh_comps": 0, "canaries": 0, "seller_watch": 0, "refill": 0}
    src = _RecordingSource()
    watchlist = _watchlist(cfg, high_band=_HIGH_BAND_BLOCK)
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"hb_src": src}, watchlist)
    monkeypatch.setattr(Pipeline, "_price_ceiling_jpy", lambda self, site: 8624.0)

    def spy_refresh(self, *, force=False):
        calls["refresh_comps"] += 1
        return 0

    def spy_canaries(self, summary):
        calls["canaries"] += 1
        return []

    def spy_watch(self, candidates, *, force=False):
        calls["seller_watch"] += 1
        return [], {"enabled": False}

    def spy_refill(self, auction_titles, watch_fixed_titles=None):
        calls["refill"] += 1
        return None

    monkeypatch.setattr(Pipeline, "refresh_comps", spy_refresh)
    monkeypatch.setattr(Pipeline, "_run_canaries", spy_canaries)
    monkeypatch.setattr(Pipeline, "_scan_watched_sellers", spy_watch)
    monkeypatch.setattr(Pipeline, "_refill_comps", spy_refill)

    try:
        pipe.scan(high_band=True)
    finally:
        pipe.close()

    assert calls == {"refresh_comps": 0, "canaries": 0, "seller_watch": 0, "refill": 0}
    assert src.calls, "高價帶查詢應該真的跑了，不然這個測試沒有驗到東西"


# ---------------------------------------------------------------------------
# 3. band 欄位
# ---------------------------------------------------------------------------
def test_high_band_signal_gets_band_high_std_scan_gets_band_std(
    monkeypatch, tmp_path, cfg,
):
    low_listing = make_listing(
        price=5000, site=Site.BUYEE_MERCARI, external_id="low1",
        title=_TITLE, source="hb_src",
    )
    high_listing = make_listing(
        price=22222, site=Site.BUYEE_MERCARI, external_id="high1",
        title=_TITLE, source="hb_src",
    )
    src = _RecordingSource(listings=[low_listing])
    watchlist = _watchlist(
        cfg,
        std_queries=[{"name": "std", "keyword": "遊戯王 PSA", "sources": ["hb_src"]}],
        high_band=_HIGH_BAND_BLOCK,
    )
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"hb_src": src}, watchlist)
    monkeypatch.setattr(Pipeline, "_price_ceiling_jpy", lambda self, site: 8624.0)
    try:
        pipe.scan(skip_comps=True)                  # 低價帶輪
        src.listings = [high_listing]
        pipe.scan(skip_comps=True, high_band=True)   # 高價帶輪
    finally:
        pipe.close()

    low_row = pipe.store.get_signal(low_listing.key)
    high_row = pipe.store.get_signal(high_listing.key)
    assert low_row is not None and high_row is not None
    assert low_row["band"] == "std"
    assert high_row["band"] == "high"


def test_migration_adds_band_column_defaulting_existing_rows_to_std(tmp_path):
    """舊 db（沒有 `band` 欄位）開一次就補上，既有列一律 `'std'`（additive
    migration，重跑安全——比照 `_migrate_signals` 對 `bucket` 的既有測試）。
    """
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as c:
        c.execute(
            """CREATE TABLE signals (
                 key TEXT PRIMARY KEY, site TEXT, title TEXT, url TEXT,
                 score REAL, flags TEXT, payload TEXT,
                 state TEXT DEFAULT 'new', note TEXT DEFAULT '',
                 first_seen TEXT, last_seen TEXT)"""
        )
        c.executemany(
            "INSERT INTO signals (key, title, score, state) VALUES (?,?,?,?)",
            [("old:1", "舊卡一", 10.0, "watching"), ("old:2", "舊卡二", 20.0, "bought")],
        )

    def _columns() -> set[str]:
        with sqlite3.connect(db) as c:
            return {r[1] for r in c.execute("PRAGMA table_info(signals)")}

    assert "band" not in _columns()

    Store(db)  # 開一次 = 觸發 migration
    Store(db)  # 重跑安全（不得因為欄位已存在而炸掉）

    assert "band" in _columns()
    with sqlite3.connect(db) as c:
        bands = [r[0] for r in c.execute("SELECT band FROM signals ORDER BY key")]
    assert bands == ["std", "std"]


# ---------------------------------------------------------------------------
# 4. 地平線：高價帶批次不建地平線，不誤判帶外標的離場
# ---------------------------------------------------------------------------
def test_high_band_round_does_not_exit_horizon_low_band_listings(monkeypatch, tmp_path, cfg):
    """**2026-08-22 reviewer Critical 1 修法後改寫**（原測試名
    `test_high_band_batch_does_not_create_exit_horizon`，斷言的是舊行為：
    高價帶批次 `exit_scope=False`，靠「完全不建地平線」來保護低價帶標的。
    reviewer 抓到那條路是死巷——高價帶批次自己的商品因此永遠沒有管道被判
    離場（CLAUDE.md 第五節第 8 條）。修法是分帶（`store.record_listing_scan`
    docstring「地平線分帶」段）：高價帶批次現在 `exit_scope=True`，但地平線
    判定的分組鍵是 (site, band)，所以低價帶標的（band='std'，價格帶外，
    高價帶批次天生看不到）在高價帶輪缺席，仍然不會被誤判離場——保護的
    機制從「不建地平線」換成「地平線只在同一個 band 裡比對」。
    """
    low_listing = make_listing(
        price=5000, site=Site.BUYEE_MERCARI, external_id="low1",
        title=_TITLE, source="hb_src",
    )
    high_listing = make_listing(
        price=22222, site=Site.BUYEE_MERCARI, external_id="high1",
        title=_TITLE, source="hb_src",
    )
    src = _RecordingSource(listings=[low_listing])
    watchlist = _watchlist(
        cfg,
        std_queries=[{"name": "std", "keyword": "遊戯王 PSA", "sources": ["hb_src"]}],
        high_band=_HIGH_BAND_BLOCK,
    )
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"hb_src": src}, watchlist)
    monkeypatch.setattr(Pipeline, "_price_ceiling_jpy", lambda self, site: 8624.0)

    captured: list = []
    orig = pipe.store.record_listing_scan

    def spy(batches, **kw):
        captured.append(batches)
        return orig(batches, **kw)

    monkeypatch.setattr(pipe.store, "record_listing_scan", spy)

    try:
        pipe.scan(skip_comps=True)                  # 低價帶輪：low1 進 listing_obs
        src.listings = [high_listing]                # 這一輪只有高價帶會撈到 high1
        pipe.scan(skip_comps=True, high_band=True)   # high1 進場，low1 這輪缺席
    finally:
        pipe.close()

    assert captured, "record_listing_scan 沒被呼叫，測試沒有驗到東西"
    high_round_batches = captured[-1]
    assert high_round_batches, "高價帶輪應該至少有一個批次"
    # exit_scope 改回 True（plan Task 8 第 4 點）：高價帶批次現在能且應該
    # 為自己的 band 建地平線，不再靠關掉判定來保護低價帶標的。
    assert all(b.get("exit_scope") is True for b in high_round_batches)
    assert all(b.get("band") == "high" for b in high_round_batches)

    obs = {r["key"]: r for r in pipe.store.listing_obs()}
    # 保護低價帶標的的機制換成了「地平線只在同一個 band 裡比對」——
    # low1 是 band='std'，高價帶批次的地平線（band='high'）不涉及它。
    assert obs[low_listing.key]["band"] == "std"
    assert obs[low_listing.key]["disappeared_at"] is None
    assert obs[low_listing.key]["window_exit_at"] is None
    assert obs[high_listing.key]["band"] == "high"
