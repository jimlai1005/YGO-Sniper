# 實證復活徽章與在架天數 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「已復活」徽章從舊誤判帳（`restored_count`）改綁新的實證帳（`verified_restored_count`＝實證下架後又重新上架的次數，這才是議價訊號），並在兩種卡片上顯示「在架 ≥N 天」（自首次觀測的下界）。

**Architecture:** `signals` 表加兩個欄位（`cleared_verified` 戳記＋`verified_restored_count` 帳本），沿用 `_SIGNALS_MIGRATE_COLUMNS` 的雙寫 additive migration（store.py:346-356——新 db 走 `_SCHEMA` 的 CREATE TABLE、舊 db 由 `_migrate_signals` PRAGMA 看過再 ALTER，冪等）。`clear_expired_signals`（store.py:1915）清除時蓋戳記、`restore_revived_signals`（store.py:2012）還原時把戳記轉進帳本、`update_state`（store.py:674）人工接管時歸零戳記。`list_signals`（store.py:607）的 `SELECT s.*` 讓新欄位自動流進 `/api/signals`（web/app.py:173），LEFT JOIN 加帶 `obs_first_seen` 給前端算在架天數。前端只改 `expiryBadges`（index.html:698）與兩個卡片掛載點（`card` index.html:946、`auctionCard` index.html:1653），天數在前端用同一個時鐘算（後端不另算，避免兩個時鐘源）。**不建事件表**（使用者選了輕量版）。

**Tech Stack:** SQLite additive migration（PRAGMA + ALTER，照 store.py:456-466 既有寫法）、FastAPI（端點零改動，靠 `SELECT s.*` 自動帶欄位）、vanilla JS + 標記區塊抽出丟 node 的前端測試（照 tests/test_expiry_banner.py:29-58 的 harness）、pytest（fixture 與 helper 全部沿用 tests/test_expiry_clear.py；conftest.py 的 autouse fixture 已斷真實 Telegram 外呼，新測試不出網）。

**帳本語意（兩本帳並存，不互相取代）：**

| 欄位 | 語意 | 誰寫 | 誰清 |
|---|---|---|---|
| `restored_count` | 舊誤判帳：被清掉（不論實證與否）後又回來的次數 | `restore_revived_signals` +1 | 永不歸零（store.py:686 的既有規則） |
| `cleared_verified` | 戳記：**這一次**清除是實證的（ended 事實或 verifier 判 SOLD/DELISTED） | `clear_expired_signals` 設 1 | 還原時歸 0；`update_state` 人工接管時歸 0 |
| `verified_restored_count` | 新徽章帳：**實證下架後又重新上架**的次數 | 還原時 `+= cleared_verified` | 永不歸零（與 `restored_count` 同規則） |

2026-08-07 起清除已走頁面實證（`verify_departed.py`），所以此後每一筆程式清除都會帶 `cleared_verified=1`；舊時代（推論清除）殘留的 expired 列沒有戳記，回來時只加舊帳不加新帳——17 個舊徽章因此自動消失，**這是預期行為**。

---

### Task 1: migration——`signals` 加 `cleared_verified` 與 `verified_restored_count`（設計決策 A1）

**Files:**
- Modify: `src/ygo_sniper/store.py`（`_SCHEMA` signals 表 25-60 行、`_SIGNALS_MIGRATE_COLUMNS` 349-356 行）
- Test: `tests/test_expiry_clear.py`（新測試加在 `test_migration_is_idempotent`（160-165 行）之後）

- [ ] **Step 1: 寫紅燈測試**

在 `tests/test_expiry_clear.py` 的 `NEW_COLUMNS`（22 行）下方加常數，並在 `test_migration_is_idempotent`（165 行）後加兩個測試：

```python
#: 實證復活徽章（2026-08-08）：戳記＋帳本。
VERIFIED_COLUMNS = ("cleared_verified", "verified_restored_count")
```

```python
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
```

