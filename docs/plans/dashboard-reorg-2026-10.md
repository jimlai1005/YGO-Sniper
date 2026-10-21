# Dashboard 版面重整（2026-10-20 使用者九項）Implementation Plan

> **For agentic workers:** 主線程逐 task 派 `builder`（@inline），驗收由主線程親跑。Steps 用 `- [ ]` 追蹤。實作者只讀本檔，不依賴規劃對話。先讀 `CLAUDE.md` 第五、六節與 `docs/dashboard.md`。

**Goal:** 依使用者 2026-10-20 的九項要求整理 dashboard：合併掃描按鈕、修補跑按鈕 404、拿掉重新整理、隱藏漂移／籃子／行情統計、匯率改顯示數值與更新時間、狀態分頁只留四個、篩選列分三排、新增「其他」頂層分頁並把五個區塊搬進去（鑑價留原位）。

**Architecture:** 全部是 `web/static/index.html`（單檔 CSS/HTML/JS）與 `web/app.py` 的改動，外加 `src/ygo_sniper/fx.py` 多記一個更新時間。**隱藏一律用 HTML `hidden` 屬性，不刪 DOM**：快捷鍵（`hotkeyAction` 只作用在 `#grid .card`）、測試（`tests/test_signal_filters.py:133-134` 斷言 `data-bucket` 各出現一次）、後端 API 全部不動，之後要復原只是拿掉一個屬性。搬動區塊只搬 HTML，JS 靠 id 找元素，不受位置影響。

**Tech Stack:** FastAPI、原生 JS、pytest（`tests/test_scan_high_web.py` 的 client fixture 樣板）。

**使用者原話對照（編號沿用）：**
1. 立即掃描＋高價掃描合併成一顆，內容包含兩個掃描。
2. 補跑 daily 按下去出現 `啟動失敗：{"detail":"Not Found"}`。→ **根因不在程式**：使用者的 8321 是重啟前的舊行程（launchd `com.jim.ygosniper.dashboard`），靜態頁已是新版但行程內沒有 `/api/daily-run` 路由。修法＝主線程重啟服務（Task 3）。程式端只加一個更清楚的錯誤提示。
3. 拿掉重新整理按鈕。
4. 隱藏「漂移 …」。
5. 「匯率 cache」：**不是壞掉**——`fx.py:37-47` 線上抓到後寫 12 小時快取，期間內讀快取就標 `cache`（`data/cache/fx.json` 2026-10-20 22:03 剛更新）。修法：顯示「JPY→TWD 0.213 · 更新 22:03」，只有真的退回手動值（`fallback`／`config`）才顯示警告。
6. 隱藏「湊單籃 0」「行情 4541」，留「待處理 N」。
7. 狀態分頁只留 待處理／已出價／觀察中／全部。
8. 篩選列：最後三個控制項（`#flag-filter`、`#sort-mode`、`#filter-reset`）放第二排；`#list-count`（「顯示 70 / 70 筆 · …」）放第三排。
9. 頂層分頁「賣家」「狙擊」後面加「其他」，把 湊單籃攤提／破口價／怎麼用／關鍵字搜尋／平台研究 搬進去；**鑑價留在原位**。

---

## Task 1 @inline：後端——合併掃描 API、匯率更新時間

**Files:**
- Modify: `web/app.py`（`/api/scan` 1237-1284、`/api/scan-high` 1287 之後加 `/api/scan-both`；`/api/breakeven` 445-462）
- Modify: `src/ygo_sniper/fx.py`
- Test: `tests/test_scan_high_web.py`（加 `/api/scan-both` 測試）、`tests/test_fx.py`（若不存在就新建；先 `ls tests | grep -i fx`）

- [ ] **Step 1: 失敗測試——`/api/scan-both` 依序跑低價帶、高價帶，共用同一個 scan 狀態**

照 `tests/test_scan_high_web.py` 既有的假 Pipeline 樣式（它記錄 `scan(...)` 的呼叫參數）：

