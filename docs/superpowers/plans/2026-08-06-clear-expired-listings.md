# 清除已離場標的 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 dashboard 的「觀察中／已詢問／已出價」三個分頁能看見並一鍵清除已結標或疑似已售出的標的，且誤殺會自動還原。

**Architecture:** 判定邏輯集中在新模組 `expiry.py`（純函式，無 IO），兩種判準（`end_time` 已過＝確定事實、`listing_obs.disappeared_at`＝推論）分開計算並各帶信心度，只在使用者按下按鈕時合併成一個清除動作。清除＝把 `state` 改成 `expired` 並記下 `cleared_from`；掃描時若該筆重新出現，`upsert_signal` 會依 `cleared_from` 自動還原並累加 `restored_count`——這個計數就是本功能自己的誤殺率。

**Tech Stack:** Python 3.12、SQLite（additive migration）、FastAPI + Pydantic、typer CLI、rich Table、vanilla JS 單檔 SPA、pytest。

**設計依據：** `docs/superpowers/specs/2026-08-06-clear-expired-listings-design.md`（含 2026-08-06 的實測數據：watching 81 筆中 47 筆疑似已離場、`disappeared_at` 誤判率 56.5%）。

---

## 背景：實作者必須先知道的四件事

1. **`\b` 對 CJK 無效**——本計畫不新增 regex，但若你想加，先讀專案 `CLAUDE.md` 第二節。
2. **`SELECT *` 是刻意的**（`store.py:577` 的 docstring）：新欄位自動被帶上，不要改成手寫欄位清單。Task 4 加 JOIN 時保留 `s.*`。
3. **`upsert_signal` 不覆寫人工狀態**（`store.py:481-486`）是紅線。Task 6 加的自動還原只作用於本功能自己寫下的 `cleared_from`，不碰使用者手動標的 `expired`。
4. **測試不准碰正式庫**：`tests/test_card_bucket.py:203-206` 那行 `assert app_mod.store.db_path == db` 是承重斷言，照抄不要刪。

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `src/ygo_sniper/expiry.py` | 判定「還在不在架上」的唯一真相來源，純函式 | 建立 |
| `tests/test_expiry.py` | 判定層單元測試 | 建立 |
| `tests/test_expiry_clear.py` | migration + store + API 整合測試 | 建立 |
| `src/ygo_sniper/store.py` | 三個新欄位、JOIN、清除方法、自動還原 | 修改 |
| `src/ygo_sniper/domain.py` | 無需修改（`TriageState.EXPIRED` 已存在，`domain.py:113`） | — |
| `config/settings.yaml` | `scan.gone_confidence` 來源信心度表 | 修改 |
| `web/app.py` | `GET /api/signals` 帶 `expiry`、新增批次清除端點 | 修改 |
| `src/ygo_sniper/cli.py` | `revive-rate`、`expiry-stats` 兩支唯讀指令 | 修改 |
| `web/static/index.html` | 徽章、banner、清除按鈕、確認對話框 | 修改 |

---

### Task 1: 判定層核心（`expiry.py`）

**Files:**
- Create: `src/ygo_sniper/expiry.py`
- Test: `tests/test_expiry.py`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_expiry.py`：

```python
"""判定層：「這筆標的還在不在架上」。

兩種判準的語意不同，測試也分開寫——這不是形式主義：
`end_time` 已過是確定事實，`disappeared_at` 是推論（2026-08-06 實測誤判率
56.5%）。任何把兩者合成同一個布林值的改動都應該讓這裡紅掉。
"""

from datetime import UTC, datetime, timedelta

from ygo_sniper.expiry import ExpiryStatus, expiry_status

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


def _row(**kw) -> dict:
    """一列 signals（已 JOIN listing_obs）。預設是「還在架上的一口價」。"""
    row = {
        "key": "buyee_yahoo:x1",
        "site": "buyee_yahoo",
        "state": "watching",
        "payload": "{}",
        "obs_disappeared_at": None,
        "obs_window_exit_at": None,
        "obs_revived_count": 0,
    }
    row.update(kw)
    return row


def _payload_with_end(end_time: str) -> str:
    import json

    return json.dumps({"listing": {"end_time": end_time}})


def test_ended_auction_is_certain():
    row = _row(payload=_payload_with_end("2026-08-06T11:00:00+00:00"))
    st = expiry_status(row, now=NOW)
    assert st.kind == "ended"
    assert st.confidence == "certain"
    assert st.detail == "已結標"


def test_future_end_time_is_live():
    row = _row(payload=_payload_with_end("2026-08-07T11:00:00+00:00"))
    assert expiry_status(row, now=NOW).kind == "live"


def test_disappeared_is_gone_not_ended():
    """消失是推論，不能冒充結標。"""
    row = _row(obs_disappeared_at=(NOW - timedelta(hours=6)).isoformat())
    st = expiry_status(row, now=NOW)
    assert st.kind == "gone"
    assert "6 小時" in st.detail


def test_ended_beats_gone():
    """兩者同時成立時，確定事實壓過推論。"""
    row = _row(
        payload=_payload_with_end("2026-08-06T11:00:00+00:00"),
        obs_disappeared_at=(NOW - timedelta(hours=6)).isoformat(),
    )
    assert expiry_status(row, now=NOW).kind == "ended"


def test_window_exit_alone_does_not_expire():
    """被擠出觀測窗是右設限，沒有結論——不能當成離場。"""
    row = _row(obs_window_exit_at=(NOW - timedelta(days=3)).isoformat())
    assert expiry_status(row, now=NOW).kind == "live"


def test_offer_sent_gets_different_wording():
    """已出價的標的消失，很可能代表你標下了，不能寫成「疑似已售出」。"""
    row = _row(state="offer_sent", obs_disappeared_at=(NOW - timedelta(hours=2)).isoformat())
    assert "標下" in expiry_status(row, now=NOW).detail


def test_live_status_has_empty_detail():
    st = expiry_status(_row(), now=NOW)
    assert st == ExpiryStatus(kind="live", confidence="certain", detail="", note=None)


def test_naive_timestamp_is_treated_as_utc():
    """庫裡的時間戳都帶 +00:00；naive 當本地時間會產生 8 小時靜默偏移。"""
    row = _row(payload=_payload_with_end("2026-08-06T11:00:00"))
    assert expiry_status(row, now=NOW).kind == "ended"


def test_broken_payload_does_not_crash():
    """payload 壞掉不能讓整張卡片消失——回 live，讓它繼續顯示。"""
    assert expiry_status(_row(payload="not json"), now=NOW).kind == "live"
    assert expiry_status(_row(payload=None), now=NOW).kind == "live"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ygo_sniper.expiry'`

