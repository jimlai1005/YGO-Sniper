"""SQLite 儲存層。

CLI 與 web 共用同一顆 db —— 這就是為什麼 dashboard 不需要自己的後端邏輯，
它只是同一份資料的另一個視圖。

用 stdlib sqlite3 而不是 ORM：schema 只有三張表，
ORM 帶來的抽象成本大於收益，而且你之後想直接開 db 跑 SQL 分析
（命中率、平均折價、哪個 query 最有產出）會方便很多。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .domain import SaleKind, Signal, TriageState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    key             TEXT PRIMARY KEY,
    site            TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    image_url       TEXT,
    price_native    REAL,
    currency        TEXT,
    landed_twd      REAL,
    route           TEXT,
    grader          TEXT,
    grade           REAL,
    set_code        TEXT,
    comps_n         INTEGER,
    comps_median    REAL,
    discount_pct    REAL,
    score           REAL,
    flags           TEXT,
    reason          TEXT,
    payload         TEXT,
    state           TEXT DEFAULT 'new',
    -- 卡片分類（domain.CardBucket）。與 state **正交**：NULL = 未分類。
    -- 舊 db 由 _migrate_signals 補上；索引也在那裡建（欄位要先存在）。
    bucket          TEXT,
    note            TEXT DEFAULT '',
    first_seen      TEXT,
    last_seen       TEXT,
    notified_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
CREATE INDEX IF NOT EXISTS idx_signals_score ON signals(score DESC);

-- 推播帳（規則推播的去重依據）。**每條規則各記一列**，這是刻意的：
-- 同一個標的先因為「P>70」被推播過，之後進入 24 小時結標窗時仍然要再響一次
-- ——那是**另一件事實**（「快結標了」），不是重複。用單一 `signals.notified_at`
-- 記帳的話，一個布林值同時代表兩種訊息，第二則必然被吞掉。
-- `signals.notified_at` 是舊的全量推播帳，規則推播不再寫它（見 mark_notified）。
CREATE TABLE IF NOT EXISTS notify_log (
    key         TEXT NOT NULL,
    rule        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (key, rule)
);

-- 行情（成交價）。**怎麼分桶是查詢時的決定，不是寫入時的決定**：
-- signature 保留（相容既有資料與 CompsEngine 的舊路徑），但它是不透明字串，
-- 實測 372 個 signature 裝 405 筆、353 個只有 1 筆樣本 —— 改分桶邏輯就等於
-- 歷史資料作廢。所以真正的地基是下面那組**結構化屬性欄位**（rarity/grader/
-- grade/set_code/era_evidence/card_name）＋永久保留的原始 title：
-- 上層想怎麼分組（同稀有度同分數、同卡號跨分數…）都能重新算，不必重抓資料。
CREATE TABLE IF NOT EXISTS comps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signature   TEXT NOT NULL,
    title       TEXT,
    price_twd   REAL,
    price_native REAL,
    currency    TEXT,
    url         TEXT,
    site        TEXT,
    sold_at     TEXT,
    confidence  TEXT,
    rarity      TEXT,
    grader      TEXT,
    grade       REAL,
    set_code    TEXT,
    era_evidence TEXT,
    card_name   TEXT,
    UNIQUE(signature, url)
);
CREATE INDEX IF NOT EXISTS idx_comps_sig ON comps(signature);

-- 每天在架標的快照。用途有二：
-- 1. 消失的標的可推估為成交（低信心 comps）
-- 2. 追蹤賣家降價行為，找出「快撐不住了」的賣家丟 offer
CREATE TABLE IF NOT EXISTS snapshots (
    day         TEXT NOT NULL,
    key         TEXT NOT NULL,
    price_twd   REAL,
    PRIMARY KEY (day, key)
);

-- 在架觀測帳（listing_obs）。**與 signals 是兩件事，不要合併。**
--
-- signals 只存「通過篩選的候選」而且每輪 upsert 覆寫，所以它回答不了
-- 「這個標的在架多久」「什麼時候不見了」——欄位被最後一次掃描蓋掉了。
-- 這張表相反：每輪掃描看到的每個候選都落一列，價格與 last_seen 更新、
-- **first_seen 與 seen_count 只進不退**，離場時間永久保留。
--
-- 離場分成兩種，混在一起就毀了整個「賣得掉率」的量測（工程原則 1）：
--   disappeared_at  觀測窗還蓋得到它、它卻不在了 → **可推論為下架／賣掉**
--   window_exit_at  它只是被更新的貨擠出觀測窗 → **右設限（censored），無結論**
-- 判準見 `Store.record_listing_scan` 的 docstring（新着降冪＋只抓第 1 頁的
-- 抓取形態下，「不見了」的預設解讀是「被擠出去」，不是「賣掉了」）。
--
-- revived_count 是**這條推論規則自己的錯誤率**：被判 disappeared 之後又出現
-- 的次數。它不是統計噪音，是「這個 proxy 有多不可信」的直接證據。
CREATE TABLE IF NOT EXISTS listing_obs (
    key            TEXT PRIMARY KEY,
    source         TEXT,
    site           TEXT,
    title          TEXT,
    url            TEXT,
    price_native   REAL,
    currency       TEXT,
    price_twd      REAL,
    landed_twd     REAL,
    rarity         TEXT,
    grader         TEXT,
    grade          REAL,
    card_name      TEXT,
    era_evidence   TEXT,
    price_kind     TEXT,
    seller_id      TEXT,
    first_seen     TEXT,
    last_seen      TEXT,
    seen_count     INTEGER DEFAULT 1,
    disappeared_at TEXT,
    window_exit_at TEXT,
    revived_count  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listing_obs_site ON listing_obs(site, last_seen);
CREATE INDEX IF NOT EXISTS idx_listing_obs_gone ON listing_obs(disappeared_at);

-- 賣家帳本（Seller Alpha 的地基）。**這張表是 listing_obs＋comps 的聚合快取，
-- 不是事實來源**（與 comps.card_name 同一哲學）：listing_count／sold_count
-- 一律從兩張帳本重算（COUNT），不用「每次 +1」累加——同一個標的每小時被
-- 重複掃到不該讓計數膨脹，而重算天生冪等，回填舊資料後 refresh 一次就對帳。
--
-- seller_key = "{site}:{seller_id}"。**跨站同名不可合併**：ebay 的 "psa" 與
-- 任何日本站碰巧同名的帳號是兩個人。同站才合流（例：yahoo_closed 的フリマ
-- 賣家與 paypay_direct 的 sellerId 同為 p\d+ 空間、同掛 buyee_paypay）。
--
-- feedback_score／feedback_pct 的**語意逐站不同**（ebay: feedbackScore／
-- feedbackPercentage；paypay: numRating／goodRatio），只能同站比較，
-- 不可跨站排序——site 就在 key 裡，結構上先擋一半。
CREATE TABLE IF NOT EXISTS sellers (
    seller_key     TEXT PRIMARY KEY,
    site           TEXT NOT NULL,
    seller_id      TEXT NOT NULL,
    first_seen     TEXT,
    last_seen      TEXT,
    listing_count  INTEGER DEFAULT 0,
    sold_count     INTEGER DEFAULT 0,
    feedback_score INTEGER,
    feedback_pct   REAL,
    note           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sellers_site ON sellers(site, listing_count DESC);

-- 賣家監控名單（Seller Alpha Phase 3）。**存 db 不存 config**：它會被排程
-- （自動入選）與使用者（手動加入）雙向寫入，而 config 是人在編輯的檔案——
-- 兩邊都寫同一個 yaml 遲早互相覆蓋，而且「上次掃描時間」本來就是狀態不是設定。
--
-- source：`auto`（分數過門檻自動入選）／`manual`（使用者自己觀察到的賣家）。
-- ⚠️ `score` 只有 auto 有值。manual 一律 NULL —— 手動加入的賣家**不假裝有分數**，
--    它就是「還沒有證據，但使用者想追蹤」，畫面上必須看得出這個差別。
-- batch：0..N-1 的輪替批次，加入時算一次就固定（見 seller_watch.batch_of）。
--    每輪重算的話，名單一變動整個輪替表就洗牌，「每 240 分鐘掃一次」的保證消失。
-- active：移除走軟刪除（active=0 ＋ removed_at），歷史留著——「這個賣家曾經
--    在名單上、為什麼被移除」是下次要不要再加回來的依據。
CREATE TABLE IF NOT EXISTS seller_watch (
    seller_key      TEXT PRIMARY KEY,
    site            TEXT NOT NULL,
    seller_id       TEXT NOT NULL,
    source          TEXT NOT NULL,
    reason          TEXT DEFAULT '',
    added_at        TEXT,
    active          INTEGER DEFAULT 1,
    batch           INTEGER DEFAULT 0,
    score           REAL,
    last_scanned_at TEXT,
    last_result     TEXT DEFAULT '',
    removed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_seller_watch_batch ON seller_watch(active, batch);

-- 來源健康告警的去重帳本（PLAN Q5）。fingerprint = "{source}:{kind}"。
-- 觀測帳（occurrences / first_seen / last_seen）由 scan 落；
-- 通知帳（notify_count / last_notified_at，冷卻的依據）只在真的送出成功後才落。
CREATE TABLE IF NOT EXISTS alerts (
    fingerprint      TEXT PRIMARY KEY,
    source           TEXT,
    kind             TEXT,
    first_seen       TEXT,
    last_seen        TEXT,
    occurrences      INTEGER DEFAULT 0,
    last_notified_at TEXT,
    notify_count     INTEGER DEFAULT 0,
    detail           TEXT
);

-- 需求驅動回補的節流帳（refill.py）。鍵是卡名（主檔 name_ja）。
-- 「查過但 0 筆」也要落帳——市場上就是沒有的卡，冷卻期內重查只是浪費請求。
-- 但只有「至少一個來源真的完成觀測（ok/empty）」才記帳：被擋／斷線＝
-- 什麼都不知道，記了帳等於把一次 WAF 挑戰當成「市場沒貨」冷卻七天
-- （讀不到 ≠ 沒有；記帳與否由 refill.run_refill 判定，這裡只管存取）。
CREATE TABLE IF NOT EXISTS comps_refill (
    card_name    TEXT PRIMARY KEY,
    last_run_at  TEXT NOT NULL,
    runs         INTEGER NOT NULL DEFAULT 0,
    last_found   INTEGER,
    last_kept    INTEGER
);

-- 跨行程的小狀態（comps 已售出查詢的輪次計數器、掃描進行中狀態）。
-- 為什麼不用 runs 表推算：runs 只在 dry_run=False 時才落列，
-- 拿一張「有時候不寫」的表當節流依據，dry-run 跑幾次就會把節流繞掉。
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    scanned     INTEGER,
    candidates  INTEGER,
    signals     INTEGER,
    notified    INTEGER,
    notes       TEXT
);
"""


