"""掃描狀態與清單清理。

這裡釘的是「使用者按下立即掃描之後看到什麼」。最重要的一條是
`test_running_but_timed_out_is_not_running`：掃描中途被 kill／機器睡著時
`finish_scan` 永遠不會被呼叫，只看 running 旗標的話 dashboard 的按鈕會
**永遠** disabled，而使用者沒有任何自救手段。逾時是唯一的兜底。
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from conftest import FakeFx, make_listing

import ygo_sniper.pipeline as pipeline_mod
from ygo_sniper.domain import Site, TriageState
from ygo_sniper.pipeline import Pipeline
from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "t.db")


# ---------------------------------------------------------------------------
# 1. 狀態機本身
# ---------------------------------------------------------------------------
def test_fresh_db_is_not_running(store):
    st = store.scan_status()
    assert st["running"] is False
    assert st["stale"] is False
    assert st["started_at"] is None
    assert st["last_run"] is None


def test_begin_marks_running(store):
    started = store.begin_scan(trigger="dashboard")
    st = store.scan_status(timeout_seconds=1800)
    assert st["running"] is True
    assert st["started_at"] == started
    assert st["trigger"] == "dashboard"
    assert st["elapsed_seconds"] >= 0


def test_finish_clears_running_and_keeps_result(store):
    started = store.begin_scan()
    store.finish_scan(started, result={"scanned": 5, "signals": 2})

    st = store.scan_status()
    assert st["running"] is False
    assert st["finished_at"]
    assert st["last_result"] == {"scanned": 5, "signals": 2}
    assert st["error"] is None


def test_finish_with_error_is_not_running_and_keeps_reason(store):
    """掃爆了也必須離開 running——而且要留下原因，不是靜靜地變成『沒在掃』。"""
    started = store.begin_scan()
    store.finish_scan(started, error="BlockedError: 被擋")

    st = store.scan_status()
    assert st["running"] is False
    assert st["error"] == "BlockedError: 被擋"


def test_running_but_timed_out_is_not_running(store):
    """⚠️ 本檔最重要的一條：有 started_at、沒有 finished_at、但已超過逾時。

    模擬的正是「掃描中途崩潰」——那一輪永遠不會呼叫 finish_scan。
    如果這條紅了，dashboard 的掃描按鈕會被一次崩潰永久鎖死。
    """
    store.begin_scan()
    # 把開始時間往前推到兩小時前（崩潰的那一輪就長這樣）
    raw = json.loads(store.get_meta(Store.SCAN_STATUS_KEY))
    raw["started_at"] = "2020-01-01T00:00:00+00:00"
    store.set_meta(Store.SCAN_STATUS_KEY, json.dumps(raw))

    st = store.scan_status(timeout_seconds=1800)
    assert st["running"] is False, "逾時的掃描仍被當成進行中 → 按鈕永遠鎖死"
    assert st["stale"] is True, "要標成 stale，前端才說得出『上次沒回報完成』"
    assert st["elapsed_seconds"] > 1800


def test_still_running_just_under_timeout(store):
    """逾時是有方向的判斷：還沒超過就仍然算在跑，不可以兩邊都放行。"""
    store.begin_scan()
    st = store.scan_status(timeout_seconds=1800)
    assert st["running"] is True and st["stale"] is False


def test_unparseable_started_at_is_treated_as_dead(store):
    """讀不到開始時間 ≠ 它還在跑。無法證明活著就當死的，寧可多放行一次掃描。"""
    store.set_meta(
        Store.SCAN_STATUS_KEY,
        json.dumps({"running": True, "started_at": "not-a-timestamp"}),
    )
    st = store.scan_status(timeout_seconds=1800)
    assert st["running"] is False
    assert st["stale"] is True


def test_corrupt_status_json_degrades_to_not_running(store):
    store.set_meta(Store.SCAN_STATUS_KEY, "{壞掉的 json")
    assert store.scan_status()["running"] is False


def test_begin_overwrites_stale_running(store):
    """殘留的 running 不該擋住下一次掃描。"""
    store.begin_scan(trigger="cli")
    second = store.begin_scan(trigger="dashboard")
    st = store.scan_status()
    assert st["started_at"] == second
    assert st["trigger"] == "dashboard"


def test_last_run_comes_from_runs_table(store):
    """『上一次掃完是什麼時候』只有一個來源：runs 表最後一列。"""
    store.log_run(started_at="2026-08-01T00:00:00+00:00", scanned=10, signals=3)
    store.log_run(started_at="2026-08-02T00:00:00+00:00", scanned=20, signals=7)
    last = store.scan_status()["last_run"]
    assert last["scanned"] == 20 and last["signals"] == 7


# ---------------------------------------------------------------------------
# 2. Pipeline 端：兩個入口都會落狀態，例外也不會卡住
# ---------------------------------------------------------------------------
class _GoodSource:
    name = "src_b"
    site = Site.BUYEE_YAHOO
    supports_sold = False

    def __init__(self, listings):
        self.listings = listings

    def search(self, keyword, **_kw):
        return list(self.listings)


class _ExplodingSource:
    name = "boom"
    site = Site.BUYEE_YAHOO
    supports_sold = False

    def search(self, keyword, **_kw):
        raise RuntimeError("source 炸了")


def _pipeline(monkeypatch, tmp_path, cfg, registry, queries, **over):
    test_cfg = dataclasses.replace(
        cfg, root=tmp_path, watchlist={**cfg.watchlist, "queries": queries}, **over
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    return Pipeline(test_cfg)


def test_scan_leaves_status_finished(monkeypatch, tmp_path, cfg):
    listings = [make_listing(price=1500, site=Site.BUYEE_YAHOO, external_id="b1")]
    pipe = _pipeline(
        monkeypatch, tmp_path, cfg,
        {"src_b": _GoodSource(listings)},
        [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["src_b"]}],
    )
    try:
        pipe.scan(skip_comps=True, dry_run=True, trigger="dashboard")
        st = pipe.store.scan_status()
    finally:
        pipe.close()

    assert st["running"] is False
    assert st["trigger"] == "dashboard"
    assert st["finished_at"] and st["last_result"]["scanned"] == 1


def test_scan_crash_does_not_leave_status_running(monkeypatch, tmp_path, cfg):
    """掃描過程拋例外 → 例外照樣往外傳，但狀態必須離開 running。

    來源層的例外被 _scan_source 吃掉了，所以這裡改讓 store 寫入時炸，
    模擬「掃到一半整個流程死掉」。
    """
    listings = [make_listing(price=1500, site=Site.BUYEE_YAHOO, external_id="b1")]
    pipe = _pipeline(
        monkeypatch, tmp_path, cfg,
        {"src_b": _GoodSource(listings)},
        [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["src_b"]}],
    )
    monkeypatch.setattr(
        pipe.store, "snapshot",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db 掛了")),
    )
    try:
        with pytest.raises(RuntimeError):
            pipe.scan(skip_comps=True)
        st = pipe.store.scan_status()
    finally:
        pipe.close()

    assert st["running"] is False, "掃描炸了卻卡在 running，按鈕會鎖到逾時為止"
    assert "db 掛了" in (st["error"] or "")


# ---------------------------------------------------------------------------
# 3. 清單清理：只動你從沒碰過的 new 列
# ---------------------------------------------------------------------------
def _insert(store: Store, key: str, state: str, last_seen: str) -> None:
    with store._conn() as c:
        c.execute(
            "INSERT INTO signals (key, site, external_id, title, url, state, last_seen, score)"
            " VALUES (?,?,?,?,?,?,?,0)",
            (key, "buyee_yahoo", key, "t", "u", state, last_seen),
        )


def test_expire_only_touches_untouched_new_rows(store):
    old, fresh = "2020-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00"
    _insert(store, "old_new", TriageState.NEW.value, old)
    _insert(store, "old_asked", TriageState.ASKED_SELLER.value, old)   # 人工標過，不准動
    _insert(store, "old_bundle", TriageState.IN_BUNDLE.value, old)     # 同上
    _insert(store, "fresh_new", TriageState.NEW.value, fresh)

    assert store.expire_stale_signals(30) == 1
    assert store.get_signal("old_new")["state"] == TriageState.EXPIRED.value
    assert store.get_signal("old_asked")["state"] == TriageState.ASKED_SELLER.value
    assert store.get_signal("old_bundle")["state"] == TriageState.IN_BUNDLE.value
    assert store.get_signal("fresh_new")["state"] == TriageState.NEW.value
    # 冪等：第二次沒有東西可過期
    assert store.expire_stale_signals(30) == 0


def test_expire_disabled_when_days_not_positive(store):
    _insert(store, "old_new", TriageState.NEW.value, "2020-01-01T00:00:00+00:00")
    assert store.expire_stale_signals(0) == 0
    assert store.get_signal("old_new")["state"] == TriageState.NEW.value
