"""清除已離場標的：migration、store 方法、自動還原、API。

骨架照 `tests/test_card_bucket.py`——bucket 欄位當初就是用同一套
「migration＋store＋web API」三段結構加進來的。
"""

import importlib
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ygo_sniper.domain import TriageState
from ygo_sniper.store import Store
from ygo_sniper.verify_departed import VerifyResult

ROOT = Path(__file__).resolve().parents[1]

NEW_COLUMNS = ("cleared_at", "cleared_from", "restored_count")

#: 實證復活徽章（2026-08-08）：戳記＋帳本。
VERIFIED_COLUMNS = ("cleared_verified", "verified_restored_count")

LOW = {"_default": "low"}


def _verifier(verdicts: dict[str, str]):
    """key → verdict 的假驗證器，並記錄呼叫。

    沒列在表裡的 key 被驗證就直接 KeyError——測試裡不准有未預期的驗證
    （ended 與 live 的列**不該**花驗證流量）。
    """

    def _v(key: str, url: str) -> VerifyResult:
        _v.calls.append((key, url))
        return VerifyResult(key, verdicts[key], "test")

    _v.calls = []
    return _v


def _sold_verifier():
    """所有 gone 候選一律回 SOLD——給只關心「有實證就清」的測試用。"""

    def _v(key: str, url: str) -> VerifyResult:
        return VerifyResult(key, "SOLD", "test")

    return _v


def _legacy_db(path: Path) -> None:
    """新欄位出現之前的 signals 表，外加兩列既有資料。"""
    with sqlite3.connect(path) as c:
        c.execute(
            """
            CREATE TABLE signals (
                key          TEXT PRIMARY KEY,
                site         TEXT NOT NULL,
                external_id  TEXT NOT NULL,
                title        TEXT NOT NULL,
                url          TEXT NOT NULL,
                score        REAL,
                flags        TEXT,
                payload      TEXT,
                state        TEXT DEFAULT 'new',
                bucket       TEXT,
                note         TEXT DEFAULT '',
                first_seen   TEXT,
                last_seen    TEXT,
                notified_at  TEXT
            )
            """
        )
        for key, state in (("old-a", "watching"), ("old-b", "bought")):
            c.execute(
                "INSERT INTO signals (key, site, external_id, title, url, score, state,"
                " flags, payload, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, "buyee_yahoo", key, f"卡 {key}", f"https://example.test/{key}",
                 50.0, state, "[]", "{}", "2026-08-01T00:00:00+00:00",
                 "2026-08-01T00:00:00+00:00"),
            )


def _columns(path: Path) -> set[str]:
    with sqlite3.connect(path) as c:
        return {r[1] for r in c.execute("PRAGMA table_info(signals)")}


def _insert(store: Store, key: str, *, state: str = "watching", site: str = "buyee_yahoo",
            score: float = 50.0, payload: str = "{}") -> None:
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "INSERT INTO signals (key, site, external_id, title, url, score, state,"
            " flags, payload, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, site, key, f"卡 {key}", f"https://example.test/{key}", score, state,
             "[]", payload, "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        )


def _mark_gone(store: Store, key: str, when: str = "2026-08-05T00:00:00+00:00",
               site: str = "buyee_yahoo") -> None:
    """在 listing_obs 記一筆「已離場」的觀測。"""
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "INSERT INTO listing_obs (key, site, title, url, first_seen, last_seen,"
            " seen_count, disappeared_at) VALUES (?,?,?,?,?,?,?,?)",
            (key, site, f"卡 {key}", f"https://example.test/{key}",
             "2026-08-01T00:00:00+00:00", when, 3, when),
        )


def _rescan(store: Store, key: str, *, site: str = "buyee_yahoo") -> dict:
    """跑一輪**真的**掃描：這一輪有看到這筆標的。

    直接 `UPDATE listing_obs SET disappeared_at = NULL` 也能造出同樣的狀態，
    但那樣測到的就不是 `_upsert_listing_obs` 真的會做的事（CLAUDE.md 第六節：
    測試路徑必須等於生產路徑）。還原的唯一觸發點就是這條路徑，測試也走它。
    """
    return store.record_listing_scan(
        [
            {
                "source": "test",
                "site": site,
                "healthy": True,
                "rows": [
                    {
                        "key": key,
                        "site": site,
                        "title": f"卡 {key}",
                        "url": f"https://example.test/{key}",
                    }
                ],
            }
        ]
    )


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / "sniper.db")


def test_migration_adds_columns_without_touching_existing_rows(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    assert not (set(NEW_COLUMNS) & _columns(db))

    Store(db)

    assert set(NEW_COLUMNS) <= _columns(db)
    with sqlite3.connect(db) as c:
        rows = dict(c.execute("SELECT key, state FROM signals").fetchall())
        assert rows == {"old-a": "watching", "old-b": "bought"}
        cleared = c.execute("SELECT cleared_at, cleared_from FROM signals").fetchall()
        assert all(a is None and b is None for a, b in cleared)
        restored = c.execute("SELECT restored_count FROM signals").fetchall()
        assert all(n == 0 for (n,) in restored)


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    for _ in range(3):
        Store(db)
    assert set(NEW_COLUMNS) <= _columns(db)


def test_migration_adds_verified_columns_with_zero_default(tmp_path):
    """舊 db 補上實證欄位，既有列一律 0（不是 NULL——後面 `+ cleared_verified`
    遇到 NULL 會得到 NULL，與 `_migrate_signals` 對 restored_count 的顧慮同款）。"""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    assert not (set(VERIFIED_COLUMNS) & _columns(db))

    Store(db)

    assert set(VERIFIED_COLUMNS) <= _columns(db)
    with sqlite3.connect(db) as c:
        rows = c.execute(
            "SELECT cleared_verified, verified_restored_count FROM signals"
        ).fetchall()
        assert rows and all(cv == 0 and vr == 0 for cv, vr in rows)
        # 既有列的狀態原封不動（additive migration 的底線）
        states = dict(c.execute("SELECT key, state FROM signals").fetchall())
        assert states == {"old-a": "watching", "old-b": "bought"}


def test_verified_migration_is_idempotent(tmp_path):
    """正式庫每 30 分鐘被排程開啟一次，冪等是必要條件不是加分項。"""
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    for _ in range(3):
        Store(db)
    assert set(VERIFIED_COLUMNS) <= _columns(db)


def test_clear_stamps_cleared_verified_on_every_cleared_row(store):
    """2026-08-07 起清除的兩條路徑都是實證：ended（end_time 已過的事實）與
    gone 經 verifier 判 SOLD/DELISTED。**每一筆**被清的列都要蓋 cleared_verified=1
    ——徽章帳的分母就是從這個戳記來的。"""
    _insert(store, "buyee_yahoo:ended-1", payload=json.dumps(
        {"listing": {"end_time": "2026-01-01T00:00:00+00:00"}}
    ))
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")

    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )

    assert result["cleared"] == 2
    for key in ("buyee_yahoo:ended-1", "buyee_yahoo:gone-1"):
        row = store.get_signal(key)
        assert row["state"] == TriageState.EXPIRED.value
        assert row["cleared_verified"] == 1