- [ ] **Step 3: 實作 `src/ygo_sniper/expiry.py`**

```python
"""「這筆標的還在不在架上」的唯一判定來源。

兩種判準的語意完全不同，本模組刻意讓它們**不合成同一個布林值**：

- `end_time` 已過 → 確定事實（競標結標了就是結標了）
- `listing_obs.disappeared_at` → 推論（掃描看不到它了，但可能只是分頁抖動）

2026-08-06 實測 `data/sniper.db`：曾被判離場的 262 列裡有 148 列後來又出現，
誤判率 56.5%；分來源更懸殊——buyee_paypay 70.4%、ebay 66.4%，而 buyee_yahoo
只有 23.9%。所以推論那一側一律附信心度，永遠不假裝它跟結標一樣可靠。

`window_exit_at`（只是被擠出觀測窗）**不觸發任何過期判定**——它的語意是右設限，
本來就沒有結論（`store.py:114-119`）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Kind = Literal["ended", "gone", "live"]
Confidence = Literal["certain", "medium", "low"]

#: 沒給設定時的保守預設：一律 low。寧可標示得比實際不確定，不要反過來。
DEFAULT_GONE_CONFIDENCE: dict[str, str] = {"_default": "low"}


@dataclass(frozen=True)
class ExpiryStatus:
    kind: Kind
    confidence: Confidence
    detail: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_iso(value: Any) -> datetime | None:
    """naive 一律當 UTC。

    庫裡所有時間戳都帶 `+00:00`，naive 只可能來自測試或未來的新來源；
    當成本地時間會產生 8 小時的靜默偏移（台灣 UTC+8），而且不會有人發現。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _humanize(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(int(seconds // 60), 1)} 分鐘"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時"
    return f"{int(seconds // 86400)} 天"


def _end_time_of(row: dict[str, Any]) -> Any:
    """從 signals 列裡挖出 `payload.listing.end_time`。

    payload 在 store 層是 JSON 字串、在 web 層已經被 `json.loads` 展開過，
    兩種都要吃——判定層被兩邊呼叫，不能只認一種形狀。
    """
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    listing = payload.get("listing")
    if not isinstance(listing, dict):
        return None
    return listing.get("end_time")


def expiry_status(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
    gone_confidence: dict[str, str] | None = None,
) -> ExpiryStatus:
    """判定一列 signals（已 LEFT JOIN listing_obs）的在架狀態。

    `row` 需要的欄位：`payload`、`state`、`site`、`obs_disappeared_at`。
    缺欄位不會炸——缺就當作沒有那個證據。
    """
    now = now or datetime.now(UTC)
    table = gone_confidence or DEFAULT_GONE_CONFIDENCE

    end_time = _parse_iso(_end_time_of(row))
    if end_time is not None and end_time <= now:
        return ExpiryStatus(kind="ended", confidence="certain", detail="已結標")

    disappeared = _parse_iso(row.get("obs_disappeared_at"))
    if disappeared is not None:
        site = str(row.get("site") or "")
        confidence = table.get(site) or table.get("_default", "low")
        ago = _humanize(max((now - disappeared).total_seconds(), 0))
        if row.get("state") == "offer_sent":
            # 已出價的標的消失，很可能代表**你標下了**，正確歸宿是 bought
            # 而不是 expired。文案不能寫「疑似已售出」誤導使用者按下清除。
            detail = f"已離場 · 確認是否標下？（{ago}前）"
        else:
            detail = f"疑似已售出 · 消失 {ago}"
        note = None
        if confidence == "low":
            note = f"{site} 的離場判定復活率偏高，這筆可能還在架上"
        return ExpiryStatus(kind="gone", confidence=confidence, detail=detail, note=note)

    return ExpiryStatus(kind="live", confidence="certain", detail="", note=None)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry.py -v`
Expected: PASS，9 passed

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/expiry.py tests/test_expiry.py
git commit -m "feat(expiry): 在架判定層——結標是事實、消失是推論，兩者不合成一個值"
```

---

### Task 2: 來源信心度設定接上

**Files:**
- Modify: `config/settings.yaml`（`scan:` 區塊，約 :436 之後）
- Modify: `src/ygo_sniper/expiry.py`（新增讀設定的 helper）
- Test: `tests/test_expiry.py`（增補）

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_expiry.py` 末尾加：

```python
def test_confidence_table_is_consulted():
    row = _row(site="buyee_yahoo", obs_disappeared_at=(NOW - timedelta(hours=2)).isoformat())
    st = expiry_status(row, now=NOW, gone_confidence={"buyee_yahoo": "medium", "_default": "low"})
    assert st.confidence == "medium"
    assert st.note is None          # medium 不加警語


def test_unknown_source_falls_back_to_default():
    row = _row(site="brand_new_site", obs_disappeared_at=(NOW - timedelta(hours=2)).isoformat())
    st = expiry_status(row, now=NOW, gone_confidence={"buyee_yahoo": "medium", "_default": "low"})
    assert st.confidence == "low"
    assert "復活率偏高" in st.note


def test_gone_confidence_from_config_reads_scan_block():
    from ygo_sniper.expiry import gone_confidence_from_config

    class _Cfg:
        scan = {"gone_confidence": {"buyee_yahoo": "medium", "_default": "low"}}

    assert gone_confidence_from_config(_Cfg())["buyee_yahoo"] == "medium"


def test_gone_confidence_defaults_when_config_missing():
    """設定沒寫時不能炸，也不能假裝有信心——退回全部 low。"""
    from ygo_sniper.expiry import gone_confidence_from_config

    class _Cfg:
        scan = {}

    assert gone_confidence_from_config(_Cfg()) == {"_default": "low"}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry.py -v -k confidence`
Expected: FAIL — `ImportError: cannot import name 'gone_confidence_from_config'`

- [ ] **Step 3a: 在 `config/settings.yaml` 的 `scan:` 區塊末尾（`listing_obs_retain_days: 180` 那行之後）加入**

```yaml
  # 「疑似已離場」（listing_obs.disappeared_at）的可信度，依來源分級。
  # ⚠️ 這些是**實測值**，不是猜的，而且會隨爬蟲改進漂移——要調之前先跑
  # `ygo-sniper revive-rate` 重新量，不要憑印象改（CLAUDE.md 第七節）。
  #
  # 量測定義：分母 = 曾被判離場的列（disappeared_at 非空 OR revived_count > 0），
  # 分子 = 其中 revived_count > 0 的列。2026-08-06 實測（data/sniper.db，598 列）：
  #   ebay 66.4% (n=113) / buyee_paypay 70.4% (n=81)
  #   buyee_yahoo 23.9% (n=67) / buyee_mercari 0% (n=1，樣本不足)
  #
  # 分級規則：復活率 < 35% 且 n >= 20 → medium；其餘一律 low。
  # 沒有任何來源夠格拿 certain——那個等級只給「end_time 已過」的確定事實。
  gone_confidence:
    buyee_yahoo: medium
    buyee_paypay: low
    ebay: low
    buyee_mercari: low      # n=1，樣本不足，保守給 low
    _default: low           # 未列舉的新來源一律 low
```

