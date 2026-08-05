"""清除已離場標的：migration、store 方法、自動還原、API。

骨架照 `tests/test_card_bucket.py`——bucket 欄位當初就是用同一套
「migration＋store＋web API」三段結構加進來的。
"""

import sqlite3
from pathlib import Path

import pytest

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