def test_uncleared_rows_never_get_the_stamp(store):
    """STILL_LIVE／UNVERIFIABLE／live 的列沒被清，戳記必須維持 0。"""
    _insert(store, "buyee_yahoo:phantom")
    _mark_gone(store, "buyee_yahoo:phantom")
    _insert(store, "buyee_yahoo:blocked")
    _mark_gone(store, "buyee_yahoo:blocked")
    _insert(store, "buyee_yahoo:live-1")

    store.clear_expired_signals(
        "watching", gone_confidence=LOW,
        verifier=_verifier({
            "buyee_yahoo:phantom": "STILL_LIVE",
            "buyee_yahoo:blocked": "UNVERIFIABLE",
        }),
    )

    for key in ("buyee_yahoo:phantom", "buyee_yahoo:blocked", "buyee_yahoo:live-1"):
        assert store.get_signal(key)["cleared_verified"] == 0


def test_list_signals_brings_obs_columns(store):
    _insert(store, "a")
    _mark_gone(store, "a", when="2026-08-05T00:00:00+00:00")
    row = store.list_signals(state="watching")[0]
    assert row["obs_disappeared_at"] == "2026-08-05T00:00:00+00:00"
    assert row["obs_window_exit_at"] is None
    assert row["obs_revived_count"] == 0


def test_list_signals_join_does_not_clobber_signals_columns(store):
    """兩表有 11 個同名欄位（last_seen / landed_twd / grade …）。
    JOIN 之後 signals 那一側的值必須原封不動——這是 CLAUDE.md 第三節的混源陷阱。
    """
    _insert(store, "a")
    with sqlite3.connect(store.db_path) as c:
        # listing_obs 故意寫入不同的 last_seen 與 title
        c.execute(
            "INSERT INTO listing_obs (key, site, title, url, first_seen, last_seen,"
            " seen_count) VALUES (?,?,?,?,?,?,?)",
            ("a", "ebay", "別的標題", "https://other.test/a",
             "2026-07-01T00:00:00+00:00", "2026-07-02T00:00:00+00:00", 9),
        )
    row = store.list_signals(state="watching")[0]
    assert row["title"] == "卡 a"
    assert row["site"] == "buyee_yahoo"
    assert row["last_seen"] == "2026-08-01T00:00:00+00:00"


def test_list_signals_without_obs_row_is_fine(store):
    """沒有觀測列的標的照樣要出現在清單裡（LEFT JOIN 不是 INNER）。"""
    _insert(store, "solo")
    rows = store.list_signals(state="watching")
    assert [r["key"] for r in rows] == ["solo"]
    assert rows[0]["obs_disappeared_at"] is None


def test_gone_rows_are_not_cleared_without_a_verifier(store):
    """安全預設（2026-08-07 事故的修法核心）：沒有驗證器就只清事實（ended），
    gone **推論**一筆都不清。忘記接 verifier 的呼叫端自動退到安全行為，
    而不是退回舊的推論清除——那個舊行為一鍵誤清了 43 筆（誤殺率 100%）。"""
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")

    result = store.clear_expired_signals("watching", gone_confidence=LOW)

    assert result["cleared"] == 0
    assert result["keys"] == []
    # 沒被驗證的 gone 候選要**看得見**（skipped），不是消失在回報裡
    assert result["by_verdict"]["skipped"] == 1
    assert store.get_signal("buyee_yahoo:gone-1")["state"] == "watching"


def test_clear_expired_moves_verified_gone_rows_to_expired(store):
    """gone 候選逐筆開頁驗證，只清拿到實證的（SOLD／DELISTED）。"""
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")
    _insert(store, "buyee_yahoo:gone-2")
    _mark_gone(store, "buyee_yahoo:gone-2")
    _insert(store, "buyee_yahoo:live-1")
    verifier = _verifier(
        {"buyee_yahoo:gone-1": "SOLD", "buyee_yahoo:gone-2": "DELISTED"}
    )

    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=verifier
    )

    assert result["cleared"] == 2
    assert sorted(result["keys"]) == ["buyee_yahoo:gone-1", "buyee_yahoo:gone-2"]
    assert result["by_source"] == {"buyee_yahoo": 2}
    assert result["by_verdict"] == {
        "ended": 0, "sold": 1, "delisted": 1,
        "still_live": 0, "unverifiable": 0, "skipped": 0,
    }
    for key in ("buyee_yahoo:gone-1", "buyee_yahoo:gone-2"):
        row = store.get_signal(key)
        assert row["state"] == TriageState.EXPIRED.value
        assert row["cleared_from"] == "watching"
        assert row["cleared_at"] is not None
    # live 的列不花驗證流量，也不被清
    assert store.get_signal("buyee_yahoo:live-1")["state"] == "watching"
    assert sorted(k for k, _ in verifier.calls) == [
        "buyee_yahoo:gone-1", "buyee_yahoo:gone-2",
    ]