- [ ] **Step 3b: 在 `src/ygo_sniper/expiry.py` 末尾加 helper**

```python
def gone_confidence_from_config(cfg: Any) -> dict[str, str]:
    """從設定取來源信心度表。`cfg.scan` 是原始 dict（`config.py:162`）。

    設定缺漏時退回 `DEFAULT_GONE_CONFIDENCE`——不能因為沒設定就假裝有信心。
    """
    table = (getattr(cfg, "scan", None) or {}).get("gone_confidence") or {}
    if not table:
        return dict(DEFAULT_GONE_CONFIDENCE)
    merged = dict(DEFAULT_GONE_CONFIDENCE)
    merged.update({str(k): str(v) for k, v in table.items()})
    return merged
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry.py -v`
Expected: PASS，13 passed

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/expiry.py tests/test_expiry.py config/settings.yaml
git commit -m "feat(expiry): 來源信心度查表——實測復活率寫進設定，附量測方法"
```

---

### Task 3: Migration 三個新欄位

**Files:**
- Modify: `src/ygo_sniper/store.py:23-55`（`_SCHEMA` 的 signals 表）
- Modify: `src/ygo_sniper/store.py:339-344`（`_SIGNALS_MIGRATE_COLUMNS`）
- Test: `tests/test_expiry_clear.py`（建立）

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_expiry_clear.py`（骨架照抄 `tests/test_card_bucket.py:17-107`）：

```python
"""清除已離場標的：migration、store 方法、自動還原、API。

骨架照 `tests/test_card_bucket.py`——bucket 欄位當初就是用同一套
「migration＋store＋web API」三段結構加進來的。
"""

import importlib
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: FAIL — `assert {'cleared_at', 'cleared_from', 'restored_count'} <= {...}`

- [ ] **Step 3a: `store.py` 的 `_SCHEMA`，在 `notified_at    TEXT` 那行之後、`);` 之前加入**

```
    notified_at     TEXT,
    -- 清除已離場標的（expiry.py）。cleared_from 非空 = 這是**程式**清的，
    -- 重新掃到時要自動還原回去；使用者手動標的 expired 沒有它，不受影響。
    -- restored_count 是這個功能自己的誤殺率：清掉的東西有幾成又回來了。
    cleared_at      TEXT,
    cleared_from    TEXT,
    restored_count  INTEGER DEFAULT 0
);
```

- [ ] **Step 3b: `store.py` 的 `_SIGNALS_MIGRATE_COLUMNS` 改成**

```python
_SIGNALS_MIGRATE_COLUMNS: dict[str, str] = {
    "bucket": "TEXT",
    # 清除已離場標的（2026-08-06）。與 _SCHEMA 雙寫：新 db 走 CREATE TABLE、
    # 舊 db 走 ALTER。DEFAULT 直接寫在型別字串裡即可。
    "cleared_at": "TEXT",
    "cleared_from": "TEXT",
    "restored_count": "INTEGER DEFAULT 0",
}
```

- [ ] **Step 3c: 舊列的 `restored_count` 會是 NULL 不是 0**（`ALTER TABLE ADD COLUMN ... DEFAULT 0` 對既有列補的是 DEFAULT 值，SQLite 3.35+ 會填 0，但為了跨版本安全）。在 `_migrate_signals` 的迴圈之後、`CREATE INDEX` 之前加一行：

```python
        # 舊列補 0：後面 `restored_count + 1` 遇到 NULL 會得到 NULL 而不是 1。
        c.execute("UPDATE signals SET restored_count = 0 WHERE restored_count IS NULL")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，2 passed

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(store): signals 加 cleared_at/cleared_from/restored_count 三欄"
```

---

### Task 4: `list_signals` 帶出離場觀測

**Files:**
- Modify: `src/ygo_sniper/store.py:569-593`（`list_signals`）
- Test: `tests/test_expiry_clear.py`（增補）

- [ ] **Step 1: 寫失敗測試**

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k list_signals`
Expected: FAIL — `KeyError: 'obs_disappeared_at'`

- [ ] **Step 3: 改 `list_signals` 的 SQL**

把 docstring 之後的三行：

```python
        q = "SELECT * FROM signals WHERE score >= ?"
        params: list[Any] = [min_score]
        if state and state != "all":
            q += " AND state = ?"
```

改成：

```python
        # `s.*` 保留「新欄位自動被帶上」（見上方 docstring）；listing_obs 只挑
        # 三個 signals 沒有的欄位並加 obs_ 前綴——兩表有 11 個同名欄位
        # （last_seen / landed_twd / grade …），不加前綴會被靜默覆蓋，
        # 那正是 CLAUDE.md 第三節的混源陷阱。
        q = (
            "SELECT s.*, o.disappeared_at AS obs_disappeared_at, "
            "o.window_exit_at AS obs_window_exit_at, "
            "COALESCE(o.revived_count, 0) AS obs_revived_count "
            "FROM signals s LEFT JOIN listing_obs o ON o.key = s.key "
            "WHERE s.score >= ?"
        )
        params: list[Any] = [min_score]
        if state and state != "all":
            q += " AND s.state = ?"
```

接著把同一個函式裡剩下兩處也加上 `s.` 限定：

```python
        if bucket and bucket != "all":
            q += " AND s.bucket = ?"
            params.append(bucket)
        q += " ORDER BY s.score DESC, s.last_seen DESC LIMIT ?"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，5 passed

- [ ] **Step 5: 跑全量回歸**（這個改動碰到所有讀清單的路徑）

Run: `make test`
Expected: 1400+ passed，沒有新的 FAIL

- [ ] **Step 6: Commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(store): list_signals LEFT JOIN listing_obs，obs_ 前綴避免同名覆蓋"
```

---

### Task 5: `clear_expired_signals` store 方法

**Files:**
- Modify: `src/ygo_sniper/store.py`（加在 `expire_stale_signals` 之後，約 :1696）
- Test: `tests/test_expiry_clear.py`（增補）

- [ ] **Step 1: 寫失敗測試**

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k clear_expired`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'clear_expired_signals'`

- [ ] **Step 3: 實作（加在 `expire_stale_signals` 之後）**

先在 `store.py` 的 import 區塊加：

```python
from .expiry import expiry_status
```

