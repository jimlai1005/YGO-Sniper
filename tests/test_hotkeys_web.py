"""`GET /api/signals/{key}/state`：dashboard 的 u 復原在送出前先問後端「這張卡現在是什麼」。

帳上記的狀態與後端不同（清除實證、CLI、另一個視窗改過）就不復原——所以這支必須
回**寫完讀回來的值**，不是快取。client fixture 與 `_signal_for` 照抄
`tests/test_valuation_cache_web.py`（含「不准開正式庫」承重斷言）。
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


def _signal_for(key: str, *, site: str = "buyee_yahoo"):
    from ygo_sniper.domain import (
        CardInfo,
        CompStats,
        Currency,
        Listing,
        RouteQuote,
        Signal,
        Site,
    )

    prefix = f"{site}:"
    external_id = key[len(prefix):] if key.startswith(prefix) else key
    listing = Listing(
        site=Site(site), external_id=external_id, title=f"卡 {key}",
        url=f"https://example.test/{key}", price=1000.0, currency=Currency.JPY,
    )
    assert listing.key == key, f"測試 key 形狀不對：{listing.key!r} != {key!r}"
    route = RouteQuote(
        route="direct", label="直寄", landed_twd=250.0, item_twd=220.0,
        fee_twd=10.0, shipping_twd=20.0, bundle_size=1,
    )
    return Signal(
        listing=listing,
        card=CardInfo(),
        best_route=route,
        all_routes=[route],
        comps=CompStats(n=0, median_twd=None, p25_twd=None, p40_twd=None,
                        p75_twd=None, window_days=90),
        flags=[],
        score=50.0,
        reason="",
    )


def test_get_state_reflects_latest_write(client):
    c, app_mod = client
    key = "buyee_yahoo:u123456789"
    app_mod.store.upsert_signal(_signal_for(key))

    r = c.get(f"/api/signals/{key}/state")
    assert r.status_code == 200
    assert r.json() == {"key": key, "state": "new"}

    # 走別的路徑改掉（模擬 CLI／另一個視窗），GET 必須看到新值——u 復原靠這個拒絕過期的帳
    app_mod.store.update_state(key, "skipped")
    assert c.get(f"/api/signals/{key}/state").json()["state"] == "skipped"


def test_get_state_unknown_key_is_404_not_empty(client):
    c, _ = client
    r = c.get("/api/signals/buyee_yahoo:nope/state")
    # 讀不到要大聲說讀不到：前端據此「不復原」，不能回 200＋空值被當成某個狀態
    assert r.status_code == 404