def test_still_live_gone_row_is_revived_not_cleared(store):
    """STILL_LIVE 順手修帳：頁面實證比掃描推論權威——
    `disappeared_at` 清掉、`revived_count` +1（那是推論規則自己的錯誤率帳本）。"""
    key = "buyee_yahoo:phantom"
    _insert(store, key)
    _mark_gone(store, key)

    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_verifier({key: "STILL_LIVE"})
    )

    assert result["cleared"] == 0
    assert result["by_verdict"]["still_live"] == 1
    assert result["still_live_keys"] == [key]
    assert store.get_signal(key)["state"] == "watching"
    with sqlite3.connect(store.db_path) as c:
        gone, revived = c.execute(
            "SELECT disappeared_at, revived_count FROM listing_obs WHERE key = ?",
            (key,),
        ).fetchone()
    assert gone is None
    assert revived == 1


def test_unverifiable_gone_row_is_left_untouched(store):
    """被擋／逾時／不支援的站：讀不到 ≠ 賣光。不清、不修帳、計數回報。"""
    key = "buyee_yahoo:blocked"
    _insert(store, key)
    _mark_gone(store, key, when="2026-08-05T00:00:00+00:00")

    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_verifier({key: "UNVERIFIABLE"})
    )

    assert result["cleared"] == 0
    assert result["by_verdict"]["unverifiable"] == 1
    assert store.get_signal(key)["state"] == "watching"
    with sqlite3.connect(store.db_path) as c:
        gone, revived = c.execute(
            "SELECT disappeared_at, revived_count FROM listing_obs WHERE key = ?",
            (key,),
        ).fetchone()
    # 原封不動：離場推論保留（下次還會再驗），帳本不動
    assert gone == "2026-08-05T00:00:00+00:00"
    assert revived == 0


def test_ended_rows_clear_without_verification(store):
    """`end_time` 已過是事實，不花驗證流量——verifier 完全不被呼叫。"""
    _insert(store, "buyee_yahoo:ended-1", payload=json.dumps(
        {"listing": {"end_time": "2026-01-01T00:00:00+00:00"}}
    ))
    verifier = _verifier({})          # 誰被驗證誰就 KeyError

    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=verifier
    )

    assert result["cleared"] == 1
    assert result["keys"] == ["buyee_yahoo:ended-1"]
    assert result["by_verdict"]["ended"] == 1
    assert verifier.calls == []


def test_clear_expired_is_idempotent(store):
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    again = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert again["cleared"] == 0
    assert again["keys"] == []


def test_clear_expired_only_touches_the_named_state(store):
    """清觀察中不能順手把已購買的也清掉——就算驗證說它真的賣掉了。"""
    _insert(store, "buyee_yahoo:bought-1", state="bought")
    _mark_gone(store, "buyee_yahoo:bought-1")
    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert result["cleared"] == 0
    assert store.get_signal("buyee_yahoo:bought-1")["state"] == "bought"


def test_clear_expired_rejects_unknown_state(store):
    with pytest.raises(ValueError, match="不可清除"):
        store.clear_expired_signals("bought", gone_confidence=LOW)