然後加方法：

```python
    #: 允許被「清除已離場」動作碰到的狀態。清單刻意很短——
    #: bought/skipped 是終點站，in_bundle 是進行中的湊單，都不該被批次清掉。
    CLEARABLE_STATES = (
        TriageState.WATCHING.value,
        TriageState.ASKED_SELLER.value,
        TriageState.OFFER_SENT.value,
    )

    # ------------------------------------------------------------------
    def clear_expired_signals(
        self, state: str, *, gone_confidence: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """把某個狀態底下已離場的標的移到 expired，回傳清了哪些。

        與 `expire_stale_signals` 的差別：那支是排程自動跑、只敢動 `state='new'`；
        這支是**使用者按按鈕**觸發的，所以可以動人工狀態——但也因此一定要記下
        `cleared_from`，讓 `upsert_signal` 在標的重新上架時把它放回原位。

        判定沿用 `expiry.expiry_status`（唯一真相來源），不在這裡重寫一套 SQL 條件：
        判準散成兩份，遲早會漂移成兩種答案。

        回傳 `{"cleared": int, "keys": [...], "by_source": {site: n}}`——
        照 `purge_signals` 的慣例回 dict 而不是單一 int，呼叫端要能把細節印給使用者看。
        """
        if state not in self.CLEARABLE_STATES:
            raise ValueError(
                f"不可清除的狀態 {state}；可清除：{list(self.CLEARABLE_STATES)}"
            )
        rows = self.list_signals(state=state, limit=100_000)
        doomed = [
            r for r in rows
            if expiry_status(r, gone_confidence=gone_confidence).kind != "live"
        ]
        if not doomed:
            return {"cleared": 0, "keys": [], "by_source": {}}

        now = _now_iso()
        keys = [r["key"] for r in doomed]
        by_source: dict[str, int] = {}
        for r in doomed:
            site = str(r.get("site") or "unknown")
            by_source[site] = by_source.get(site, 0) + 1

        marks = ",".join("?" * len(keys))
        with self._conn() as c:
            c.execute(
                f"UPDATE signals SET state = ?, cleared_at = ?, cleared_from = ? "
                f"WHERE key IN ({marks})",
                [TriageState.EXPIRED.value, now, state, *keys],
            )
        return {"cleared": len(keys), "keys": keys, "by_source": by_source}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，9 passed

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(store): clear_expired_signals——使用者觸發的清除，記下 cleared_from"
```

---

### Task 6: 自動還原（本功能的防線）

**Files:**
- Modify: `src/ygo_sniper/store.py:449-496`（`upsert_signal`）
- Test: `tests/test_expiry_clear.py`（增補）

- [ ] **Step 1: 寫失敗測試**

```python
def _signal_for(key: str, *, site: str = "buyee_yahoo"):
    """組一個最小可用的 Signal 給 upsert_signal 用。

    `Signal` 的八個欄位全是必填（`domain.py:289-298`），`Listing` 的價格欄位
    叫 `price` 不是 `price_native`（`domain.py:170`）——照抄，不要憑印象改。
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

    listing = Listing(
        site=Site(site), external_id=key, title=f"卡 {key}",
        url=f"https://example.test/{key}", price=1000.0, currency=Currency.JPY,
    )
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
    """清掉的東西又上架 → 自動放回原狀態，並累加誤殺計數。"""
    _insert(store, "gone-1")
    _mark_gone(store, "gone-1")
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
    assert store.get_signal("gone-1")["state"] == TriageState.EXPIRED.value

    store.upsert_signal(_signal_for("gone-1"))

    row = store.get_signal("gone-1")
    assert row["state"] == "watching"
    assert row["cleared_at"] is None
    assert row["cleared_from"] is None
    assert row["restored_count"] == 1


def test_manually_expired_is_never_restored(store):
    """紅線：使用者手動標的 expired 沒有 cleared_from，程式不准動它。"""
    _insert(store, "manual", state=TriageState.EXPIRED.value)
    store.upsert_signal(_signal_for("manual"))
    row = store.get_signal("manual")
    assert row["state"] == TriageState.EXPIRED.value
    assert row["restored_count"] == 0


def test_restore_counts_accumulate(store):
    """反覆進出的標的，誤殺計數要累加——它就是這個功能的錯誤率。"""
    _insert(store, "flappy")
    _mark_gone(store, "flappy")
    for expected in (1, 2):
        store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
        store.upsert_signal(_signal_for("flappy"))
        assert store.get_signal("flappy")["restored_count"] == expected
        _mark_gone_again(store, "flappy")


def _mark_gone_again(store: Store, key: str) -> None:
    with sqlite3.connect(store.db_path) as c:
        c.execute(
            "UPDATE listing_obs SET disappeared_at = ? WHERE key = ?",
            ("2026-08-05T00:00:00+00:00", key),
        )


def test_normal_upsert_still_preserves_manual_state(store):
    """既有紅線不能被這次改動弄壞：一般的重掃不覆寫人工狀態。"""
    _insert(store, "asked", state="asked_seller")
    store.upsert_signal(_signal_for("asked"))
    assert store.get_signal("asked")["state"] == "asked_seller"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k restore`
Expected: FAIL — `assert 'expired' == 'watching'`

- [ ] **Step 3a: 擴充 `existing` 的 SELECT**（`store.py:455-457`）

```python
            existing = c.execute(
                "SELECT key, state, note, first_seen, cleared_from, "
                "COALESCE(restored_count, 0) AS restored_count "
                "FROM signals WHERE key = ?",
                (key,),
            ).fetchone()
```

- [ ] **Step 3b: 改 `if existing:` 分支**（`store.py:481-486`）

```python
            if existing:
                # 保留人工狀態與筆記 —— 這是狀態機的重點，
                # 每天重掃不能把你昨天標的「已詢問」洗掉
                sets = ", ".join(f"{k} = :{k}" for k in row if k != "key")
                # 唯一的例外：**程式自己**清掉的（cleared_from 非空）標的又上架了，
                # 把它放回原狀態。這不是覆寫人工決策，是**恢復**人工決策——
                # 使用者標的是 watching，是我們依 56.5% 誤判率的推論把它移走的。
                # 使用者手動標的 expired 沒有 cleared_from，走不到這裡。
                if (
                    existing["state"] == TriageState.EXPIRED.value
                    and existing["cleared_from"]
                ):
                    row["state"] = existing["cleared_from"]
                    row["cleared_at"] = None
                    row["cleared_from"] = None
                    row["restored_count"] = (existing["restored_count"] or 0) + 1
                    sets = ", ".join(f"{k} = :{k}" for k in row if k != "key")
                c.execute(f"UPDATE signals SET {sets} WHERE key = :key", row)
                return False
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，13 passed

- [ ] **Step 5: 跑全量回歸**（改到了掃描主路徑）

Run: `make test`
Expected: 1400+ passed

- [ ] **Step 6: Commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(store): 誤殺自癒——被清掉的標的重新上架就放回原狀態"
```