```python
def test_scan_both_runs_std_then_high_in_one_background_task(client, monkeypatch):
    c, app_mod = client
    calls = []

    class FakePipeline:
        def __init__(self, cfg): pass
        def scan(self, **kw): calls.append(kw); return {"scanned": 0, "signals": 0}
        def close(self): pass

    import ygo_sniper.pipeline as pm
    monkeypatch.setattr(pm, "Pipeline", FakePipeline)
    r = c.post("/api/scan-both")
    assert r.status_code == 200 and r.json()["started"] is True
    assert [k.get("high_band", False) for k in calls] == [False, True]
    assert calls[0]["trigger"] == "dashboard" and calls[1]["trigger"] == "dashboard-high"


def test_scan_both_refuses_when_running(client, monkeypatch):
    c, app_mod = client
    app_mod.store.begin_scan(trigger="cli")
    r = c.post("/api/scan-both")
    assert r.json()["started"] is False
```

- [ ] **Step 2: 實作 `/api/scan-both`**

```python
@app.post("/api/scan-both")
def trigger_scan_both(tasks: BackgroundTasks):
    """一顆按鈕跑完低價帶＋高價帶（2026-10-20 使用者要求合併）。

    兩輪**依序**在同一個背景任務裡跑：先 `scan(trigger="dashboard")`，再
    `scan(high_band=True, trigger="dashboard-high")`。不並行——兩條 pipeline 同時打
    同一批來源只會更快被擋（見 /api/scan docstring）。每輪各自 `begin_scan`／`finish_scan`，
    前端的 scan pill 會先顯示「掃描中」再顯示「高價掃描中」。
    第一輪拋例外時第二輪**仍然跑**：低價帶壞掉不該讓高價帶也沒掃到（兩者是獨立的來源組合），
    但錯誤要留在 scan 狀態裡讓前端看得到。
    """
    st = store.scan_status(timeout_seconds=cfg.scan_timeout_seconds)
    if st["running"]:
        return {"ok": True, "started": False, "running": True,
                "message": f"已經有一輪掃描在跑（{st.get('trigger') or '?'} 觸發），這次不重複啟動", "status": st}
    started = store.begin_scan(trigger="dashboard")

    def _run_one(*, high_band: bool, trigger: str) -> None:
        from ygo_sniper.pipeline import Pipeline
        p = Pipeline(cfg)
        try:
            p.scan(high_band=high_band, trigger=trigger)
        finally:
            p.close()

    def _run() -> None:
        errors = []
        for high_band, trigger in ((False, "dashboard"), (True, "dashboard-high")):
            try:
                _run_one(high_band=high_band, trigger=trigger)
            except Exception as exc:  # noqa: BLE001 - 一輪壞掉不吃掉另一輪，但要留痕
                errors.append(f"{trigger}: {type(exc).__name__}: {exc}")
        if errors:
            store.finish_scan(started, error="；".join(errors))
            raise RuntimeError("；".join(errors))

    tasks.add_task(_run)
    return {"ok": True, "started": True, "running": True,
            "message": "背景掃描已啟動（低價帶 → 高價帶），完成後清單會自動更新"}
```

`/api/scan` 與 `/api/scan-high` **保留**（CLI 測試與其他呼叫端），只是前端不再有各自的按鈕。

- [ ] **Step 3: 失敗測試——FxRates 記錄更新時間**