def _signal_for(key: str, *, site: str = "buyee_yahoo"):
    """組一個最小可用的 Signal 給 upsert_signal 用。

    `Signal` 的八個欄位全是必填（`domain.py:289-298`），`Listing` 的價格欄位
    叫 `price` 不是 `price_native`（`domain.py:170`）。

    `key` 必須是 signals 表的主鍵形狀 `site:external_id`（正式庫實測：
    `buyee_yahoo:n1238185137`）——`upsert_signal` 拿 `Listing.key`
    （`domain.py:193-194` 現算 `f"{site}:{external_id}"`）去比對既有列，
    餵裸 key 會**新插一列**而不是走 `existing` 分支，紅線測試
    `test_manually_expired_is_never_restored` 會因此假性通過。
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


def test_cleared_signal_is_restored_when_it_comes_back(store):
    """清掉的東西**真的又出現在架上** → 自動放回原狀態，並累加誤殺計數。

    「回來了」的定義只有一個：`listing_obs` 的離場標記被清掉——也就是
    `record_listing_scan` 這一輪真的又看到它。判定與清除同源（`expiry_status`），
    兩份條件遲早會漂移成兩種答案。
    """
    key = "buyee_yahoo:gone-1"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert store.get_signal(key)["state"] == TriageState.EXPIRED.value

    # 還沒被看到 → 一筆都不還原
    assert store.restore_revived_signals() == {"restored": 0, "keys": []}
    assert store.get_signal(key)["state"] == TriageState.EXPIRED.value

    _rescan(store, key)                       # 這一輪真的又掃到它
    assert store.restore_revived_signals() == {"restored": 1, "keys": [key]}

    row = store.get_signal(key)
    assert row["state"] == "watching"
    assert row["cleared_at"] is None
    assert row["cleared_from"] is None
    assert row["restored_count"] == 1


def test_bare_upsert_never_restores_a_cleared_signal(store):
    """還原的前提是**觀測證據**，不是「有人寫了一筆 Signal」。

    `recalc-bids --apply` 與 `resolve-grades --apply` 都會對 expired 列重跑
    `upsert_signal`（前者取 `state="all"`、後者 `WHERE grade IS NULL` 無 state
    過濾）。還原掛在 upsert 上的話，它們每跑一次 `restored_count` 就 +1，
    而 `listing_obs` 從頭到尾都說這筆不在架上。

    那個計數是 `expiry-stats` 印給使用者看的**誤殺率**，並且要他據此調
    `gone_confidence`——分子被灌水的指標比沒有指標更糟。
    """
    key = "buyee_yahoo:gone-1"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )

    for _ in range(4):
        store.upsert_signal(_signal_for(key))

    row = store.get_signal(key)
    assert row["state"] == TriageState.EXPIRED.value
    assert row["cleared_from"] == "watching"
    assert row["restored_count"] == 0
    assert store.expiry_stats()["restored_total"] == 0
    # 判定沒有跟著變：listing_obs 仍然記著它離場
    assert store.list_signals(state="expired")[0]["obs_disappeared_at"] is not None


def test_restore_is_idempotent(store):
    """還原完就不再符合條件（cleared_from 已清空），重跑回 0。"""
    key = "buyee_yahoo:gone-1"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    _rescan(store, key)
    assert store.restore_revived_signals()["restored"] == 1
    assert store.restore_revived_signals() == {"restored": 0, "keys": []}


def test_restore_needs_an_observation_row(store):
    """沒有觀測列 ≠ 標的回來了。

    `prune_listing_obs` 會把過保留期的觀測列刪掉；LEFT JOIN 之後那些列的
    `disappeared_at` 也是 NULL，但那是「我們不知道」不是「它回來了」。
    讀不到 ≠ 東西回來了——與「讀不到 ≠ 東西不見了」同一條原則。
    """
    key = "buyee_yahoo:pruned"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    with sqlite3.connect(store.db_path) as c:
        c.execute("DELETE FROM listing_obs WHERE key = ?", (key,))

    assert store.restore_revived_signals() == {"restored": 0, "keys": []}
    assert store.get_signal(key)["state"] == TriageState.EXPIRED.value


def test_ended_auction_is_not_restored_just_because_it_is_still_listed(store):
    """結標的競標常常還留在搜尋結果裡（有觀測列、沒有離場標記）。

    還原只看 `disappeared_at IS NULL` 的話，它會被清掉 → 立刻還原 →
    下次又被清掉，無限打轉；那正是 flip-flop 換一個入口重演。
    所以還原與清除共用**同一個判準**（`expiry_status`），不是兩套條件。
    """
    key = "buyee_yahoo:ended-1"
    _insert(store, key, payload=json.dumps(
        {"listing": {"end_time": "2026-01-01T00:00:00+00:00"}}
    ))
    _rescan(store, key)                       # 有觀測列、從沒被判離場
    # 刻意不給 verifier：ended 是事實，安全預設下也照清
    cleared = store.clear_expired_signals("watching", gone_confidence=LOW)
    assert cleared["cleared"] == 1            # end_time 已過 → 確定事實

    assert store.restore_revived_signals() == {"restored": 0, "keys": []}
    assert store.get_signal(key)["state"] == TriageState.EXPIRED.value


def test_manually_expired_is_never_restored(store):
    """紅線：使用者手動標的 expired 沒有 cleared_from，程式不准動它。"""
    key = "buyee_yahoo:manual"
    _insert(store, key, state=TriageState.EXPIRED.value)
    _rescan(store, key)                       # 就算它好端端在架上也一樣
    store.upsert_signal(_signal_for(key))
    store.restore_revived_signals()
    row = store.get_signal(key)
    assert row["state"] == TriageState.EXPIRED.value
    assert row["restored_count"] == 0
    # 走的必須是 existing 分支——多插一列的話上面兩條會假性通過（見 _signal_for）
    with sqlite3.connect(store.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1


def test_user_expire_after_a_program_clear_is_not_reverted(store):
    """紅線（本次修正的核心）：程式清過一次的標的，使用者拉回觀察中、
    幾天後自己標成 expired——那是**使用者的決定**，掃描不准把它改回去。

    `cleared_from` 記的是「程式做了什麼」。人一旦動手接管這一筆，那段歷史
    就不該再有效力；否則使用者自己標的 expired 會被靜默改回 watching，
    沒有 log、沒有 toast，外顯與「這個分頁本來就有這筆」一模一樣。
    """
    key = "buyee_yahoo:tug-of-war"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )

    store.update_state(key, "watching")                 # 使用者不同意，拉回來
    assert store.get_signal(key)["cleared_from"] is None
    assert store.get_signal(key)["cleared_at"] is None
    store.update_state(key, TriageState.EXPIRED.value)  # 幾天後自己放棄

    # 下一輪掃描做的全部事情
    _rescan(store, key)
    store.upsert_signal(_signal_for(key))
    store.restore_revived_signals()

    row = store.get_signal(key)
    assert row["state"] == TriageState.EXPIRED.value
    assert row["restored_count"] == 0


def test_update_state_clears_the_clear_marks(store):
    """使用者手動改狀態 = 重新接管這一筆，清除標記要一起歸零。"""
    key = "buyee_yahoo:taken-over"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert store.get_signal(key)["cleared_from"] == "watching"

    store.update_state(key, "watching", note="我再看看")

    row = store.get_signal(key)
    assert row["cleared_from"] is None
    assert row["cleared_at"] is None
    assert row["note"] == "我再看看"
    # 誤殺計數是帳本，不歸零——它記的是「這個功能錯過幾次」
    assert row["restored_count"] == 0


def test_restore_counts_accumulate(store):
    """反覆進出的標的，誤殺計數要累加——它就是這個功能的錯誤率。"""
    key = "buyee_yahoo:flappy"
    _insert(store, key)
    _mark_gone(store, key)
    for expected in (1, 2):
        store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
        _rescan(store, key)
        assert store.restore_revived_signals()["restored"] == 1
        assert store.get_signal(key)["restored_count"] == expected
        _mark_gone_again(store, key)


def _mark_gone_again(store: Store, key: str) -> None:
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "UPDATE listing_obs SET disappeared_at = ? WHERE key = ?",
            ("2026-08-05T00:00:00+00:00", key),
        )


def test_clear_expired_survives_more_keys_than_sqlite_takes_host_params(store):
    """`WHERE key IN (?,?,…)` 的參數上限是 SQLITE_MAX_VARIABLE_NUMBER（32766），
    而取資料那一側寫的是 `limit=100_000`——兩個上限對不起來就是一顆定時炸彈。

    超過就整支 `OperationalError: too many SQL variables`，一筆都清不掉。
    觸發情境：某來源整站被擋、或 `listing_obs` 累積後一次大清。
    """
    n = 33_000
    rows = [
        (f"buyee_yahoo:bulk-{i}", "buyee_yahoo", f"bulk-{i}", f"卡 {i}",
         f"https://example.test/{i}", 50.0, "watching", "[]", "{}",
         "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00")
        for i in range(n)
    ]
    with sqlite3.connect(store.db_path) as c:
        c.executemany(
            "INSERT INTO signals (key, site, external_id, title, url, score, state,"
            " flags, payload, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        c.executemany(
            "INSERT INTO listing_obs (key, site, title, url, first_seen, last_seen,"
            " seen_count, disappeared_at) VALUES (?,?,?,?,?,?,?,?)",
            [(r[0], "buyee_yahoo", r[3], r[4], "2026-08-01T00:00:00+00:00",
              "2026-08-05T00:00:00+00:00", 3, "2026-08-05T00:00:00+00:00")
             for r in rows],
        )

    result = store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert result["cleared"] == n
    with sqlite3.connect(store.db_path) as c:
        assert c.execute(
            "SELECT COUNT(*) FROM signals WHERE state = 'expired'"
        ).fetchone()[0] == n

    # 還原走同一個分批路徑，同樣不能在同一個上限上炸掉
    with sqlite3.connect(store.db_path) as c:
        c.execute("UPDATE listing_obs SET disappeared_at = NULL")
    assert store.restore_revived_signals()["restored"] == n


def test_normal_upsert_still_preserves_manual_state(store):
    """既有紅線不能被這次改動弄壞：一般的重掃不覆寫人工狀態。"""
    key = "buyee_yahoo:asked"
    _insert(store, key, state="asked_seller")
    store.upsert_signal(_signal_for(key))
    assert store.get_signal(key)["state"] == "asked_seller"


def test_revive_rate_by_source(store):
    """量測定義：分母 = 曾被判離場的列，分子 = 其中 revived_count > 0 的。"""
    with sqlite3.connect(store.db_path) as c:
        rows = [
            ("a", "buyee_yahoo", "2026-08-05T00:00:00+00:00", 0),   # 離場、沒復活
            ("b", "buyee_yahoo", None, 2),                          # 復活過
            ("c", "ebay", "2026-08-05T00:00:00+00:00", 1),          # 離場且復活過
            ("d", "ebay", None, 0),                                 # 兩者皆非 → 不列入
        ]
        for key, site, gone, revived in rows:
            c.execute(
                "INSERT INTO listing_obs (key, site, title, url, first_seen, last_seen,"
                " seen_count, disappeared_at, revived_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, site, key, f"https://example.test/{key}",
                 "2026-08-01T00:00:00+00:00", "2026-08-02T00:00:00+00:00",
                 3, gone, revived),
            )
    stats = store.revive_rate_by_source()
    assert stats["buyee_yahoo"] == {"ever_gone": 2, "revived": 1, "pct": 50.0}
    assert stats["ebay"] == {"ever_gone": 1, "revived": 1, "pct": 100.0}
    assert "d" not in stats


def test_expiry_stats_reports_cleared_and_restored(store):
    # key 用生產形狀 `site:external_id`（見 `_signal_for` 的自我把關）：
    # 裸 key 的話 upsert_signal 會另插一列，還原分支根本不會被走到。
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")
    _insert(store, "buyee_yahoo:gone-2")
    _mark_gone(store, "buyee_yahoo:gone-2")
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    _rescan(store, "buyee_yahoo:gone-1")                        # 誤殺，自己回來了
    store.restore_revived_signals()

    stats = store.expiry_stats()
    assert stats["cleared_now"] == 1               # gone-2 還在 expired
    assert stats["restored_total"] == 1
    assert stats["by_cleared_from"] == {"watching": 1}


# ---------------------------------------------------------------------------
# 候選挑選的唯一入口：clear 與 CLI dry-run 共用，不准各寫一份判定
# ---------------------------------------------------------------------------
def test_departed_candidates_splits_ended_and_gone(store):
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")
    _insert(store, "buyee_yahoo:ended-1", payload=json.dumps(
        {"listing": {"end_time": "2026-01-01T00:00:00+00:00"}}
    ))
    _insert(store, "buyee_yahoo:live-1")

    cand = store.departed_candidates("watching", gone_confidence=LOW)

    assert [r["key"] for r in cand["ended"]] == ["buyee_yahoo:ended-1"]
    assert [r["key"] for r in cand["gone"]] == ["buyee_yahoo:gone-1"]
    # 候選列要帶得動後續動作：驗證要 url、dry-run 表要 title
    assert cand["gone"][0]["url"] == "https://example.test/buyee_yahoo:gone-1"
    assert cand["gone"][0]["title"] == "卡 buyee_yahoo:gone-1"


def test_departed_candidates_rejects_bad_state(store):
    with pytest.raises(ValueError, match="不可清除"):
        store.departed_candidates("bought", gone_confidence=LOW)


# ---------------------------------------------------------------------------
# 驗證清除的背景狀態組（meta 表，照 scan_status 的寫法）
# ---------------------------------------------------------------------------
def test_verify_clear_status_fresh_db_is_not_running(store):
    st = store.verify_clear_status()
    assert st["running"] is False
    assert st["stale"] is False
    assert st["started_at"] is None
    assert st["progress"] == {"done": 0, "total": None}
    assert st["last_result"] is None


def test_verify_clear_begin_progress_finish_roundtrip(store):
    started = store.begin_verify_clear(
        trigger="dashboard", state="watching", total=3
    )
    st = store.verify_clear_status(timeout_seconds=1800)
    assert st["running"] is True
    assert st["started_at"] == started
    assert st["trigger"] == "dashboard"
    assert st["state"] == "watching"
    assert st["progress"] == {"done": 0, "total": 3}

    store.set_verify_clear_progress(2, 3)
    assert store.verify_clear_status()["progress"] == {"done": 2, "total": 3}

    result = {"cleared": 1, "by_verdict": {"sold": 1}}
    store.finish_verify_clear(started, result=result)
    st = store.verify_clear_status()
    assert st["running"] is False
    assert st["finished_at"] is not None
    assert st["error"] is None
    assert st["last_result"] == result
    # 完成後 progress 保留最後的值——前端 toast 要顯示「驗了幾筆」
    assert st["progress"] == {"done": 2, "total": 3}


def test_verify_clear_failure_is_recorded_not_stuck(store):
    started = store.begin_verify_clear(state="watching", total=5)
    store.finish_verify_clear(started, error="RuntimeError: 驗證炸了")
    st = store.verify_clear_status()
    assert st["running"] is False
    assert "驗證炸了" in st["error"]
    assert st["last_result"] is None


def test_verify_clear_timeout_is_not_running(store):
    """崩潰後沒人呼叫 finish → 靠逾時兜底，前端按鈕才有自救手段
    （與 scan_status 同一個理由：讀不到 ≠ 還在跑）。"""
    store.begin_verify_clear(state="watching", total=1)
    st = store.verify_clear_status(timeout_seconds=0.0)
    assert st["running"] is False
    assert st["stale"] is True


def test_begin_verify_clear_overwrites_stale_state(store):
    """殘留的 running 狀態不該擋住下一輪（覆寫是刻意的，同 begin_scan）。"""
    store.begin_verify_clear(state="watching", total=9)
    started = store.begin_verify_clear(state="watching", total=2)
    st = store.verify_clear_status(timeout_seconds=1800)
    assert st["running"] is True
    assert st["started_at"] == started
    assert st["progress"] == {"done": 0, "total": 2}


# ---------------------------------------------------------------------------
# API：TestClient 打真的端點（fixture 照抄 `tests/test_card_bucket.py:189-220`）
# ---------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    import ygo_sniper.config as config_mod

    db = tmp_path / "web.db"
    real_load = config_mod.load_config

    def _tmp_config(*a, **kw):
        # storage["db_path"] 給**絕對路徑**：Config.db_path 是 `root / 那個值`，
        # 右運算元是絕對路徑時 root 會被忽略，剛好就是我們要的效果。
        # replace() 而不是就地改：real_load 有 lru_cache，改它回傳的物件
        # 會汙染整個 suite 的設定。
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


def test_signals_api_carries_expiry(client):
    """判定只有 expiry.py 一份：API 把它的結果原樣送出，前端不自己算。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")
    _insert(app_mod.store, "buyee_yahoo:live-1")

    items = c.get("/api/signals?state=watching").json()["items"]
    by_key = {i["key"]: i for i in items}

    assert by_key["buyee_yahoo:gone-1"]["expiry"]["kind"] == "gone"
    assert "消失" in by_key["buyee_yahoo:gone-1"]["expiry"]["detail"]
    assert by_key["buyee_yahoo:live-1"]["expiry"]["kind"] == "live"


