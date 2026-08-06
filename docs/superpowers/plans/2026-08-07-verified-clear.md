# 清除改為商品頁實證 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 清除「已離場」標的不再依賴 `disappeared_at` 推論——競標看 `end_time`（事實），非競標在清除當下**實際打開商品頁**確認已售出或已下架（事實），驗證不了的一律不清、大聲回報。

**背景（2026-08-07 事故）**：`disappeared_at` 由「觀測窗地平線」推論產生，但賣家頁輪替（每 4 小時）挖回的標的會在關鍵字掃描（每 30 分）的盲區反覆「假消失」。使用者一鍵清除 43 筆，實際一大半仍在架上——15 筆隔夜自動還原、28 筆由使用者指示手動還原，`expiry-stats` 記錄的誤殺率為 100%。天然對照組：無賣家輪替的 buyee_mercari 誤判數為 1，有輪替的三站為 37/64/94。根因分析全文見 scratchpad `disappeared-diagnosis.md`；本計畫是使用者指定的修法——**驗證取代推論**。

**Architecture:** 新模組 `verify_departed.py` 把「這筆還買得到嗎」的頁面驗證做成單一入口，重用 `appraise.fetch_item_page`（appraise.py:694-739，四路分流已存在）與 `sources/base.py` 的錯誤分類。`clear_expired_signals` 改成兩段：`ended`（end_time 已過）直接清＝事實；`gone` 候選逐筆驗頁，只清 `SOLD` 與 `DELISTED`，`STILL_LIVE` 不清，`UNVERIFIABLE`（被擋／逾時／不支援的站）**絕不當成已離場**——讀不到 ≠ 賣光（全域工程原則事故 #4 的同構）。端點改走 `/api/scan` 的 begin/finish/status 背景模式（驗 40 筆約 1-2 分鐘）。

**分類表（結構性強制，寫死在 VerifyResult）：**

| 頁面結果 | verdict | 清除？ | 依據 |
|---|---|---|---|
| `ItemPage.is_sold == True` | `SOLD` | ✅ | yahoo：`__NEXT_DATA__` status ∈ closed/sold（appraise.py:343-345）；buyee 鏡像：class 含 soldout（appraise.py:437-441） |
| `FetchError(status=404, transient=False)`／`EbayItemNotFound` | `DELISTED` | ✅ | 連結已失效＝下架 |
| 頁面正常且未售出 | `STILL_LIVE` | ❌ | 這筆是 `disappeared_at` 的誤判，回報筆數 |
| `BlockedError`／transient FetchError／`UnsupportedUrlError`／mercari_tw（無可靠標記，appraise.py:1490-1493） | `UNVERIFIABLE` | ❌ | 被擋或讀不到不是證據；大聲回報 |

**驗證抓取紀律**：`CachedFetcher.get(..., use_cache=False)`（base.py:179-184 的 12 小時快取會回舊頁）；沿用 `fetch.delay_seconds` 節流與 transient 重試（base.py:107-112, 186-215）；buyee URL 需 `WafSession`，照 `resolve_grades` 的開法（cli.py:2769-2778）。

**Tech Stack:** 既有 `appraise.parse_target`/`fetch_item_page`、`CachedFetcher`、FastAPI BackgroundTasks（照 web/app.py:985-1030 的 begin/finish/status 三件組，meta 表新增 verify 一組 key）、pytest（**全部 mock `fetch_item_page`，測試不出網**——工程原則 4）。

---

### Task 1: `verify_departed.py` 驗證分類層

**Files:**
- Create: `src/ygo_sniper/verify_departed.py`
- Test: `tests/test_verify_departed.py`（mock 一切 IO）

介面：

```python
@dataclass(frozen=True)
class VerifyResult:
    key: str
    verdict: Literal["SOLD", "DELISTED", "STILL_LIVE", "UNVERIFIABLE"]
    detail: str            # 給人看的一句話（含錯誤分類）

def verify_listing(key: str, url: str, *, fetch_page) -> VerifyResult
    # fetch_page: 注入的 callable（生產時包 appraise.fetch_item_page），
    # 拋出的例外在這裡分類成 verdict——分類邏輯是本模組的全部價值，必須可單測
```

