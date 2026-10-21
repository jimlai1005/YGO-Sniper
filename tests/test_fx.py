"""FxRates.updated_at：前端要顯示「JPY→TWD 0.213 · 更新 22:03」而不是 `cache` 字樣。"""
from __future__ import annotations

import json
import time
from dataclasses import replace

import httpx

from ygo_sniper import fx as fx_mod
from ygo_sniper.fx import FxRates


def test_fx_updated_at_comes_from_cache_timestamp(cfg, tmp_path):
    scoped = replace(cfg, storage={**cfg.storage, "cache_dir": str(tmp_path)})
    ts = time.time() - 3600
    (tmp_path / "fx.json").write_text(json.dumps({"ts": ts, "rates": {"JPY": 0.213, "USD": 31.2}}))
    fx = FxRates(scoped)
    assert fx.source == "cache"
    assert abs(fx.updated_at - ts) < 1
    assert fx.rates["JPY"] == 0.213


def test_fx_updated_at_none_when_fallback(cfg, tmp_path, monkeypatch):
    scoped = replace(cfg, storage={**cfg.storage, "cache_dir": str(tmp_path)})

    class Boom:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(fx_mod.httpx, "Client", Boom)
    fx = fx_mod.FxRates(scoped)
    assert fx.source == "fallback" and fx.updated_at is None
