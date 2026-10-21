"""補跑模式：漏跑期間狙擊卡成交回顧（catchup.py）。

時間基準是這裡最容易錯的地方：排程帳是本地 naive、`sold_at` 是 UTC aware。
系統時區不同結果就不同，所以整個檔案把系統時區故意設成 America/Chicago——排程基準釘台北（clock.py），不看機器時區。
"""
from __future__ import annotations

import time

import pytest

from ygo_sniper import catchup
from ygo_sniper.store import Store

WINDOW = ("2026-10-19T09:00:01", "2026-10-19T20:35:34")   # 本地 09:00～20:35 ＝ 01:00Z～12:35Z


@pytest.fixture(autouse=True)
def _taipei_tz(monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def store_with_watches(tmp_path):
    """兩個 active watch（id 1、9，同一張卡、不同鑑定目標）＋一個 inactive（id 4）。"""
    store = Store(tmp_path / "t.db")
    kw = dict(name_ja="魔法の筒", name_en="Magic Cylinder", code_raw="P4-06", code_norm="P4-6")
    store.insert_card_watch(grader="ARS", grade=10.0, grade_label="10", **kw)        # id 1
    store.insert_card_watch(grader="PSA", grade=9.0, grade_label="9", **kw)          # → id 9
    store.insert_card_watch(grader="ARS", grade=8.0, grade_label="8", **kw)          # → id 4（停用）
    with store._conn() as c:
        c.execute("UPDATE card_watch SET id = 9 WHERE id = 2")
        c.execute("UPDATE card_watch SET id = 4, active = 0 WHERE id = 3")

    def put_sale(*, watch_id, sale_key, tier, sold_at, price):
        store.upsert_card_watch_sale(
            watch_id, sale_key, tier=tier, title="t", url=f"https://buyee.jp/{sale_key}",
            site="buyee_yahoo", seller_id="", price_native=price, currency="JPY",
            sold_at=sold_at, bid_count=1, sale_kind="auction",
            source=sale_key.split(":")[0],
        )

    store.put_sale = put_sale
    return store


def test_window_to_utc_converts_local_naive_to_aware_utc():
    lo, hi = catchup.window_to_utc(*WINDOW)
    assert lo.tzinfo is not None and hi.tzinfo is not None
    assert lo.utcoffset().total_seconds() == 0
    # 本地（Asia/Taipei，UTC+8）09:00 → 01:00Z
    assert lo.hour == 1 and hi.hour == 12


def test_gap_sales_filters_by_window_and_tier_and_dedupes(store_with_watches):
    store = store_with_watches
    put = store.put_sale
    put(watch_id=1, sale_key="buyee_yahoo:a1", tier="exact", sold_at="2026-10-19T03:00:00+00:00", price=3441)
    put(watch_id=9, sale_key="buyee_yahoo:a1", tier="partial", sold_at="2026-10-19T03:00:00+00:00", price=3441)
    put(watch_id=1, sale_key="buyee_yahoo:b2", tier="near", sold_at="2026-10-19T05:00:00+00:00", price=100)
    put(watch_id=1, sale_key="buyee_yahoo:c3", tier="exact", sold_at="2026-10-19T13:00:00+00:00", price=9999)  # 視窗外
    put(watch_id=1, sale_key="buyee_mercari:d4", tier="exact", sold_at="", price=5000)                          # 無時刻
    put(watch_id=4, sale_key="buyee_yahoo:e5", tier="exact", sold_at="2026-10-19T03:30:00+00:00", price=1)     # inactive
    rep = catchup.gap_sales(store, *WINDOW)
    assert [s["sale_key"] for s in rep.graded] == ["buyee_yahoo:a1"]
    assert rep.graded[0]["tier"] == "exact"          # 同一筆取較高 tier
    assert rep.near_count == 1
    assert rep.undated_sources == ["buyee_mercari"]


def test_window_boundaries_are_compared_in_utc_not_as_strings(store_with_watches):
    """視窗 (01:00:01Z, 12:35:34Z]：頭開尾閉；字串比較（本地 '20:35' vs UTC '12:35'）會全錯。"""
    put = store_with_watches.put_sale
    put(watch_id=1, sale_key="buyee_yahoo:in", tier="exact", sold_at="2026-10-19T12:35:34+00:00", price=1)
    put(watch_id=1, sale_key="buyee_yahoo:late", tier="exact", sold_at="2026-10-19T12:35:35+00:00", price=2)
    put(watch_id=1, sale_key="buyee_yahoo:early", tier="exact", sold_at="2026-10-19T01:00:02+00:00", price=3)
    put(watch_id=1, sale_key="buyee_yahoo:before", tier="exact", sold_at="2026-10-19T01:00:01+00:00", price=4)
    rep = catchup.gap_sales(store_with_watches, *WINDOW)
    assert sorted(s["sale_key"] for s in rep.graded) == ["buyee_yahoo:early", "buyee_yahoo:in"]


def test_naive_sold_at_is_excluded_not_guessed(store_with_watches):
    store_with_watches.put_sale(watch_id=1, sale_key="buyee_yahoo:naive", tier="exact",
                                sold_at="2026-10-19T03:00:00", price=1)
    assert catchup.gap_sales(store_with_watches, *WINDOW).graded == []


def test_format_gap_review_none_when_nothing(store_with_watches):
    rec = {"uncovered_from": WINDOW[0], "uncovered_to": WINDOW[1], "missed_slots": 10}
    rep = catchup.gap_sales(store_with_watches, *WINDOW)
    assert catchup.format_gap_review(rec, rep) is None


def test_format_gap_review_lists_graded_and_counts_near(store_with_watches):
    put = store_with_watches.put_sale
    put(watch_id=1, sale_key="buyee_yahoo:a1", tier="exact", sold_at="2026-10-19T03:00:00+00:00", price=3441)
    put(watch_id=1, sale_key="buyee_yahoo:b2", tier="near", sold_at="2026-10-19T05:00:00+00:00", price=100)
    rec = {"uncovered_from": WINDOW[0], "uncovered_to": WINDOW[1], "missed_slots": 10}
    text = catchup.format_gap_review(rec, catchup.gap_sales(store_with_watches, *WINDOW))
    assert text.startswith("🕳️ 漏跑期間狙擊卡成交回顧")
    assert "🎯" in text and "3,441" in text and "另有 1 筆" in text
    assert "10-19 11:00" in text     # 03:00Z → 本地 11:00
    assert "漏 10 格" in text and "10-19 09:00 ～ 10-19 20:35" in text
    assert "魔法の筒 ARS10 ｜ ¥3,441 ｜ 10-19 11:00" in text


def test_format_gap_review_only_near_still_sends_and_marks_partial(store_with_watches):
    put = store_with_watches.put_sale
    put(watch_id=9, sale_key="buyee_yahoo:p1", tier="partial", sold_at="2026-10-19T04:00:00+00:00", price=2000)
    put(watch_id=1, sale_key="buyee_mercari:u1", tier="exact", sold_at="", price=5000)
    rec = {"uncovered_from": WINDOW[0], "uncovered_to": WINDOW[1], "missed_slots": 3}
    text = catchup.format_gap_review(rec, catchup.gap_sales(store_with_watches, *WINDOW))
    assert "👀 魔法の筒 PSA9（部分符合）｜ ¥2,000 ｜ 10-19 12:00" in text
    assert "⚠️ buyee_mercari 的成交沒有時刻" in text

    # 只有 near：仍送，只給筆數
    s2 = store_with_watches
    s2.put_sale(watch_id=1, sale_key="buyee_yahoo:n1", tier="near", sold_at="2026-10-19T02:00:00+00:00", price=1)
    rep = catchup.GapSalesReport(graded=[], near_count=1, undated_sources=[])
    only_near = catchup.format_gap_review(rec, rep)
    assert only_near is not None and "另有 1 筆" in only_near and "•" not in only_near


def test_undated_sources_empty_when_every_row_has_sold_at(store_with_watches):
    store_with_watches.put_sale(watch_id=1, sale_key="buyee_yahoo:a", tier="exact",
                                sold_at="2026-10-19T03:00:00+00:00", price=1)
    assert store_with_watches.card_watch_sale_undated_sources() == []