- [ ] **Step 2: 跑測試看紅**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -k verified_columns -q
```

預期輸出：2 failed（`assert set(VERIFIED_COLUMNS) <= _columns(db)` 失敗——欄位不存在）。

- [ ] **Step 3: 最小實作——雙寫 migration**

`src/ygo_sniper/store.py` `_SCHEMA` 的 signals 表（59 行），把：

```sql
    restored_count  INTEGER DEFAULT 0
);
```

改為：

```sql
    restored_count  INTEGER DEFAULT 0,
    -- 實證復活帳（2026-08-08）。cleared_verified 是**戳記**：這一次清除有頁面
    -- 實證（ended 事實、或 verifier 判 SOLD/DELISTED）。verified_restored_count
    -- 是**帳本**：實證下架後又重新上架的次數——這才是議價訊號，「已復活」
    -- 徽章綁它。restored_count 是舊誤判帳（清除功能自己的誤殺率），語意不動。
    cleared_verified        INTEGER NOT NULL DEFAULT 0,
    verified_restored_count INTEGER NOT NULL DEFAULT 0
);
```

`_SIGNALS_MIGRATE_COLUMNS`（349-356 行）加兩個 entry：

```python
_SIGNALS_MIGRATE_COLUMNS: dict[str, str] = {
    "bucket": "TEXT",
    # 清除已離場標的（2026-08-06）。與 _SCHEMA 雙寫：新 db 走 CREATE TABLE、
    # 舊 db 走 ALTER。DEFAULT 直接寫在型別字串裡即可。
    "cleared_at": "TEXT",
    "cleared_from": "TEXT",
    "restored_count": "INTEGER DEFAULT 0",
    # 實證復活帳（2026-08-08，見 _SCHEMA 的欄位註解）。NOT NULL DEFAULT 0：
    # SQLite 的 ADD COLUMN 會用 DEFAULT 回填既有列，舊列直接是 0 不是 NULL。
    "cleared_verified": "INTEGER NOT NULL DEFAULT 0",
    "verified_restored_count": "INTEGER NOT NULL DEFAULT 0",
}
```

`_migrate_signals`（456-466 行）不必改：它逐 entry PRAGMA 檢查再 ALTER，新 entry 自動被涵蓋（464 行對 restored_count 的 NULL 回填是舊欄位沒有 NOT NULL 才需要的，本次兩欄有 NOT NULL DEFAULT 0，不需要對應的 UPDATE）。

- [ ] **Step 4: 跑測試看綠**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -q
```

預期輸出：全綠（既有 migration 測試 143-165 行也必須維持綠——它們驗證舊欄位那組，與本次互不干擾）。

- [ ] **Step 5: commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(store): signals 加實證復活帳兩欄（cleared_verified／verified_restored_count）"
```

---

### Task 2: 清除時蓋實證戳記（設計決策 A2）

**Files:**
- Modify: `src/ygo_sniper/store.py`（`clear_expired_signals` 的 UPDATE，1988-1992 行）
- Test: `tests/test_expiry_clear.py`

- [ ] **Step 1: 寫紅燈測試**

加在 Task 1 的新測試之後：

```python
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
```

- [ ] **Step 2: 跑測試看紅**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -k stamp -q
```

預期輸出：`test_clear_stamps_cleared_verified_on_every_cleared_row` failed（`cleared_verified == 0`，UPDATE 還沒寫它）；`test_uncleared_rows_never_get_the_stamp` passed（DEFAULT 0 本來就成立——它是防回歸的圍欄，不是本步的紅燈）。

- [ ] **Step 3: 最小實作**

`clear_expired_signals` 的 UPDATE（store.py:1988-1992），把：

```python
                c.execute(
                    f"UPDATE signals SET state = ?, cleared_at = ?, cleared_from = ? "
                    f"WHERE key IN ({marks}) AND state = ?",
                    [TriageState.EXPIRED.value, now, state, *chunk, state],
                )
```

改為：

```python
                # cleared_verified = 1：走到這裡的每一筆都有實證——ended 是
                # end_time 已過的事實，gone 是 verifier 開頁拿到的 SOLD/DELISTED
                # （沒實證的根本進不了 doomed，見上方分流）。這個戳記是
                # 「實證下架後又上架」徽章帳的依據，還原時轉進
                # verified_restored_count（restore_revived_signals）。
                c.execute(
                    f"UPDATE signals SET state = ?, cleared_at = ?, cleared_from = ?, "
                    f"cleared_verified = 1 "
                    f"WHERE key IN ({marks}) AND state = ?",
                    [TriageState.EXPIRED.value, now, state, *chunk, state],
                )
```

- [ ] **Step 4: 跑測試看綠**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -q
```

預期輸出：全綠。

- [ ] **Step 5: commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(clear): 清除寫入蓋實證戳記 cleared_verified=1（ended 與驗證通過的 gone 皆蓋）"
```

---

### Task 3: 還原時把戳記轉進實證帳（設計決策 A3）

**Files:**
- Modify: `src/ygo_sniper/store.py`（`restore_revived_signals` 的 UPDATE，2057-2063 行）
- Test: `tests/test_expiry_clear.py`

- [ ] **Step 1: 寫紅燈測試**