_ENDED_PAYLOAD = json.dumps({"listing": {"end_time": "2026-01-01T00:00:00+00:00"}})


class _FakeEndpointVerifier:
    """給端點測試用的假 verifier（mock verifier、不 mock store）。

    順手扮演 `build_page_verifier` 回傳物的完整介面（callable + close），
    並在**每筆驗證當下**偷看 store 的背景狀態——TestClient 會在回應返回前
    把 BackgroundTask 跑完，所以「驗證中 running=True、progress 有前進」
    只能從驗證器內部觀測。
    """

    def __init__(self, verdicts: dict[str, str], store: Store | None = None):
        self.verdicts = verdicts
        self.store = store
        self.calls: list[str] = []
        self.closed = False
        self.observed: list[dict] = []

    def __call__(self, key: str, url: str) -> VerifyResult:
        self.calls.append(key)
        if self.store is not None:
            st = self.store.verify_clear_status()
            self.observed.append(
                {"running": st["running"], "progress": st["progress"]}
            )
        return VerifyResult(key, self.verdicts[key], "test")

    def close(self) -> None:
        self.closed = True


def test_clear_expired_endpoint_verifies_in_background(client, monkeypatch):
    """端點三件組的主流程：POST 開始 → 驗證期間狀態是 running、progress
    逐筆前進 → 完成後 status 的 last_result 分項正確、資源已關。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")
    _insert(app_mod.store, "buyee_yahoo:phantom")
    _mark_gone(app_mod.store, "buyee_yahoo:phantom")
    _insert(app_mod.store, "buyee_yahoo:ended-1", payload=_ENDED_PAYLOAD)
    _insert(app_mod.store, "buyee_yahoo:live-1")
    fake = _FakeEndpointVerifier(
        {"buyee_yahoo:gone-1": "SOLD", "buyee_yahoo:phantom": "STILL_LIVE"},
        store=app_mod.store,
    )
    monkeypatch.setattr(app_mod, "build_page_verifier", lambda _cfg: fake)

    r = c.post("/api/signals/clear-expired", json={"state": "watching"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["started"] is True
    assert body["running"] is True

    # 驗證期間：狀態是 running，progress 每筆前進一次（total = gone 候選數）
    assert len(fake.observed) == 2
    assert all(o["running"] for o in fake.observed)
    assert [o["progress"] for o in fake.observed] == [
        {"done": 0, "total": 2}, {"done": 1, "total": 2},
    ]

    # 完成後：status 端點回 last_result 分項
    st = c.get("/api/signals/clear-expired/status").json()
    assert st["running"] is False
    assert st["error"] is None
    assert st["progress"] == {"done": 2, "total": 2}
    assert st["last_result"]["cleared"] == 2          # ended-1 + gone-1
    assert st["last_result"]["by_verdict"] == {
        "ended": 1, "sold": 1, "delisted": 0,
        "still_live": 1, "unverifiable": 0, "skipped": 0,
    }
    assert st["last_result"]["still_live_keys"] == ["buyee_yahoo:phantom"]

    # db 是真的（不 mock store）：清了誰、修了誰的帳，逐筆對
    assert app_mod.store.get_signal("buyee_yahoo:gone-1")["state"] == "expired"
    assert app_mod.store.get_signal("buyee_yahoo:ended-1")["state"] == "expired"
    assert app_mod.store.get_signal("buyee_yahoo:phantom")["state"] == "watching"
    assert app_mod.store.get_signal("buyee_yahoo:live-1")["state"] == "watching"
    assert fake.closed is True, "背景任務結束必須關掉 verifier 的資源"


def test_clear_expired_endpoint_does_not_start_twice(client, monkeypatch):
    """防重入：已經在跑（例如 CLI 那邊）就回 started:false，
    不建 verifier、不動任何一列——兩條驗證管線同時打同一批站只會更快被擋。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")
    app_mod.store.begin_verify_clear(trigger="cli", state="watching", total=5)
    monkeypatch.setattr(
        app_mod, "build_page_verifier",
        lambda _cfg: (_ for _ in ()).throw(
            AssertionError("已在跑時不准再建 verifier")
        ),
    )

    r = c.post("/api/signals/clear-expired", json={"state": "watching"})
    assert r.status_code == 200
    body = r.json()
    assert body["started"] is False
    assert body["running"] is True
    assert app_mod.store.get_signal("buyee_yahoo:gone-1")["state"] == "watching"