#: comps 的結構化屬性欄位。既有 db 的 comps 表是舊 schema（CREATE TABLE IF NOT
#: EXISTS 對它完全無作用），所以開機時逐欄補齊。`ALTER TABLE ADD COLUMN` 是
#: sqlite 的 O(1) metadata 操作，不重寫既有列、不可能遺失資料；重跑也安全
#: （先讀 PRAGMA table_info 再決定加誰）。
_COMPS_ATTR_COLUMNS: dict[str, str] = {
    "rarity": "TEXT",
    "grader": "TEXT",
    "grade": "REAL",
    "set_code": "TEXT",
    "era_evidence": "TEXT",
    "card_name": "TEXT",
    #: **`sold_at` 到底是不是成交時間**（1 = 不是，那是我們入庫的時間）。
    #:
    #: 2026-08-02 診斷：Buyee 系（Mercari／PayPay 鏡像）的已售出搜尋頁**完全
    #: 沒有成交時間**——實測搜尋頁（95 個 SOLD tile）與商品頁都不含任何日期，
    #: tile 的全部內容就是「SOLD｜標題｜價格」。所以那批 comps 的 `sold_at`
    #: 一直是 `datetime.now(UTC)`，也就是我們抓到它的時間。
    #: 後果：`scoring.comps_window_days: 90` 的視窗對那批資料形同虛設
    #: （它們的「時間」是我們抓取的時間，永遠落在視窗內），任何時間序列分析
    #: （漂移、覆蓋率時間切分、賣得掉率）在那批資料上都不可信。
    #:
    #: 這一欄不修資料，它只是**把兩種資料分開**：下游要嘛只用真時間，
    #: 要嘛明知故用並在報表上標明。猜一個成交時間才是真正不可接受的做法。
    "sold_at_is_ingest": "INTEGER",
    #: 賣掉這筆的賣家（Seller Alpha）。來源：yahoo_closed 的 `seller.userId`
    #: （AUCTION 是 28-29 字混淆 ID、フリマ是 `p\d+`，2026-08-03 實測 50/50 有值，
    #: 且混淆 ID 跨日穩定 26/26——見 reports/seller-id-availability.md）。
    #: 折價歷史因此有主人：同一賣家「持續低於行情成交」才看得出來。
    "seller_id": "TEXT",
    #: **這個價格是買家喊上去的還是賣家開的**（`domain.SaleKind`：
    #: `auction`／`fixed`／`unknown`）。2026-08-06 加入。
    #:
    #: 診斷：`sold` 這一桶混著兩種語意——ヤフオク落札價反映**買家搶到多高**
    #: （賣家只設了開始価格），フリマ／Mercari／一口價即決反映**賣家開多少**。
    #: Seller Alpha 問的是後者，拿前者當同儕量到的是熱度不是定價行為，而且
    #: 方向永遠是「這個賣家好便宜」。實測 468 個計分點裡 251 筆是競標結標，
    #: 而 13 筆 Yahoo 一口價標的的同儕有 22/24 筆是競標結標——已經在混池。
    #:
    #: NULL ＝ 還沒回填的舊列，下游一律讀成 `unknown`（**不准當成 `fixed`**）。
    #: 回填見 `comps.backfill_sale_kind` / CLI `backfill-sale-kind`。
    "sale_kind": "TEXT",
    #: **這一列是不是「同時出品」的重複成交**——指向被保留的那一列的 `id`。
    #: 2026-08-06 加入。日本賣家可以把同一件實體商品同時掛在ヤフオク!與
    #: Yahoo!フリマ（PayPay），賣掉一邊另一邊自動下架；後果是同一筆實體成交
    #: 在 comps 出現兩次，同儕中位數被自己汙染（同一個價格算了兩票）。
    #:
    #: NULL ＝ 不是重複（含全部尚未偵測過的舊列——**不是重複的預設值也是
    #: NULL**，跟「還沒判斷」share 同一個值是刻意的：下游的判斷永遠是
    #: `IS NOT NULL` 才跳過，多一個「未知」狀態只會讓人忘記處理它）。
    #: 非 NULL ＝ 這一列**不參與**同儕比較與計分，真正的成交由 `dup_of_id`
    #: 指到的那一列代表。**只標記，不刪除**——刪除不可逆，標記可以撤回，
    #: 而且保留原始列才查得出判錯（本專案第一節）。
    #: 判定見 `comps.find_dual_listing_duplicates` / CLI `dedupe-comps`。
    "dup_of_id": "INTEGER",
}

#: save_comps 寫入的欄位（不含 id）。順序即 INSERT 的欄位順序。
_COMPS_WRITE_COLUMNS: tuple[str, ...] = (
    "signature", "title", "price_twd", "price_native", "currency", "url",
    "site", "sold_at", "confidence", *_COMPS_ATTR_COLUMNS,
)

#: listing_obs 每輪都會被覆寫的「內容」欄位（價格會變、標題會被賣家改）。
#: 帳務欄位（first_seen / seen_count / *_at）**不在這裡**：它們的更新規則
#: 各不相同，混進同一組批次覆寫就會把「只進不退」的欄位一起洗掉。
_LISTING_OBS_CONTENT_COLUMNS: tuple[str, ...] = (
    "source", "site", "title", "url", "price_native", "currency", "price_twd",
    "landed_twd", "rarity", "grader", "grade", "card_name", "era_evidence", "price_kind",
    "seller_id",
)

#: **重掃時不得被 NULL 蓋掉的欄位**（2026-08-04 事故）。
#:
#: `_upsert_listing_obs` 的更新路徑本來把全部內容欄位無條件覆寫，而
#: **搜尋頁不一定帶得出賣家**：Mercari 的搜尋結果就完全沒有 seller_id。
#: 於是「從商品頁挖回來的 25 個 Mercari 賣家」被下一輪例行掃描抹成 NULL，
#: 一小時後只剩 3 個——沒有錯誤訊息，外顯與「這條管道本來就沒有賣家」一模一樣。
#:
#: 判準：**「這次不知道」≠「這筆沒有賣家」**。來源給了新值就用新值（一筆標的
#: 的賣家不會變，給了就是更正確的值），沒給就保留既有值（COALESCE）。
#: 價格與標題不在這個名單裡是刻意的——那些欄位本來就該跟著最新一次觀測走。
_LISTING_OBS_STICKY_COLUMNS: frozenset[str] = frozenset({"seller_id"})

#: 舊 db 的 listing_obs 沒有 seller_id（與 _COMPS_ATTR_COLUMNS 同一套 additive
#: migration：PRAGMA 看過再 ADD COLUMN，O(1)、不重寫既有列、重跑安全）。
_LISTING_OBS_MIGRATE_COLUMNS: dict[str, str] = {
    "seller_id": "TEXT",
}

#: 舊 db 的 signals 沒有 bucket（卡片分類，見 `domain.CardBucket`）。同一套
#: additive migration：PRAGMA 看過再 ADD COLUMN，O(1)、不重寫既有列、重跑安全。
#: 正式庫每 30 分鐘被排程開啟一次，所以「冪等」不是加分項而是必要條件。
_SIGNALS_MIGRATE_COLUMNS: dict[str, str] = {
    "bucket": "TEXT",
}