```python
def test_verified_clear_then_comeback_bumps_both_counters(store):
    """實證清除 → 重新掃到 → 還原：舊帳（restored_count）與新帳
    （verified_restored_count）都 +1，戳記歸 0（下一輪要重新取證）。"""
    key = "buyee_yahoo:proof-1"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert store.get_signal(key)["cleared_verified"] == 1

    _rescan(store, key)
    assert store.restore_revived_signals()["restored"] == 1

    row = store.get_signal(key)
    assert row["state"] == "watching"
    assert row["restored_count"] == 1
    assert row["verified_restored_count"] == 1
    assert row["cleared_verified"] == 0


def test_legacy_clear_comeback_does_not_count_as_verified(store):
    """舊時代（推論清除）殘留的 expired 列：沒有戳記，回來時只加舊帳。
    這正是「17 個舊徽章自動消失」的機制——舊帳不流進新徽章。"""
    key = "buyee_yahoo:legacy-1"
    _insert(store, key)
    _mark_gone(store, key)
    with sqlite3.connect(store.db_path) as c:
        # 手動模擬舊時代清除：有 cleared_from、沒有實證戳記
        c.execute(
            "UPDATE signals SET state = 'expired', cleared_from = 'watching', "
            "cleared_at = '2026-08-05T00:00:00+00:00', cleared_verified = 0 "
            "WHERE key = ?",
            (key,),
        )

    _rescan(store, key)
    assert store.restore_revived_signals()["restored"] == 1

    row = store.get_signal(key)
    assert row["state"] == "watching"
    assert row["restored_count"] == 1            # 誤殺帳照記，語意不動
    assert row["verified_restored_count"] == 0   # 沒實證就不進徽章帳


def test_verified_restore_counts_accumulate(store):
    """反覆「實證下架→又上架」的標的要累加——徽章的 ×N 就是它。"""
    key = "buyee_yahoo:flappy-verified"
    _insert(store, key)
    _mark_gone(store, key)
    for expected in (1, 2):
        store.clear_expired_signals(
            "watching", gone_confidence=LOW, verifier=_sold_verifier()
        )
        _rescan(store, key)
        store.restore_revived_signals()
        assert store.get_signal(key)["verified_restored_count"] == expected
        _mark_gone_again(store, key)
```

注意：`_mark_gone_again` 是既有 helper（tests/test_expiry_clear.py:585-590），定義在 `test_restore_counts_accumulate` 之後——Python 模組層名稱在測試執行時才解析，新測試放在它前面也能用，但為了可讀性把這三個測試放在 585 行之後。

- [ ] **Step 2: 跑測試看紅**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -k "comeback or accumulate" -q
```

預期輸出：`test_verified_clear_then_comeback_bumps_both_counters` 與 `test_verified_restore_counts_accumulate` failed（`verified_restored_count == 0`）；`test_legacy_clear_comeback_does_not_count_as_verified` passed（防回歸圍欄）；既有 `test_restore_counts_accumulate` passed。

- [ ] **Step 3: 最小實作**

`restore_revived_signals` 的 UPDATE（store.py:2057-2063），把：

```python
                c.execute(
                    f"UPDATE signals SET state = cleared_from, cleared_at = NULL, "
                    f"cleared_from = NULL, "
                    f"restored_count = COALESCE(restored_count, 0) + 1 "
                    f"WHERE key IN ({marks}) AND state = ? AND cleared_from IS NOT NULL",
                    [*chunk, TriageState.EXPIRED.value],
                )
```

改為：

```python
                # restored_count（舊誤判帳）照舊無條件 +1；verified_restored_count
                # 只加 cleared_verified（0 或 1）——只有**實證清除後**又回來的才算
                # 「實證下架後重新上架」。同一句 UPDATE 裡把戳記歸 0 是安全的：
                # SQLite 的 SET 全部以**更新前**的列值求值，順序不影響結果。
                c.execute(
                    f"UPDATE signals SET state = cleared_from, cleared_at = NULL, "
                    f"cleared_from = NULL, "
                    f"restored_count = COALESCE(restored_count, 0) + 1, "
                    f"verified_restored_count = COALESCE(verified_restored_count, 0) "
                    f"  + COALESCE(cleared_verified, 0), "
                    f"cleared_verified = 0 "
                    f"WHERE key IN ({marks}) AND state = ? AND cleared_from IS NOT NULL",
                    [*chunk, TriageState.EXPIRED.value],
                )
```

- [ ] **Step 4: 跑測試看綠**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -q
```

預期輸出：全綠（含既有的 33k 筆分批測試 `test_clear_expired_survives_more_keys_than_sqlite_takes_host_params`——還原走同一條分批路徑，改動不得弄壞它）。

