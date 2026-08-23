"""兩趟抓取（新着＋即將結標）在 pipeline 這一層的行為。

釘住三件事，每一件壞掉的症狀都不會有錯誤訊息：

1. **每一趟都真的跑到、而且帶對排序**——只跑一趟的話畫面看起來完全正常，
   只是永遠看不到快結標的標的（實測新着那趟的結標倒數中位數 115 小時）。
2. **合併去重**——同一個標的兩趟都出現時它是一筆。不去重的話 `scanned` 會
   憑空膨脹，而評分／落庫會對同一筆做兩次。
3. **一趟壞掉時，這一批不可觀測**（healthy=False）。離場判定拿殘缺的批次去
   推論「它下架了」，就是把一次 WAF 挑戰記成賣光。

全程零網路：假 source，db/cache 落在 tmp_path。
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import FakeFx, make_listing

import ygo_sniper.pipeline as pipeline_mod
from ygo_sniper.domain import Site
from ygo_sniper.pipeline import Pipeline, dedupe_listings
from ygo_sniper.sources.health import ParseHealth, SearchResult
from ygo_sniper.sources.yahoo import ScanPass


class _TwoPassSource:
    """有 scan_passes() 的假來源：每一趟回自己的 listings，並記下收到的 sort。"""

    name = "fake_yahoo"
    site = Site.BUYEE_YAHOO
    supports_sold = False

    def __init__(
        self,
        per_mode: dict[str, list],
        *,
        health: dict | None = None,
        page_size: int = 1,
    ) -> None:
        self.per_mode = per_mode
        self.health = health or {}
        self.calls: list[tuple[str | None, int]] = []
        # pipeline 用 `page_size` 判斷第一趟有沒有裝滿。預設 1 = 第一趟必定滿，
        # 所以「會不會跑第二趟」這件事在測試裡是由這個值明確控制的，不是巧合。
        self.page_size = page_size

    def scan_passes(self) -> list[ScanPass]:
        return [ScanPass(mode, 1) for mode in self.per_mode]

    def search_detailed(self, keyword, *, max_price=None, pages=1, sort=None):
        self.calls.append((sort, pages))
        listings = list(self.per_mode.get(sort, []))
        return SearchResult(
            source=self.name,
            site=self.site.value,
            query=keyword,
            listings=listings,
            parsed_count=len(listings),
            health=self.health.get(sort, ParseHealth.OK),
        )


def _make_pipeline(monkeypatch, tmp_path, cfg, registry, queries) -> Pipeline:
    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,
        watchlist={**cfg.watchlist, "queries": queries},
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    return Pipeline(test_cfg)


def _auction(external_id: str, title: str = "遊戯王 初期 PSA 10 青眼の白龍"):
    """一筆競標中的 Yahoo 標的（price_kind=current_bid，會被當候選收進來）。"""
    return make_listing(
        price=1500,
        site=Site.BUYEE_YAHOO,
        title=title,
        external_id=external_id,
        source="fake_yahoo",
        raw={"price_kind": "current_bid", "current_bid": 1500},
    )


# ---------------------------------------------------------------------------
# 1. dedupe_listings 本身
# ---------------------------------------------------------------------------
def test_dedupe_listings_keeps_first_and_preserves_order():
    a, b, c = _auction("a1"), _auction("b2"), _auction("c3")
    dup = _auction("b2")  # 同一個 key，第二趟又看到一次
    res = [
        SearchResult(source="s", site="buyee_yahoo", query="q", listings=[a, b]),
        SearchResult(source="s", site="buyee_yahoo", query="q", listings=[dup, c]),
    ]

    out = dedupe_listings(res)

    assert [x.external_id for x in out] == ["a1", "b2", "c3"]
    assert out[1] is b  # 先到者留，不是被第二趟覆蓋


# ---------------------------------------------------------------------------
# 2. pipeline 真的跑兩趟、帶對排序、合併去重
# ---------------------------------------------------------------------------
def test_scan_runs_both_passes_and_dedupes(monkeypatch, tmp_path, cfg):
    shared = _auction("shared")          # 兩趟都出現的那一筆
    # page_size=2 → 第一趟解析 2 筆＝裝滿一頁 → pool 可能被截斷 → 才跑第二趟。
    # 這是 2026-08-02 的實測結論落成的規則：pool 沒滿的時候第二趟必然是同一批，
    # 跑它只是白花一倍請求（實測新增 0 筆）。
    src = _TwoPassSource({
        "newest": [_auction("new1"), shared],
        "ending_soon": [shared, _auction("end1"), _auction("end2")],
    }, page_size=2)
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["fake_yahoo"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"fake_yahoo": src}, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    # 兩趟都真的跑了，而且各自帶自己的排序模式
    assert [sort for sort, _pages in src.calls] == ["newest", "ending_soon"]
    # 5 筆原始 − 1 筆重複 = 4
    assert result["scanned"] == 4
    assert result["sources"]["fake_yahoo"]["count"] == 4
    # 兩趟都留下自己的 SearchResult（健康判定逐趟保留）
    assert sum(1 for r in result["search_results"] if r.source == "fake_yahoo") == 2


def test_pass_count_does_not_double_count_summary(monkeypatch, tmp_path, cfg):
    """兩趟拿到**完全相同**的一批時，摘要筆數不可以變成兩倍。"""
    same = [_auction("x1"), _auction("x2")]
    src = _TwoPassSource({"newest": list(same), "ending_soon": list(same)})
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["fake_yahoo"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"fake_yahoo": src}, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    assert result["scanned"] == 2
    assert result["sources"]["fake_yahoo"]["count"] == 2


# ---------------------------------------------------------------------------
# 3. 一趟壞掉：健康取最嚴重，而且這一批不可觀測
# ---------------------------------------------------------------------------
def test_one_broken_pass_makes_batch_unobservable(monkeypatch, tmp_path, cfg):
    src = _TwoPassSource(
        {"newest": [_auction("n1")], "ending_soon": []},
        health={"ending_soon": ParseHealth.BLOCKED},
    )
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["fake_yahoo"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"fake_yahoo": src}, queries)

    captured: list[dict] = []
    monkeypatch.setattr(
        pipe.store, "record_listing_scan",
        lambda batches, **kw: captured.extend(batches) or {},
    )
    try:
        pipe.scan(skip_comps=True, dry_run=False)
    finally:
        pipe.close()

    assert captured, "沒有落任何在架觀測批次"
    batch = next(b for b in captured if b["source"] == "fake_yahoo")
    assert batch["healthy"] is False, "一趟被擋時，這一批的缺席不可以被當成離場證據"


def test_sort_kwarg_not_passed_to_sources_without_passes(monkeypatch, tmp_path, cfg):
    """沒有 scan_passes 的來源不得收到 sort 參數（會 TypeError 變成假 parser_broken）。"""

    class _Plain:
        name = "plain"
        site = Site.BUYEE_MERCARI
        supports_sold = False

        def search_detailed(self, keyword, *, max_price=None, pages=1):
            return SearchResult(
                source=self.name, site=self.site.value, query=keyword,
                listings=[], parsed_count=0,
            )

    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["plain"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"plain": _Plain()}, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    assert result["sources"]["plain"]["health"] == ParseHealth.OK.value


# ---------------------------------------------------------------------------
# 4. 出貨設定：兩個通道都開著（這是本次功能的重點，改掉要有人知道）
# ---------------------------------------------------------------------------
def test_shipping_config_enables_both_yahoo_channels(cfg):
    passes = (cfg.sources.get("yahoo_direct") or {}).get("scan_passes") or {}
    enabled = [m for m, b in passes.items() if (b or {}).get("enabled", True)]
    assert enabled == ["newest", "ending_soon"], (
        "出貨設定應該兩個通道都開：只跑新着會系統性地看不到快結標的標的"
    )


@pytest.mark.parametrize("mode", ["newest", "ending_soon"])
def test_shipping_config_pages_are_sane(cfg, mode):
    block = ((cfg.sources.get("yahoo_direct") or {}).get("scan_passes") or {})[mode]
    assert int(block.get("pages", 1)) >= 1


# ---------------------------------------------------------------------------
# 第二趟是**條件式**的：只有第一趟裝滿（pool 可能被截斷）才跑
# ---------------------------------------------------------------------------
def test_second_pass_is_skipped_when_pool_fits_in_one_page(monkeypatch, tmp_path, cfg):
    """第一趟沒裝滿 = 已經看到整個 pool，第二趟必然是同一批。

    2026-08-02 實測：帶價格上限後各 query 的 pool 是 34/38/12/2 件，一頁 50 筆
    裝得下，ending_soon 那趟新增 **0 筆**——固定跑兩趟等於白花一倍請求。
    """
    src = _TwoPassSource({
        "newest": [_auction("a"), _auction("b")],
        "ending_soon": [_auction("c")],
    }, page_size=50)          # 解析 2 筆 << 50，沒裝滿
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["fake_yahoo"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"fake_yahoo": src}, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    assert [sort for sort, _pages in src.calls] == ["newest"], "沒被截斷卻還跑第二趟"
    assert result["scanned"] == 2
    # ending_soon 的 "c" 不該出現——它本來就在 pool 內，只是這次沒被抓
    assert sum(1 for r in result["search_results"] if r.source == "fake_yahoo") == 1


def test_second_pass_runs_when_first_pass_fills_the_page(monkeypatch, tmp_path, cfg):
    """第一趟裝滿 = 視野外可能還有東西，而**被截斷是無聲的**。

    單看新着那一趟，被截掉的正好是「快結標」那一群——也就是唯一可行動的那群。
    所以裝滿時必須補跑，成本隨市場自動調整，不必有人記得改設定。
    """
    src = _TwoPassSource({
        "newest": [_auction("a"), _auction("b")],
        "ending_soon": [_auction("c")],
    }, page_size=2)           # 解析 2 筆 == 一頁 2 筆，滿了
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["fake_yahoo"]}]
    pipe = _make_pipeline(monkeypatch, tmp_path, cfg, {"fake_yahoo": src}, queries)
    try:
        result = pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    assert [sort for sort, _pages in src.calls] == ["newest", "ending_soon"]
    assert result["scanned"] == 3