測試（先紅後綠）：is_sold→SOLD；404 FetchError→DELISTED；EbayItemNotFound→DELISTED；正常頁→STILL_LIVE；BlockedError→UNVERIFIABLE；transient FetchError→UNVERIFIABLE；UnsupportedUrlError→UNVERIFIABLE；mercari_tw 的 ItemPage（is_sold 恆 False 且無標記）→UNVERIFIABLE 而非 STILL_LIVE（appraise.py:555 明講不判斷，不能當成「還在」）。

### Task 2: `clear_expired_signals` 改兩段式

**Files:**
- Modify: `src/ygo_sniper/store.py:1771-1818`
- Test: `tests/test_expiry_clear.py`（改寫既有清除測試）

簽章改為 `clear_expired_signals(state, *, gone_confidence, verifier=None)`：
- `ended`（end_time 已過）→ 直接清（維持現狀，事實）
- `gone` 候選：`verifier` 為 None 時**一筆都不清**（安全預設——沒有驗證器就只清事實）；有 verifier 時逐筆呼叫，只清 SOLD/DELISTED
- 回傳擴充：`{"cleared", "keys", "by_source", "by_verdict": {"ended": n, "sold": n, "delisted": n, "still_live": n, "unverifiable": n}, "still_live_keys": [...]}`
- `still_live` 的列順手把該筆 `listing_obs.disappeared_at` 清掉並 `revived_count += 1`——頁面實證比推論權威，讓帳本立刻修正

### Task 3: 背景端點三件組

**Files:**
- Modify: `web/app.py`（clear-expired 改背景模式）、`src/ygo_sniper/store.py`（meta 表加 verify 狀態組，照 scan_status 的寫法）
- Test: `tests/test_expiry_clear.py`

- `POST /api/signals/clear-expired`：已 running → `200 {"started": false}`；否則 begin → BackgroundTask 跑驗證+清除 → finish（內層 try/except 保證失敗也 finish，照 app.py:1010-1027）
- `GET /api/signals/clear-expired/status`：`{running, progress: {done, total}, last_result}`
- 背景任務組 verifier：`CachedFetcher(cfg)` + 條件開 `WafSession`（照 cli.py:2769-2778）+ ebay token，包成 callable 傳給 store
- 測試 mock verifier，不 mock store

### Task 4: 前端

**Files:** `web/static/index.html`

- 確認框文案改為：`將驗證 N 筆的商品頁，僅清除確認「已售出／已下架」者；驗證不了的會留下`
- 按下清除 → POST → 按鈕變「驗證中…」＋輪詢 status 顯示 `done/total` → 完成後 toast 分項結果：`已清 X（售出 a、下架 b）· 仍在架 c 筆已解除離場標記 · 無法驗證 d 筆保留`
- 徽章不變（仍顯示「疑似」，那本來就是推論的誠實標示）

### Task 5: CLI `clear-departed`（含 --dry-run）

**Files:** `src/ygo_sniper/cli.py`

`ygo-sniper clear-departed --state watching [--dry-run]`：dry-run 逐筆印 verdict 不寫庫——使用者可先看驗證結果再決定；非 dry-run 走與端點同一條 store 路徑。這也是端到端驗收的載體（CLAUDE.md 第六節：驗證使用者真的會打的指令）。

### Task 6: 端到端驗收（主對話親跑）

1. `make test` 全綠（基線 1453）
2. `ygo-sniper clear-departed --state watching --dry-run` 對正式庫實跑（唯讀 GET，少量、有節流）——逐筆 verdict 與抽查 3 個 URL 人工比對
3. dashboard 實跑一次完整清除流程（真實驗證，確認只清有實證的）