- [ ] **Step 5: commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(clear): 還原把實證戳記轉進 verified_restored_count——舊誤判帳語意不動"
```

---

### Task 4: `update_state` 人工接管時歸零戳記（設計決策 A4）

**Files:**
- Modify: `src/ygo_sniper/store.py`（`update_state` 的兩句 UPDATE，690-700 行）
- Test: `tests/test_expiry_clear.py`

- [ ] **Step 1: 寫紅燈測試**

放在既有 `test_update_state_clears_the_clear_marks`（550-567 行）之後：

```python
def test_update_state_resets_the_verified_stamp(store):
    """人工接管＝那段「程式做了什麼」的歷史失效，戳記跟 cleared_at/cleared_from
    一起歸零。留著的話：使用者拉回 → 標的自己回到搜尋結果 → 還原機制不會跑
    （cleared_from 已清），但下一次程式再清、再還原時會把**上一輪**的實證
    誤記進帳——戳記描述的必須是「最近一次清除」，不是殘影。"""
    key = "buyee_yahoo:handover"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert store.get_signal(key)["cleared_verified"] == 1

    store.update_state(key, "watching")            # note=None 的那一句
    assert store.get_signal(key)["cleared_verified"] == 0

    # note 非 None 走另一句 UPDATE，兩句都要歸零
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    assert store.get_signal(key)["cleared_verified"] == 1
    store.update_state(key, "watching", note="我再看看")
    assert store.get_signal(key)["cleared_verified"] == 0


def test_update_state_does_not_zero_verified_restored_count(store):
    """帳本欄位不歸零：verified_restored_count 與 restored_count 同一條規則
    （store.py update_state docstring：帳本不是狀態）。"""
    key = "buyee_yahoo:keeper"
    _insert(store, key)
    _mark_gone(store, key)
    store.clear_expired_signals(
        "watching", gone_confidence=LOW, verifier=_sold_verifier()
    )
    _rescan(store, key)
    store.restore_revived_signals()
    assert store.get_signal(key)["verified_restored_count"] == 1

    store.update_state(key, "skipped", note="不追了")

    assert store.get_signal(key)["verified_restored_count"] == 1
```

- [ ] **Step 2: 跑測試看紅**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -k "verified_stamp or keeper" -q
```

預期輸出：`test_update_state_resets_the_verified_stamp` failed（第一個 `== 0` 斷言失敗，戳記還是 1）；`test_update_state_does_not_zero_verified_restored_count` passed（圍欄）。

- [ ] **Step 3: 最小實作**

`update_state`（store.py:688-700），兩句 UPDATE 都加 `cleared_verified = 0`，並在 docstring 的 `restored_count` 那行後面補一句：

```python
        `restored_count` **不歸零**：那是這個功能自己的錯誤帳本，不是狀態。
        `verified_restored_count` 同理不歸零；`cleared_verified` 是**戳記**不是
        帳本，跟 cleared_at/cleared_from 一起歸零。
        """
        with self._conn() as c:
            if note is None:
                c.execute(
                    "UPDATE signals SET state = ?, cleared_at = NULL, "
                    "cleared_from = NULL, cleared_verified = 0 WHERE key = ?",
                    (state, key),
                )
            else:
                c.execute(
                    "UPDATE signals SET state = ?, note = ?, cleared_at = NULL, "
                    "cleared_from = NULL, cleared_verified = 0 WHERE key = ?",
                    (state, note, key),
                )
```

- [ ] **Step 4: 跑測試看綠**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -q
```

預期輸出：全綠（含既有紅線 `test_user_expire_after_a_program_clear_is_not_reverted`——人工接管後的行為不受影響）。

- [ ] **Step 5: commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "fix(store): update_state 人工接管一併歸零 cleared_verified（帳本欄位不動）"
```

---

### Task 5: `list_signals` 帶出 `obs_first_seen`＋確認新欄位流進 API（設計決策 B7、A6）

**Files:**
- Modify: `src/ygo_sniper/store.py`（`list_signals` 的 SELECT，624-630 行）
- Test: `tests/test_expiry_clear.py`

`list_signals` 是唯一餵前端卡片的查詢：`/api/signals`（web/app.py:198）與 `store.bundle()`（store.py:726-727，委派給 `list_signals`）都走它；競標視圖沒有自己的端點，前端只打一次 `/api/signals`（index.html:1369）再在客戶端分流成兩種卡片。`restore_revived_signals` 裡那份形狀相似的查詢（store.py:2037-2044）不餵前端，不改。