---

### Task 7: `GET /api/signals` 帶 `expiry`

**Files:**
- Modify: `web/app.py:167-254`
- Test: `tests/test_expiry_clear.py`（增補 client fixture）

- [ ] **Step 1: 加入 client fixture 與測試**

在 `tests/test_expiry_clear.py` 末尾加（fixture 照抄 `tests/test_card_bucket.py:189-220`）：

```python
@pytest.fixture
def client(tmp_path, monkeypatch):
    import ygo_sniper.config as config_mod

    db = tmp_path / "web.db"
    real_load = config_mod.load_config

    def _tmp_config(*a, **kw):
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
    c, app_mod = client
    _insert(app_mod.store, "gone-1")
    _mark_gone(app_mod.store, "gone-1")
    _insert(app_mod.store, "live-1")

    items = c.get("/api/signals?state=watching").json()["items"]
    by_key = {i["key"]: i for i in items}

    assert by_key["gone-1"]["expiry"]["kind"] == "gone"
    assert "消失" in by_key["gone-1"]["expiry"]["detail"]
    assert by_key["live-1"]["expiry"]["kind"] == "live"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k carries_expiry`
Expected: FAIL — `KeyError: 'expiry'`

- [ ] **Step 3a: `web/app.py` 的 import 區塊加一行**（維持字母序，放在 `from ygo_sniper.domain import (...)` 之後）

```python
from ygo_sniper.expiry import expiry_status, gone_confidence_from_config  # noqa: E402
```

- [ ] **Step 3b: 在 `signals()` 裡，逐列展開 payload 的那個迴圈內加一行**

找到把 `flags` / `payload` 展開的迴圈（`web/app.py:190-244` 之間），在 `r["payload"] = json.loads(...)` 之後加：

```python
        # 在架狀態：判定只有 expiry.py 一份，前端不自己算
        # （前端算的話，CLI 與通知那兩條路徑就會拿到不同答案）。
        r["expiry"] = expiry_status(r, gone_confidence=_GONE_CONFIDENCE).to_dict()
```

並在模組級全域（`web/app.py:66-72` 的 `store = Store(cfg.db_path)` 之後）加：

```python
_GONE_CONFIDENCE = gone_confidence_from_config(cfg)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，14 passed

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_expiry_clear.py
git commit -m "feat(web): /api/signals 每列帶 expiry，判定只有一份"
```

---

### Task 8: `POST /api/signals/clear-expired`

**Files:**
- Modify: `web/app.py`（加在 `set_bucket` 之後，約 :297）
- Test: `tests/test_expiry_clear.py`（增補）

- [ ] **Step 1: 寫失敗測試**

```python
def test_clear_expired_endpoint(client):
    c, app_mod = client
    _insert(app_mod.store, "gone-1")
    _mark_gone(app_mod.store, "gone-1")
    _insert(app_mod.store, "live-1")

    r = c.post("/api/signals/clear-expired", json={"state": "watching"})
    assert r.status_code == 200
    body = r.json()
    assert body["cleared"] == 1
    assert body["keys"] == ["gone-1"]
    assert body["by_source"] == {"buyee_yahoo": 1}


def test_clear_expired_endpoint_is_idempotent(client):
    c, app_mod = client
    _insert(app_mod.store, "gone-1")
    _mark_gone(app_mod.store, "gone-1")
    c.post("/api/signals/clear-expired", json={"state": "watching"})
    body = c.post("/api/signals/clear-expired", json={"state": "watching"}).json()
    assert body["cleared"] == 0


def test_clear_expired_endpoint_rejects_bad_state(client):
    c, _ = client
    r = c.post("/api/signals/clear-expired", json={"state": "bought"})
    assert r.status_code == 400
    assert "可清除" in r.json()["detail"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k clear_expired_endpoint`
Expected: FAIL — 404

- [ ] **Step 3: 實作端點**（加在 `set_bucket` 之後）

```python
class ClearExpiredRequest(BaseModel):
    #: 要清哪個分頁。只接受 Store.CLEARABLE_STATES 裡的三個。
    state: str


@app.post("/api/signals/clear-expired")
def clear_expired(body: ClearExpiredRequest):
    """把某個分頁裡已離場的標的移到 expired。

    冪等：清完就不在原 state，重按第二次回 `cleared: 0`（工程原則二——
    非冪等寫入不可重試，所以這支刻意設計成冪等）。
    """
    try:
        return store.clear_expired_signals(
            body.state, gone_confidence=_GONE_CONFIDENCE
        )
    except ValueError as e:
        # 不可清除的狀態是**語意錯誤**，不是暫時性失敗——回 400 讓前端看見，
        # 不要安靜地回 cleared: 0 假裝成功（CLAUDE.md 第五節）。
        raise HTTPException(400, str(e)) from e
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，17 passed

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_expiry_clear.py
git commit -m "feat(web): POST /api/signals/clear-expired 批次清除端點（冪等）"
```

---

### Task 9: CLI `revive-rate`

**Files:**
- Modify: `src/ygo_sniper/store.py`（加查詢方法）
- Modify: `src/ygo_sniper/cli.py`（加指令）
- Test: `tests/test_expiry_clear.py`（增補）

- [ ] **Step 1: 寫失敗測試**

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k revive_rate`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'revive_rate_by_source'`

- [ ] **Step 3a: `store.py` 加方法**（放在 `clear_expired_signals` 之後）

```python
    # ------------------------------------------------------------------
    def revive_rate_by_source(self) -> dict[str, dict[str, float]]:
        """量「離場判定」自己的錯誤率，依來源分。

        分母 = 曾被判離場的列（`disappeared_at` 非空 **或** `revived_count > 0`
        ——復活時離場標記會被清掉，所以只看 `disappeared_at` 會漏掉復活過的）。
        分子 = 其中 `revived_count > 0` 的列。

        ⚠️ 這個比率是**下界**：目前仍標記離場的列未來還可能復活。
        """
        q = (
            "SELECT site, COUNT(*) AS ever_gone, "
            "SUM(CASE WHEN revived_count > 0 THEN 1 ELSE 0 END) AS revived "
            "FROM listing_obs "
            "WHERE disappeared_at IS NOT NULL OR revived_count > 0 "
            "GROUP BY site"
        )
        out: dict[str, dict[str, float]] = {}
        with self._conn() as c:
            for r in c.execute(q):
                ever, rev = int(r["ever_gone"]), int(r["revived"] or 0)
                out[str(r["site"])] = {
                    "ever_gone": ever,
                    "revived": rev,
                    "pct": round(100.0 * rev / ever, 1) if ever else 0.0,
                }
        return out
```

