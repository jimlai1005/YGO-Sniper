"""種子策略：從「觀察中」的標的補賣家（2026-08-04）。零網路。

三件事，每件都對應一種會靜默出錯的病：

1. **挑對象**：只挑觀測列**還沒有賣家**的標的。挑錯的代價是每筆一個對外請求
   ——把已經知道賣家的標的再抓一次，是純粹的浪費而且完全看不出來。
2. **冪等**：寫入只補 `seller_id IS NULL`，第二次跑 `written` 必為 0。
3. **失敗要說得出來**：抓取失敗、網址不支援、頁面沒有賣家欄位是三件不同的事，
   三者都不得表現成「這筆沒有賣家」（那會讓人以為市場上就是查不到）。
"""

from __future__ import annotations

import pytest

from ygo_sniper.appraise import ItemPage
from ygo_sniper.domain import Currency
from ygo_sniper.seller_seed import backfill_signal_sellers, seed_targets
from ygo_sniper.sources.base import FetchError
from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def obs_row(key, *, site="buyee_mercari", seller_id=None, url=None):
    return {
        "key": key, "source": site, "site": site, "title": f"卡 {key}",
        "url": url or f"https://buyee.jp/mercari/item/{key.split(':')[-1]}",
        "price_native": 5000.0, "currency": "JPY", "price_twd": 1000.0,
        "landed_twd": 1300.0, "rarity": "ultra", "grader": "PSA", "grade": 9.0,
        "card_name": None, "era_evidence": "初期", "price_kind": "fixed",
        "seller_id": seller_id, "seller_feedback_score": None, "seller_feedback_pct": None,
    }


def seed_signal(store, key, *, state="watching", site="buyee_mercari", url=None):
    """直接寫 signals 列（與 test_sellers 同一手法）：這裡要驗的是種子挑選，
    不是 Signal 物件怎麼組——走完整管線只會讓失敗訊息離題。"""
    url = url or f"https://buyee.jp/mercari/item/{key.split(':')[-1]}"
    with store._conn() as c:
        c.execute(
            "INSERT INTO signals (key, site, external_id, title, url, score, state) "
            "VALUES (?, ?, ?, ?, ?, 50.0, ?)",
            (key, site, key.split(":", 1)[1], f"卡 {key}", url, state),
        )


def fill_obs(store, rows, *, site="buyee_mercari"):
    store.record_listing_scan([
        {"source": site, "site": site, "healthy": True, "rows": rows}
    ])


class FakeItemFetcher:
    """`fetch_item_page` 的替身。key → ItemPage 或例外。"""

    def __init__(self, by_id):
        self.by_id = by_id
        self.calls: list[str] = []

    def __call__(self, cfg, target, **_kw):
        self.calls.append(target.external_id)
        out = self.by_id.get(target.external_id)
        if isinstance(out, Exception):
            raise out
        return out


def page(seller=None, name=None):
    return ItemPage(
        title="遊戯王 初期 ウルトラ PSA9", price=5000.0, currency=Currency.JPY,
        price_kind="fixed", price_note="", seller=seller, seller_name=name,
    )


# ---------------------------------------------------------------------------
# 1. 挑對象
# ---------------------------------------------------------------------------
def test_seed_targets_only_picks_rows_without_a_seller(store):
    for key in ("buyee_mercari:m1", "buyee_mercari:m2"):
        seed_signal(store, key)
    seed_signal(store, "buyee_mercari:m3", state="skipped")
    fill_obs(store, [
        obs_row("buyee_mercari:m1"),
        obs_row("buyee_mercari:m2", seller_id="already"),
        obs_row("buyee_mercari:m3"),
    ])

    keys = [t["key"] for t in seed_targets(store)]
    assert keys == ["buyee_mercari:m1"], "已有賣家的、狀態不對的都不該進來"


def test_seed_targets_covers_bought_as_well(store):
    seed_signal(store, "buyee_mercari:m9", state="bought")
    fill_obs(store, [obs_row("buyee_mercari:m9")])

    assert [t["key"] for t in seed_targets(store)] == ["buyee_mercari:m9"]


def test_seed_targets_respects_the_limit(store):
    for i in range(5):
        seed_signal(store, f"buyee_mercari:m{i}")
    fill_obs(store, [obs_row(f"buyee_mercari:m{i}") for i in range(5)])

    assert len(seed_targets(store, limit=2)) == 2