- [ ] **Step 1: 寫紅燈測試**

放在既有 `test_list_signals_brings_obs_columns`（168-174 行）之後：

```python
def test_list_signals_brings_obs_first_seen(store):
    """在架天數的分子：listing_obs.first_seen（首次觀測，只進不退——
    store.py 對它的規則是「永不改寫」）。"""
    _insert(store, "a")
    _mark_gone(store, "a")     # helper 寫入 first_seen='2026-08-01T00:00:00+00:00'
    row = store.list_signals(state="watching")[0]
    assert row["obs_first_seen"] == "2026-08-01T00:00:00+00:00"


def test_list_signals_without_obs_row_has_null_first_seen(store):
    """沒有觀測列 → obs_first_seen 是 None，前端不顯示天數（不猜）。"""
    _insert(store, "solo")
    assert store.list_signals(state="watching")[0]["obs_first_seen"] is None
```

再放一個 API 層的確認測試（用既有 `client` fixture，779-810 行；放在 `test_signals_api_carries_expiry`（813-825 行）之後）：

```python
def test_signals_api_carries_verified_badge_and_shelf_age_fields(client):
    """A6 的確認：payload 是 `SELECT s.*`＋JOIN 的 obs_ 欄位，新欄位自動流到
    前端，web/app.py 一行都不用改——這條測試釘住「自動」不會被日後的
    欄位白名單重構弄掉。"""
    c, app_mod = client
    _insert(app_mod.store, "buyee_yahoo:gone-1")
    _mark_gone(app_mod.store, "buyee_yahoo:gone-1")

    item = c.get("/api/signals?state=watching").json()["items"][0]

    assert item["verified_restored_count"] == 0
    assert item["cleared_verified"] == 0
    assert item["obs_first_seen"] == "2026-08-01T00:00:00+00:00"
```

- [ ] **Step 2: 跑測試看紅**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -k "first_seen or shelf_age_fields" -q
```

預期輸出：3 failed（`KeyError: 'obs_first_seen'`；API 測試在 `verified_restored_count` 已過（Task 1 起就會自動流出）、在 `obs_first_seen` 斷言失敗）。

- [ ] **Step 3: 最小實作**

`list_signals` 的查詢（store.py:624-630），把：

```python
        q = (
            "SELECT s.*, o.disappeared_at AS obs_disappeared_at, "
            "o.window_exit_at AS obs_window_exit_at, "
            "COALESCE(o.revived_count, 0) AS obs_revived_count "
            "FROM signals s LEFT JOIN listing_obs o ON o.key = s.key "
            "WHERE s.score >= ?"
        )
```

改為：

```python
        q = (
            "SELECT s.*, o.disappeared_at AS obs_disappeared_at, "
            "o.window_exit_at AS obs_window_exit_at, "
            "COALESCE(o.revived_count, 0) AS obs_revived_count, "
            # 在架天數的分子。obs_ 前綴同上：signals 自己也有 first_seen
            # （首次成為候選），與觀測帳的首次觀測是兩件事，不加前綴會被
            # 靜默覆蓋（CLAUDE.md 第三節的混源陷阱）。
            "o.first_seen AS obs_first_seen "
            "FROM signals s LEFT JOIN listing_obs o ON o.key = s.key "
            "WHERE s.score >= ?"
        )
