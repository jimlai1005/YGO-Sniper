"""平台研究（venue_study）與在架觀測帳（listing_obs）的測試。

三件事要驗，而且**都不碰網路**：

1. `listing_obs` 的寫入／更新／離場標記——特別是「來源壞掉那一輪不准判離場」
   與「被新貨擠出觀測窗 ≠ 賣掉」這兩條，它們是 Q3 唯一的正確性防線。
2. 分層統計函式用固定資料驗算（手算得出來的數字）。
3. **樣本不足時回報「不足以判定」而不是硬給結論**——這條是本研究的紅線：
   使用者拿這份結論決定把錢投到哪個平台，一個沒有樣本支撐的倍率比沒有答案危險。
"""

from __future__ import annotations

import pytest
from conftest import make_listing

from ygo_sniper.store import Store
from ygo_sniper.venue_study import (
    MIN_PER_CELL,
    YAHOO_BID_VENUE,
    answer_q1,
    answer_q2,
    answer_q3,
    build_study,
    listing_row,
    quartiles,
    ratio_across_strata,
    stratum_medians,
    stratum_table,
)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def obs_row(
    key, *, site="buyee_paypay", price=1000.0, price_native=None, currency="JPY",
    rarity="ultra", grader="PSA", grade=9.0, seller_id=None,
):
    """`price_native` 可以直接覆寫（不吃 `price * 5` 的預設換算）——band
    推導（`store._derive_listing_band`）比的是 `price_native`（JPY），高價帶
    測試要能精準控制它是否落在 ceiling 之上（見「band 語意重建」測試段）。
    """
    return {
        "key": key, "source": site, "site": site, "title": f"卡 {key}",
        "url": f"https://example.test/{key}",
        "price_native": price * 5 if price_native is None else price_native,
        "currency": currency, "price_twd": price, "landed_twd": price + 300,
        "rarity": rarity, "grader": grader, "grade": grade,
        "card_name": None, "era_evidence": "初期", "price_kind": "fixed",
        "seller_id": seller_id,
    }


def batch(*keys, site="buyee_paypay", healthy=True, **kw):
    """批次**沒有 band 宣告權**（修正回合二 Task 12 拿掉了——band 由每一列
    自己觀測到的價格推導，不是「哪一批看到它」，見 `store.record_listing_scan`
    docstring「地平線分帶」段）。"""
    return {
        "source": site, "site": site, "healthy": healthy,
        "rows": [obs_row(k, site=site, **kw) for k in keys],
    }


def seller_batch(*keys, site="buyee_paypay", seller_id="s1", healthy=True, seen_keys=None, **kw):
    """賣家頁完整列舉批次。`seen_keys` 預設等於 `keys`（過濾前後一致）；
    要模擬「候選過濾器濾掉了某個仍在架的商品」就傳一個比 `keys` 更寬的集合。
    """
    return {
        "source": site, "site": site, "healthy": healthy, "exit_scope": False,
        "seller_id": seller_id,
        "seller_seen_keys": list(keys) if seen_keys is None else list(seen_keys),
        "rows": [obs_row(k, site=site, seller_id=seller_id, **kw) for k in keys],
    }


# ---------------------------------------------------------------------------
# listing_obs：寫入／更新／離場
# ---------------------------------------------------------------------------
def test_first_scan_inserts_rows(store):
    rep = store.record_listing_scan([batch("a", "b")], now="2026-08-02T00:00:00+00:00")
    assert (rep["new"], rep["updated"], rep["seen"]) == (2, 0, 2)
    rows = {r["key"]: r for r in store.listing_obs()}
    assert set(rows) == {"a", "b"}
    assert rows["a"]["seen_count"] == 1
    assert rows["a"]["first_seen"] == rows["a"]["last_seen"] == "2026-08-02T00:00:00+00:00"
    assert rows["a"]["disappeared_at"] is None and rows["a"]["window_exit_at"] is None


def test_second_scan_updates_last_seen_and_count_but_not_first_seen(store):
    store.record_listing_scan([batch("a")], now="2026-08-02T00:00:00+00:00")
    store.record_listing_scan([batch("a")], now="2026-08-02T01:00:00+00:00")
    row = store.listing_obs()[0]
    assert row["seen_count"] == 2
    assert row["first_seen"] == "2026-08-02T00:00:00+00:00"
    assert row["last_seen"] == "2026-08-02T01:00:00+00:00"