def test_clear_expired_endpoint_failure_does_not_stick_running(
    client, monkeypatch
):
    """verifier 半路炸掉：例外照樣大聲往外傳，但狀態必須離開 running
    （error 落地、資源關閉），不然按鈕會鎖到逾時為止。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")

    class _Boom(_FakeEndpointVerifier):
        def __call__(self, key: str, url: str) -> VerifyResult:
            raise RuntimeError("驗證炸了")

    boom = _Boom({})
    monkeypatch.setattr(app_mod, "build_page_verifier", lambda _cfg: boom)

    with pytest.raises(RuntimeError, match="驗證炸了"):
        c.post("/api/signals/clear-expired", json={"state": "watching"})

    st = app_mod.store.verify_clear_status()
    assert st["running"] is False, "驗證炸了卻卡在 running，按鈕會鎖到逾時為止"
    assert "驗證炸了" in (st["error"] or "")
    assert boom.closed is True
    # 一筆都沒清（清除寫入在驗證全數完成之後）
    assert app_mod.store.get_signal("buyee_yahoo:gone-1")["state"] == "watching"


def test_clear_expired_endpoint_is_idempotent(client, monkeypatch):
    """清完就不在原 state，重按第二次的 last_result 回 cleared: 0
    （工程原則二——非冪等寫入不可重試，所以這支刻意設計成冪等）。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:ended-1", payload=_ENDED_PAYLOAD)
    monkeypatch.setattr(
        app_mod, "build_page_verifier", lambda _cfg: _FakeEndpointVerifier({})
    )

    c.post("/api/signals/clear-expired", json={"state": "watching"})
    first = c.get("/api/signals/clear-expired/status").json()
    assert first["last_result"]["cleared"] == 1

    c.post("/api/signals/clear-expired", json={"state": "watching"})
    again = c.get("/api/signals/clear-expired/status").json()
    assert again["last_result"]["cleared"] == 0


