"""狙擊 tab 的 API（dashboard 與 CLI 共用 `card_snipe` 那一支政策）。

client fixture 照抄 `tests/test_seller_watch.py` 的模式——包含那條**承重的**
斷言：`app_mod.store.db_path` 沒指到 tmp 就直接紅，因為 `web/app.py` 是在
import 時就建好 `store`／`cfg` 單例的，monkeypatch 晚一步就會開到正式庫。

網路：`POST /api/snipe` 用 `mine=False` ＋ PSA ＋ 無 evidence ——三者都是
「不外呼」的路徑（PSA 的 census 未支援、沒有證據 URL 可抓、sources=None
跳過市場檔案挖掘），所以這個檔案一個外部請求都不會發。
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
        # ⚠️ 這裡**不要**呼叫 config_mod.load_config.cache_clear()：此刻它還是被
        # monkeypatch 換上去的純函式（monkeypatch 是上游 fixture，teardown 在後），
        # 沒有 cache_clear → AttributeError。既有的 test_seller_watch.py 原版就沒有這行。
        for mod in ("web.app", "web"):
            sys.modules.pop(mod, None)


def test_snipe_list_empty(client):
    c, _app = client
    r = c.get("/api/snipe")
    assert r.status_code == 200 and r.json() == {"watches": []}


def test_snipe_add_detail_remove_roundtrip(client):
    c, _app = client
    # mine=False ＋ PSA ＋ 無 evidence → 完全不打網路
    r = c.post("/api/snipe", json={
        "name_ja": "魔法の筒", "grader": "PSA", "grade": "10", "code": "P4-06",
        "mine": False,
    })
    assert r.status_code == 200, r.text
    wid = r.json()["watch_id"]
    assert any("已登錄狙擊" in m for m in r.json()["messages"])

    r = c.get("/api/snipe")
    ws = r.json()["watches"]
    assert len(ws) == 1 and ws[0]["code_norm"] == "P4-6"
    assert ws[0]["hit_counts"] == {"exact": 0, "partial": 0, "near": 0}

    r = c.get(f"/api/snipe/{wid}")
    body = r.json()
    assert body["watch"]["id"] == wid
    assert isinstance(body["recommendation"], list) and body["recommendation"]
    # 三個桶必須各自獨立回傳（出處不同的數字不可合併）
    assert body["sales"] == [] and body["local_history"] == []
    assert "evidence" in body and "hits" in body

    r = c.post(f"/api/snipe/{wid}/remove")
    assert r.status_code == 200
    assert c.get("/api/snipe").json()["watches"] == []


def test_snipe_add_rejects_bad_input_with_400(client):
    c, _app = client
    r = c.post("/api/snipe", json={"name_ja": "x", "grader": "CGC", "grade": "10"})
    assert r.status_code == 400
    assert "不認得的鑑定機構" in r.text


def test_snipe_tab_is_wired_in_the_spa(client):
    """SPA 是單檔——tab 按鈕、view 容器、loadSnipe 三件都要在。"""
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-view="snipe"' in html
    assert 'id="snipe-view"' in html
    assert "function loadSnipe" in html
    assert "function snipeMine" in html
    assert "市場成交檔案" in html          # 主要資料桶要畫在使用者臉上
    assert "snipe-view" in html.split("function setView")[1].split("}")[0] or \
           'getElementById("snipe-view")' in html