def test_price_change_is_overwritten_but_history_columns_are_not(store):
    store.record_listing_scan([batch("a", price=1000.0)], now="2026-08-02T00:00:00+00:00")
    store.record_listing_scan([batch("a", price=800.0)], now="2026-08-02T01:00:00+00:00")
    row = store.listing_obs()[0]
    assert row["price_twd"] == 800.0
    assert row["seen_count"] == 2 and row["first_seen"] == "2026-08-02T00:00:00+00:00"


def test_absent_newer_listing_is_marked_disappeared(store):
    """b 比本輪地平線（a 的 first_seen）更新，卻不在結果裡 → 可推論為下架／賣掉。"""
    store.record_listing_scan([batch("a")], now="2026-08-02T00:00:00+00:00")
    store.record_listing_scan([batch("a", "b")], now="2026-08-02T01:00:00+00:00")
    rep = store.record_listing_scan([batch("a")], now="2026-08-02T02:00:00+00:00")
    assert rep["disappeared"] == 1 and rep["window_exit"] == 0
    rows = {r["key"]: r for r in store.listing_obs()}
    assert rows["b"]["disappeared_at"] == "2026-08-02T02:00:00+00:00"
    assert rows["a"]["disappeared_at"] is None


def test_absent_older_listing_is_only_censored_not_disappeared(store):
    """a 比地平線更舊 → 它只是被新貨擠出第 1 頁，**不可**當成賣掉了。"""
    store.record_listing_scan([batch("a")], now="2026-08-02T00:00:00+00:00")
    rep = store.record_listing_scan([batch("b")], now="2026-08-02T01:00:00+00:00")
    assert rep["disappeared"] == 0 and rep["window_exit"] == 1
    rows = {r["key"]: r for r in store.listing_obs()}
    assert rows["a"]["window_exit_at"] == "2026-08-02T01:00:00+00:00"
    assert rows["a"]["disappeared_at"] is None


def test_unhealthy_batch_never_marks_anything_gone(store):
    """來源被擋那一輪什麼都看不到——照常判離場等於把一次 WAF 挑戰記成整站賣光。"""
    store.record_listing_scan([batch("a", "b")], now="2026-08-02T00:00:00+00:00")
    rep = store.record_listing_scan(
        [{"source": "buyee_paypay", "site": "buyee_paypay", "healthy": False, "rows": []}],
        now="2026-08-02T01:00:00+00:00",
    )
    assert rep["batches_skipped"] == 1
    assert (rep["disappeared"], rep["window_exit"]) == (0, 0)
    assert all(r["disappeared_at"] is None for r in store.listing_obs())


def test_other_site_is_untouched(store):
    """只掃了 PayPay 的那一輪，不可以動 Mercari 的觀測列。"""
    store.record_listing_scan(
        [batch("p1"), batch("m1", site="buyee_mercari")], now="2026-08-02T00:00:00+00:00"
    )
    store.record_listing_scan([batch("p1")], now="2026-08-02T01:00:00+00:00")
    rows = {r["key"]: r for r in store.listing_obs()}
    assert rows["m1"]["disappeared_at"] is None and rows["m1"]["window_exit_at"] is None


def test_reappearing_after_disappeared_records_revived(store):
    """判定為消失之後又出現 = 這條推論規則自己錯了一次，必須留下紀錄。"""
    store.record_listing_scan([batch("a")], now="2026-08-02T00:00:00+00:00")
    store.record_listing_scan([batch("a", "b")], now="2026-08-02T01:00:00+00:00")
    store.record_listing_scan([batch("a")], now="2026-08-02T02:00:00+00:00")
    rep = store.record_listing_scan([batch("a", "b")], now="2026-08-02T03:00:00+00:00")
    assert rep["revived"] == 1
    row = {r["key"]: r for r in store.listing_obs()}["b"]
    assert row["revived_count"] == 1 and row["disappeared_at"] is None


def test_same_key_in_two_queries_counts_once(store):
    rep = store.record_listing_scan(
        [batch("a"), batch("a")], now="2026-08-02T00:00:00+00:00"
    )
    assert rep["seen"] == 1 and rep["new"] == 1
    assert store.listing_obs()[0]["seen_count"] == 1