#: 既有資料的 `sold_at_is_ingest` 回填規則。**確定性的，不是猜的。**
#:
#: 入庫時間是 `datetime.now(UTC).isoformat()` 寫進去的 → 一律帶微秒
#: （`2026-08-01T14:54:21.213426+00:00`）；真實成交時間一律出自各站的
#: `endTime` 經 `to_utc_iso()` 正規化 → 秒精度、沒有小數點
#: （`2026-03-15T13:40:41+00:00`）。所以「有沒有小數點」就是「是不是入庫時間」。
#:
#: 2026-08-02 在正式庫（1593 列）上驗證，三個站台各自 100% 一致：
#:   buyee_mercari 550/550 有小數點（Buyee sold_out 搜尋，頁面沒有成交時間）
#:   buyee_yahoo   718/718 無小數點（yahoo_closed 的 endTime）
#:   buyee_paypay  77 有／248 無（前者是 Buyee 鏡像，後者是 closedsearch 的
#:                 フリマ 標的——同一個站台混著兩種來路，正是「只看 site 猜
#:                 不出來」的原因）
#: 佐證：有小數點的那批只有 20 個相異值、單一值最多 82 列——那是 ingest 批次
#: （`fallback_sold_at` 一次 ingest 只算一次），不可能是 82 筆同微秒的成交。
#:
#: 冪等：只寫 `IS NULL` 的列。已經有明確值的（新寫入端在 ingest 當下就填了）
#: 一律不碰——回填是補歷史，不是重新判斷。
_BACKFILL_SOLD_AT_PROVENANCE_SQL = """
UPDATE comps
   SET sold_at_is_ingest = CASE WHEN instr(COALESCE(sold_at, ''), '.') > 0 THEN 1 ELSE 0 END
 WHERE sold_at_is_ingest IS NULL
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _age_seconds(iso: str | None) -> float | None:
    """ISO 時間字串距今幾秒。解析不出來回 None（呼叫端必須自己決定怎麼降級）。

    無時區的字串一律當 UTC：本專案所有寫入端（`_now_iso`）都帶時區，
    只有手工塞進 db 的值可能沒有——把它當本地時間會在時區偏移下算出負的年齡。
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            self._migrate_comps(c)
            self._migrate_listing_obs(c)
            self._migrate_signals(c)

    @staticmethod
    def _migrate_comps(c: sqlite3.Connection) -> None:
        """把舊 db 的 comps 表補上結構化屬性欄位。既有資料原封不動。"""
        have = {r["name"] for r in c.execute("PRAGMA table_info(comps)")}
        for col, col_type in _COMPS_ATTR_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE comps ADD COLUMN {col} {col_type}")
        c.execute(_BACKFILL_SOLD_AT_PROVENANCE_SQL)
        # 索引必須等欄位存在才能建，所以不放在 _SCHEMA 裡（舊 db 會炸）
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_comps_attrs ON comps(grader, grade, rarity)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_comps_seller ON comps(seller_id)")

    @staticmethod
    def _migrate_listing_obs(c: sqlite3.Connection) -> None:
        """把舊 db 的 listing_obs 補上 seller_id。既有資料原封不動；重跑安全。"""
        have = {r["name"] for r in c.execute("PRAGMA table_info(listing_obs)")}
        for col, col_type in _LISTING_OBS_MIGRATE_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE listing_obs ADD COLUMN {col} {col_type}")
        # 同 idx_comps_attrs：索引要等欄位存在，不能進 _SCHEMA
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_listing_obs_seller ON listing_obs(seller_id)"
        )

    @staticmethod
    def _migrate_signals(c: sqlite3.Connection) -> None:
        """把舊 db 的 signals 補上 bucket。既有列原封不動（新欄位一律 NULL）；重跑安全。"""
        have = {r["name"] for r in c.execute("PRAGMA table_info(signals)")}
        for col, col_type in _SIGNALS_MIGRATE_COLUMNS.items():
            if col not in have:
                c.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_type}")
        # 同 idx_comps_attrs：索引要等欄位存在，不能進 _SCHEMA
        c.execute("CREATE INDEX IF NOT EXISTS idx_signals_bucket ON signals(bucket)")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def upsert_signal(self, sig: Signal) -> bool:
        """回傳 True 代表這是新標的（值得推播）。"""
        key = sig.listing.key
        now = _now_iso()
        with self._conn() as c:
            existing = c.execute(
                "SELECT key, state, note, first_seen FROM signals WHERE key = ?", (key,)
            ).fetchone()

            row = {
                "key": key,
                "site": sig.listing.site.value,
                "external_id": sig.listing.external_id,
                "title": sig.listing.title,
                "url": sig.listing.url,
                "image_url": sig.listing.image_url,
                "price_native": sig.listing.price,
                "currency": sig.listing.currency.value,
                "landed_twd": sig.best_route.landed_twd,
                "route": sig.best_route.route,
                "grader": sig.card.grader.value,
                "grade": sig.card.grade,
                "set_code": sig.card.set_code,
                "comps_n": sig.comps.n,
                "comps_median": sig.comps.median_twd,
                "discount_pct": sig.discount_pct,
                "score": sig.score,
                "flags": json.dumps([f.value for f in sig.flags]),
                "reason": sig.reason,
                "payload": json.dumps(sig.to_dict(), default=str, ensure_ascii=False),
                "last_seen": now,
            }

            if existing:
                # 保留人工狀態與筆記 —— 這是狀態機的重點，
                # 每天重掃不能把你昨天標的「已詢問」洗掉
                sets = ", ".join(f"{k} = :{k}" for k in row if k != "key")
                c.execute(f"UPDATE signals SET {sets} WHERE key = :key", row)
                return False

            row["first_seen"] = now
            row["state"] = TriageState.NEW.value
            row["note"] = ""
            cols = ", ".join(row)
            vals = ", ".join(f":{k}" for k in row)
            c.execute(f"INSERT INTO signals ({cols}) VALUES ({vals})", row)
            return True

    def mark_notified(self, keys: list[str]) -> None:
        """舊的全量推播帳（`signals.notified_at`）。

        規則推播（notify_rules）**不走這裡**，它用 `notify_log` 逐規則記帳——
        一個標的可以先因為 P>70 被推播、之後因為進入結標窗再被推播一次，
        單一時間戳表達不了兩件事。這支留著是為了不動既有欄位與歷史資料。
        """
        if not keys:
            return
        with self._conn() as c:
            c.executemany(
                "UPDATE signals SET notified_at = ? WHERE key = ?",
                [(_now_iso(), k) for k in keys],
            )

    def pending_notification(self, limit: int) -> list[dict[str, Any]]:
        """舊的全量推播候選（同 `mark_notified`，規則推播改用 `notification_candidates`）。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT * FROM signals
                   WHERE notified_at IS NULL AND state = 'new'
                   ORDER BY score DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- 規則推播的候選與帳本 ------------------------------------------------
    #: 哪些人工狀態還「值得被打擾」。`skipped`／`bought`／`expired` 是使用者
    #: 已經做過的決定，推播不該再去翻案；`asked_seller`／`offer_sent`／`in_bundle`
    #: 是定價標的的議價流程，競標急件與高信心標的都不對著它們說話。
    NOTIFY_CANDIDATE_STATES = (TriageState.NEW.value, TriageState.WATCHING.value)

    def notification_candidates(self, limit: int = 500) -> list[dict[str, Any]]:
        """規則推播的候選池：使用者還沒下決定的那些列（不看 notified_at）。

        **刻意不是 `pending_notification`**：競標急件是「這一筆現在進入 24 小時
        結標窗」，那件事發生在標的被發現的好幾天之後——用「從未通知過」當
        候選條件的話，這種訊息永遠送不出去。
        """
        marks = ", ".join("?" for _ in self.NOTIFY_CANDIDATE_STATES)
        with self._conn() as c:
            rows = c.execute(
                f"""SELECT * FROM signals WHERE state IN ({marks})
                    ORDER BY score DESC LIMIT ?""",
                (*self.NOTIFY_CANDIDATE_STATES, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def notify_log_map(self) -> dict[tuple[str, str], str]:
        """`{(key, rule): sent_at}`。一次讀完，去重判斷不要逐筆打 db。"""
        with self._conn() as c:
            rows = c.execute("SELECT key, rule, sent_at FROM notify_log").fetchall()
        return {(r["key"], r["rule"]): r["sent_at"] for r in rows}

    def mark_rule_notified(
        self, pairs: list[tuple[str, str]], now: str | None = None
    ) -> None:
        """**只對真的送出成功的那幾則**落帳（與 alerts.mark_sent 同一個立場）。

        送失敗卻記成已通知，那則訊息就永遠消失了（工程原則 3）。
        """
        if not pairs:
            return
        ts = now or _now_iso()
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO notify_log (key, rule, sent_at) VALUES (?, ?, ?)",
                [(k, r, ts) for k, r in pairs],
            )

    # ------------------------------------------------------------------
    def list_signals(
        self,
        state: str | None = None,
        limit: int = 200,
        min_score: float = 0,
        bucket: str | None = None,
    ) -> list[dict[str, Any]]:
        """`SELECT *` 是刻意的：新欄位（bucket）自動被帶上，不必再改一份欄位清單
        （CLAUDE.md 第五節：手寫欄位清單一定會跟定義漂移）。

        `state` 與 `bucket` 是**兩個獨立的維度**，可以各自給也可以併用：
        分類分頁要的是「不分狀態的高價卡」→ `state="all", bucket="high_value"`。
        """
        q = "SELECT * FROM signals WHERE score >= ?"
        params: list[Any] = [min_score]
        if state and state != "all":
            q += " AND state = ?"
            params.append(state)
        if bucket and bucket != "all":
            q += " AND bucket = ?"
            params.append(bucket)
        q += " ORDER BY score DESC, last_seen DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def signals_missing_grade(self, limit: int = 500) -> list[dict[str, Any]]:
        """鑑定分數不明的訊號（`grade IS NULL`）。

        這一批在出價那一側是**完全不可行動**的：`bidding.EvidenceGate` 的
        `require_known_grade` 對 grade=None 一律拒絕給上限。所以這支查詢的
        用途是「量這個缺口有多大」與「去商品描述補抓」（cli.resolve_grades）。
        排序用 last_seen：還在架上的比早就消失的值得先補。
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM signals WHERE grade IS NULL "
                "ORDER BY last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def grade_coverage(self) -> list[dict[str, Any]]:
        """逐來源的「分數已知／未知」筆數。規模統計，不是抽樣。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT site, COUNT(*) AS n, "
                "SUM(CASE WHEN grade IS NULL THEN 1 ELSE 0 END) AS unknown "
                "FROM signals GROUP BY site ORDER BY unknown DESC, site"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_signal(self, key: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM signals WHERE key = ?", (key,)).fetchone()
        return dict(r) if r else None

    def update_state(self, key: str, state: str, note: str | None = None) -> None:
        with self._conn() as c:
            if note is None:
                c.execute("UPDATE signals SET state = ? WHERE key = ?", (state, key))
            else:
                c.execute(
                    "UPDATE signals SET state = ?, note = ? WHERE key = ?",
                    (state, note, key),
                )

    def update_bucket(self, key: str, bucket: str | None) -> None:
        """指派／清除卡片分類（`bucket=None` 就是清除）。

        **state 與 bucket 互不覆寫**，只有一個刻意的例外：指派分類時若目前
        還是 `new`，一併升成 `watching`——使用者原話是高價卡「也是在觀察中」，
        分類這個動作本身就代表他開始盯它了。其餘狀態一律不動：
        把 `bought`／`skipped` 打回 `watching` 會憑空捏造出一個沒發生過的決策。
        清除分類**不動 state**（升上去的 watching 是使用者真的看過的事實，
        不因為他改了分類就退回去）。

        用一句 SQL 的 CASE 而不是「先讀再寫」：正式庫每 30 分鐘有排程在寫，
        read-modify-write 中間那一格是真的會被別人插進來的。
        """
        with self._conn() as c:
            if bucket is None:
                c.execute("UPDATE signals SET bucket = NULL WHERE key = ?", (key,))
                return
            c.execute(
                "UPDATE signals SET bucket = ?, "
                "state = CASE WHEN state = ? THEN ? ELSE state END "
                "WHERE key = ?",
                (bucket, TriageState.NEW.value, TriageState.WATCHING.value, key),
            )

    def bundle(self) -> list[dict[str, Any]]:
        return self.list_signals(state=TriageState.IN_BUNDLE.value, limit=100)

    def all_signal_images(self) -> list[dict[str, Any]]:
        """每一筆訊號的 (key, site, url, image_url)。

        刻意**不在 SQL 裡判斷「是不是佔位圖」**：判準只有一份，在
        `sources.buyee.BuyeeSource.normalize_image_url`。SQL 這邊再寫一次
        `LIKE '%loading-spinner%'` 就是第二份定義，parser 加了新的佔位圖
        pattern 之後回填會安靜地漏掉它們（工程原則 1：同源同基準）。
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, site, url, image_url FROM signals ORDER BY key"
            ).fetchall()
        return [dict(r) for r in rows]

    def set_signal_images(self, updates: list[tuple[str, str | None]]) -> int:
        """改寫 signals.image_url（同時同步 payload 內那一份），回傳改動列數。

        payload 裡另存了一份 listing dict（/api/bundle 會拿它重建 Listing），
        只改欄位不改 payload 的話，同一筆標的在清單與湊單籃會顯示不同的圖——
        兩份資料同源才不會分岔。
        """
        if not updates:
            return 0
        changed = 0
        with self._conn() as c:
            for key, image_url in updates:
                row = c.execute(
                    "SELECT payload FROM signals WHERE key = ?", (key,)
                ).fetchone()
                if row is None:
                    continue
                payload = row["payload"]
                try:
                    doc = json.loads(payload or "{}")
                except ValueError:
                    doc = {}
                if isinstance(doc.get("listing"), dict):
                    doc["listing"]["image_url"] = image_url
                    payload = json.dumps(doc, default=str, ensure_ascii=False)
                c.execute(
                    "UPDATE signals SET image_url = ?, payload = ? WHERE key = ?",
                    (image_url, payload, key),
                )
                changed += 1
        return changed

    # ------------------------------------------------------------------
    def save_comps(self, index: dict[str, list[dict]]) -> None:
        """寫入行情。欄位清單由 _COMPS_WRITE_COLUMNS 單一定義，
        呼叫端的 row dict 用同一組鍵——欄位清單只有一處，加欄位不會兩邊漂掉。

        帶 seller_id 的列同一次交易更新賣家帳本（成交紀錄也是賣家活動）。
        """
        rows = [
            tuple(sig if col == "signature" else r.get(col) for col in _COMPS_WRITE_COLUMNS)
            for sig, lst in index.items() for r in lst
        ]
        if not rows:
            return
        sellers = {
            (str(r.get("site") or ""), str(r["seller_id"]))
            for lst in index.values() for r in lst if r.get("seller_id")
        }
        cols = ", ".join(_COMPS_WRITE_COLUMNS)
        marks = ", ".join("?" * len(_COMPS_WRITE_COLUMNS))
        stamp = _now_iso()
        with self._conn() as c:
            c.executemany(f"INSERT OR IGNORE INTO comps ({cols}) VALUES ({marks})", rows)
            # seller_id 只補不改：同一筆成交（UNIQUE(signature, url)）在 seller
            # 欄位上線前就入庫的話，INSERT OR IGNORE 會整列跳過、賣家永遠是
            # NULL——而 yahoo_closed 的視窗有 180 天，重掃必然一直碰到這批列。
            # 這裡把「這次帶著賣家的同一筆」的 seller_id 補進 NULL 的舊列；
            # 已有值的一律不碰（回填是補歷史，不是重新判斷）。冪等。
            c.executemany(
                "UPDATE comps SET seller_id = ? "
                "WHERE signature = ? AND url = ? AND seller_id IS NULL",
                [
                    (str(r["seller_id"]), sig, r.get("url"))
                    for sig, lst in index.items() for r in lst if r.get("seller_id")
                ],
            )
            # sale_kind 同一個道理，只是它的「還沒有值」多一種寫法：`unknown`
            # ＝「當時查不到證據」。重掃碰到同一筆時如果這次**有**證據，就升級；
            # 已經是 auction／fixed 的一律不碰。不補的話那批列會永遠卡在
            # unknown（＝永遠不進同儕比較），而且完全看不出來。
            c.executemany(
                "UPDATE comps SET sale_kind = ? WHERE signature = ? AND url = ?"
                " AND (sale_kind IS NULL OR sale_kind = '' OR sale_kind = 'unknown')",
                [
                    (str(r["sale_kind"]), sig, r.get("url"))
                    for sig, lst in index.items() for r in lst
                    if r.get("sale_kind") and r["sale_kind"] != SaleKind.UNKNOWN.value
                ],
            )
            for site, sid in sellers:
                self._upsert_seller(c, site, sid, stamp=stamp)

    def load_comps(
        self,
        since: datetime,
        *,
        era_verified_only: bool = True,
        real_sold_at_only: bool = False,
    ) -> dict[str, list[dict]]:
        """讀出視窗內的成交樣本。

        `era_verified_only`：只回傳有年代證據的列。入庫過濾是後來才加的，
        資料庫裡還躺著一批早期未過濾就寫進來的垃圾（寶可夢、卡套、現代卡）。
        在讀取端擋掉比 DELETE 好：刪除不可逆，而判準本身還在演進——
        今天判定為垃圾的列，明天補了年代標記可能就變成有效樣本
        （實例：補上「旧レリーフ」之後就多回收了 11 筆）。

        ⚠️ **`since` 這道視窗對 `sold_at_is_ingest = 1` 的列形同虛設**：
        它們的「時間」是我們入庫的時間，永遠落在最近的視窗內（Buyee 系的
        已售出頁根本沒有成交時間，見 `_COMPS_ATTR_COLUMNS` 的註記）。
        預設仍然把它們撈出來——行情的價格資訊是真的，扔掉會讓樣本數腰斬；
        但**每一列都帶著 `sold_at_is_ingest` 旗標**，下游要做時間序列分析
        時必須自己分開處理。`real_sold_at_only=True` 給的是嚴格那條路。
        """
        sql = "SELECT * FROM comps WHERE sold_at >= ?"
        if era_verified_only:
            sql += " AND era_evidence IS NOT NULL AND era_evidence != ''"
        if real_sold_at_only:
            sql += " AND COALESCE(sold_at_is_ingest, 0) = 0"
        with self._conn() as c:
            rows = c.execute(sql, (since.isoformat(),)).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["signature"], []).append(dict(r))
        return out

    def comps_by(
        self,
        *,
        rarity: str | None = None,
        grader: str | None = None,
        grade: float | None = None,
        set_code: str | None = None,
        site: str | None = None,
        signature: str | None = None,
        since_days: int | None = None,
        real_sold_at_only: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """用結構化屬性組合撈樣本，回傳**原始列**。

        故意不在這一層算中位數／百分位：分桶與統計是上層（建模）的決定，
        store 只負責「照條件把原始資料交出來」。任一參數為 None 就不加該條件。

        `real_sold_at_only=True` 排除 `sold_at` 是入庫時間的列——任何用到
        `since_days`、或拿 `sold_at` 排序／切分的分析都該考慮開它
        （見 `load_comps` 的警告）。回傳列一律帶 `sold_at_is_ingest` 欄位。
        """
        q = "SELECT * FROM comps WHERE 1=1"
        params: list[Any] = []
        if real_sold_at_only:
            q += " AND COALESCE(sold_at_is_ingest, 0) = 0"
        for col, val in (
            ("rarity", rarity), ("grader", grader), ("grade", grade),
            ("set_code", set_code), ("site", site), ("signature", signature),
        ):
            if val is not None:
                q += f" AND {col} = ?"
                params.append(val)
        if since_days is not None:
            cutoff = datetime.now(UTC) - timedelta(days=since_days)
            q += " AND sold_at >= ?"
            params.append(cutoff.isoformat())
        q += " ORDER BY sold_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    # ------------------------------------------------------------------
    def backfill_sold_at_provenance(self) -> int:
        """把既有 comps 的 `sold_at_is_ingest` 補起來，回傳實際改動的列數。

        **冪等**：判準只看 `sold_at` 的字面（有小數點＝入庫時間，見
        `_BACKFILL_SOLD_AT_PROVENANCE_SQL` 的實測依據），而且只寫 NULL 的列，
        所以跑幾次都一樣、第二次起回 0。開機時（`__init__` 的 `_migrate_comps`）
        已經自動跑過一次；這支是給 CLI 明確重跑與回報用的。
        """
        with self._conn() as c:
            cur = c.execute(_BACKFILL_SOLD_AT_PROVENANCE_SQL)
            return cur.rowcount or 0

    def set_sale_kinds(self, pairs: Iterable[tuple[int, str]]) -> int:
        """把 `comps.sale_kind` 寫進指定的列（id → 型態），回傳實際改動列數。

        **只寫「還沒有值」或「已經是 unknown」的列**——判斷邏輯在
        `comps.backfill_sale_kind`（單一判定處），這裡的 WHERE 條件是同一條
        規則的結構性複述：已經有明確型態的列，任何重跑都不得把它抹掉。
        """
        rows = [(kind, int(rid)) for rid, kind in pairs]
        if not rows:
            return 0
        with self._conn() as c:
            cur = c.executemany(
                "UPDATE comps SET sale_kind = ? WHERE id = ?"
                " AND (sale_kind IS NULL OR sale_kind = '' OR sale_kind = 'unknown')",
                rows,
            )
            return cur.rowcount or 0

    def mark_comps_duplicates(self, pairs: Iterable[tuple[int, int]]) -> int:
        """把 `comps.dup_of_id` 寫進指定的列（dup_id → keep_id），回傳實際改動列數。

        **只寫還沒有標記的列**——判斷邏輯在 `comps.find_dual_listing_duplicates`
        （單一判定處），這裡的 WHERE 條件只是同一條規則的結構性複述：已經標記
        過的列，任何重跑都不得再改（避免鏈狀重標，也讓整條流程冪等）。
        **不刪除任何列**——標記可以撤回，刪除不可逆。
        """
        rows = [(int(keep_id), int(dup_id)) for dup_id, keep_id in pairs]
        if not rows:
            return 0
        with self._conn() as c:
            cur = c.executemany(
                "UPDATE comps SET dup_of_id = ? WHERE id = ? AND dup_of_id IS NULL",
                rows,
            )
            return cur.rowcount or 0

    def comps_provenance(self) -> dict[str, Any]:
        """`sold_at` 來歷的帳：兩種資料各有幾筆、逐站台分布、還有幾筆未標記。

        報表要標明「這些數字建立在什麼樣的時間上」時讀這個。
        """
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM comps").fetchone()[0]
            real = c.execute(
                "SELECT COUNT(*) FROM comps WHERE sold_at_is_ingest = 0"
            ).fetchone()[0]
            ingest = c.execute(
                "SELECT COUNT(*) FROM comps WHERE sold_at_is_ingest = 1"
            ).fetchone()[0]
            unknown = c.execute(
                "SELECT COUNT(*) FROM comps WHERE sold_at_is_ingest IS NULL"
            ).fetchone()[0]
            by_site = [
                dict(r)
                for r in c.execute(
                    "SELECT site,"
                    " SUM(CASE WHEN sold_at_is_ingest = 1 THEN 1 ELSE 0 END) AS ingest,"
                    " SUM(CASE WHEN sold_at_is_ingest = 0 THEN 1 ELSE 0 END) AS real_time,"
                    " COUNT(*) AS n"
                    " FROM comps GROUP BY site ORDER BY n DESC"
                )
            ]
        return {
            "total": total,
            "real": real,
            "ingest": ingest,
            "unknown": unknown,
            "by_site": by_site,
        }

    def set_card_names(self, updates: list[tuple[int, str | None]]) -> int:
        """把比對出來的卡名物化到 comps.card_name，回傳實際改動的列數。

        **這一欄是快取，不是事實來源**。事實來源永遠是同一列上永久保留的
        `title`：卡名比對規則還在演進（別名、假名折疊、最長匹配），寫死在欄位裡
        等於「改比對邏輯就要重抓資料」。估價模組一律在查詢時重新比對 title，
        這一欄只服務 SQL 分析與 dashboard 這種不想跑 Python 的場合。
        """
        if not updates:
            return 0
        with self._conn() as c:
            cur = c.executemany(
                "UPDATE comps SET card_name = ? WHERE id = ?",
                [(name, row_id) for row_id, name in updates],
            )
            return cur.rowcount

    def set_era_evidence(self, updates: list[tuple[int, str]]) -> int:
        """重寫既有列的 era_evidence，回傳改動列數。

        **這一欄是寫入時算的，判準改了它不會自己更新**——而 `load_comps` /
        `load_comps_rows` 正是靠它決定一列算不算有效樣本。所以每次調整
        watchlist 的排除字或年代標記之後，都要用這個回填把既有列重過一次，
        否則新判準只對「之後才抓到的資料」生效，庫裡的舊污染永遠留著。

        呼叫端只傳「真的變了」的列（見 cli.backfill_era），所以回傳值就是
        實際變更數，重跑第二次會是 0——冪等看得出來，不是用信的。
        """
        if not updates:
            return 0
        with self._conn() as c:
            cur = c.executemany(
                "UPDATE comps SET era_evidence = ? WHERE id = ?",
                [(evidence, row_id) for row_id, evidence in updates],
            )
            return cur.rowcount

    def count_era_verified_comps(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM comps "
                "WHERE era_evidence IS NOT NULL AND era_evidence != ''"
            ).fetchone()["n"]

    def count_named_comps(self) -> int:
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) n FROM comps "
                "WHERE card_name IS NOT NULL AND card_name != ''"
            ).fetchone()["n"]

    # ------------------------------------------------------------------
    # 在架觀測帳（listing_obs）
    def record_listing_scan(
        self, batches: list[dict[str, Any]], *, now: str | None = None
    ) -> dict[str, int]:
        """一輪掃描的在架觀測：**寫入與離場判定必須在同一次交易裡**。

        `batches` 是每個 (發現管道, 關鍵字) 一格：
        `{"source": str, "site": str, "healthy": bool, "rows": [ {key, ...}, … ]}`。
        `healthy=False`（被擋／解析壞／連線失敗）的批次**整批不採信**——
        來源壞掉時它的標的全都「看不到」，照常判離場等於把一次 WAF 挑戰
        記成整站的商品一夜賣光（工程原則：讀不到 ≠ 東西不見了）。

        ── `exit_scope`（預設 True）─────────────────────────────────────
        「這一批看得到整個站的第 1 頁嗎」。賣家頁列舉（seller_watch 的輪替監控）
        只看得到**一個賣家**的貨，所以它 `exit_scope=False`：它的列照樣落帳、
        它看到的 key 照樣算「本輪有看到」（保護那些標的不被誤判離場），但它
        **不提供地平線**。少了這一條，一輪「主掃描被擋、只有賣家頁成功」的
        掃描會拿一個賣家的清單去判定整個站的商品是不是不見了——那是同一個
        「讀不到 ≠ 東西不見了」的錯誤換一個入口重演。

        ── 離場判定：觀測窗地平線（horizon）─────────────────────────
        本專案的在架掃描是**新着降冪 + 只抓第 1 頁**。在這個形態下「這輪沒看到」
        的預設解釋是「被更新的貨擠出第 1 頁」，不是「賣掉了」——直接把消失當成交
        會得到一個幾乎只反映上架速度的假「賣得掉率」。

        所以每個批次算一條地平線：本輪看到的標的裡**最早的 first_seen**。
        比地平線更新的標的若不在本輪結果中，代表這一頁還蓋得到它的位置、
        它卻不在了 → 記 `disappeared_at`（可推論為下架／賣掉）。
        比地平線更舊的 → 只是被擠出去了 → 記 `window_exit_at`（右設限，無結論）。
        一個站有多個查詢時取**各批次地平線的最大值**（最保守：地平線越新，
        被判定成「真的消失」的越少）。

        回傳計數報告；`revived` 是被判 disappeared 之後又出現的筆數，
        也就是這條規則自己打自己臉的次數——請把它當成 proxy 可信度的量測值。
        """
        stamp = now or _now_iso()
        report = {
            "seen": 0, "new": 0, "updated": 0, "revived": 0,
            "disappeared": 0, "window_exit": 0, "batches_skipped": 0,
        }
        healthy = []
        for b in batches:
            if b.get("healthy"):
                healthy.append(b)
            else:
                report["batches_skipped"] += 1
        if not healthy:
            return report

        seen_by_site: dict[str, set[str]] = {}
        horizons: dict[str, str] = {}
        handled: set[str] = set()
        #: 這一輪觀測到的賣家 → feedback（可能是 None）。同一交易內聚合更新
        #: sellers 表；feedback 只有部分來源給（eBay/paypay 的 row 會帶）。
        touched_sellers: dict[tuple[str, str], tuple[Any, Any]] = {}

        with self._conn() as c:
            for batch in healthy:
                site = str(batch.get("site") or "")
                batch_first: list[str] = []
                for row in batch.get("rows") or []:
                    key = str(row.get("key") or "")
                    if not key:
                        continue
                    sid = row.get("seller_id")
                    if sid:
                        prev = touched_sellers.get((site, str(sid)), (None, None))
                        touched_sellers[(site, str(sid))] = (
                            row.get("seller_feedback_score", prev[0]),
                            row.get("seller_feedback_pct", prev[1]),
                        )
                    seen_by_site.setdefault(site, set()).add(key)
                    if key in handled:
                        # 同一輪被兩個查詢撈到 → 只算一次觀測，但地平線仍要納入
                        first = c.execute(
                            "SELECT first_seen FROM listing_obs WHERE key = ?", (key,)
                        ).fetchone()
                        batch_first.append((first["first_seen"] if first else stamp) or stamp)
                        continue
                    handled.add(key)
                    batch_first.append(self._upsert_listing_obs(c, key, row, stamp, report))
                if batch_first and batch.get("exit_scope", True):
                    h = min(batch_first)
                    if h > horizons.get(site, ""):
                        horizons[site] = h

            report["seen"] = len(handled)

            for site, seen in seen_by_site.items():
                horizon = horizons.get(site)
                if horizon is None:
                    continue
                open_rows = c.execute(
                    "SELECT key, first_seen FROM listing_obs "
                    "WHERE site = ? AND disappeared_at IS NULL AND window_exit_at IS NULL",
                    (site,),
                ).fetchall()
                for r in open_rows:
                    if r["key"] in seen:
                        continue
                    if (r["first_seen"] or "") > horizon:
                        c.execute(
                            "UPDATE listing_obs SET disappeared_at = ? WHERE key = ?",
                            (stamp, r["key"]),
                        )
                        report["disappeared"] += 1
                    else:
                        c.execute(
                            "UPDATE listing_obs SET window_exit_at = ? WHERE key = ?",
                            (stamp, r["key"]),
                        )
                        report["window_exit"] += 1

            # 賣家帳本：與觀測寫入同一次交易（要嘛一起落、要嘛一起 rollback）。
            for (site, sid), (fb_score, fb_pct) in touched_sellers.items():
                self._upsert_seller(
                    c, site, sid, stamp=stamp,
                    feedback_score=fb_score, feedback_pct=fb_pct,
                )
            report["sellers"] = len(touched_sellers)
        return report

    @staticmethod
    def _upsert_listing_obs(
        c: sqlite3.Connection, key: str, row: dict[str, Any], stamp: str, report: dict[str, int]
    ) -> str:
        """寫一筆觀測，回傳這一列的 `first_seen`（給地平線計算用）。

        既有列只覆寫「內容」欄位（價格／標題會變），帳務欄位各走各的規則：
        `first_seen` 永不改寫、`seen_count` 只加、被判離場後又出現則清掉離場
        標記並把 `revived_count` +1（那是判定規則自己的錯誤紀錄，不可靜默抹掉）。
        """
        existing = c.execute(
            "SELECT first_seen, disappeared_at FROM listing_obs WHERE key = ?", (key,)
        ).fetchone()
        content = {col: row.get(col) for col in _LISTING_OBS_CONTENT_COLUMNS}
        if existing is None:
            payload = {**content, "key": key, "first_seen": stamp, "last_seen": stamp}
            cols = ", ".join(payload)
            vals = ", ".join(f":{k}" for k in payload)
            c.execute(
                f"INSERT INTO listing_obs ({cols}, seen_count) VALUES ({vals}, 1)", payload
            )
            report["new"] += 1
            return stamp

        # sticky 欄位用 COALESCE：來源這次沒帶不代表這筆沒有賣家
        # （見 `_LISTING_OBS_STICKY_COLUMNS` 的事故記錄）。
        sets = ", ".join(
            f"{col} = COALESCE(:{col}, {col})" if col in _LISTING_OBS_STICKY_COLUMNS
            else f"{col} = :{col}"
            for col in _LISTING_OBS_CONTENT_COLUMNS
        )
        revived = 1 if existing["disappeared_at"] else 0
        c.execute(
            f"UPDATE listing_obs SET {sets}, last_seen = :last_seen, "
            "seen_count = seen_count + 1, disappeared_at = NULL, window_exit_at = NULL, "
            "revived_count = revived_count + :revived WHERE key = :key",
            {**content, "key": key, "last_seen": stamp, "revived": revived},
        )
        report["updated"] += 1
        report["revived"] += revived
        return existing["first_seen"] or stamp

    # ------------------------------------------------------------------
    # 賣家帳本（sellers）
    @staticmethod
    def _upsert_seller(
        c: sqlite3.Connection,
        site: str,
        seller_id: str,
        *,
        stamp: str | None,
        feedback_score: Any = None,
        feedback_pct: Any = None,
    ) -> None:
        """更新一個賣家的聚合列。**計數一律重算，不累加**（冪等；重複掃到
        同一標的不膨脹）。首見／最後活躍走各自的規則：first_seen 只往前收斂、
        last_seen 只往後推進；`stamp=None` 表示「這不是一次新觀測」（回填走
        這條），此時兩個時間戳只由帳本裡的既有觀測時間決定，不蓋成 now()。

        時間基準刻意**只用觀測時間**（listing_obs 的 first/last_seen 與呼叫
        當下的 stamp），不混 comps.sold_at——那是事件時間，而且有一批是入庫
        時間（sold_at_is_ingest），混進來就是兩種基準（工程原則 1）。
        """
        if not site or not seller_id:
            return
        seller_key = f"{site}:{seller_id}"
        ex = c.execute(
            "SELECT first_seen, last_seen, feedback_score, feedback_pct, note "
            "FROM sellers WHERE seller_key = ?", (seller_key,)
        ).fetchone()
        lo = c.execute(
            "SELECT COUNT(*) AS n, MIN(first_seen) AS fs, MAX(last_seen) AS ls "
            "FROM listing_obs WHERE site = ? AND seller_id = ?",
            (site, seller_id),
        ).fetchone()
        sold_n = c.execute(
            "SELECT COUNT(*) AS n FROM comps WHERE site = ? AND seller_id = ?",
            (site, seller_id),
        ).fetchone()["n"]

        firsts = [x for x in ((ex["first_seen"] if ex else None), lo["fs"], stamp) if x]
        lasts = [x for x in ((ex["last_seen"] if ex else None), lo["ls"], stamp) if x]
        if not firsts:
            return  # 沒有任何觀測依據（帳本裡查無、也不是一次新觀測）

        def _to_int(v: Any) -> int | None:
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        def _to_float(v: Any) -> float | None:
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        c.execute(
            """INSERT OR REPLACE INTO sellers
               (seller_key, site, seller_id, first_seen, last_seen,
                listing_count, sold_count, feedback_score, feedback_pct, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                seller_key, site, seller_id, min(firsts), max(lasts),
                int(lo["n"]), int(sold_n),
                # feedback：新值優先、沒有就保留舊值（來源只有部分掃描帶它，
                # 一次沒帶不該把上次的評價洗掉）
                _to_int(feedback_score) if feedback_score is not None
                else (ex["feedback_score"] if ex else None),
                _to_float(feedback_pct) if feedback_pct is not None
                else (ex["feedback_pct"] if ex else None),
                (ex["note"] if ex else "") or "",
            ),
        )

    def list_sellers(
        self, *, site: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """賣家帳本（依累計在架觀測數排序）。原始列，統計是上層的事。"""
        q = "SELECT * FROM sellers"
        params: list[Any] = []
        if site is not None:
            q += " WHERE site = ?"
            params.append(site)
        q += " ORDER BY listing_count DESC, sold_count DESC, seller_key LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    # ------------------------------------------------------------------
    # 賣家監控名單（seller_watch）。**這一層只做 CRUD**：名單上限、淘汰規則、
    # 分批規則全部在 `seller_watch.py`——政策寫在 SQL 裡就沒辦法單測，也沒辦法
    # 在 CLI 與 dashboard 之間共用同一份說明（工程原則 1：判準只有一份）。
    def list_seller_watch(
        self, *, active_only: bool = True, batch: int | None = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM seller_watch WHERE 1=1"
        params: list[Any] = []
        if active_only:
            q += " AND active = 1"
        if batch is not None:
            q += " AND batch = ?"
            params.append(int(batch))
        # 排序：先分數高的（auto），再手動加入的（score IS NULL 沉底但不消失）
        q += " ORDER BY active DESC, score IS NULL, score DESC, added_at, seller_key"
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def get_seller_watch(self, seller_key: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM seller_watch WHERE seller_key = ?", (seller_key,)
            ).fetchone()
        return dict(r) if r else None

    def upsert_seller_watch(
        self,
        seller_key: str,
        *,
        source: str,
        reason: str,
        batch: int,
        score: float | None = None,
        now: str | None = None,
    ) -> None:
        """加入（或重新啟用）一個監控賣家。

        重新啟用時 `added_at` 會被重寫成現在——那是「這一次開始追蹤」的時間，
        而輪替與冷卻都以它為基準。`last_scanned_at` 一律清空：重新加入之後
        第一次輪到它就該掃，不該沿用上次移除前的時間戳。
        """
        site, _, sid = seller_key.partition(":")
        stamp = now or _now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO seller_watch
                   (seller_key, site, seller_id, source, reason, added_at,
                    active, batch, score, last_scanned_at, last_result, removed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, '', NULL)""",
                (seller_key, site, sid, source, reason or "", stamp, int(batch),
                 None if score is None else float(score)),
            )

    def deactivate_seller_watch(
        self, seller_key: str, *, reason: str, now: str | None = None
    ) -> bool:
        """移出名單（軟刪除）。回傳「本來在不在名單上」。"""
        stamp = now or _now_iso()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE seller_watch SET active = 0, removed_at = ?, reason = ? "
                "WHERE seller_key = ? AND active = 1",
                (stamp, reason or "", seller_key),
            )
            return bool(cur.rowcount)

    def mark_seller_watch_scanned(
        self, seller_key: str, *, result: str, now: str | None = None
    ) -> None:
        """記一次輪替掃描的結果。**失敗也要記**（result 帶病名）——
        「上次掃描 3 小時前」與「上次掃描失敗了」不可以長得一樣（工程原則 3）。"""
        with self._conn() as c:
            c.execute(
                "UPDATE seller_watch SET last_scanned_at = ?, last_result = ? "
                "WHERE seller_key = ?",
                (now or _now_iso(), result or "", seller_key),
            )

    def set_listing_obs_seller(self, key: str, seller_id: str) -> bool:
        """把單一觀測列的 `seller_id` 補上（商品頁挖回來的賣家走這條）。

        **只補不改**（`WHERE seller_id IS NULL`），與 `backfill_seller_ids`
        同一個規則：回填是補歷史，不是重新判斷。回傳「這次有沒有真的寫到」
        ——第二次跑同一個 key 必為 False，那就是冪等的證據。

        寫進去之後同一筆交易重算賣家聚合，`stamp=None`：從商品頁補一個賣家
        **不是一次新觀測**，把 `last_seen` 蓋成 now() 會讓「這個賣家最近還活著」
        變成我們自己回填的副作用（工程原則 1：時間戳要說得出它量的是什麼）。
        """
        if not key or not seller_id:
            return False
        with self._conn() as c:
            row = c.execute(
                "SELECT site, seller_id FROM listing_obs WHERE key = ?", (key,)
            ).fetchone()
            if row is None or row["seller_id"]:
                return False
            c.execute(
                "UPDATE listing_obs SET seller_id = ? WHERE key = ? AND seller_id IS NULL",
                (str(seller_id), key),
            )
            self._upsert_seller(c, str(row["site"] or ""), str(seller_id), stamp=None)
        return True

    def backfill_seller_ids(self, *, dry_run: bool = False) -> dict[str, int]:
        """把 signals payload 裡既有的 `listing.seller_id` 回填進 listing_obs。

        歷史脈絡：eBay 的 seller_id 從第一天就在 payload 裡（`_to_listing` 一直
        有填），但 listing_obs 落帳時沒帶——這支把那批補回來。

        **冪等**：只寫 `listing_obs.seller_id IS NULL` 的列；已有值的一律不碰
        （新寫入端在掃描當下就填了，回填是補歷史，不是重新判斷）。第二次跑
        `updated` 必為 0。`dry_run=True` 只算不寫。回填後把觸到的賣家聚合
        重算一次（`stamp=None`：回填不是新觀測，不得把 last_seen 蓋成 now）。
        """
        report = {"payload_with_seller": 0, "updated": 0, "already_set": 0, "no_obs_row": 0}
        with self._conn() as c:
            sellers_touched: set[tuple[str, str]] = set()
            rows = c.execute(
                "SELECT key, payload FROM signals WHERE payload IS NOT NULL"
            ).fetchall()
            for r in rows:
                try:
                    doc = json.loads(r["payload"] or "{}")
                except ValueError:
                    continue
                listing = doc.get("listing")
                sid = (listing or {}).get("seller_id") if isinstance(listing, dict) else None
                if not sid:
                    continue
                report["payload_with_seller"] += 1
                obs = c.execute(
                    "SELECT site, seller_id FROM listing_obs WHERE key = ?", (r["key"],)
                ).fetchone()
                if obs is None:
                    report["no_obs_row"] += 1
                    continue
                if obs["seller_id"]:
                    report["already_set"] += 1
                    continue
                report["updated"] += 1
                if dry_run:
                    continue
                c.execute(
                    "UPDATE listing_obs SET seller_id = ? "
                    "WHERE key = ? AND seller_id IS NULL",
                    (str(sid), r["key"]),
                )
                sellers_touched.add((str(obs["site"] or ""), str(sid)))
            if not dry_run:
                for site, sid in sellers_touched:
                    self._upsert_seller(c, site, sid, stamp=None)
        return report

    def listing_obs(
        self,
        *,
        site: str | None = None,
        since_days: int | None = None,
        open_only: bool = False,
        limit: int = 20000,
    ) -> list[dict[str, Any]]:
        """撈在架觀測列（原始列；分層與統計是上層的決定，見 comps_by 的同一哲學）。

        `open_only=True` 只回「本專案還看得到它在架」的列（兩個離場欄都是空）。
        """
        q = "SELECT * FROM listing_obs WHERE 1=1"
        params: list[Any] = []
        if site is not None:
            q += " AND site = ?"
            params.append(site)
        if open_only:
            q += " AND disappeared_at IS NULL AND window_exit_at IS NULL"
        if since_days is not None:
            q += " AND last_seen >= ?"
            params.append((datetime.now(UTC) - timedelta(days=since_days)).isoformat())
        q += " ORDER BY last_seen DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [dict(r) for r in c.execute(q, params).fetchall()]

    def listing_obs_summary(self) -> dict[str, Any]:
        """每個平台的在架觀測帳現況。dashboard 與 CLI 共用同一份數字。"""
        with self._conn() as c:
            rows = c.execute(
                """SELECT site,
                          COUNT(*)                                             AS total,
                          SUM(disappeared_at IS NULL AND window_exit_at IS NULL) AS still_open,
                          SUM(disappeared_at IS NOT NULL)                      AS disappeared,
                          SUM(window_exit_at IS NOT NULL)                      AS window_exit,
                          SUM(revived_count > 0)                               AS revived,
                          SUM(seen_count > 1)                                  AS multi_seen,
                          MIN(first_seen)                                      AS oldest,
                          MAX(last_seen)                                       AS newest
                     FROM listing_obs GROUP BY site"""
            ).fetchall()
            total = c.execute("SELECT COUNT(*) n FROM listing_obs").fetchone()["n"]
        return {"total": total, "by_site": [dict(r) for r in rows]}

    def prune_listing_obs(self, days: int) -> int:
        """刪掉「已離場且夠久沒再被看到」的觀測列。days <= 0 = 關閉。

        只刪已離場的：還在架上的標的不論多老都留著（它正是「賣不掉」的證據，
        刪掉等於把最有資訊量的那一批洗掉）。
        """
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM listing_obs WHERE last_seen < ? "
                "AND (disappeared_at IS NOT NULL OR window_exit_at IS NOT NULL)",
                (cutoff,),
            )
            return cur.rowcount

    # ------------------------------------------------------------------
    def snapshot(self, entries: list[tuple[str, float]]) -> None:
        day = datetime.now(UTC).date().isoformat()
        with self._conn() as c:
            c.executemany(
                "INSERT OR REPLACE INTO snapshots (day, key, price_twd) VALUES (?,?,?)",
                [(day, k, p) for k, p in entries],
            )

    # ------------------------------------------------------------------
    # 來源健康告警（alerts.AlertEngine 的持久層）
    def get_alert(self, fingerprint: str) -> dict[str, Any] | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM alerts WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return dict(r) if r else None

    def upsert_alert(
        self,
        fingerprint: str,
        *,
        source: str,
        kind: str,
        first_seen: str,
        last_seen: str,
        occurrences: int,
        last_notified_at: str | None,
        notify_count: int,
        detail: str,
    ) -> None:
        """整列覆寫。呼叫端（AlertEngine）先讀舊列再算出新值，
        所以這裡不做 COALESCE —— 只有一處決定欄位怎麼演進（工程原則 1）。"""
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO alerts
                   (fingerprint, source, kind, first_seen, last_seen,
                    occurrences, last_notified_at, notify_count, detail)
                   VALUES (:fingerprint, :source, :kind, :first_seen, :last_seen,
                           :occurrences, :last_notified_at, :notify_count, :detail)""",
                {
                    "fingerprint": fingerprint,
                    "source": source,
                    "kind": kind,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "occurrences": int(occurrences),
                    "last_notified_at": last_notified_at,
                    "notify_count": int(notify_count),
                    "detail": detail or "",
                },
            )

    def list_alerts(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM alerts ORDER BY source, kind"
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_alert(self, fingerprint: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM alerts WHERE fingerprint = ?", (fingerprint,))

    # ------------------------------------------------------------------
    # 需求驅動回補的節流帳（refill.py）
    def record_refill(
        self, card_name: str, *, found: int, kept: int, now: str | None = None
    ) -> None:
        """落一筆回補帳（UPSERT）。**found=0 也要落**——「查過但市場上沒有」
        正是這張表要記住的事，不記的話每輪都會把零結果的卡重查一遍。
        跨行程冪等：鍵是卡名，重跑只是更新時間戳與累加 runs。"""
        stamp = now or _now_iso()
        with self._conn() as c:
            c.execute(
                """INSERT INTO comps_refill (card_name, last_run_at, runs, last_found, last_kept)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT(card_name) DO UPDATE SET
                       last_run_at = excluded.last_run_at,
                       runs = runs + 1,
                       last_found = excluded.last_found,
                       last_kept = excluded.last_kept""",
                (card_name, stamp, int(found), int(kept)),
            )

    def refill_cooldown_active(self, days: float) -> set[str]:
        """還在冷卻中的卡名集合。days <= 0 = 冷卻關閉（回空集合）。

        比較用 ISO 字串：寫入端只有 `record_refill`（`_now_iso()`，UTC 含時區），
        同源同基準，字串比大小成立。"""
        if days <= 0:
            return set()
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._conn() as c:
            rows = c.execute(
                "SELECT card_name FROM comps_refill WHERE last_run_at >= ?", (cutoff,)
            ).fetchall()
        return {r["card_name"] for r in rows}

    def refill_ledger(self, limit: int = 1000) -> list[dict[str, Any]]:
        """整份回補帳（最近查的在前），CLI 與 dashboard 檢視用。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM comps_refill ORDER BY last_run_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 跨行程小狀態（comps 的已售出查詢節流計數器）
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value))
            )

    # ------------------------------------------------------------------
    # 掃描狀態（跨行程）。
    #
    # 為什麼落在 meta 而不是「runs 有 started_at 無 finished_at」：
    # 1. runs 只在 dry_run=False 時落列（見 pipeline.scan）。拿一張「有時候不寫」
    #    的表當狀態來源，dry-run 掃描期間 dashboard 會顯示「沒有在掃」——
    #    那正是使用者按下第二次的時機。（同一個理由已寫在 meta 表的 schema 註解裡。）
    # 2. 要用 runs 表就得改成「開始時插一列、結束時 UPDATE」，而 `stats()` 與
    #    `health` 指令都拿「runs 最後一列」當「最近一次掃描的結果」——半寫的列
    #    會讓 health 顯示一次沒有來源健康資料的空掃描，是無聲的退化。
    # 3. meta 是單列覆寫，掃再多次也不會長大。
    #
    # 分工（刻意單源）：**「正在掃嗎」只問 meta，「上一次掃完是什麼時候」只問 runs。**
    # 兩個問題各有唯一的資料來源，不會出現「meta 說掃完了、runs 沒有那一列」這種
    # 兩邊互相矛盾的顯示（工程原則 1）。
    SCAN_STATUS_KEY = "scan_status"

    def begin_scan(self, *, trigger: str = "cli", dry_run: bool = False) -> str:
        """標記「掃描開始」，回傳 started_at。

        直接覆寫舊狀態是刻意的：卡在 running 的殘留狀態不該擋住下一次掃描。
        併發保護在呼叫端（web 的 /api/scan 先看 scan_status），不在這裡。
        """
        started = _now_iso()
        self.set_meta(
            self.SCAN_STATUS_KEY,
            json.dumps(
                {
                    "running": True,
                    "started_at": started,
                    "finished_at": None,
                    "trigger": trigger,
                    "dry_run": bool(dry_run),
                    "error": None,
                    "result": None,
                },
                ensure_ascii=False,
            ),
        )
        return started

    def finish_scan(
        self,
        started_at: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """標記「掃描結束」。**失敗也要呼叫**（帶 error）——

        崩潰時沒人呼叫得到這裡，那條路由靠 scan_status 的逾時兜底；
        但「抓到例外」這種還活著的失敗必須明確落成 finished + error，
        不能讓它跟真正的崩潰長得一樣（工程原則 3）。
        """
        cur = self._read_scan_status()
        cur.update(
            {
                "running": False,
                "started_at": started_at,
                "finished_at": _now_iso(),
                "error": error,
                "result": result,
            }
        )
        self.set_meta(self.SCAN_STATUS_KEY, json.dumps(cur, ensure_ascii=False, default=str))

    def _read_scan_status(self) -> dict[str, Any]:
        raw = self.get_meta(self.SCAN_STATUS_KEY)
        if not raw:
            return {}
        try:
            val = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return val if isinstance(val, dict) else {}

    def scan_status(self, *, timeout_seconds: float = 1800) -> dict[str, Any]:
        """「現在有沒有在掃」＋「上一次掃完是什麼時候」。

        **逾時判定是這個函式存在的主要理由**：掃描中途被 kill、機器睡著、
        web 行程重啟，`finish_scan` 就永遠不會被呼叫。只看 running 旗標的話
        按鈕會永遠 disabled，使用者完全沒有自救手段。所以 running 的定義是
        「狀態說在跑 **而且** 開始到現在還沒超過逾時」——逾時的那些回報
        `running: false, stale: true`，前端據此把按鈕放行並顯示「上次掃描沒有回報完成」。
        """
        st = self._read_scan_status()
        started_at = st.get("started_at")
        running = bool(st.get("running")) and bool(started_at)
        stale = False
        age = None
        if running:
            age = _age_seconds(started_at)
            # 解析不出開始時間 = 無法證明它還活著，一律當死的（讀不到 ≠ 還在跑）
            if age is None or age > float(timeout_seconds):
                running, stale = False, True

        with self._conn() as c:
            last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()

        return {
            "running": running,
            "stale": stale,
            "started_at": started_at,
            "finished_at": st.get("finished_at"),
            "trigger": st.get("trigger"),
            "dry_run": bool(st.get("dry_run")),
            "error": st.get("error"),
            "elapsed_seconds": round(age, 1) if age is not None else None,
            "timeout_seconds": float(timeout_seconds),
            "last_result": st.get("result"),
            # 顯示用的「最後一次掃描完成時間」只有這一個來源：runs 表最後一列
            "last_run": dict(last) if last else None,
        }

    # ------------------------------------------------------------------
    def expire_stale_signals(self, days: int) -> int:
        """把「很久沒再被掃到、而且你從沒動過」的訊號標成 expired，回傳筆數。

        `upsert_signal` 以 `Listing.key` 去重，所以重複掃到的標的是更新不是新增，
        db 不會因為每小時掃一輪而暴增。真正會累積的是**賣掉／下架之後再也不會
        出現**的舊列——它們的 `last_seen` 從此凍結，卻永遠佔著「待處理」清單。

        只動 `state='new'`：任何你手動標過的狀態（已詢問、湊單籃、已買…）都是
        人工決策，程式不准覆蓋。這也是為什麼標成 expired 而不是 DELETE——
        歷史資料留著才能回頭看「當初那批標的後來怎麼了」。
        days <= 0 視為關閉這個功能。
        """
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._conn() as c:
            cur = c.execute(
                "UPDATE signals SET state = ? WHERE state = ? AND last_seen IS NOT NULL "
                "AND last_seen < ?",
                (TriageState.EXPIRED.value, TriageState.NEW.value, cutoff),
            )
            return cur.rowcount

    def all_signal_titles(self) -> list[dict[str, Any]]:
        """每一筆訊號的 (key, site, title, state)。重跑解析判準用。"""
        with self._conn() as c:
            return [
                dict(r)
                for r in c.execute("SELECT key, site, title, state FROM signals ORDER BY key")
            ]

    def purge_signals(self, keys: list[str]) -> dict[str, int]:
        """刪掉這些 key 的訊號（連同它的在架觀測列與推播帳），**只刪 `state='new'`**。

        為什麼這一支是 DELETE 而 `expire_stale_signals` 是 UPDATE：那一支處理的是
        「真的標的後來消失了」——那段歷史有資訊（賣得掉率）。這一支處理的是
        「這筆根本不是遊戲王卡，當初就不該入庫」，留著只會污染統計，沒有任何
        可回答的問題需要它。

        紅線同一條：任何你手動標過的狀態（已詢問／湊單籃／已買／已略過）都是
        人工決策，程式不准刪——回傳的 `kept_manual` 就是這種列的筆數，
        呼叫端要把它印出來讓使用者自己處理，不可以靜默跳過。
        """
        if not keys:
            return {"deleted": 0, "kept_manual": 0, "obs_deleted": 0}
        with self._conn() as c:
            marks = ",".join("?" * len(keys))
            deletable = [
                r["key"]
                for r in c.execute(
                    f"SELECT key FROM signals WHERE key IN ({marks}) AND state = ?",
                    (*keys, TriageState.NEW.value),
                )
            ]
            kept = len(
                c.execute(
                    f"SELECT key FROM signals WHERE key IN ({marks}) AND state != ?",
                    (*keys, TriageState.NEW.value),
                ).fetchall()
            )
            if not deletable:
                return {"deleted": 0, "kept_manual": kept, "obs_deleted": 0}
            dm = ",".join("?" * len(deletable))
            deleted = c.execute(
                f"DELETE FROM signals WHERE key IN ({dm})", deletable
            ).rowcount
            obs = c.execute(
                f"DELETE FROM listing_obs WHERE key IN ({dm})", deletable
            ).rowcount
            c.execute(f"DELETE FROM notify_log WHERE key IN ({dm})", deletable)
        return {"deleted": deleted, "kept_manual": kept, "obs_deleted": obs}

    # ------------------------------------------------------------------
    def log_run(self, **kw: Any) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO runs (started_at, finished_at, scanned, candidates,
                                     signals, notified, notes)
                   VALUES (:started_at, :finished_at, :scanned, :candidates,
                           :signals, :notified, :notes)""",
                {
                    "started_at": kw.get("started_at"),
                    "finished_at": kw.get("finished_at", _now_iso()),
                    "scanned": kw.get("scanned", 0),
                    "candidates": kw.get("candidates", 0),
                    "signals": kw.get("signals", 0),
                    "notified": kw.get("notified", 0),
                    "notes": kw.get("notes", ""),
                },
            )

    def stats(self) -> dict[str, Any]:
        with self._conn() as c:
            counts = {
                r["state"]: r["n"]
                for r in c.execute("SELECT state, COUNT(*) n FROM signals GROUP BY state")
            }
            total = c.execute("SELECT COUNT(*) n FROM signals").fetchone()["n"]
            comps_n = c.execute("SELECT COUNT(*) n FROM comps").fetchone()["n"]
            last_run = c.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "total": total,
            "by_state": counts,
            "comps": comps_n,
            "last_run": dict(last_run) if last_run else None,
        }