```python
def test_fx_updated_at_comes_from_cache_timestamp(cfg, tmp_path):
    import json, time
    from dataclasses import replace
    from ygo_sniper.fx import FxRates
    scoped = replace(cfg, storage={**cfg.storage, "cache_dir": str(tmp_path)})
    ts = time.time() - 3600
    (tmp_path / "fx.json").write_text(json.dumps({"ts": ts, "rates": {"JPY": 0.213, "USD": 31.2}}))
    fx = FxRates(scoped)
    assert fx.source == "cache"
    assert abs(fx.updated_at - ts) < 1
    assert fx.rates["JPY"] == 0.213


def test_fx_updated_at_none_when_fallback(cfg, tmp_path, monkeypatch):
    from dataclasses import replace
    import httpx
    from ygo_sniper import fx as fx_mod
    scoped = replace(cfg, storage={**cfg.storage, "cache_dir": str(tmp_path)})
    class Boom:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, *a, **k): raise httpx.ConnectError("offline")
    monkeypatch.setattr(fx_mod.httpx, "Client", Boom)
    fx = fx_mod.FxRates(scoped)
    assert fx.source == "fallback" and fx.updated_at is None
```

（`cfg` fixture 與 `cache_dir` 的覆寫方式照 `tests/test_yahoo_source.py` 的 `make_source`；若 `Config` 的快取目錄鍵不是 `storage.cache_dir`，用 `grep -n "cache_dir" src/ygo_sniper/config.py` 確認。）

- [ ] **Step 4: 實作 fx.py**

`__init__` 加 `self.updated_at: float | None = None`；`_load()` 讀到有效快取時 `self.updated_at = float(blob.get("ts", 0)) or None`；`refresh()` 成功時 `self.updated_at = time.time()`；失敗維持 None（fallback）。

`/api/breakeven` 回傳多兩個欄位：

```python
        "fx_updated_at": (
            datetime.fromtimestamp(fx.updated_at).astimezone().isoformat() if fx.updated_at else None
        ),
        "fx_jpy_twd": fx.rates.get("JPY"),
```

（`datetime` 若 app.py 尚未 import 就補。）

- [ ] **Step 5: 跑測試 ＋ commit**

Run: `cd /Users/jim/projects/ygo-sniper && .venv/bin/pytest tests/test_scan_high_web.py tests/test_fx.py tests/test_card_snipe_web.py 2>&1 | tail -2` → 全綠。
`make test 2>&1 | tail -3`（前景）→ 全綠。

```bash
git add web/app.py src/ygo_sniper/fx.py tests/test_scan_high_web.py tests/test_fx.py
git commit -m "feat(dashboard): /api/scan-both 一次跑低價帶＋高價帶；fx 記錄更新時間並由 /api/breakeven 回傳"
```

---

## Task 2 @inline：前端——九項版面改動

**Files:**
- Modify: `web/static/index.html`
- Modify: `tests/test_scan_high_web.py:140-142`（按鈕斷言改成新按鈕）
- 不動其他測試；改完要跑 `tests/test_signal_filters.py tests/test_expiry_banner.py tests/test_hotkeys.py tests/test_card_snipe_web.py tests/test_scan_high_web.py`，全部要綠。

**行號以 scout 2026-10-20 盤點為準（改之前再 grep 一次確認）。**

- [ ] **Step 1（#1 合併掃描按鈕）**
  - `:389` `scan-btn`：`onclick="scanBothNow()"`，文字 `立即掃描`，`title="先掃低價帶再掃高價帶，一次跑完"`。
  - `:393` `scan-high-btn`：**整行刪除**。
  - JS：`scanNow`（`:2306`）與 `scanHighNow`（`:2331`）合併成一個 `scanBothNow()`，POST `/api/scan-both`，其餘骨架照 `scanNow`（先鎖按鈕、toast、`wasRunning = true`、700ms 後 `pollScanStatus`）。刪掉舊的兩個函式。
  - `renderScanStatus`（`:2223-2280`）：移除所有 `btnHigh` 行；`isHigh` 判斷保留（pill 文字「高價掃描中…」仍有用）；`btn.textContent` 閒置時為 `立即掃描`。
  - `tests/test_scan_high_web.py:140-142`：改成斷言 `id="scan-btn"`、`onclick="scanBothNow()"`，並斷言 `scan-high-btn` **不再出現**、`/api/scan-both` 出現在 index.html。

