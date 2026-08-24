"""`/api/signals` 只讀估價快取——連 lazy 補算都不做。

client fixture 照抄 `tests/test_expiry_clear.py:970-1000`（含「不准開正式庫」
承重斷言）；`_signal_for` 照抄同檔 :435-470；`_val_row` 照抄
`tests/test_valuation_cache_store.py`（Task 1 helper，同形）。
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
    """組一個最小可用的 Signal 給 upsert_signal 用。

    照抄 tests/test_expiry_clear.py::_signal_for（同 helper，理由與 key 形狀
    斷言見該檔 docstring）。
    """
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


def _val_row(key: str, **over):
    row = {
        "key": key, "p_worth_buying": 0.42, "fair_twd": 1234.0,
        "est_level_label": "L1", "resale_json": '{"ok": false, "reason": "stub"}',
        "comps_n": 100, "computed_at": "2026-08-24T00:00:00+00:00",
    }
    row.update(over)
    return row


def test_signals_reads_cache_and_never_builds_valuator(client, monkeypatch):
    """/api/signals 只讀快取。`build_valuator` 被打到就代表又在現算——
    守在源頭而不是 web 端的包裝函式（那個包裝已在 2026-08-24 確認為死碼並移除）。"""
    tc, app_mod = client
    app_mod.store.upsert_signal(_signal_for("buyee_yahoo:a1"))
    app_mod.store.upsert_valuations([_val_row("buyee_yahoo:a1")])

    def _boom(*a, **kw):
        raise AssertionError("/api/signals 不准建 valuator")

    monkeypatch.setattr("ygo_sniper.valuation.build_valuator", _boom)
    res = tc.get("/api/signals?state=all&limit=10").json()
    it = res["items"][0]
    assert it["p_worth_buying"] == 0.42
    assert it["fair_twd"] == 1234.0
    assert it["est_level_label"] == "L1"
    assert it["resale"] == {"ok": False, "reason": "stub"}


def test_signals_without_cache_shows_honest_nulls(client):
    tc, app_mod = client
    app_mod.store.upsert_signal(_signal_for("buyee_yahoo:fresh"))
    res = tc.get("/api/signals?state=all&limit=10").json()
    it = res["items"][0]
    assert it["p_worth_buying"] is None and it["resale"] is None
    assert res["p_worth_known"] == 0


def test_signals_surfaces_cache_error_and_timestamp(client):
    tc, app_mod = client
    from ygo_sniper.valuation_cache import (
        VALUATION_CACHE_AT_KEY, VALUATION_CACHE_ERROR_KEY)
    app_mod.store.set_meta(VALUATION_CACHE_ERROR_KEY, "3 列估價失敗（首例：x）")
    app_mod.store.set_meta(VALUATION_CACHE_AT_KEY, "2026-08-24T12:00:00+00:00")
    res = tc.get("/api/signals?state=all&limit=10").json()
    assert "估價失敗" in res["valuation_error"]
    assert res["valuation_cached_at"] == "2026-08-24T12:00:00+00:00"
