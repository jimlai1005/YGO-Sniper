"""signal_valuations 快取表：整批 upsert、list_signals 帶回 val_ 欄位。

signal 的塞法照抄 tests/test_expiry_clear.py::_signal_for（Signal 八欄必填、
key 必須是 site:external_id 形狀，理由見該函式 docstring）。
"""
from __future__ import annotations

import pytest

from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    # Store 沒有持久連線／close()（每次操作各自開關，見 _conn 的
    # docstring）；照 tests/test_expiry_clear.py 的既有 fixture 慣例直接回傳。
    return Store(tmp_path / "t.db")


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


def test_upsert_then_list_signals_carries_val_columns(store):
    store.upsert_signal(_signal_for("buyee_yahoo:a1"))
    store.upsert_valuations([_val_row("buyee_yahoo:a1")])
    rows = store.list_signals(state="all", limit=10)
    r = {x["key"]: x for x in rows}["buyee_yahoo:a1"]
    assert r["val_p_worth_buying"] == 0.42
    assert r["val_fair_twd"] == 1234.0
    assert r["val_level_label"] == "L1"
    assert r["val_resale_json"] == '{"ok": false, "reason": "stub"}'
    assert r["val_computed_at"] == "2026-08-24T00:00:00+00:00"


def test_upsert_overwrites_same_key(store):
    store.upsert_signal(_signal_for("buyee_yahoo:a1"))
    store.upsert_valuations([_val_row("buyee_yahoo:a1")])
    store.upsert_valuations([_val_row("buyee_yahoo:a1", p_worth_buying=None, fair_twd=None)])
    r = store.list_signals(state="all", limit=10)[0]
    assert r["val_p_worth_buying"] is None  # 新一輪算不出來就要照實蓋掉，不留舊值


def test_signal_without_cache_row_yields_nulls(store):
    store.upsert_signal(_signal_for("buyee_yahoo:nocache"))
    r = store.list_signals(state="all", limit=10)[0]
    assert r["val_p_worth_buying"] is None
    assert r["val_resale_json"] is None
