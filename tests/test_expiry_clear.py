"""清除已離場標的：migration、store 方法、自動還原、API。

骨架照 `tests/test_card_bucket.py`——bucket 欄位當初就是用同一套
「migration＋store＋web API」三段結構加進來的。
"""

import sqlite3
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