- [ ] **Step 2（#2 補跑按鈕錯誤提示）**
  `catchupNow`（`:2379`）的 `catch(e)`：若 `e.message` 含 `Not Found`，toast 改成
  `"後端沒有 /api/daily-run：dashboard 服務還在跑舊版，請重啟（launchctl kickstart -k gui/$UID/com.jim.ygosniper.dashboard）後再按"`；其他錯誤維持原文。

- [ ] **Step 3（#3 拿掉重新整理）** `:397` 那顆 `onclick="loadAll()"` 的按鈕整行刪除。`loadAll` 函式本身保留（輪詢與其他地方還在用）。

- [ ] **Step 4（#4、#6 隱藏統計）** `:382` `stat-bundle`、`:383` `stat-comps`、`:384` `stat-drift` 三個元素各加 `hidden` 屬性（渲染函式 `loadStats` 不動，照常寫值，只是看不到）。在元素旁加一行 HTML 註解：`<!-- 2026-10-20 使用者要求隱藏；要復原拿掉 hidden 即可 -->`。

- [ ] **Step 5（#5 匯率顯示）** `loadBreakeven`（`:2179-2188`）的 `:2187` 改成：

```js
  const fxEl = document.getElementById("stat-fx");
  if(b.fx_source === "fallback" || b.fx_source === "config"){
    fxEl.textContent = `⚠️ 匯率用手動值 ${Number(b.fx_jpy_twd).toFixed(3)}`;
    fxEl.title = "線上匯率抓不到，用 config/settings.yaml 的 fx.jpy_twd";
  }else{
    const t = b.fx_updated_at ? new Date(b.fx_updated_at) : null;
    const hhmm = t ? t.toLocaleTimeString("zh-TW", {hour:"2-digit", minute:"2-digit", hour12:false}) : "–";
    fxEl.textContent = `匯率 JPY→TWD ${Number(b.fx_jpy_twd).toFixed(3)} · 更新 ${hhmm}`;
    fxEl.title = `來源 ${b.fx_source}（線上抓到後 12 小時內讀快取）`;
  }
```

  `fxNote`（`:1229-1240`，成本拆解裡的匯率行）若也印 `fx_source` 字樣，同樣改成數值＋時間。

- [ ] **Step 6（#7 狀態分頁只留四個）** `:402` asked_seller、`:404` in_bundle、`:408` bought、`:413` high_value、`:415` rare、`:417` expired 六個分頁按鈕各加 `hidden`。**不刪、不改 data 屬性**（`tests/test_signal_filters.py:133-134` 斷言 `data-bucket` 各出現一次）。分頁 click handler（`:3403-3425`）不動。

- [ ] **Step 7（#8 篩選列分三排）** `#filterbar`（`:425-465`）內部用三個 `<div class="filter-row">` 包起來：
  - 第一排：`data-filter` 四顆（`:427-431`）、`data-quick` 三顆（`:433-442`）、`p-filter`（`:446-447`）。
  - 第二排：`#flag-filter`（`:449`）、`#sort-mode`（`:453`）、`#filter-reset`（`:463`）。
  - 第三排：`#list-count`（`:464`）。
  - CSS 在 `#filterbar` 既有規則旁加：`.filter-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;width:100%}`；若 `#filterbar` 本身是 `display:flex`，改成 `flex-direction:column;align-items:stretch`。
  - **逐字保留** `class="quick" data-quick="noauction"`（`tests/test_signal_filters.py:114` 斷言這個字串）。
  - `syncFilterUI`（`:952`）與事件綁定（`:3432-3451`）靠 id／data 屬性選元素，不用改；改完 grep 一次確認沒有用 `#filterbar > button` 這種結構選擇器。