- [ ] **Step 3b: `cli.py` 加指令**（typer 會把底線轉成連字號 → `revive-rate`）

```python
@app.command()
def revive_rate():
    """量「疑似已離場」判定的錯誤率——調 gone_confidence 之前先跑這個。

    復活率 = 曾被判離場、後來又出現的比例。設定檔 `scan.gone_confidence`
    的分級規則是：復活率 < 35% 且 n >= 20 → medium，其餘一律 low。
    """
    from rich.table import Table

    cfg = load_config()
    stats = Store(cfg.db_path).revive_rate_by_source()
    if not stats:
        console.print("[dim]還沒有任何離場觀測——爬蟲跑幾輪之後再來量。[/dim]")
        return

    current = (cfg.scan or {}).get("gone_confidence", {})
    t = Table(title="離場判定的復活率（越高越不可信）")
    t.add_column("來源")
    t.add_column("曾判離場", justify="right")
    t.add_column("復活過", justify="right")
    t.add_column("復活率", justify="right")
    t.add_column("目前設定", justify="right")
    t.add_column("建議", justify="right")
    for site, s in sorted(stats.items(), key=lambda kv: -kv[1]["ever_gone"]):
        suggest = "medium" if s["pct"] < 35 and s["ever_gone"] >= 20 else "low"
        now = current.get(site, current.get("_default", "low"))
        t.add_row(
            site, str(int(s["ever_gone"])), str(int(s["revived"])),
            f"{s['pct']}%", now,
            f"[yellow]{suggest}[/yellow]" if suggest != now else suggest,
        )
    console.print(t)
    console.print(
        "[dim]這個比率是下界：目前仍標記離場的列未來還可能復活。[/dim]"
    )
```

- [ ] **Step 4: 跑測試 ＋ 實跑指令**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，18 passed

Run: `.venv/bin/ygo-sniper revive-rate`
Expected: 印出一張表，`buyee_paypay` / `ebay` 的復活率在 60-75% 之間，`buyee_yahoo` 約 24%

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/store.py src/ygo_sniper/cli.py tests/test_expiry_clear.py
git commit -m "feat(cli): revive-rate——離場判定的錯誤率可以被重新量測"
```

---

### Task 10: CLI `expiry-stats`

**Files:**
- Modify: `src/ygo_sniper/store.py`（加查詢方法）
- Modify: `src/ygo_sniper/cli.py`（加指令）
- Test: `tests/test_expiry_clear.py`（增補）

- [ ] **Step 1: 寫失敗測試**

```python
def test_expiry_stats_reports_cleared_and_restored(store):
    _insert(store, "gone-1")
    _mark_gone(store, "gone-1")
    _insert(store, "gone-2")
    _mark_gone(store, "gone-2")
    store.clear_expired_signals("watching", gone_confidence={"_default": "low"})
    store.upsert_signal(_signal_for("gone-1"))      # 誤殺，自己回來了

    stats = store.expiry_stats()
    assert stats["cleared_now"] == 1               # gone-2 還在 expired
    assert stats["restored_total"] == 1
    assert stats["by_cleared_from"] == {"watching": 1}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v -k expiry_stats`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'expiry_stats'`

- [ ] **Step 3a: `store.py` 加方法**

```python
    # ------------------------------------------------------------------
    def expiry_stats(self) -> dict[str, Any]:
        """清除功能的自我體檢。

        `restored_total` 是**這個功能自己的誤殺率**——清掉的東西有幾成又回來了。
        沒有這個數字，功能會安靜地錯下去（CLAUDE.md 第一節：誤殺是靜默的）。
        """
        with self._conn() as c:
            cleared_now = c.execute(
                "SELECT COUNT(*) FROM signals WHERE cleared_from IS NOT NULL"
            ).fetchone()[0]
            restored_total = c.execute(
                "SELECT COALESCE(SUM(restored_count), 0) FROM signals"
            ).fetchone()[0]
            by_from = {
                str(r[0]): int(r[1])
                for r in c.execute(
                    "SELECT cleared_from, COUNT(*) FROM signals "
                    "WHERE cleared_from IS NOT NULL GROUP BY cleared_from"
                )
            }
            pending = {
                str(r[0]): int(r[1])
                for r in c.execute(
                    "SELECT state, COUNT(*) FROM signals WHERE state IN "
                    f"({','.join('?' * len(self.CLEARABLE_STATES))}) GROUP BY state",
                    list(self.CLEARABLE_STATES),
                )
            }
        return {
            "cleared_now": int(cleared_now),
            "restored_total": int(restored_total),
            "by_cleared_from": by_from,
            "by_state": pending,
        }
```

- [ ] **Step 3b: `cli.py` 加指令**

```python
@app.command()
def expiry_stats():
    """清除功能的自我體檢：清掉幾筆、其中幾筆又自己回來了。"""
    from rich.table import Table

    cfg = load_config()
    s = Store(cfg.db_path).expiry_stats()

    t = Table(title="清除已離場標的")
    t.add_column("指標")
    t.add_column("值", justify="right")
    t.add_row("目前處於「被清除」狀態", str(s["cleared_now"]))
    t.add_row("累計自動還原（誤殺）", str(s["restored_total"]))
    for state, n in sorted(s["by_cleared_from"].items()):
        t.add_row(f"　來源分頁：{state}", str(n))
    console.print(t)

    if s["restored_total"]:
        total = s["cleared_now"] + s["restored_total"]
        pct = round(100.0 * s["restored_total"] / total, 1) if total else 0.0
        console.print(
            f"[yellow]清掉的東西有 {pct}% 後來又上架了[/yellow]"
            "[dim]——這是本功能自己的誤殺率，偏高就去看 revive-rate 調 gone_confidence。[/dim]"
        )

    t2 = Table(title="各分頁現況")
    t2.add_column("分頁")
    t2.add_column("筆數", justify="right")
    for state, n in sorted(s["by_state"].items()):
        t2.add_row(state, str(n))
    console.print(t2)
```

- [ ] **Step 4: 跑測試 ＋ 實跑指令**

Run: `.venv/bin/pytest tests/test_expiry_clear.py -v`
Expected: PASS，19 passed

