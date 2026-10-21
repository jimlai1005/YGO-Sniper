"""`POST /api/daily-run`（dashboard「補跑 daily」）與 `GET /api/schedule/gap`。

按鈕走 `scripts/run_daily.sh --catchup`（subprocess），測試絕不真的跑腳本：
`web.app._spawn_daily` 一律換成假函式。鎖路徑用 `cfg.db_path.parent / "run_daily.lock"`，
fixture 覆寫 db_path 到 tmp，所以測試碰不到正式的 `data/run_daily.lock`。

client fixture 照抄 `tests/test_scan_high_web.py`，含那條承重的 `db_path` 斷言。
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


def test_daily_run_spawns_run_daily_with_catchup(client, monkeypatch):
    c, app_mod = client
    calls = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda argv, **kw: calls.append((argv, kw)) or object())
    r = c.post("/api/daily-run")
    assert r.status_code == 200 and r.json()["started"] is True
    argv, kw = calls[0]
    assert argv[-2:] == ["scripts/run_daily.sh", "--catchup"]
    assert str(kw.get("cwd")).endswith("ygo-sniper")
    assert kw.get("start_new_session") is True


def test_daily_run_refuses_when_scan_running(client, monkeypatch):
    c, app_mod = client
    app_mod.store.begin_scan(trigger="cli")
    called = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda *a, **k: called.append(1))
    r = c.post("/api/daily-run")
    assert r.json()["started"] is False and not called


def test_daily_run_refuses_when_lock_held_by_live_pid(client, monkeypatch):
    import os

    c, app_mod = client
    lock = app_mod.cfg.db_path.parent / "run_daily.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(os.getpid()))
    called = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda *a, **k: called.append(1))
    r = c.post("/api/daily-run")
    assert r.json()["started"] is False and "鎖" in r.json()["message"] and not called


def test_daily_run_ignores_stale_lock_with_dead_pid(client, monkeypatch):
    """殘鎖（pid 已死）不擋——清殘鎖是腳本自己的事，route 只在確定有人活著時才拒絕。"""
    import subprocess

    c, app_mod = client
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    lock = app_mod.cfg.db_path.parent / "run_daily.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(str(p.pid))
    called = []
    monkeypatch.setattr(app_mod, "_spawn_daily", lambda *a, **k: called.append(1) or object())
    assert c.post("/api/daily-run").json()["started"] is True and called


def test_schedule_gap_endpoint_returns_record_or_null(client):
    import json

    from ygo_sniper.schedule_watch import GAP_WINDOW_KEY

    c, app_mod = client
    assert c.get("/api/schedule/gap").json() == {"gap": None}
    rec = {"uncovered_from": "2026-10-19T09:00:01", "uncovered_to": "2026-10-19T20:35:34",
           "missed_slots": 10, "first_slot": "2026-10-19T09:30:00", "last_slot": "2026-10-19T20:00:00",
           "detected_at": "2026-10-19T20:35:34", "caught_up_at": None}
    app_mod.store.set_meta(GAP_WINDOW_KEY, json.dumps(rec))
    assert c.get("/api/schedule/gap").json()["gap"]["missed_slots"] == 10