- [ ] **Step 8（#9 新增「其他」分頁並搬區塊）**
  - `:423`（`data-view="snipe"` 那顆）之後加 `<button class="tab" data-view="other">其他</button>`。
  - 在 `#snipe-view`（`:633` 起）的結束標籤之後新增 `<section id="other-view" style="display:none">…</section>`，內容依序**剪下貼上**：
    1. `div.split`（`:470-498`，含 攤提／破口價／怎麼用 三個 panel）
    2. 關鍵字搜尋 panel（`:526-548`，含 `details#sr-sec`）
    3. 平台研究 `div#vs-panel`（`:550-565`）
  - 鑑價 panel（`:500-524`，`details#apr-sec`）**留在 `#signals-view` 原位**。
  - `setView`（`:2866-2874`）加 `other-view`：`view === "other"` 時只顯示它，其餘三個 view 與 `#filterbar` 隱藏；其他 view 時把 `#other-view` 設 `display:none`。照既有 inline style 的寫法，不要改成 class。
  - 改完 grep 確認：`bundle-total`、`bundle-detail`、`breakeven`、`sr-sec`、`vs-out`、`vs-panel` 各仍恰好出現在 HTML 一次（JS 靠 id 找，搬家不影響）。

- [ ] **Step 9: 語法與測試**

```bash
cd /Users/jim/projects/ygo-sniper && node -e "
const fs=require('fs');const h=fs.readFileSync('web/static/index.html','utf8');
for(const m of h.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)){ try{ new Function(m[1]);}catch(e){ console.log('JS syntax error:', e.message); process.exit(1);} }
console.log('JS syntax OK');"
.venv/bin/pytest tests/test_signal_filters.py tests/test_expiry_banner.py tests/test_hotkeys.py tests/test_card_snipe_web.py tests/test_scan_high_web.py 2>&1 | tail -2
grep -c 'id="scan-high-btn"\|onclick="loadAll()"\|scanHighNow\|function scanNow' web/static/index.html   # 必須是 0
grep -c 'hidden' web/static/index.html   # 比改前多 9（3 統計 + 6 分頁）
make test 2>&1 | tail -3
```

- [ ] **Step 10: Commit**

```bash
git add web/static/index.html tests/test_scan_high_web.py
git commit -m "feat(dashboard): 版面重整——合併掃描按鈕、拿掉重新整理、隱藏漂移/籃子/行情與六個分頁、匯率顯示數值與更新時間、篩選列分三排、新增「其他」分頁"
```

---

> **進度（2026-10-20 23:15 CDT）**：Task 1 `2b9d63c`、Task 2 `2f003c2` 完成。Task 3 主線程：`launchctl kickstart -k` 重啟後新行程 pid 84779，`openapi.json` 含 `/api/scan-both`、`/api/daily-run`、`/api/schedule/gap`；Chrome 實看 8321：一顆「立即掃描」＋「補跑 daily」、無重新整理、統計只剩「待處理 71」與「匯率 JPY→TWD 0.206 · 更新 22:03」、分頁 待處理／已出價／觀察中／全部＋賣家／狙擊／其他、篩選三排、71 張卡正常、鑑價留訊號頁；「其他」分頁含攤提／破口價／怎麼用／關鍵字搜尋／平台研究。全部符合九項。沒有實際按補跑（會再跑一輪推 Telegram），路由存在已由 openapi 證實。

## Task 3（主線程）：重啟 8321 dashboard 服務並用瀏覽器驗收

- [ ] `launchctl kickstart -k gui/$(id -u)/com.jim.ygosniper.dashboard`，`sleep 3`，`curl -s -o /dev/null -w "%{http_code}" localhost:8321/api/schedule/gap` 必須 200（舊行程回 404）。
- [ ] 用 Chrome 開 `http://127.0.0.1:8321`，截圖確認：一顆「立即掃描」、沒有「高價掃描」與「重新整理」、沒有漂移／籃子／行情、匯率顯示數值＋時間、分頁只剩四個、篩選三排、頂層有「其他」且內容五個區塊、鑑價仍在訊號頁。
- [ ] 按「補跑 daily」確認不再 404（會真的跑一輪並推 Telegram；按之前先確認 `data/run_daily.lock` 不存在）。
- [ ] `docs/dashboard.md` 的三顆按鈕段落改成兩顆（主線程順手改，1 檔）。
