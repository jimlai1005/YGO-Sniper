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

ROOT = Path(__file__).resolve().parents[1]

NEW_COLUMNS = ("cleared_at", "cleared_from", "restored_count")


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


def test_clear_expired_moves_gone_rows_to_expired(store):
    _insert(store, "gone-1")
    _mark_gone(store, "gone-1")
    _insert(store, "live-1")

    result = store.clear_expired_signals(
        "watching", gone_confidence={"_default": "low"}
    )

    assert result["cleared"] == 1
    assert result["keys"] == ["gone-1"]
    assert result["by_source"] == {"buyee_yahoo": 1}
    assert store.get_signal("gone-1")["state"] == TriageState.EXPIRED.value
    assert store.get_signal("gone-1")["cleared_from"] == "watching"
    assert store.get_signal("gone-1")["cleared_at"] is not None
    assert store.get_signal("live-1")["state"] == "watching"


def test_clear_expired_is_idempotent(store):
    _insert(store, "gone-1")
    _mark_gone(store, "gone-1")
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
    again = store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
    assert again["cleared"] == 0
    assert again["keys"] == []


def test_clear_expired_only_touches_the_named_state(store):
    """清觀察中不能順手把已購買的也清掉。"""
    _insert(store, "bought-1", state="bought")
    _mark_gone(store, "bought-1")
    result = store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
    assert result["cleared"] == 0
    assert store.get_signal("bought-1")["state"] == "bought"


def test_clear_expired_rejects_unknown_state(store):
    with pytest.raises(ValueError, match="不可清除"):
        store.clear_expired_signals("bought", gone_confidence={"_default": "low"})


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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})

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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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
    cleared = store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})

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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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
        store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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

    result = store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
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
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
    _rescan(store, "buyee_yahoo:gone-1")                        # 誤殺，自己回來了
    store.restore_revived_signals()

    stats = store.expiry_stats()
    assert stats["cleared_now"] == 1               # gone-2 還在 expired
    assert stats["restored_total"] == 1
    assert stats["by_cleared_from"] == {"watching": 1}


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


def test_clear_expired_endpoint(client):
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")
    _insert(app_mod.store, "buyee_yahoo:live-1")

    r = c.post("/api/signals/clear-expired", json={"state": "watching"})
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] == 1
    assert body["keys"] == ["buyee_yahoo:gone-1"]
    assert body["by_source"] == {"buyee_yahoo": 1}


def test_clear_expired_endpoint_is_idempotent(client):
    """清完就不在原 state，重按第二次回 cleared: 0（工程原則二）。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")
    c.post("/api/signals/clear-expired", json={"state": "watching"})
    body = c.post("/api/signals/clear-expired", json={"state": "watching"}).json()
    assert body["cleared"] == 0


def test_clear_expired_endpoint_rejects_bad_state(client):
    """不可清除的狀態是語意錯誤，要回 400，不是安靜地回 cleared: 0。"""
    c, _ = client
    r = c.post("/api/signals/clear-expired", json={"state": "bought"})
    assert r.status_code == 400
    assert "可清除" in r.json()["detail"]


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
            "watching", gone_confidence={"_default": "low"}
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