# ---------------------------------------------------------------------------
# 2. 補賣家與冪等
# ---------------------------------------------------------------------------
def test_backfill_writes_the_seller_and_is_idempotent(store, cfg, monkeypatch):
    import ygo_sniper.seller_seed as seed_mod

    seed_signal(store, "buyee_mercari:m11")
    fill_obs(store, [obs_row("buyee_mercari:m11")])
    fake = FakeItemFetcher({"m11": page("901019808", "りり")})
    monkeypatch.setattr("ygo_sniper.appraise.fetch_item_page", fake)
    assert seed_mod is not None

    targets = seed_targets(store)
    first = backfill_signal_sellers(cfg=cfg, store=store, targets=targets)
    assert first.resolved == 1 and first.written == 1
    assert first.fills[0].seller_name == "りり"
    assert store.listing_obs(limit=10)[0]["seller_id"] == "901019808"
    # 賣家帳本同步長出這個人
    assert any(r["seller_key"] == "buyee_mercari:901019808" for r in store.list_sellers())

    # 第二次：這筆已經有賣家了 → 根本不會被挑中，一個請求都不打
    again = backfill_signal_sellers(cfg=cfg, store=store, targets=seed_targets(store))
    assert again.fills == [] and again.requests == 0


def test_writing_the_same_seller_twice_reports_not_written(store, cfg, monkeypatch):
    """直接餵同一個 target 兩次：第二次 `written=False`（**只補不改**）。"""
    seed_signal(store, "buyee_mercari:m12")
    fill_obs(store, [obs_row("buyee_mercari:m12")])
    monkeypatch.setattr(
        "ygo_sniper.appraise.fetch_item_page", FakeItemFetcher({"m12": page("123")})
    )
    targets = seed_targets(store)

    backfill_signal_sellers(cfg=cfg, store=store, targets=targets)
    second = backfill_signal_sellers(cfg=cfg, store=store, targets=targets)

    assert second.fills[0].seller_id == "123"
    assert second.fills[0].written is False
    assert "冪等" in second.fills[0].note


def test_dry_run_does_not_fetch_or_write(store, cfg, monkeypatch):
    seed_signal(store, "buyee_mercari:m13")
    fill_obs(store, [obs_row("buyee_mercari:m13")])
    fake = FakeItemFetcher({"m13": page("123")})
    monkeypatch.setattr("ygo_sniper.appraise.fetch_item_page", fake)

    report = backfill_signal_sellers(
        cfg=cfg, store=store, targets=seed_targets(store), dry_run=True
    )
    assert fake.calls == []
    assert report.written == 0
    assert store.listing_obs(limit=10)[0]["seller_id"] is None


# ---------------------------------------------------------------------------
# 3. 三種失敗要分得開
# ---------------------------------------------------------------------------
def test_three_failure_modes_are_distinguishable(store, cfg, monkeypatch):
    seed_signal(store, "buyee_mercari:m21")
    seed_signal(store, "buyee_mercari:m22")
    seed_signal(store, "ruten:9", site="ruten", url="https://example.test/nope")
    fill_obs(store, [obs_row("buyee_mercari:m21"), obs_row("buyee_mercari:m22")])
    fill_obs(
        store,
        [obs_row("ruten:9", site="ruten", url="https://example.test/nope")],
        site="ruten",
    )
    monkeypatch.setattr(
        "ygo_sniper.appraise.fetch_item_page",
        FakeItemFetcher({
            "m21": FetchError("抓不到", url="u", transient=True),
            "m22": page(None),          # 頁面正常但沒有賣家欄位
        }),
    )

    report = backfill_signal_sellers(cfg=cfg, store=store, targets=seed_targets(store))
    notes = {f.key: f.note for f in report.fills}

    assert "抓取失敗" in notes["buyee_mercari:m21"]
    assert "沒有賣家欄位" in notes["buyee_mercari:m22"]
    assert "網址不支援" in notes["ruten:9"]
    assert report.resolved == 0
    # 一筆失敗不得讓整輪停擺
    assert len(report.fills) == 3


def test_sellers_map_groups_keys_by_seller(store, cfg, monkeypatch):
    for key in ("buyee_mercari:m31", "buyee_mercari:m32"):
        seed_signal(store, key)
    fill_obs(store, [obs_row("buyee_mercari:m31"), obs_row("buyee_mercari:m32")])
    monkeypatch.setattr(
        "ygo_sniper.appraise.fetch_item_page",
        FakeItemFetcher({"m31": page("777"), "m32": page("777")}),
    )

    report = backfill_signal_sellers(cfg=cfg, store=store, targets=seed_targets(store))
    assert report.sellers() == {
        "buyee_mercari:777": ["buyee_mercari:m31", "buyee_mercari:m32"]
    }
