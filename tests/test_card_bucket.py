"""卡片分類（`domain.CardBucket`）：migration、store、以及 dashboard 的 API。

這個功能的**核心不變式只有一條**：bucket 與 state 正交——改 state 不動 bucket。
沒有這條的話，使用者對一張高價卡按下「已購買」的瞬間，他手動指派的分類就沒了，
而且是靜默的（畫面不會有任何提示，他只會在分類分頁裡發現卡片變少）。
所以本檔的第一個測試就是「改 state 之後 bucket 還在」。

這裡也是這個 repo 第一組**打到 web 端點**的測試。fixture 的第一要務是把
`web.app` 的全域 store 指到 tmp 上：那支模組在 **import 的時候**就
`Store(cfg.db_path)`，照原樣 import 等於在測試裡開正式庫（CLAUDE.md 的紅線，
以及全域工程原則 4：測試不碰真實世界）。所以 load_config 要在 import 之前換掉，
而且換完要 assert 真的換到了——「以為指到 tmp」與「真的指到 tmp」外顯相同。
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ygo_sniper.domain import CardBucket, TriageState
from ygo_sniper.store import Store

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# migration：舊 db（沒有 bucket 欄）→ 開一次就補上，既有列原封不動，重跑安全
# ---------------------------------------------------------------------------
def _legacy_db(path: Path) -> None:
    """造一顆「bucket 欄位出現之前」的 signals 表，並塞兩列既有資料。"""
    with sqlite3.connect(path) as c:
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


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute("PRAGMA table_info(signals)")}


def _count(path: Path) -> int:
    with sqlite3.connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]


def test_migration_adds_bucket_without_touching_existing_rows(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    assert "bucket" not in _columns(db)
    before = _count(db)

    Store(db)

    assert "bucket" in _columns(db)
    assert _count(db) == before                 # 既有列一列都沒少
    with sqlite3.connect(db) as c:
        rows = dict(c.execute("SELECT key, state FROM signals").fetchall())
        buckets = [r[0] for r in c.execute("SELECT bucket FROM signals")]
    assert rows == {"old:1": "watching", "old:2": "bought"}   # state 沒被動到
    assert buckets == [None, None]                            # 新欄位一律 NULL


def test_migration_is_idempotent(tmp_path):
    """正式庫每 30 分鐘被排程開一次，所以「重跑安全」不是加分項是必要條件。"""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    Store(db)
    Store(db)          # 第二次不得炸（duplicate column name）
    Store(db)
    assert _count(db) == 2
    with sqlite3.connect(db) as c:
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_signals_bucket" in idx


# ---------------------------------------------------------------------------
# store：指派／清除／過濾／state 連動
# ---------------------------------------------------------------------------
def _insert(store: Store, key: str, *, state: str = "new", score: float = 50.0) -> None:
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "INSERT INTO signals (key, site, external_id, title, url, score, state,"
            " flags, payload, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, "buyee_mercari", key, f"卡 {key}", f"https://example.test/{key}",
             score, state, "[]", "{}", "2026-08-01T00:00:00+00:00",
             "2026-08-01T00:00:00+00:00"),
        )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "sniper.db")


def test_update_bucket_promotes_new_to_watching(store):
    """使用者原話：高價卡「**也是在觀察中**」。分類這個動作本身就是開始盯它了。"""
    _insert(store, "a", state=TriageState.NEW.value)
    store.update_bucket("a", CardBucket.HIGH_VALUE.value)
    row = store.get_signal("a")
    assert row["bucket"] == "high_value"
    assert row["state"] == TriageState.WATCHING.value


@pytest.mark.parametrize(
    "state",
    [TriageState.BOUGHT.value, TriageState.SKIPPED.value,
     TriageState.ASKED_SELLER.value, TriageState.IN_BUNDLE.value],
)
def test_update_bucket_never_rewinds_other_states(store, state):
    """把 bought／skipped 打回 watching＝憑空捏造一個沒發生過的決策。"""
    _insert(store, "a", state=state)
    store.update_bucket("a", CardBucket.RARE.value)
    row = store.get_signal("a")
    assert row["bucket"] == "rare"
    assert row["state"] == state


def test_clearing_bucket_leaves_state_alone(store):
    _insert(store, "a", state=TriageState.NEW.value)
    store.update_bucket("a", CardBucket.RARE.value)      # new → watching
    store.update_bucket("a", None)
    row = store.get_signal("a")
    assert row["bucket"] is None
    assert row["state"] == TriageState.WATCHING.value    # 不退回 new


def test_list_signals_filters_by_bucket_across_states(store):
    _insert(store, "hv-bought", state=TriageState.BOUGHT.value)
    _insert(store, "hv-new", state=TriageState.NEW.value)
    _insert(store, "rare-1", state=TriageState.NEW.value)
    _insert(store, "plain", state=TriageState.NEW.value)
    store.update_bucket("hv-bought", "high_value")
    store.update_bucket("hv-new", "high_value")
    store.update_bucket("rare-1", "rare")

    keys = {r["key"] for r in store.list_signals(state="all", bucket="high_value")}
    assert keys == {"hv-bought", "hv-new"}      # 跨狀態：已購買的也在分類裡
    assert {r["key"] for r in store.list_signals(state="all", bucket="rare")} == {"rare-1"}
    # 兩個維度可以併用
    both = store.list_signals(state=TriageState.BOUGHT.value, bucket="high_value")
    assert {r["key"] for r in both} == {"hv-bought"}


def test_rescan_does_not_wipe_bucket(store, fx, cfg):
    """例行掃描重寫既有列時不得洗掉分類（`_upsert_listing_obs` 抹掉 seller_id
    的那次事故是同一個形狀：無條件覆寫使用者手動維護的欄位）。"""
    from conftest import make_listing

    from ygo_sniper.domain import CardInfo, CompStats, Grader, RouteQuote, Signal

    lst = make_listing(price=1000)
    sig = Signal(
        listing=lst,
        card=CardInfo(grader=Grader.PSA, grade=9.0),
        best_route=RouteQuote(route="jp_direct", label="日本直寄", landed_twd=210.0,
                              item_twd=210.0, fee_twd=0.0, shipping_twd=0.0,
                              bundle_size=1),
        all_routes=[],
        comps=CompStats(n=0, median_twd=None, p25_twd=None, p40_twd=None,
                        p75_twd=None, window_days=90),
        flags=[],
        score=50.0,
        reason="test",
    )
    store.upsert_signal(sig)
    store.update_bucket(lst.key, "high_value")
    store.upsert_signal(sig)                     # 第二輪掃描看到同一筆
    assert store.get_signal(lst.key)["bucket"] == "high_value"


# ---------------------------------------------------------------------------
# API：TestClient 打真的端點
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    import ygo_sniper.config as config_mod

    db = tmp_path / "web.db"
    real_load = config_mod.load_config

    def _tmp_config(*a, **kw):
        # storage["db_path"] 給**絕對路徑**：Config.db_path 是 `root / 那個值`，
        # 右運算元是絕對路徑時 root 會被忽略，剛好就是我們要的效果。
        # replace() 而不是就地改：real_load 有 lru_cache，改它回傳的物件
        # 會汙染整個 suite 的設定。
        c = real_load(*a, **kw)
        return replace(c, storage={**c.storage, "db_path": str(db)})

    monkeypatch.setattr(config_mod, "load_config", _tmp_config)
    monkeypatch.syspath_prepend(str(ROOT))
    for mod in ("web.app", "web"):
        sys.modules.pop(mod, None)
    app_mod = importlib.import_module("web.app")
    try:
        # 承重的斷言，不是裝飾：這一行紅掉就代表測試正在開正式庫。
        assert app_mod.store.db_path == db, (
            f"web.app 的 store 沒有指到 tmp（{app_mod.store.db_path}）——"
            "測試絕不能碰正式庫 data/sniper.db"
        )
        from fastapi.testclient import TestClient

        yield TestClient(app_mod.app), app_mod
    finally:
        for mod in ("web.app", "web"):
            sys.modules.pop(mod, None)


def _keys(res) -> set[str]:
    return {i["key"] for i in res.json()["items"]}


def test_bucket_survives_a_state_change(client):
    """**本設計的核心**：指派分類 → 改 state 為 bought → 分類仍在。

    這正是「把分類塞進 state」會失敗的那一格：state 是互斥欄位，
    按下已購買的瞬間分類就被覆寫，而且沒有任何錯誤訊息。
    """
    c, app_mod = client
    _insert(app_mod.store, "sig:1", state=TriageState.NEW.value)

    r = c.post("/api/signals/sig:1/bucket", json={"bucket": "high_value"})
    assert r.status_code == 200, r.text
    assert r.json()["bucket"] == "high_value"
    assert r.json()["state"] == "watching"       # new 被連動升級

    assert _keys(c.get("/api/signals?state=all&bucket=high_value")) == {"sig:1"}

    r = c.post("/api/signals/sig:1/state", json={"state": "bought"})
    assert r.status_code == 200, r.text

    # 分類還在，而且分類分頁仍然撈得到（跨狀態）
    row = app_mod.store.get_signal("sig:1")
    assert row["bucket"] == "high_value"
    assert row["state"] == "bought"
    assert _keys(c.get("/api/signals?state=all&bucket=high_value")) == {"sig:1"}


def test_bucket_endpoint_clears_with_null(client):
    c, app_mod = client
    _insert(app_mod.store, "sig:1")
    c.post("/api/signals/sig:1/bucket", json={"bucket": "rare"})
    r = c.post("/api/signals/sig:1/bucket", json={"bucket": None})
    assert r.status_code == 200, r.text
    assert r.json()["bucket"] is None
    assert _keys(c.get("/api/signals?state=all&bucket=rare")) == set()


def test_bucket_endpoint_rejects_unknown_values(client):
    """未知值要**大聲拒絕**：安靜地寫進去，分類分頁就會永遠撈不到那張卡。"""
    c, app_mod = client
    _insert(app_mod.store, "sig:1")
    assert c.post("/api/signals/sig:1/bucket", json={"bucket": "expensive"}).status_code == 400
    assert c.post("/api/signals/nope/bucket", json={"bucket": "rare"}).status_code == 404
    # 查詢參數同樣不吞：未知分類回空清單的話，「沒有卡」與「打錯字」外顯相同
    assert c.get("/api/signals?state=all&bucket=expensive").status_code == 400
    assert app_mod.store.get_signal("sig:1")["bucket"] is None


def test_signals_listing_carries_bucket_field(client):
    """前端要靠這個欄位畫徽章與按鈕的亮暗——沒帶出來就是永遠不亮。"""
    c, app_mod = client
    _insert(app_mod.store, "sig:1")
    _insert(app_mod.store, "sig:2")
    c.post("/api/signals/sig:1/bucket", json={"bucket": "rare"})
    items = {i["key"]: i for i in c.get("/api/signals?state=all").json()["items"]}
    assert items["sig:1"]["bucket"] == "rare"
    assert items["sig:2"]["bucket"] is None