def test_horizon_uses_max_across_queries_of_same_site(store):
    """同一站多個查詢時取最保守的地平線：某查詢還看得到老貨，就不該判老貨消失。"""
    store.record_listing_scan([batch("old")], now="2026-08-02T00:00:00+00:00")
    store.record_listing_scan([batch("old", "new1")], now="2026-08-02T01:00:00+00:00")
    # 查詢 A 只回 new1（地平線 = new1 的 first_seen），查詢 B 回 old（地平線 = old）
    # 取 max → 地平線是 new1，old 比它舊 → 只算 censored，不算消失
    rep = store.record_listing_scan(
        [batch("new1"), batch("old")], now="2026-08-02T02:00:00+00:00"
    )
    assert rep["disappeared"] == 0 and rep["window_exit"] == 0


# ---------------------------------------------------------------------------
# band 語意重建：價格推導＋判定用即時價格窗（2026-08-23，修正回合二 Task 12）。
#
# Task 8 曾經讓批次自己宣告 `band` 並寫進地平線的分組鍵——但 `_scan` 把整輪
# 的 band 蓋到每一批（含賣家頁批次）上，監控賣家撈到的高價商品因此被洗回
# 'std'，下一輪低價帶批次的地平線把它判離場（reviewer 實跑重現：C1 殘口）。
# 新語意：band 由**這一列自己觀測到的價格**對照 `price_bounds` 推導，批次
# 沒有宣告權；地平線判定改用**判定當下重算的價格窗**（`round_band` ＋
# `price_bounds`）過濾 open_rows，不讀儲存的 band（W1：儲存值會因 fx 漂移
# 過期）。
# ---------------------------------------------------------------------------
_HB_BOUNDS = {"buyee_mercari": (8624.0, 50000.0)}
_HB_SITE = "buyee_mercari"


def test_high_band_listing_seen_by_seller_batch_keeps_high_band(store):
    """(a) C1 殘口重現：高價帶列入庫後，下一輪**賣家頁批次**（不是關鍵字
    批次）看到它，價格依然是 ¥22,222——band 必須仍是 'high'，兩個離場欄位
    必須維持 NULL。批次本身沒有宣告 band 的能力，band 完全由這一列自己的
    價格決定，賣家頁批次因此自然正確。"""
    store.record_listing_scan(
        [batch("high1", site=_HB_SITE, price_native=22222)],
        now="2026-08-22T18:15:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    store.record_listing_scan(
        [seller_batch("high1", site=_HB_SITE, price_native=22222)],
        now="2026-08-22T18:30:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="std",
    )
    row = {r["key"]: r for r in store.listing_obs()}["high1"]
    assert row["band"] == "high"
    assert row["disappeared_at"] is None
    assert row["window_exit_at"] is None