def test_clear_expired_endpoint_rejects_bad_state(client):
    """不可清除的狀態是語意錯誤，要回 400——而且要在**背景任務開跑之前**擋，
    不是安靜地回 cleared: 0。"""
    c, app_mod = client
    r = c.post("/api/signals/clear-expired", json={"state": "bought"})
    assert r.status_code == 400
    assert "可清除" in r.json()["detail"]
    # 連狀態都不准動：begin 過的話按鈕會被一個不存在的任務鎖住
    assert app_mod.store.verify_clear_status()["running"] is False


# ---------------------------------------------------------------------------
# CLI `clear-departed`：與端點走同一條 store 路徑；--dry-run 只驗不寫
# ---------------------------------------------------------------------------
@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """臨時 db 的 CLI 環境：`cli.load_config` 指到 tmp，回傳 (runner, store)。

    承重的斷言在最後：CLI 測試絕不能碰正式庫 data/sniper.db。
    """
    import ygo_sniper.cli as cli_mod
    import ygo_sniper.config as config_mod

    db = tmp_path / "cli.db"
    base = config_mod.load_config()
    test_cfg = replace(base, storage={**base.storage, "db_path": str(db)})
    monkeypatch.setattr(cli_mod, "load_config", lambda: test_cfg)
    assert test_cfg.db_path == db, "CLI 測試的 cfg 沒有指到 tmp db"

    from typer.testing import CliRunner

    return CliRunner(), Store(db), cli_mod


def _seed_departed_mix(store: Store) -> None:
    """一組涵蓋四種 verdict 去向的標的：SOLD／STILL_LIVE／UNVERIFIABLE 的
    gone 候選＋一筆 ended（事實）＋一筆在架的。"""
    _insert(store, "buyee_yahoo:gone-1")
    _mark_gone(store, "buyee_yahoo:gone-1")
    _insert(store, "buyee_yahoo:phantom")
    _mark_gone(store, "buyee_yahoo:phantom")
    _insert(store, "buyee_yahoo:blocked")
    _mark_gone(store, "buyee_yahoo:blocked")
    _insert(store, "buyee_yahoo:ended-1", payload=_ENDED_PAYLOAD)
    _insert(store, "buyee_yahoo:live-1")


_MIX_VERDICTS = {
    "buyee_yahoo:gone-1": "SOLD",
    "buyee_yahoo:phantom": "STILL_LIVE",
    "buyee_yahoo:blocked": "UNVERIFIABLE",
}


