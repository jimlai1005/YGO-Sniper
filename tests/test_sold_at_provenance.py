"""`sold_at` 到底是不是成交時間（`comps.sold_at_is_ingest`）。

**診斷（2026-08-02 實測）**：Buyee 系的已售出頁**完全沒有成交時間**——
搜尋頁的 tile 只有「SOLD｜標題｜價格」，商品頁也沒有任何日期。所以那批 comps
的 `sold_at` 一直是 `datetime.now(UTC)`，也就是我們入庫的時間。後果：
`scoring.comps_window_days: 90` 的視窗對它們形同虛設（時間永遠是「剛剛」），
任何時間序列分析（漂移、覆蓋率時間切分、賣得掉率）在那批資料上都不可信。

修法不是猜一個成交時間（那會讓所有數字看起來很正常、然後全部是錯的），
而是**把兩種資料標記開來**，讓下游自己決定要不要用、並在報表上說清楚。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeFx, make_listing

from ygo_sniper.comps import CompsEngine
from ygo_sniper.domain import Site
from ygo_sniper.store import Store

_TITLE = "PSA10 遊戯王 初期 青眼の白龍 シークレット"


@pytest.fixture
def engine(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    return CompsEngine(cfg, FakeFx(), store)


def _sold(ext: str, *, sold_at: str | None = None, site=Site.BUYEE_PAYPAY):
    lst = make_listing(price=9000, site=site, title=_TITLE, external_id=ext)
    lst = dataclasses.replace(lst, url=f"https://x.test/{ext}", is_sold=True)
    lst.raw = {"sold_at": sold_at} if sold_at else {}
    return lst


# ---------------------------------------------------------------------------
# 1. 入庫時就標記（新資料不需要回填）
# ---------------------------------------------------------------------------
def test_real_sale_time_is_marked_as_not_ingest(engine):
    engine.ingest_sold([_sold("r1", sold_at="2026-05-01T10:00:00+00:00")])
    row = engine.store.comps_by()[0]
    assert row["sold_at"] == "2026-05-01T10:00:00+00:00"
    assert row["sold_at_is_ingest"] == 0


def test_missing_sale_time_falls_back_to_now_and_is_marked(engine):
    """來源給不出時間 → 退回 now()，但**必須標記**。

    不標記的話下游沒有任何辦法分辨這個時間戳是成交時間還是抓取時間，
    而兩者長得一模一樣（都是合法的 ISO UTC 字串）。
    """
    before = datetime.now(UTC)
    engine.ingest_sold([_sold("i1")])
    row = engine.store.comps_by()[0]

    assert row["sold_at_is_ingest"] == 1
    assert datetime.fromisoformat(row["sold_at"]) >= before


def test_both_kinds_survive_the_same_ingest(engine):
    engine.ingest_sold([
        _sold("r2", sold_at="2026-05-02T10:00:00+00:00"),
        _sold("i2"),
    ])
    flags = {r["url"]: r["sold_at_is_ingest"] for r in engine.store.comps_by()}
    assert flags["https://x.test/r2"] == 0
    assert flags["https://x.test/i2"] == 1


# ---------------------------------------------------------------------------
# 2. 回填既有資料：確定性、冪等
# ---------------------------------------------------------------------------
def _legacy_rows(db_path, rows: list[tuple[str, str]]) -> None:
    """直接塞舊資料（`sold_at_is_ingest` 為 NULL），模擬回填前的 db。"""
    with sqlite3.connect(db_path) as c:
        c.executemany(
            "INSERT INTO comps (signature, title, url, site, sold_at, sold_at_is_ingest)"
            " VALUES (?,?,?,?,?,NULL)",
            [(f"SIG{i}", _TITLE, f"https://x.test/legacy{i}", site, sold_at)
             for i, (site, sold_at) in enumerate(rows)],
        )


def test_backfill_uses_a_deterministic_rule_not_a_guess(cfg, tmp_path):
    """判準：入庫時間由 `datetime.now(UTC).isoformat()` 寫入 → **一律帶微秒**；
    真實成交時間出自各站 endTime 經 `to_utc_iso()` → 秒精度、沒有小數點。

    2026-08-02 在正式庫（1593 列）驗證，三個站台各自 100% 一致：
    mercari 550/550 有小數點、yahoo 718/718 無、paypay 77 有／248 無
    （同一個站台混著 Buyee 鏡像與 closedsearch 兩種來路——這正是「只看 site
    猜不出來」的原因）。
    """
    db = tmp_path / "legacy.db"
    Store(db)  # 建 schema
    _legacy_rows(db, [
        ("buyee_mercari", "2026-08-01T14:54:21.213426+00:00"),  # 入庫時間（有微秒）
        ("buyee_mercari", "2026-08-01T14:54:21.213426+00:00"),  # 同一批 ingest
        ("buyee_yahoo", "2026-03-15T13:40:41+00:00"),           # 真成交（秒精度）
        ("buyee_paypay", "2026-07-31T15:52:31+00:00"),          # closedsearch 的フリマ
    ])
    store = Store(db)  # 重開 → _migrate_comps 自動回填

    p = store.comps_provenance()
    assert (p["ingest"], p["real"], p["unknown"]) == (2, 2, 0)
    by_site = {r["site"]: r for r in p["by_site"]}
    assert by_site["buyee_mercari"]["ingest"] == 2
    assert by_site["buyee_yahoo"]["real_time"] == 1
    assert by_site["buyee_paypay"]["real_time"] == 1


def test_backfill_is_idempotent(cfg, tmp_path):
    db = tmp_path / "idem.db"
    Store(db)
    _legacy_rows(db, [
        ("buyee_mercari", "2026-08-01T14:54:21.213426+00:00"),
        ("buyee_yahoo", "2026-03-15T13:40:41+00:00"),
    ])
    store = Store(db)

    first = store.backfill_sold_at_provenance()
    second = store.backfill_sold_at_provenance()
    third = store.backfill_sold_at_provenance()

    # 開機時已經回填過，所以這裡三次都是 0；重點是**不會再改動任何列**
    assert (first, second, third) == (0, 0, 0)
    assert store.comps_provenance()["unknown"] == 0


def test_backfill_never_overwrites_an_explicit_flag(cfg, tmp_path):
    """回填是補歷史，不是重新判斷。寫入端已經標好的一律不碰——
    否則 PayPay 直抓那批（真成交時間、秒精度）萬一哪天格式帶了微秒，
    回填就會把正確的標記改成錯的。"""
    db = tmp_path / "explicit.db"
    store = Store(db)
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO comps (signature, title, url, site, sold_at, sold_at_is_ingest)"
            " VALUES ('S','t','u','buyee_paypay','2026-07-01T00:00:00.123456+00:00', 0)"
        )
    store.backfill_sold_at_provenance()
    assert store.comps_by()[0]["sold_at_is_ingest"] == 0


# ---------------------------------------------------------------------------
# 3. 下游看得見、也篩得掉
# ---------------------------------------------------------------------------
def test_load_comps_carries_the_flag_and_can_filter(engine):
    engine.ingest_sold([
        _sold("r3", sold_at=datetime.now(UTC).replace(microsecond=0).isoformat()),
        _sold("i3"),
    ])
    since = datetime.now(UTC) - timedelta(days=90)

    everything = engine.store.load_comps(since, era_verified_only=False)
    rows = [r for lst in everything.values() for r in lst]
    assert len(rows) == 2
    assert {r["sold_at_is_ingest"] for r in rows} == {0, 1}

    clean = engine.store.load_comps(since, era_verified_only=False, real_sold_at_only=True)
    clean_rows = [r for lst in clean.values() for r in lst]
    assert len(clean_rows) == 1
    assert clean_rows[0]["sold_at_is_ingest"] == 0


def test_comps_by_can_exclude_ingest_time_rows(engine):
    engine.ingest_sold([_sold("r4", sold_at="2026-06-01T00:00:00+00:00"), _sold("i4")])
    assert len(engine.store.comps_by()) == 2
    assert len(engine.store.comps_by(real_sold_at_only=True)) == 1


def test_venue_study_q2_reports_how_many_are_ingest_time():
    """報表上要標明——這是「不要讓下游把入庫時間當成交時間用」的最後一道。"""
    from ygo_sniper.venue_study import answer_q2

    sold_rows = [
        {"site": "buyee_paypay", "price_twd": 100.0, "rarity": "ultra",
         "grader": "PSA", "grade": 9.0, "sold_at_is_ingest": 1},
        {"site": "buyee_paypay", "price_twd": 120.0, "rarity": "ultra",
         "grader": "PSA", "grade": 9.0, "sold_at_is_ingest": 0},
    ]
    rep = answer_q2([], sold_rows)

    assert rep["by_venue"]["buyee_paypay"]["sold_ingest_time_n"] == 1
    assert any("入庫時間" in c for c in rep["caveats"])


def test_paypay_direct_sold_items_are_not_ingest_time(cfg, engine):
    """PayPay 直抓的已售出樣本帶**真實**成交時間，所以旗標必須是 0。

    這是這次改動的實際收益：PayPay 是三個平台裡成交樣本最少、係數又最極端的
    那一個，而它原本的樣本全部只有入庫時間。
    """
    from pathlib import Path

    from ygo_sniper.sources.paypay import PayPayDirectSource

    html = (Path(__file__).parent / "fixtures" / "paypay_search_ok.html").read_text(
        encoding="utf-8"
    )

    class _F:
        def get(self, url, **kw):
            return html

    src = PayPayDirectSource(cfg, _F())
    sold = src.search_detailed("遊戯王", sold=True, pages=1).listings
    report = engine.ingest_sold(sold)
    assert report.kept > 0

    rows = engine.store.comps_by(limit=500)
    assert rows and all(r["sold_at_is_ingest"] == 0 for r in rows)
    assert all("." not in (r["sold_at"] or "") for r in rows), "真成交時間是秒精度"
