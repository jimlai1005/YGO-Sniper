"""`POST /api/scan-high`（高價帶掃描 plan Task 14）：獨立按鈕、共用同一個
全域 scan 狀態，兩顆按鈕互斥不並跑（見 `web/app.py:trigger_scan_high` docstring）。

client fixture 照抄 `tests/test_card_snipe_web.py` 的模式——包含那條**承重的**
斷言：`app_mod.store.db_path` 沒指到 tmp 就直接紅，因為 `web/app.py` 是在
import 時就建好 `store`／`cfg` 單例的，monkeypatch 晚一步就會開到正式庫。

背景任務：Starlette 的 `BackgroundTasks` 在 `TestClient` 底下會在回應送出前
（ASGI 呼叫完整結束前）同步跑完，所以 `c.post(...)` 一回來背景任務就已經
執行過了——若不 monkeypatch `ygo_sniper.pipeline.Pipeline`，這裡會真的去
建 Pipeline、開 Playwright、打外部來源。monkeypatch 一個假 Pipeline 只記錄
呼叫參數，驗證「打對了 high_band=True／trigger」，不驗證掃描本身
（那是 `tests/test_high_band_scan.py` 的事）。
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    import ygo_sniper.config as config_mod

    db = tmp_path / "web.db"
    config_mod.load_config.cache_clear()
    real_load = config_mod.load_config

    def _tmp_config(*a, **kw):
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


class _DummyPipeline:
    """記錄呼叫參數，不碰任何外部來源。"""

    calls: list[dict] = []

    def __init__(self, *a, **kw):
        pass

    def scan(self, **kw):
        _DummyPipeline.calls.append(kw)
        return {"scanned": 0, "candidates": 0, "signals": 0, "new": 0}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _reset_dummy_pipeline():
    _DummyPipeline.calls = []
    yield
    _DummyPipeline.calls = []


def _patch_pipeline(monkeypatch):
    import ygo_sniper.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "Pipeline", _DummyPipeline)


def test_scan_high_triggers_with_high_band_flag(client, monkeypatch):
    """沒有掃描在跑時，`/api/scan-high` 要回 `started:true`，
    而且背景任務打的是 `Pipeline.scan(high_band=True, trigger="dashboard-high")`
    ——不是複製貼上漏改成低價帶那條路徑。"""
    c, _app = client
    _patch_pipeline(monkeypatch)

    r = c.post("/api/scan-high")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["started"] is True
    assert body["running"] is True

    assert _DummyPipeline.calls == [
        {"high_band": True, "trigger": "dashboard-high"}
    ]


def test_scan_high_is_mutually_exclusive_with_a_running_scan(client, monkeypatch):
    """另一輪（不論低價帶或高價帶）已經在跑時，`/api/scan-high` 回
    `started:false`——兩條掃描共用同一個全域 scan 狀態，不並跑
    （工程原則：Playwright 不該兩個並開）。"""
    c, app_mod = client
    _patch_pipeline(monkeypatch)

    app_mod.store.begin_scan(trigger="dashboard")  # 模擬「立即掃描」正在跑
    r = c.post("/api/scan-high")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["started"] is False
    assert body["running"] is True
    assert _DummyPipeline.calls == []  # 沒有另外排一輪背景任務


def test_scan_is_mutually_exclusive_with_a_running_high_scan(client, monkeypatch):
    """反過來：高價掃描在跑時，「立即掃描」（`/api/scan`）也要回
    `started:false`——互斥是雙向的，不是只有高價帶讓步。"""
    c, app_mod = client
    _patch_pipeline(monkeypatch)

    app_mod.store.begin_scan(trigger="dashboard-high")
    r = c.post("/api/scan")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["started"] is False
    assert body["running"] is True


def test_dashboard_html_has_high_scan_button_and_band_chip():
    """SPA 是單檔——按鈕（`scan-high-btn`／`scanHighNow`）與訊號卡片的
    band chip（`bandChip`／`it.band === 'high'`）三件都要在，且 chip 要真的
    被接進 `card()` 的輸出模板，不能只是定義了一個沒人呼叫的函式。"""
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="scan-high-btn"' in html
    assert "function scanHighNow" in html
    assert 'onclick="scanHighNow()"' in html
    assert "function bandChip" in html
    assert "it.band === \"high\"" in html
    assert "${bandChip(it)}" in html