def test_cli_clear_departed_dry_run_verifies_but_never_writes(
    cli_env, monkeypatch
):
    """--dry-run：逐筆印 verdict 表、**一個 byte 都不寫庫**——
    不清、不修帳（STILL_LIVE 的 disappeared_at 原封不動），使用者先看再決定。"""
    runner, store, cli_mod = cli_env
    _seed_departed_mix(store)
    fake = _FakeEndpointVerifier(_MIX_VERDICTS)
    monkeypatch.setattr(
        "ygo_sniper.verify_departed.build_page_verifier", lambda _cfg: fake
    )

    result = runner.invoke(
        cli_mod.app, ["clear-departed", "--state", "watching", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    # 三個 gone 候選都被驗、verdict 都有印
    assert sorted(fake.calls) == sorted(_MIX_VERDICTS)
    for verdict in ("SOLD", "STILL_LIVE", "UNVERIFIABLE"):
        assert verdict in result.output
    # ended（事實）不花驗證流量，但要讓使用者知道非 dry-run 會清它
    assert "end_time 已過" in result.output
    # UNVERIFIABLE 要大聲：筆數＋原因
    assert "無法驗證 1 筆" in result.output
    # 不寫庫：狀態、離場標記、帳本全部原封不動
    for key in ("buyee_yahoo:gone-1", "buyee_yahoo:phantom",
                "buyee_yahoo:blocked", "buyee_yahoo:ended-1",
                "buyee_yahoo:live-1"):
        assert store.get_signal(key)["state"] == "watching"
    with sqlite3.connect(store.db_path) as c:
        gone, revived = c.execute(
            "SELECT disappeared_at, revived_count FROM listing_obs WHERE key = ?",
            ("buyee_yahoo:phantom",),
        ).fetchone()
    assert gone is not None and revived == 0
    assert fake.closed is True


def test_cli_clear_departed_applies_via_the_same_store_path(
    cli_env, monkeypatch
):
    """非 dry-run 與端點走**同一條** store 路徑（clear_expired_signals）：
    清實證、STILL_LIVE 修帳、UNVERIFIABLE 保留＋大聲回報。"""
    runner, store, cli_mod = cli_env
    _seed_departed_mix(store)
    fake = _FakeEndpointVerifier(_MIX_VERDICTS)
    monkeypatch.setattr(
        "ygo_sniper.verify_departed.build_page_verifier", lambda _cfg: fake
    )

    result = runner.invoke(cli_mod.app, ["clear-departed", "--state", "watching"])

    assert result.exit_code == 0, result.output
    assert store.get_signal("buyee_yahoo:gone-1")["state"] == "expired"
    assert store.get_signal("buyee_yahoo:gone-1")["cleared_from"] == "watching"
    assert store.get_signal("buyee_yahoo:ended-1")["state"] == "expired"
    assert store.get_signal("buyee_yahoo:phantom")["state"] == "watching"
    assert store.get_signal("buyee_yahoo:blocked")["state"] == "watching"
    assert store.get_signal("buyee_yahoo:live-1")["state"] == "watching"
    with sqlite3.connect(store.db_path) as c:
        gone, revived = c.execute(
            "SELECT disappeared_at, revived_count FROM listing_obs WHERE key = ?",
            ("buyee_yahoo:phantom",),
        ).fetchone()
    assert gone is None and revived == 1
    # 分項結果與 UNVERIFIABLE 摘要都要印出來
    assert "已清 2 筆" in result.output
    assert "無法驗證 1 筆" in result.output
    assert fake.closed is True


def test_cli_clear_departed_rejects_bad_state(cli_env):
    runner, _store, cli_mod = cli_env
    result = runner.invoke(cli_mod.app, ["clear-departed", "--state", "bought"])
    assert result.exit_code != 0
    assert "不可清除" in result.output


def test_cli_clear_departed_closes_verifier_even_when_it_blows_up(
    cli_env, monkeypatch
):
    """驗到一半炸掉：例外大聲往外傳（exit code != 0），但資源要關。"""
    runner, store, cli_mod = cli_env
    _seed_departed_mix(store)

    class _Boom(_FakeEndpointVerifier):
        def __call__(self, key: str, url: str) -> VerifyResult:
            raise RuntimeError("驗證炸了")

    boom = _Boom({})
    monkeypatch.setattr(
        "ygo_sniper.verify_departed.build_page_verifier", lambda _cfg: boom
    )

    result = runner.invoke(cli_mod.app, ["clear-departed", "--state", "watching"])

    assert result.exit_code != 0
    assert boom.closed is True
    assert store.get_signal("buyee_yahoo:gone-1")["state"] == "watching"


# ---------------------------------------------------------------------------
# 掃描流程：還原必須真的被接進去（不是「有一支方法可以呼叫」）
# ---------------------------------------------------------------------------
class _OneListingSource:
    """永遠回同一筆在架標的的假來源（骨架照 tests/test_scan_status.py:130-140）。"""

    name = "src_b"
    site = None          # __init__ 填（避免 import 順序綁在 class body）
    supports_sold = False

    def __init__(self, listings, site):
        self.listings = listings
        self.site = site

    def search(self, keyword, **_kw):
        return list(self.listings)


def test_scan_restores_a_cleared_signal_that_is_back_on_the_shelf(
    monkeypatch, tmp_path, cfg
):
    """端到端：清除 → 標的重新出現 → **跑一輪掃描** → 自動回到觀察中。

    呼叫位置是承重的：還原必須排在 `record_listing_scan` **之後**，
    因為那裡才是清掉 `disappeared_at`／累加 `revived_count` 的地方。
    排在它前面的話還原永遠慢一輪，而症狀是「有時候會回來、有時候不會」。
    """
    import dataclasses

    from conftest import FakeFx, make_listing

    import ygo_sniper.pipeline as pipeline_mod
    from ygo_sniper.domain import Site

    listings = [make_listing(
        price=1500, site=Site.BUYEE_YAHOO, external_id="b1",
        title="遊戯王 青眼の白龍 初期 PSA10 極美品",
    )]
    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,                     # db/cache 全落 tmp
        watchlist={
            **cfg.watchlist,
            "queries": [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["src_b"]}],
            "comps_queries": {},
        },
        sources={},                        # 不跑 canary
    )
    monkeypatch.setattr(
        pipeline_mod, "build_sources",
        lambda _cfg, _f=None: {"src_b": _OneListingSource(listings, Site.BUYEE_YAHOO)},
    )
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    pipe = pipeline_mod.Pipeline(test_cfg)
    key = f"{Site.BUYEE_YAHOO.value}:b1"
    try:
        pipe.scan(skip_comps=True)
        # 承重的斷言，不是裝飾：沒落庫的話後面每一條都會假性通過
        assert pipe.store.get_signal(key), "第一輪掃描沒有把標的寫進 signals"

        pipe.store.update_state(key, "watching")
        _mark_gone_again(pipe.store, key)          # 某一輪沒看到它 → 判離場
        pipe.store.clear_expired_signals(
            "watching", gone_confidence=LOW, verifier=_sold_verifier()
        )
        assert pipe.store.get_signal(key)["state"] == TriageState.EXPIRED.value

        result = pipe.scan(skip_comps=True)        # 它又出現在搜尋結果裡

        row = pipe.store.get_signal(key)
        assert row["state"] == "watching"
        assert row["restored_count"] == 1
        assert row["cleared_from"] is None
        # 還原這件事要**看得見**（CLAUDE.md 第五節），所以也進掃描報告
        assert result["restored"]["restored"] == 1
        assert result["restored"]["keys"] == [key]
    finally:
        pipe.close()