def test_high_band_round_marks_its_own_horizon_disappeared(store):
    """高價帶自己的輪能對帶內缺席列正常判 `disappeared_at`——分帶不是關掉
    地平線，是價格窗把地平線的候選集縮小成同一個帶（高價帶批次
    `exit_scope` 維持預設 True，不再是無人判定的死巷）。比照
    `test_absent_newer_listing_is_marked_disappeared` 的時序安排：缺席的
    那筆 first_seen 比地平線新，才是「頁面還蓋得到它、它卻不在」的形狀。"""
    store.record_listing_scan(
        [batch("h_a", site=_HB_SITE, price_native=20000)],
        now="2026-08-22T10:00:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    store.record_listing_scan(
        [batch("h_a", "h_b", site=_HB_SITE, price_native=20000)],
        now="2026-08-22T11:00:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    rep = store.record_listing_scan(
        [batch("h_a", site=_HB_SITE, price_native=20000)],
        now="2026-08-22T12:00:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    assert rep["disappeared"] == 1
    rows = {r["key"]: r for r in store.listing_obs()}
    assert rows["h_b"]["disappeared_at"] == "2026-08-22T12:00:00+00:00"
    assert rows["h_a"]["disappeared_at"] is None


def test_high_band_round_marks_its_own_horizon_window_exit(store):
    """高價帶自己的輪對比地平線更舊的缺席列判 `window_exit_at`（右設限，
    無結論）——同一套地平線邏輯只是候選集縮小成價格窗，不是換了一套判準。"""
    store.record_listing_scan(
        [batch("h_old", site=_HB_SITE, price_native=20000)],
        now="2026-08-22T10:00:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    rep = store.record_listing_scan(
        [batch("h_new1", site=_HB_SITE, price_native=20000)],
        now="2026-08-22T11:00:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    assert rep["window_exit"] == 1
    row = {r["key"]: r for r in store.listing_obs()}["h_old"]
    assert row["window_exit_at"] == "2026-08-22T11:00:00+00:00"
    assert row["disappeared_at"] is None


def test_cross_band_drift_high_round_stops_judging_std_round_takes_over(store):
    """(c) 跨帶漂移：band='high' 列的價格改到 ceiling 以下之後——高價帶輪的
    價格窗不再判它（它的即時價格已經落在 std 窗），改由低價帶輪的價格窗
    接手判定。band 是價格推導、判定是即時價格窗，兩者都不看「這一列曾經
    被寫過什麼 band」，只看它**現在**的價格。"""
    # T0：高價帶輪把它寫進去，band='high'
    store.record_listing_scan(
        [batch("x", site=_HB_SITE, price_native=20000)],
        now="2026-08-23T09:00:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    # T1：低價帶輪重新觀測到它，價格已經跌到 ceiling 以下 → band 改寫成 std
    store.record_listing_scan(
        [batch("x", site=_HB_SITE, price_native=5000)],
        now="2026-08-23T09:05:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="std",
    )
    assert {r["key"]: r for r in store.listing_obs()}["x"]["band"] == "std"

    # T2：高價帶輪跑一輪（另一個高價商品建地平線）——x 現在的價格落在 std
    # 窗，不會出現在高價帶輪的 open_rows 候選裡，維持 NULL。
    store.record_listing_scan(
        [batch("z_high", site=_HB_SITE, price_native=20000)],
        now="2026-08-23T09:10:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="high",
    )
    row = {r["key"]: r for r in store.listing_obs()}["x"]
    assert row["disappeared_at"] is None and row["window_exit_at"] is None

    # T3：低價帶輪跑一輪、地平線比 x 的 first_seen（T0）新，x 這輪缺席
    # → 低價帶輪的價格窗接手判定（x 現在的價格落在 std 窗內）。
    store.record_listing_scan(
        [batch("y_std", site=_HB_SITE, price_native=3000)],
        now="2026-08-23T09:15:00+00:00",
        price_bounds=_HB_BOUNDS, round_band="std",
    )
    row = {r["key"]: r for r in store.listing_obs()}["x"]
    assert row["window_exit_at"] == "2026-08-23T09:15:00+00:00"
    assert row["disappeared_at"] is None


def test_seller_scope_marks_disappeared_even_after_window_exit(store):
    """這是這次要修的死角：z649388810 的重現案例。

    商品先被地平線判成 window_exit_at（首頁擠出去，無結論），之後永遠不會
    再被地平線邏輯評估。但同一個賣家被完整列舉重掃、商品不在結果裡——
    這是獨立證據，應該直接判 disappeared_at，不管 window_exit_at 是什麼。
    """
    store.record_listing_scan([batch("old", seller_id="s1")], now="2026-08-02T00:00:00+00:00")
    # "new1" 建立更新的地平線，"old" 比地平線舊 → 只會 censored 成 window_exit_at
    store.record_listing_scan([batch("new1")], now="2026-08-02T01:00:00+00:00")
    row = {r["key"]: r for r in store.listing_obs()}["old"]
    assert row["window_exit_at"] is not None and row["disappeared_at"] is None

    rep = store.record_listing_scan(
        [seller_batch(seller_id="s1")],  # 賣家完整列舉，"old" 不在裡面
        now="2026-08-05T00:00:00+00:00",
    )
    assert rep["seller_scope_disappeared"] == 1
    row = {r["key"]: r for r in store.listing_obs()}["old"]
    assert row["disappeared_at"] == "2026-08-05T00:00:00+00:00"
    assert row["window_exit_at"] is None  # 判定改由這條規則接管，狀態互斥


def test_seller_scope_uses_raw_listings_not_candidate_filtered_rows(store):
    """`seller_seen_keys` 要用過濾前的原始清單——候選過濾器把一筆商品濾掉，
    不代表它真的下架了，不可以被這條規則牽連著判離場。"""
    store.record_listing_scan([batch("a", seller_id="s1")], now="2026-08-02T00:00:00+00:00")
    rep = store.record_listing_scan(
        [seller_batch(seller_id="s1", seen_keys=["a"])],  # rows 空，但原始清單有 "a"
        now="2026-08-03T00:00:00+00:00",
    )
    assert rep["seller_scope_disappeared"] == 0
    assert {r["key"]: r for r in store.listing_obs()}["a"]["disappeared_at"] is None


def test_seller_scope_ignores_unhealthy_batch(store):
    store.record_listing_scan([batch("a", seller_id="s1")], now="2026-08-02T00:00:00+00:00")
    rep = store.record_listing_scan(
        [seller_batch(seller_id="s1", healthy=False)], now="2026-08-03T00:00:00+00:00"
    )
    assert rep["batches_skipped"] == 1
    assert rep["seller_scope_disappeared"] == 0
    assert {r["key"]: r for r in store.listing_obs()}["a"]["disappeared_at"] is None


def test_seller_scope_does_not_touch_other_sellers(store):
    store.record_listing_scan(
        [batch("a", seller_id="s1"), batch("b", seller_id="s2")],
        now="2026-08-02T00:00:00+00:00",
    )
    store.record_listing_scan([seller_batch(seller_id="s1")], now="2026-08-03T00:00:00+00:00")
    rows = {r["key"]: r for r in store.listing_obs()}
    assert rows["a"]["disappeared_at"] == "2026-08-03T00:00:00+00:00"
    assert rows["b"]["disappeared_at"] is None


def test_summary_and_prune(store):
    store.record_listing_scan([batch("a", "b")], now="2026-08-02T00:00:00+00:00")
    summary = store.listing_obs_summary()
    assert summary["total"] == 2
    site = summary["by_site"][0]
    assert site["site"] == "buyee_paypay" and site["still_open"] == 2
    # 沒有離場的列，保留策略一筆都不刪（在架越久越有資訊量）
    assert store.prune_listing_obs(1) == 0
    assert store.prune_listing_obs(0) == 0


# ---------------------------------------------------------------------------
# 分層統計（固定資料手算驗證）
# ---------------------------------------------------------------------------
def test_quartiles_fixed_numbers():
    q = quartiles([100, 200, 300, 400, 500])
    assert q == {"n": 5, "p25": 200.0, "median": 300.0, "p75": 400.0, "min": 100.0, "max": 500.0}
    assert quartiles([]) is None


def test_stratum_medians_drops_thin_cells():
    rows = [
        *[{"rarity": "ultra", "grader": "PSA", "grade": 9.0, "price_twd": p}
          for p in (100, 200, 300, 400)],
        *[{"rarity": "secret", "grader": "PSA", "grade": 9.0, "price_twd": p} for p in (900, 1000)],
    ]
    med = stratum_medians(rows, min_per_cell=MIN_PER_CELL)
    assert list(med) == [("ultra", "PSA", 9.0)]
    assert med[("ultra", "PSA", 9.0)]["median"] == 250.0


def test_ratio_across_strata_is_paired_not_pooled():
    """關鍵性質：兩邊的**組成不同**時，配對比值不會被組成帶歪。

    A 站賣便宜貨為主、B 站賣貴貨為主，但同一分層內 B 一律是 A 的 2 倍。
    正確答案是 2.0；把兩邊整體中位數相除會得到大很多的數字。
    """
    base, other = {}, {}
    for i, rarity in enumerate(("normal", "ultra", "secret")):
        key = (rarity, "PSA", 9.0)
        base[key] = {"n": 10, "median": 100.0 * (i + 1)}
        other[key] = {"n": 4, "median": 200.0 * (i + 1)}
    res = ratio_across_strata(base, other, min_strata=3)
    assert res["verdict"] == "ok" and res["ratio"] == 2.0
    assert res["n_strata"] == 3 and res["n_other_cheaper"] == 0


def test_ratio_reports_insufficient_instead_of_guessing():
    """**紅線**：可比分層不足時不給數字，只給「不足以判定」。"""
    base = {("ultra", "PSA", 9.0): {"n": 5, "median": 100.0}}
    other = {("ultra", "PSA", 9.0): {"n": 5, "median": 50.0}}
    res = ratio_across_strata(base, other, min_strata=3)
    assert res["verdict"] == "insufficient"
    assert res["ratio"] is None
    assert "不足以判定" in res["detail"]


def test_stratum_table_keeps_thin_cells_visible():
    table = stratum_table(
        {
            "buyee_yahoo": [
                {"rarity": "ultra", "grader": "PSA", "grade": 9.0, "price_twd": p}
                for p in (100, 200, 300, 400)
            ],
            "buyee_paypay": [{"rarity": "ultra", "grader": "PSA", "grade": 9.0, "price_twd": 500}],
        },
        min_per_cell=4,
    )
    assert len(table) == 1
    cells = table[0]["cells"]
    assert cells["buyee_yahoo"]["enough"] is True
    assert cells["buyee_paypay"] == {"n": 1, "median": 500.0, "p25": 500.0,
                                     "p75": 500.0, "enough": False}
    assert table[0]["comparable"] is False


# ---------------------------------------------------------------------------
# 三個問題的組裝
# ---------------------------------------------------------------------------
def _listing(venue, price, rarity="ultra", grade=9.0):
    return {"venue": venue, "site": venue, "rarity": rarity, "grader": "PSA",
            "grade": grade, "price_twd": price}


def _sold(site, price, rarity="ultra", grade=9.0):
    return {"site": site, "rarity": rarity, "grader": "PSA", "grade": grade, "price_twd": price}


def _spread(fn, venue, base_price, *, strata=3, n=4):
    rows = []
    for i, rarity in enumerate(("normal", "ultra", "secret")[:strata]):
        for j in range(n):
            rows.append(fn(venue, base_price * (i + 1) + j, rarity=rarity))
    return rows


def test_q1_detects_cheaper_paypay_and_excludes_bid_series():
    rows = [
        *_spread(_listing, "buyee_yahoo", 1000),
        *_spread(_listing, "buyee_mercari", 1000),
        *_spread(_listing, "buyee_paypay", 500),
        # 競標中的現在価格很低，但**不可以**進主結論
        *_spread(_listing, YAHOO_BID_VENUE, 100),
    ]
    q1 = answer_q1(rows, min_per_cell=4, min_strata=3)
    assert q1["verdict"] == "paypay_cheaper"
    assert q1["ratios"]["buyee_paypay"]["ratio"] == pytest.approx(0.5, abs=0.02)
    assert q1["ratios"]["buyee_mercari"]["ratio"] == pytest.approx(1.0, abs=0.02)
    # 現在価格只出現在參考序列，沒有把 Yahoo 的主序列拉低
    assert q1["bid_reference"]["ratio"] < 0.5
    assert any("即決" in c for c in q1["caveats"])


def test_q1_insufficient_when_paypay_has_no_samples():
    rows = [*_spread(_listing, "buyee_yahoo", 1000), *_spread(_listing, "buyee_mercari", 1000)]
    q1 = answer_q1(rows, min_per_cell=4, min_strata=3)
    assert q1["verdict"] == "insufficient"
    assert "不足以判定" in q1["headline"]
    assert q1["ratios"]["buyee_paypay"]["ratio"] is None


def test_q2_flags_selection_bias_when_sold_is_much_pricier_than_listed():
    listing = _spread(_listing, "buyee_paypay", 1000)
    sold = _spread(_sold, "buyee_paypay", 3000)
    q2 = answer_q2(listing, sold, min_per_cell=4, min_strata=3)
    res = q2["by_venue"]["buyee_paypay"]
    assert res["verdict"] == "ok" and res["ratio"] > 2.5
    assert "選擇偏差" in res["reading"]


def test_q2_says_no_bias_when_distributions_match():
    listing = _spread(_listing, "buyee_mercari", 1000)
    sold = _spread(_sold, "buyee_mercari", 1000)
    res = answer_q2(listing, sold, min_per_cell=4, min_strata=3)["by_venue"]["buyee_mercari"]
    assert res["ratio"] == pytest.approx(1.0, abs=0.05)
    assert "沒有明顯選擇偏差" in res["reading"]


def test_q2_reports_insufficient_per_venue():
    res = answer_q2([], [], min_per_cell=4, min_strata=3)["by_venue"]["buyee_paypay"]
    assert res["verdict"] == "insufficient" and res["ratio"] is None
    assert "不足以判定" in res["reading"]


def test_q3_insufficient_until_enough_decided_observations():
    summary = {"by_site": [{"site": "buyee_paypay", "total": 5, "still_open": 3,
                            "disappeared": 2, "window_exit": 0, "revived": 0, "multi_seen": 4}]}
    q3 = answer_q3(summary)
    assert q3["verdict"] == "insufficient"
    assert q3["rows"][2]["sell_through"] is None
    assert "已定案 5/30" in q3["rows"][2]["blocked_by"]


def test_q3_zero_events_never_reports_zero_percent():
    """**紅線**：分母夠大但一次消失都沒發生時，不可以報「賣得掉率 0%」。

    0/500 與 0/5 在螢幕上長得一模一樣（都是 0%），但前者是發現、後者是無知。
    分子門檻沒過就一律回「不足以判定」，不給那個看起來很確定的 0。
    """
    summary = {"by_site": [{"site": v, "total": 500, "still_open": 500, "disappeared": 0,
                            "window_exit": 900, "revived": 0, "multi_seen": 480}
                           for v in ("buyee_paypay", "buyee_yahoo", "buyee_mercari")]}
    q3 = answer_q3(summary)
    assert q3["verdict"] == "insufficient"
    assert all(r["sell_through"] is None for r in q3["rows"])
    assert "消失事件 0/10" in q3["rows"][0]["blocked_by"]


def test_q3_computes_sell_through_when_enough():
    def site(name):
        return {"site": name, "total": 100, "still_open": 60, "disappeared": 40,
                "window_exit": 200, "revived": 1, "multi_seen": 90}

    q3 = answer_q3({"by_site": [site("buyee_paypay"), site("buyee_yahoo")]})
    assert q3["verdict"] == "ok"
    assert {r["venue"]: r["sell_through"] for r in q3["rows"]}["buyee_paypay"] == 0.4


def test_build_study_survives_empty_everything():
    """沒有任何資料時也必須產出一份**說自己沒答案**的報告，而不是爆掉。"""
    report = build_study(survey=None, sold_rows=[], listing_obs_summary={"by_site": []})
    assert report["q1"]["verdict"] == "insufficient"
    assert report["q3"]["verdict"] == "insufficient"
    assert report["listing_n"] == 0


# ---------------------------------------------------------------------------
# listing_row：與 comps 同一把尺
# ---------------------------------------------------------------------------
def test_listing_row_uses_no_card_markup(fx):
    """在架價與 comps 都必須 apply_markup=False，否則兩邊的比值是假的。"""
    from ygo_sniper.domain import Grader
    from ygo_sniper.parsers import parse_card

    lst = make_listing(price=10000, title="遊戯王 初期 ウルトラ PSA9 青眼の白龍")
    info = parse_card(lst.title, {})
    row = listing_row(lst, info, source="buyee_mercari", fx=fx, price_kind="fixed")
    assert row["price_twd"] == round(10000 * 0.21, 0)
    assert row["price_kind"] == "fixed"
    assert row["grader"] in {g.value for g in Grader}


def test_listing_row_splits_yahoo_current_bid_into_reference_venue(fx):
    from ygo_sniper.domain import Site
    from ygo_sniper.parsers import parse_card

    lst = make_listing(price=5000, site=Site.BUYEE_YAHOO, title="遊戯王 初期 PSA9")
    info = parse_card(lst.title, {})
    buyout = listing_row(lst, info, source="yahoo_direct", fx=fx, price_kind="buyout")
    bid = listing_row(lst, info, source="yahoo_direct", fx=fx, price_kind="current_bid")
    assert buyout["venue"] == "buyee_yahoo"
    assert bid["venue"] == YAHOO_BID_VENUE
    # site 不變（購買路徑仍是 Yahoo），只有分析用的 venue 分家
    assert bid["site"] == "buyee_yahoo"


def test_rows_from_listing_obs_maps_venue_like_listing_row():
    """`--no-survey` 走的路徑：既有觀測要能回答同一個問題，venue 規則同一份。"""
    from ygo_sniper.venue_study import rows_from_listing_obs

    rows = rows_from_listing_obs([
        {"site": "buyee_paypay", "price_kind": "fixed", "price_twd": 100},
        {"site": "buyee_yahoo", "price_kind": "buyout", "price_twd": 200},
        {"site": "buyee_yahoo", "price_kind": "current_bid", "price_twd": 5},
    ])
    assert [r["venue"] for r in rows] == ["buyee_paypay", "buyee_yahoo", YAHOO_BID_VENUE]
    # 原欄位原封不動帶過去（分層要用 rarity/grader/grade，不能在轉換時掉東西）
    assert rows[0]["price_twd"] == 100


# ---------------------------------------------------------------------------
# pipeline 串接（零網路：假 registry + FakeFx + tmp db）
# ---------------------------------------------------------------------------
def _pipeline(monkeypatch, tmp_path, cfg, registry, queries):
    import dataclasses

    from conftest import FakeFx

    import ygo_sniper.pipeline as pipeline_mod
    from ygo_sniper.pipeline import Pipeline

    test_cfg = dataclasses.replace(
        tmp := cfg, root=tmp_path, watchlist={**tmp.watchlist, "queries": queries}
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    return Pipeline(test_cfg)


class _FakeSource:
    def __init__(self, name, listings, *, site, exc=None):
        from ygo_sniper.domain import Site

        self.name, self.site, self.supports_sold = name, site or Site.BUYEE_PAYPAY, False
        self.listings, self.exc = listings, exc

    def search(self, keyword, **_kw):
        if self.exc:
            raise self.exc
        return list(self.listings)


def _graded(i):
    from ygo_sniper.domain import Site

    return make_listing(
        price=5000 + i, site=Site.BUYEE_PAYPAY, external_id=f"z{i}",
        title=f"遊戯王 初期 ウルトラ PSA9 青眼の白龍 No.{i}",
    )


def test_scan_writes_listing_obs(monkeypatch, tmp_path, cfg):
    """一輪真的 scan 之後，listing_obs 要有列——這是 Q3 的全部資料來源。"""
    registry = {"pp": _FakeSource("pp", [_graded(i) for i in range(3)], site=None)}
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["pp"]}]
    pipe = _pipeline(monkeypatch, tmp_path, cfg, registry, queries)
    try:
        result = pipe.scan(skip_comps=True)
        rows = pipe.store.listing_obs()
    finally:
        pipe.close()

    assert result["listing_obs"]["new"] == 3
    assert len(rows) == 3
    assert {r["site"] for r in rows} == {"buyee_paypay"}
    # 到手成本是從 scoring 那一遍查表回填的，不是重算
    assert all(r["landed_twd"] and r["landed_twd"] > 0 for r in rows)


def test_dry_run_scan_writes_nothing(monkeypatch, tmp_path, cfg):
    registry = {"pp": _FakeSource("pp", [_graded(i) for i in range(3)], site=None)}
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["pp"]}]
    pipe = _pipeline(monkeypatch, tmp_path, cfg, registry, queries)
    try:
        pipe.scan(skip_comps=True, dry_run=True)
        assert pipe.store.listing_obs() == []
    finally:
        pipe.close()


def test_blocked_source_round_does_not_mark_listings_gone(monkeypatch, tmp_path, cfg):
    """**最重要的一條**：來源被擋那一輪，先前的標的不可以被記成消失。"""
    from ygo_sniper.sources.base import BlockedError

    listings = [_graded(i) for i in range(3)]
    registry = {"pp": _FakeSource("pp", listings, site=None)}
    queries = [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["pp"]}]
    pipe = _pipeline(monkeypatch, tmp_path, cfg, registry, queries)
    try:
        pipe.scan(skip_comps=True)
        registry["pp"].exc = BlockedError("WAF 擋掉", url="stub://pp")
        result = pipe.scan(skip_comps=True)
        rows = pipe.store.listing_obs()
    finally:
        pipe.close()

    assert result["sources"]["pp"]["health"] == "blocked"
    assert result["listing_obs"]["batches_skipped"] == 1
    assert result["listing_obs"]["disappeared"] == 0
    assert all(r["disappeared_at"] is None and r["window_exit_at"] is None for r in rows)