Run: `.venv/bin/ygo-sniper expiry-stats`
Expected: 兩張表；`watching` 那列應該是 81（2026-08-06 實測值，會隨掃描變動）

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/store.py src/ygo_sniper/cli.py tests/test_expiry_clear.py
git commit -m "feat(cli): expiry-stats——把這個功能自己的誤殺率量出來"
```

---

### Task 11: 卡片徽章

**Files:**
- Modify: `web/static/index.html`（CSS 約 :247 之後；`card()` 約 :847-855）

⚠️ **陷阱**：`tickCountdowns()`（`index.html:1389-1399`）每秒把 `[data-end]` 元素的**整個 `className`** 覆寫成 `"cd " + cls`。徽章**不可以**掛在帶 `data-end` 的元素上，會被每秒清掉。放在 `<h3 class="title">` 那行。

- [ ] **Step 1: 加 CSS**（在 `.cd.ended`（:247）之後）

```css
    .xp{font-size:10.5px;padding:1px 7px;border-radius:5px;margin-left:6px;
        vertical-align:2px;white-space:nowrap;background:var(--panel-2);color:var(--dim)}
    .xp.ended{border:1px solid #6b2a2a;background:#331616;color:var(--danger)}
    .xp.gone{border:1px solid #6b5322;background:#33280f;color:var(--warn)}
    .xp.low{opacity:.75}
    .xp.back{border:1px solid #3f6b5f;background:#153029;color:var(--good)}
    .card.expired{opacity:.62}
```

- [ ] **Step 2: 在 `card()` 裡加徽章計算**（`const badge = trig ? … : …` 那段之後）

```js
  // 在架狀態徽章。判定來自後端 expiry.py（唯一真相來源），前端只負責畫。
  // 舊回應沒有 expiry 欄位時整段跳過，不讓卡片壞掉。
  const xp = it.expiry || null;
  const gone = xp && xp.kind !== "live";
  const xpBadge = gone
    ? `<span class="xp ${xp.kind}${xp.confidence === "low" ? " low" : ""}"${
        xp.note ? ` title="${escapeHtml(xp.note)}"` : ""}>${escapeHtml(xp.detail)}</span>`
    : "";
  // 自動還原過的：讓「誤殺又被救回來」這件事看得見，不是只有統計數字。
  const backBadge = it.restored_count > 0
    ? `<span class="xp back" title="這筆被清除過 ${it.restored_count} 次，後來又上架了">已復活</span>`
    : "";
```

- [ ] **Step 3: 串進標題行與卡片 class**

把 `<article class="card ${hot?'hot':''} ${trig?'trig':''}">` 改成：

```js
  <article class="card ${hot?'hot':''} ${trig?'trig':''} ${gone?'expired':''}">
```

把 `${badge}${bucketChip(it)}` 改成：

```js
${badge}${bucketChip(it)}${xpBadge}${backBadge}
```

- [ ] **Step 4: 手動驗證**

Run: `.venv/bin/ygo-sniper serve`
開 http://127.0.0.1:8321 → 點「觀察中」分頁
Expected: 約 47 張卡片變暗並帶琥珀色「疑似已售出 · 消失 N 小時」徽章；滑鼠移上去有信心度說明

- [ ] **Step 5: Commit**

```bash
git add web/static/index.html
git commit -m "feat(web): 卡片顯示在架狀態徽章，已復活的標出來"
```

---

### Task 12: Banner、清除按鈕與確認對話框

**Files:**
- Modify: `web/static/index.html`（`#grid`（:528）之前加 banner；`renderSignals`（:1219）；新增 `clearExpired()`）

⚠️ 這個 SPA **沒有任何既有對話框**（`confirm` / `<dialog>` / `.modal` 全部零命中），唯一的回饋機制是 `#toast`。確認框是全新元件，配色沿用 `.dyn-note`（:239-240）與 `#toast`（:120-123）的既有變數。

- [ ] **Step 1: 加 HTML**（在 `<div class="rows" id="grid"></div>`（:528）之前）

```html
      <div id="xp-banner" class="dyn-note" style="display:none;margin-bottom:10px">
        <span id="xp-banner-text"></span>
        <button id="xp-clear-btn" style="margin-left:10px">清除</button>
      </div>
```

在 `<div id="toast"></div>`（:577）之後加確認框：

```html
      <div id="xp-confirm" style="display:none;position:fixed;inset:0;z-index:50;
           background:rgba(0,0,0,.55);align-items:center;justify-content:center">
        <div style="background:var(--panel);border:1px solid var(--line);border-radius:10px;
             padding:18px 20px;max-width:460px;font-size:13px;color:var(--ink)">
          <div id="xp-confirm-body" style="line-height:1.7"></div>
          <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">
            <button id="xp-cancel">取消</button>
            <button id="xp-ok" class="on">清除</button>
          </div>
        </div>
      </div>
```

- [ ] **Step 2: 在 `renderSignals` 裡算出待清筆數**（`document.getElementById("list-count").innerHTML = …` 那段之前）

```js
  // 待清筆數：只算目前分頁看得到的，且只在可清除的三個分頁顯示。
  const CLEARABLE = ["watching", "asked_seller", "offer_sent"];
  const expiredItems = CLEARABLE.includes(currentState)
    ? shown.filter(i => i.expiry && i.expiry.kind !== "live")
    : [];
  const banner = document.getElementById("xp-banner");
  if(expiredItems.length){
    const bySrc = {};
    for(const i of expiredItems){ const s = i.site || "?"; bySrc[s] = (bySrc[s]||0)+1; }
    const parts = Object.entries(bySrc).sort((a,b) => b[1]-a[1])
      .map(([s,n]) => `${s} ${n}`).join("、");
    document.getElementById("xp-banner-text").innerHTML =
      `<b>${expiredItems.length}</b> 筆疑似已離場（${parts}）`;
    banner.dataset.count = expiredItems.length;
    banner.dataset.sources = parts;
    banner.style.display = "";
  } else {
    banner.style.display = "none";
  }
```

- [ ] **Step 3: 加 `clearExpired()` 與確認框接線**（放在 `setState()`（:1132）之後）

```js
function askClearExpired(){
  const b = document.getElementById("xp-banner");
  const n = b.dataset.count, src = b.dataset.sources;
  // 已出價的標的消失，很可能代表**你標下了**——歸宿應該是「已購買」而不是
  // 「已過期」，而且自動還原救不了這種情況（標下的東西不會再出現在搜尋結果）。
  const warn = currentState === "offer_sent"
    ? `<div style="margin-top:10px;color:var(--warn)">
         已出價的標的消失可能代表你標下了，請先確認再清除。</div>`
    : "";
  document.getElementById("xp-confirm-body").innerHTML =
    `將清除 <b>${n}</b> 筆（${src}）
     <div style="margin-top:8px;color:var(--dim)">
       → 移到「已過期」分頁，可隨時查看<br>
       → 若重新上架會自動放回原分頁
     </div>${warn}`;
  document.getElementById("xp-confirm").style.display = "flex";
}

async function clearExpired(){
  document.getElementById("xp-confirm").style.display = "none";
  try{
    const r = await api("/api/signals/clear-expired", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({state: currentState}),
    });
    toast(`已清除 ${r.cleared} 筆 → 已過期分頁`);
    loadAll();
  }catch(e){ toast("清除失敗：" + e.message); }
}
```

在既有的 DOM 事件接線區（頁籤 handler 附近，約 :2381）加：

```js
document.getElementById("xp-clear-btn").onclick = askClearExpired;
document.getElementById("xp-ok").onclick = clearExpired;
document.getElementById("xp-cancel").onclick =
  () => { document.getElementById("xp-confirm").style.display = "none"; };
```

- [ ] **Step 4: 手動驗證**

Run: `.venv/bin/ygo-sniper serve`
1. 開「觀察中」→ 應看到 banner「47 筆疑似已離場（buyee_paypay 32、buyee_yahoo 8、ebay 6）」
2. 按「清除」→ 確認框列出筆數與來源拆解
3. 按「取消」→ 什麼都沒發生，筆數不變
4. 切到「已出價」分頁 → 確認框多一行琥珀色提醒
5. 回「觀察中」按「清除」→ 確認 → toast「已清除 N 筆」，清單刷新，banner 消失
6. 切到「已過期」分頁 → 剛清掉的都在

- [ ] **Step 5: Commit**

```bash
git add web/static/index.html
git commit -m "feat(web): 待清 banner ＋ 確認框，已出價分頁多一道提醒"
```

---

### Task 13: 端到端驗收

**Files:** 無（只跑驗證）

- [ ] **Step 1: 全量測試**

Run: `make test`
Expected: 1432+ passed（1400 基線 + 本計畫新增 32 條：`test_expiry.py` 13 條、`test_expiry_clear.py` 19 條）

- [ ] **Step 2: 對正式庫的副本跑完整流程**

```bash
cp data/sniper.db /tmp/expiry-verify.db
.venv/bin/python - <<'PY'
from ygo_sniper.store import Store
s = Store("/tmp/expiry-verify.db")
conf = {"buyee_yahoo": "medium", "_default": "low"}
before = len(s.list_signals(state="watching", limit=100000))
r = s.clear_expired_signals("watching", gone_confidence=conf)
after = len(s.list_signals(state="watching", limit=100000))
print(f"清除前 {before} 筆 → 清除 {r['cleared']} 筆 → 剩 {after} 筆")
print("來源拆解:", r["by_source"])
assert before - after == r["cleared"], "筆數對不上"
print("expiry_stats:", s.expiry_stats())
PY
```

Expected: 清除前 81 → 清除約 47 → 剩約 34；`by_source` 以 `buyee_paypay` 為大宗

- [ ] **Step 3: 驗證自動還原（同一個副本）**

```bash
.venv/bin/python - <<'PY'
import sqlite3

from ygo_sniper.domain import (
    CardInfo, CompStats, Currency, Listing, RouteQuote, Signal, Site,
)
from ygo_sniper.store import Store

db = "/tmp/expiry-verify.db"
s = Store(db)

with sqlite3.connect(db) as c:
    key, cleared_from, site = c.execute(
        "SELECT key, cleared_from, site FROM signals WHERE cleared_from IS NOT NULL LIMIT 1"
    ).fetchone()
before = s.get_signal(key)
print(f"挑一筆被清的: {key}")
print(f"  upsert 前: state={before['state']} restored_count={before['restored_count']}")
assert before["state"] == "expired", "這筆應該處於被清除狀態"

# 模擬它重新上架：離場標記消失，然後掃描再次看到它
with sqlite3.connect(db) as c:
    c.execute("UPDATE listing_obs SET disappeared_at = NULL WHERE key = ?", (key,))

listing = Listing(
    site=Site(site), external_id=key.split(":", 1)[-1], title="回鍋驗證",
    url=f"https://example.test/{key}", price=1000.0, currency=Currency.JPY,
)
route = RouteQuote(route="direct", label="直寄", landed_twd=250.0, item_twd=220.0,
                   fee_twd=10.0, shipping_twd=20.0, bundle_size=1)
sig = Signal(
    listing=listing, card=CardInfo(), best_route=route, all_routes=[route],
    comps=CompStats(n=0, median_twd=None, p25_twd=None, p40_twd=None,
                    p75_twd=None, window_days=90),
    flags=[], score=50.0, reason="",
)
s.upsert_signal(sig)

after = s.get_signal(key)
print(f"  upsert 後: state={after['state']} restored_count={after['restored_count']}")
assert after["state"] == cleared_from, f"應該回到 {cleared_from}，實際是 {after['state']}"
assert after["cleared_from"] is None and after["cleared_at"] is None, "清除標記應該被清空"
assert after["restored_count"] == (before["restored_count"] or 0) + 1, "誤殺計數沒累加"
print("✓ 自動還原正常")
PY
```

⚠️ `Listing.external_id` 不含 `site:` 前綴（`Listing.key` 是 `f"{site}:{external_id}"`，`domain.py:194`），所以上面要把 db 裡的 key 拆掉前綴才餵得回同一個 key。

Expected: `state` 從 `expired` 回到 `watching`，`restored_count` 從 0 變 1，最後印出 `✓ 自動還原正常`

- [ ] **Step 4: 驗證使用者真的會打的指令**（CLAUDE.md 第六節）

```bash
.venv/bin/ygo-sniper revive-rate
.venv/bin/ygo-sniper expiry-stats
.venv/bin/ygo-sniper serve      # 手動走一遍 Task 12 Step 4 的六個步驟
```

Expected: 三個指令都能跑；`serve` 的六步驗證全過

- [ ] **Step 5: 清理與收尾**

```bash
rm /tmp/expiry-verify.db
git branch -d backup/clear-expired-before-rebase   # rebase 的保險已無用
```

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test(expiry): 端到端驗收——對正式庫副本驗證清除與自動還原"
```

---

## 驗收條件（對照 spec 第 7 節）

1. `make test` 全綠，新增 32 條測試都在（`test_expiry.py` 13、`test_expiry_clear.py` 19）
2. 對 `data/sniper.db` 副本跑完整流程：清除 → 47 筆進 expired → 觸發 upsert → 自動還原且 `restored_count = 1`
3. `ygo-sniper serve` 實跑，三個分頁的徽章與按鈕都可見
4. `ygo-sniper revive-rate` 與 `ygo-sniper expiry-stats` 都能跑出數字
5. 手動標為 `expired` 的列在重新掃到後**不會**被還原（Task 6 的紅線測試）
