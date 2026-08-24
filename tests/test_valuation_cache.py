"""refresh_valuation_cache：整批落庫、逐列失敗不中斷、meta 誠實。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ygo_sniper import valuation_cache as vc
from ygo_sniper.store import Store


def _signal_for(key: str, *, site: str = "buyee_yahoo"):
    """組一個最小可用的 Signal 給 upsert_signal 用。

    照抄 tests/test_expiry_clear.py::_signal_for（同 Task 1）。
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


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    return s


def _stub_est(**over):
    base = dict(p_worth_buying=0.4, fair_twd=999.0, level_label="L1")
    base.update(over)
    return SimpleNamespace(**base)


def test_refresh_writes_all_rows_and_meta(store, monkeypatch):
    for k in ("buyee_yahoo:a1", "buyee_yahoo:a2"):
        store.upsert_signal(_signal_for(k))
    monkeypatch.setattr(vc, "resale_for_row", lambda *a, **kw: {"ok": False, "reason": "stub"})
    monkeypatch.setattr("ygo_sniper.valuation.build_valuator", lambda *a, **kw: object())
    monkeypatch.setattr("ygo_sniper.valuation.estimate_signal_row", lambda v, r: _stub_est())
    summary = vc.refresh_valuation_cache(cfg=None, store=store, fx=None)
    assert summary["rows"] == 2 and summary["errors"] == 0
    rows = store.list_signals(state="all", limit=10)
    assert all(r["val_p_worth_buying"] == 0.4 for r in rows)
    assert store.get_meta(vc.VALUATION_CACHE_AT_KEY)
    assert store.get_meta(vc.VALUATION_CACHE_ERROR_KEY) == ""


def test_per_row_failure_writes_nulls_and_error_meta(store, monkeypatch):
    for k in ("buyee_yahoo:ok", "buyee_yahoo:boom"):
        store.upsert_signal(_signal_for(k))
    monkeypatch.setattr(vc, "resale_for_row", lambda *a, **kw: {"ok": False, "reason": "stub"})
    monkeypatch.setattr("ygo_sniper.valuation.build_valuator", lambda *a, **kw: object())

    def _est(v, r):
        if "boom" in r["key"]:
            raise ValueError("炸")
        return _stub_est()

    monkeypatch.setattr("ygo_sniper.valuation.estimate_signal_row", _est)
    summary = vc.refresh_valuation_cache(cfg=None, store=store, fx=None)
    assert summary["errors"] == 1
    rows = {r["key"]: r for r in store.list_signals(state="all", limit=10)}
    assert rows["buyee_yahoo:boom"]["val_p_worth_buying"] is None   # 誠實留白
    assert rows["buyee_yahoo:ok"]["val_p_worth_buying"] == 0.4      # 好的照寫
    assert "估價失敗" in store.get_meta(vc.VALUATION_CACHE_ERROR_KEY)
