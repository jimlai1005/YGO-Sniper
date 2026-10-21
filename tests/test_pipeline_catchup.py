"""補跑模式（排程漏跑之後多翻頁）與漏跑視窗帳在 pipeline 這一層的行為。

全程零網路：registry 換成假來源，FxRates 換成 FakeFx，db/cache 落在 tmp_path
（與 test_pipeline_isolation.py／test_scan_passes.py 同一套建法）。

canary 的辨識**不靠 pipeline 內部旗標**：假來源的 `canary_keyword` 設成獨一無二的
字串，`_run_canaries` 會拿它當 keyword 打進來；發現查詢用的是 watchlist 的關鍵字。
再另外驗證 canary 沒帶 max_price（`price_ceiling=False`），兩個獨立訊號互相佐證。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime

import pytest
from conftest import FakeFx, make_listing

import ygo_sniper.pipeline as pipeline_mod
from ygo_sniper import schedule_watch as sw
from ygo_sniper.domain import Site
from ygo_sniper.pipeline import Pipeline
from ygo_sniper.sources.health import SearchResult

CANARY_KW = "CANARY-KEYWORD-ONLY"
DISCOVERY_KW = "遊戯王 PSA"
FETCH_PAGES = 2   # 明確釘住「基準頁數」，補跑 +1 才分得出來


class _RecordingSource:
    """記錄每一次 search_detailed／search_seller 收到的 (keyword, pages, max_price)。"""

    name = "rec_src"
    site = Site.BUYEE_YAHOO
    supports_sold = False

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.seller_calls: list[dict] = []

    def search_detailed(self, keyword, *, max_price=None, pages=1, **_kw):
        self.calls.append({"keyword": keyword, "pages": pages, "max_price": max_price})
        listings = [
            make_listing(price=1500, site=self.site, external_id=f"r{i}", source=self.name)
            for i in range(3)
        ]
        return SearchResult(
            source=self.name, site=self.site.value, query=keyword,
            listings=listings, parsed_count=len(listings), pages_fetched=pages,
        )

    def search_seller(self, seller_id, *, pages=1, sold=False):
        self.seller_calls.append({"seller": str(seller_id), "pages": pages})
        return SearchResult(
            source=self.name, site=self.site.value, query=f"seller:{seller_id}",
            listings=[], parsed_count=0, pages_fetched=1,
        )


@pytest.fixture
def pipe_factory(monkeypatch, tmp_path, cfg):
    made: list[Pipeline] = []

    def factory(*, scan_overrides: dict | None = None, register_as: str = "rec_src"):
        src = _RecordingSource()
        src.name = register_as
        test_cfg = dataclasses.replace(
            cfg,
            root=tmp_path,   # db_path／cache_dir 全落 tmp
            watchlist={**cfg.watchlist, "queries": [
                {"name": "t", "keyword": DISCOVERY_KW, "sources": [register_as]},
            ]},
            sources={register_as: {"canary_keyword": CANARY_KW, "canary_min_results": 1}},
            fetch={**cfg.fetch, "max_pages_per_query": FETCH_PAGES},
            scan={**cfg.scan, **(scan_overrides or {})},
        )
        registry = {register_as: src}
        monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
        monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
        pipe = Pipeline(test_cfg)
        pipe.src = src
        made.append(pipe)
        return pipe

    yield factory
    for p in made:
        p.close()


def _gap(missed: int, caught_up_at: str | None = None) -> str:
    return json.dumps({
        "uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34",
        "missed_slots": missed, "first_slot": "2026-10-19T09:30:00",
        "last_slot": "2026-10-19T20:00:00", "detected_at": "2026-10-19T20:35:34",
        "caught_up_at": caught_up_at,
    })


def _split(src: _RecordingSource) -> tuple[list[dict], list[dict]]:
    canary = [c for c in src.calls if c["keyword"] == CANARY_KW]
    discovery = [c for c in src.calls if c["keyword"] != CANARY_KW]
    return discovery, canary


# ---------------------------------------------------------------------------
# 翻頁
# ---------------------------------------------------------------------------
def test_catchup_flag_adds_extra_pages_to_discovery_queries(pipe_factory):
    """scan(catchup=True) → 每個發現查詢 pages = 設定值 + catchup_extra_pages；canary 仍是 1。"""
    pipe = pipe_factory(scan_overrides={"catchup_extra_pages": 1})
    pipe.scan(catchup=True, dry_run=True, skip_comps=True)
    discovery, canary = _split(pipe.src)
    assert discovery, "沒有任何發現查詢被打到，測試沒有在驗東西"
    assert all(c["pages"] == FETCH_PAGES + 1 for c in discovery), discovery
    assert canary, "canary 沒被打到，測試沒有在驗東西"
    assert all(c["pages"] == 1 for c in canary), canary
    assert all(c["max_price"] is None for c in canary), "canary 不該帶價格上限"


def test_normal_scan_does_not_add_pages(pipe_factory):
    pipe = pipe_factory(scan_overrides={"catchup_extra_pages": 1})
    result = pipe.scan(dry_run=True, skip_comps=True)
    discovery, canary = _split(pipe.src)
    assert discovery and all(c["pages"] == FETCH_PAGES for c in discovery), discovery
    assert all(c["pages"] == 1 for c in canary)
    assert result["catchup"]["active"] is False


def test_extra_pages_zero_means_no_extra_pages_but_mode_still_active(pipe_factory):
    pipe = pipe_factory(scan_overrides={"catchup_extra_pages": 0})
    result = pipe.scan(catchup=True, dry_run=True, skip_comps=True)
    discovery, _canary = _split(pipe.src)
    assert all(c["pages"] == FETCH_PAGES for c in discovery)
    assert result["catchup"]["active"] is True


def test_seller_pages_are_not_extended_by_catchup(pipe_factory):
    """賣家頁 1 頁就是完整列舉（CLAUDE.md 第五節第 8 項），補跑模式不多翻。"""
    from ygo_sniper.seller_watch import SOURCE_MANUAL, WatchParams, add_watch

    pipe = pipe_factory(register_as="yahoo_direct")
    add_watch(
        pipe.store, "buyee_yahoo:SELLERID0123456789012345678", source=SOURCE_MANUAL,
        reason="測試", params=WatchParams(),
    )
    for _ in range(4):   # 輪替：一次只認領一批
        pipe.scan(catchup=True, dry_run=True, skip_comps=True, watch_force=True)
        if pipe.src.seller_calls:
            break
    assert pipe.src.seller_calls, "賣家沒被掃到，測試沒有在驗東西"
    assert all(c["pages"] == WatchParams().pages for c in pipe.src.seller_calls)


# ---------------------------------------------------------------------------
# 啟動條件
# ---------------------------------------------------------------------------
def test_catchup_auto_activates_from_uncaught_gap_record(pipe_factory):
    """帳上有 ≥ 門檻的未補跑視窗 → 不帶 catchup=True 也進補跑模式，result['catchup'] 帶視窗。"""
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": 2})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, _gap(10))
    result = pipe.scan(dry_run=True, skip_comps=True)
    assert result["catchup"]["active"] is True
    assert result["catchup"]["record"]["missed_slots"] == 10
    discovery, _ = _split(pipe.src)
    assert all(c["pages"] == FETCH_PAGES + 1 for c in discovery)


def test_catchup_not_activated_below_threshold(pipe_factory):
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": 2})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, _gap(1))
    result = pipe.scan(dry_run=True, skip_comps=True)
    assert result["catchup"]["active"] is False
    discovery, _ = _split(pipe.src)
    assert all(c["pages"] == FETCH_PAGES for c in discovery)


def test_catchup_not_activated_after_caught_up(pipe_factory):
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": 2})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, _gap(10, caught_up_at="2026-10-19T21:15:00"))
    assert pipe.scan(dry_run=True, skip_comps=True)["catchup"]["active"] is False


def test_manual_catchup_does_not_adopt_an_already_caught_up_record(pipe_factory):
    """W1：已補跑的帳不是「這一輪要回顧的對象」——--catchup 仍多翻頁，但 record 為 None。"""
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": 2})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, _gap(10, caught_up_at="2026-10-19T21:15:00"))
    result = pipe.scan(catchup=True, dry_run=True, skip_comps=True)
    assert result["catchup"]["active"] is True
    assert result["catchup"]["record"] is None
    discovery, _ = _split(pipe.src)
    assert all(c["pages"] == FETCH_PAGES + 1 for c in discovery)


def test_broken_catchup_config_does_not_take_down_the_scan(pipe_factory, capsys):
    """S1：設定壞掉（鍵留空）→ 警告＋本輪不補跑，掃描照跑完、scan_status 不卡在 running。"""
    pipe = pipe_factory(scan_overrides={"catchup_min_missed_slots": None})
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, _gap(10))
    result = pipe.scan(dry_run=True, skip_comps=True)
    assert result["catchup"]["active"] is False
    assert result["catchup"]["record"] is None
    assert "補跑模式判定失敗" in capsys.readouterr().out
    discovery, _ = _split(pipe.src)
    assert discovery and all(c["pages"] == FETCH_PAGES for c in discovery)


def test_high_band_and_watch_only_never_catch_up(pipe_factory):
    """高價帶不碰這本帳；watch-scan 沒有關鍵字查詢，補跑沒有意義。"""
    pipe = pipe_factory()
    pipe.store.set_meta(sw.GAP_WINDOW_KEY, _gap(10))
    assert pipe.scan(dry_run=True, skip_comps=True, high_band=True, catchup=True)["catchup"]["active"] is False
    assert pipe.scan(dry_run=True, watch_only=True, catchup=True)["catchup"]["active"] is False


# ---------------------------------------------------------------------------
# 寫帳
# ---------------------------------------------------------------------------
def test_gap_record_is_written_by_update_schedule_state(pipe_factory):
    """上一輪 09:00 起跑、這一輪 20:35 起跑 → 帳上出現 10 格的視窗。"""
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T09:00:01")
    pipe.store.set_meta(sw.CLOCK_TZ_KEY, "Asia/Taipei")
    pipe.store.set_meta(sw.RUN_FINISHED_KEY, "2026-10-19T10:15:23")
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 19, 20, 35, 34))
    rec = sw.decode_gap_record(pipe.store.get_meta(sw.GAP_WINDOW_KEY))
    assert rec["missed_slots"] == 10 and rec["caught_up_at"] is None
    assert rec["uncovered_from"] == "2026-10-19T09:00:01"
    assert rec["uncovered_to"] == "2026-10-19T20:35:34"
    # 基準照常推進（既有行為不變）
    assert pipe.store.get_meta(sw.RUN_STARTED_KEY) == "2026-10-19T20:35:34"


def test_no_gap_record_when_nothing_missed(pipe_factory):
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T18:00:05")
    pipe.store.set_meta(sw.RUN_FINISHED_KEY, "2026-10-19T18:10:00")
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 19, 18, 30, 7))
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None


def test_gap_record_not_written_on_dry_run(pipe_factory):
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T09:00:01")
    pipe._update_schedule_state(dry_run=True, watch_only=False, now=datetime(2026, 10, 19, 20, 35, 34))
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None


def test_high_band_never_touches_gap_record(pipe_factory):
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY_HIGH, "2026-10-19T10:00:00")
    pipe._update_schedule_state(
        dry_run=False, watch_only=False, high_band=True, now=datetime(2026, 10, 19, 20, 35),
    )
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None


def test_gap_record_failure_does_not_block_baseline_or_scan(pipe_factory, monkeypatch, capsys):
    """寫視窗帳壞掉屬資訊性功能失敗：大聲印、不拋，且不吃掉本輪告警與基準推進。"""
    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T09:00:01")
    pipe.store.set_meta(sw.CLOCK_TZ_KEY, "Asia/Taipei")
    pipe.store.set_meta(sw.RUN_FINISHED_KEY, "2026-10-19T10:15:23")
    monkeypatch.setattr(pipeline_mod, "resolve_gap_record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 19, 20, 35, 34))
    out = capsys.readouterr().out
    assert "漏跑視窗帳寫入失敗" in out and "boom" in out
    # 告警與基準不被連坐
    assert pipe._schedule_alert and "漏跑" in pipe._schedule_alert
    assert pipe.store.get_meta(sw.RUN_STARTED_KEY) == "2026-10-19T20:35:34"
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None


# ---------------------------------------------------------------------------
# 排程時鐘基準切換的一次性保護（schedule-pin-taipei Task 1 Step 4）
# ---------------------------------------------------------------------------
def test_first_run_after_clock_change_resets_baseline_without_alert(pipe_factory):
    from ygo_sniper import schedule_watch as sw

    pipe = pipe_factory()
    pipe.store.set_meta(sw.RUN_STARTED_KEY, "2026-10-19T09:00:01")   # 舊基準（Chicago）
    pipe.store.set_meta(sw.RUN_FINISHED_KEY, "2026-10-19T10:15:23")
    assert pipe.store.get_meta(sw.CLOCK_TZ_KEY) is None
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 21, 13, 30))
    assert pipe._schedule_alert is None
    assert pipe.store.get_meta(sw.GAP_WINDOW_KEY) is None
    assert pipe.store.get_meta(sw.CLOCK_TZ_KEY) == "Asia/Taipei"
    assert pipe.store.get_meta(sw.RUN_STARTED_KEY) == "2026-10-21T13:30:00"
    # 第二輪開始正常偵測：13:30 → 18:00 之間漏了 15:30、17:30
    pipe._update_schedule_state(dry_run=False, watch_only=False, now=datetime(2026, 10, 21, 18, 0, 30))
    assert pipe._schedule_alert and "漏跑 2 個時段" in pipe._schedule_alert