```

- [ ] **Step 4: 跑測試看綠**

```bash
.venv/bin/pytest tests/test_expiry_clear.py -q
```

預期輸出：全綠（含既有 `test_list_signals_join_does_not_clobber_signals_columns`——`s.first_seen` 必須仍是 signals 自己的值）。

- [ ] **Step 5: commit**

```bash
git add src/ygo_sniper/store.py tests/test_expiry_clear.py
git commit -m "feat(store): list_signals 帶出 obs_first_seen（在架天數的分子，obs_ 前綴防混源）"
```

---

### Task 6: 前端——徽章改綁實證帳＋兩種卡片顯示「在架 ≥N 天」（設計決策 A5、B8、B9）

**Files:**
- Modify: `web/static/index.html`（CSS 255 行後、`expiryBadges` 706-708 行、`expiryBadges` 之後新增 shelfAge 標記區塊、`card` 946 行、`auctionCard` 1653 行）
- Test: `tests/test_shelf_age.py`（新檔，node harness 照 tests/test_expiry_banner.py:29-58）、`tests/test_expiry_banner.py`（追加文字圍欄測試）

- [ ] **Step 1: 寫紅燈測試（node 抽塊＋文字圍欄）**

新檔 `tests/test_shelf_age.py`：

```python
"""「在架 ≥N 天」的純函式：N = floor((now − obs_first_seen)/一天)，N ≥ 1 才顯示。

時間**全部在前端用同一個時鐘算**（後端只給 UTC ISO 字串）——後端另算天數
就是兩個時鐘源，時區偏移下會與畫面上其他倒數對不上（CLAUDE.md 第三節）。
「≥」是誠實標示：first_seen 是首次**觀測**（觀測自 2026-08-01 開始），
實際上架時間只會更早，所以它是下界。

作法沿用 tests/test_expiry_banner.py：從 index.html 抽出標記區塊丟進 node
執行，斷言留在 pytest 這一側。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[1] / "web" / "static" / "index.html"
BEGIN = "/* ==== SHELF-AGE-LOGIC:BEGIN"
END = "/* ==== SHELF-AGE-LOGIC:END"

#: node 端的驅動器：讀 stdin 的 {item, now_ms}，印出 shelfAge 的 HTML 字串。
#: **不含任何斷言**——判斷留在 pytest。
HARNESS = """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
console.log(JSON.stringify(shelfAge(input.item, input.now_ms)));
"""

#: 固定的 now：2026-08-08T00:00:00Z（測試不得依賴真實時鐘）。
#: 錨定驗算：python -c "from datetime import datetime,UTC;
#:   print(int(datetime(2026,8,8,tzinfo=UTC).timestamp())*1000)" → 1786147200000
NOW_MS = 1786147200000


def extract_block() -> str:
    text = INDEX.read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    block = text[start:end]
    assert "function shelfAge(" in block, "區塊裡找不到 shelfAge"
    return block


def run_js(item: dict, now_ms: int = NOW_MS) -> str:
    node = shutil.which("node")
    if node is None:  # pragma: no cover - 開發機沒有 node 時
        pytest.skip("找不到 node，無法執行前端邏輯測試")
    proc = subprocess.run(
        [node, "-e", extract_block() + HARNESS],
        input=json.dumps({"item": item, "now_ms": now_ms}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node 執行失敗：{proc.stderr}"
    return json.loads(proc.stdout)


def test_seven_full_days_shows_at_least_seven():
    out = run_js({"obs_first_seen": "2026-08-01T00:00:00+00:00"})
    assert "在架 ≥7 天" in out


def test_partial_day_floors_down():
    """6 天 23 小時 → ≥6 天：floor 是刻意的，「≥」的方向不能因進位變成高估。"""
    out = run_js({"obs_first_seen": "2026-08-01T01:00:00+00:00"})
    assert "在架 ≥6 天" in out


def test_less_than_one_day_shows_nothing():
    assert run_js({"obs_first_seen": "2026-08-07T12:00:00+00:00"}) == ""


def test_missing_first_seen_shows_nothing():
    """沒有觀測列（obs_first_seen 是 null）→ 不顯示，**不猜**。"""
    assert run_js({"obs_first_seen": None}) == ""
    assert run_js({}) == ""


def test_unparsable_first_seen_shows_nothing():
    assert run_js({"obs_first_seen": "not-a-date"}) == ""


def test_title_declares_the_lower_bound_semantics():
    """title 要把「下界」講清楚——這個數字的極限必須標註在它自己身上。"""
    out = run_js({"obs_first_seen": "2026-08-01T00:00:00+00:00"})
    assert "自首次觀測起算的下界" in out
    assert "2026-08-01" in out
    assert "實際上架時間只會更早" in out


def test_style_is_muted_not_a_badge():
    """低調小字（.shelf-age），不是徽章（.xp）——不能搶過既有徽章。"""
    out = run_js({"obs_first_seen": "2026-08-01T00:00:00+00:00"})
    assert 'class="shelf-age"' in out
    assert "xp" not in out
```

`tests/test_expiry_banner.py` 末尾追加文字圍欄（沿用該檔 157-165 行對 card body 做文字斷言的既有作法）：

```python
# ---------------------------------------------------------------------------
# 已復活徽章改綁實證帳＋在架天數掛載（2026-08-08）
# ---------------------------------------------------------------------------
def _function_body(name: str) -> str:
    text = INDEX.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    return text[start : text.index("\nfunction ", start + 10)]


def test_revived_badge_reads_the_verified_counter():
    """「已復活」只認實證帳（verified_restored_count）。restored_count 是舊誤判
    時代的錯誤帳（清除功能自己的誤殺率），拿它當議價訊號是混源。"""
    body = _function_body("expiryBadges")
    assert "verified_restored_count" in body, "徽章沒有改綁實證帳"
    assert "it.restored_count" not in body, "徽章仍在讀舊誤判帳 restored_count"


def test_both_cards_mount_the_shelf_age():
    """兩個卡片掛載點（觀察卡 card、競標卡 auctionCard）都要顯示在架天數
    ——與 expiryBadges 同一條教訓：只掛一邊，另一邊就看不到。"""
    for fn in ("card", "auctionCard"):
        assert "shelfAge(it)" in _function_body(fn), f"{fn} 沒有掛在架天數"
```

- [ ] **Step 2: 跑測試看紅**

```bash
.venv/bin/pytest tests/test_shelf_age.py tests/test_expiry_banner.py -q
```

預期輸出：`test_shelf_age.py` 全部 failed 或 error（`ValueError: substring not found`——標記區塊不存在）；`test_revived_badge_reads_the_verified_counter` 與 `test_both_cards_mount_the_shelf_age` failed；test_expiry_banner.py 其餘既有測試 passed。

- [ ] **Step 3: 最小實作（四處修改）**

**(a) CSS**——`.xp.back`（index.html:255）之後加一行：

```css
  /* 在架天數：低調小字，不是徽章——資訊性標註不能搶過 .xp 系列的行動性徽章 */
  .shelf-age{font-size:10.5px;color:var(--dim);margin-left:6px;white-space:nowrap;font-weight:400}
```

**(b) 徽章重定義**——`expiryBadges` 的 back（index.html:705-708），把：

```js
    // 自動還原過的：讓「誤殺又被救回來」這件事看得見，不是只有統計數字。
    back: it.restored_count > 0
      ? `<span class="xp back" title="這筆被清除過 ${it.restored_count} 次，後來又上架了">已復活</span>`
      : "",
```

改為：

```js
    // 已復活＝**實證下架後又重新上架**（verified_restored_count，2026-08-08
    // 重定義）。舊帳 restored_count 是清除功能的誤殺率，不是議價訊號——
    // 綁它的 17 個舊徽章因此消失，這是預期行為。
    back: it.verified_restored_count > 0
      ? `<span class="xp back" title="實證下架後重新上架 ${it.verified_restored_count} 次">已復活${
          it.verified_restored_count >= 2 ? ` ×${it.verified_restored_count}` : ""}</span>`
      : "",
```

**(c) shelfAge 純函式**——放在 `expiryBadges` 結尾（index.html:710 的 `}` 之後）、`AUCTION-VIEW-LOGIC:BEGIN`（index.html:712）之前，用標記區塊包起來（供 tests/test_shelf_age.py 抽出丟 node）：

```js
/* ==== SHELF-AGE-LOGIC:BEGIN ========================================= *
 * 「在架 ≥N 天」。純函式：now 由參數傳入（不傳退回 Date.now()），因為它被
 * tests/test_shelf_age.py 抽出來丟進 node 直接跑。
 * N = floor((now − obs_first_seen)/86400000)，N ≥ 1 才顯示。
 * 「≥」是誠實標示：obs_first_seen 是首次**觀測**（listing_obs.first_seen，
 * 觀測自 2026-08-01 開始），實際上架時間只會更早——這是下界，不是真實在架時長。
 * 解析不出時間一律回空字串——**不猜**。
 * ------------------------------------------------------------------- */
function shelfAge(it, nowMs){
  const t = new Date((it && it.obs_first_seen) || "").getTime();
  if(isNaN(t)) return "";
  const days = Math.floor(((nowMs != null ? nowMs : Date.now()) - t) / 86400000);
  if(days < 1) return "";
  return `<span class="shelf-age" title="自首次觀測起算的下界（觀測自 2026-08-01 開始），實際上架時間只會更早">在架 ≥${days} 天</span>`;
}
/* ==== SHELF-AGE-LOGIC:END =========================================== */
```

**(d) 兩個掛載點**——

觀察卡 `card(it)` 的標題列（index.html:946），把：

```js
      <h3 class="title"><a href="${escapeHtml(it.url)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>${badge}${bucketChip(it)}${xpBadge}${backBadge}</h3>
```

改為（只在 `${backBadge}` 後追加）：

```js
      <h3 class="title"><a href="${escapeHtml(it.url)}" target="_blank" rel="noopener">${escapeHtml(it.title)}</a>${badge}${bucketChip(it)}${xpBadge}${backBadge}${shelfAge(it)}</h3>
```

競標卡 `auctionCard(it)` 的標題列（index.html:1653），把 `${xpb.xp}${xpb.back}` 改為 `${xpb.xp}${xpb.back}${shelfAge(it)}`（該行其餘內容——`🔨 競標中` badge、`tierChip`、`bucketChip`、結尾標籤——原樣保留）。

- [ ] **Step 4: 跑測試看綠**

```bash
.venv/bin/pytest tests/test_shelf_age.py tests/test_expiry_banner.py tests/test_auction_view.py -q
```

預期輸出：全綠。`test_auction_view.py` 一併跑是因為 shelfAge 區塊插在 `AUCTION-VIEW-LOGIC:BEGIN` 標記前面——插錯位置（切進標記區塊裡）會讓它的抽塊測試炸掉，這裡就會被抓到。

- [ ] **Step 5: 手動驗證（使用者實際會打的指令，CLAUDE.md 第六節）**

```bash
ygo-sniper serve
# 開 http://127.0.0.1:8321：
# 1. 觀察中分頁：卡片標題列出現「在架 ≥N 天」小字（灰、10.5px、不搶徽章）
# 2. 競標視圖：同樣有
# 3. 「已復活」徽章：正式庫目前 verified_restored_count 全為 0（欄位剛出生），
#    預期**一個都不顯示**——舊時代 17 個徽章消失即為成功，不是壞掉
```

- [ ] **Step 6: commit**

```bash
git add web/static/index.html tests/test_shelf_age.py tests/test_expiry_banner.py
git commit -m "feat(web): 已復活徽章改認實證帳（×N）＋兩種卡片顯示在架 ≥N 天下界"
```

---

### Task 7: 全套驗收＋文件數字

**Files:**
- Modify: `CLAUDE.md`（第九節 `make test` 的測試數字，若有變動）
- Test: 全套

- [ ] **Step 1: 全套測試**

```bash
make test
```

預期輸出：`N passed`（基線 1490，本計畫新增 18 條：Task 1 兩條、Task 2 兩條、Task 3 三條、Task 4 兩條、Task 5 三條、Task 6 八條→ 預期 1508 passed；以實跑輸出為準，**不得引用這裡的預估數字當證據**）。零 failed、零 error。

- [ ] **Step 2: 驗收清單逐條核對（每條都要在上面的任務有著落）**

| 設計決策 | 落點 |
|---|---|
| A1 兩欄位＋沿用 migration 慣例 | Task 1（`_SCHEMA` 雙寫＋`_SIGNALS_MIGRATE_COLUMNS`） |
| A2 清除蓋 `cleared_verified=1` | Task 2 |
| A3 還原：舊帳照加、新帳 `+= cleared_verified`、戳記歸 0 | Task 3 |
| A4 `update_state` 歸零戳記、帳本不動 | Task 4 |
| A5 徽章改綁 `verified_restored_count`、×N、title、`.xp.back` 沿用 | Task 6 (b) |
| A6 `SELECT s.*` 自動流出（確認測試） | Task 5 的 API 測試 |
| B7 `obs_first_seen`（唯一餵卡片的查詢已核實） | Task 5 |
| B8 兩個掛載點、≥N、title 下界警語、muted | Task 6 (a)(c)(d) |
| B9 UTC ISO 給前端、前端單一時鐘 | Task 5（後端只給字串）＋ Task 6 (c) |
| C 測試（實證還原／舊時代還原／update_state 歸零／obs_first_seen／全綠／不出網） | Tasks 1-6 各 Step 1；conftest.py autouse fixture 沿用 |

- [ ] **Step 3: 更新測試數字並 commit**

`CLAUDE.md` 第九節 `make test # pytest（目前 1490 passed，2026-08-07 實測）` 改為實跑出的新數字與日期（照 repo 慣例 dec97f2）：

```bash
git add CLAUDE.md
git commit -m "docs: 測試數字更新至實跑值（2026-08-08 實測）"
```

---

## 邊界與明確不做的事

- **不建事件表**：使用者選了輕量版。`verified_restored_count` 只有次數，沒有每次的時間戳——要回溯逐次歷史時再升級，不預先建。
- **`expiry_stats`（store.py:2095）不動**：它量的是清除功能自己的誤殺率（`restored_count` 的總和），與新徽章帳是兩件事。把 verified 系列混進去反而是把「功能錯誤率」與「市場行為訊號」兩把尺焊在一起。
- **`restore_revived_signals` 裡那份 JOIN 查詢（store.py:2037-2044）不加 `obs_first_seen`**：它不餵前端，只餵 `expiry_status` 判定，加了是死欄位。
- **後端不算天數**（設計決策 B9）：天數只在前端由 `shelfAge` 用單一時鐘算。
- **`restored_count` 的一切語意不動**（設計決策 A3）：寫入點、不歸零規則、`expiry-stats` 的呈現全部照舊。
