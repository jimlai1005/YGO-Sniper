# 請求衛生與行程看門狗 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 comps 已售出查詢從「每 12 輪一次 352 請求爆量」攤平成「每輪一小片 ≤32 請求」以降低 WAF 觸發風險；並讓「醒著卡死的行程」被 watchdog 強制終止、「漏跑的排程時段」下一輪開口出聲。

**Architecture:** 三條互相獨立的防線——(1) `CompsEngine` 新增游標分片（`sold_shard`/`commit_sold_shard`），取代 `claim_sold_run` 的整批節流；(2) `scripts/run_with_timeout.py` 純 stdlib supervisor，用新 session 的 process group 包住 `ygo-sniper daily`，超時 TERM→KILL 整棵行程樹；(3) `waf.py` chromium 啟動逾時斷路器＋`schedule_watch.py` 空窗偵測。

**Tech Stack:** Python 3（stdlib only for supervisor）、pytest、bash（run_daily.sh）、launchd plist（不動）。

---

## 診斷依據（為什麼是這五個 task）

2026-08-01～08-11 的 `data/logs/daily-*.log` 全量分析結論：

1. **buyee_mercari 唯一一次硬 `blocked`（08-10 23:00）與 comps 爆量批次同輪出現**
   （`daily-20260810.log:450-477`）：88 查詢 × 最多 4 頁 ≈ 352 請求一口氣打完。
   9 次同類批次裡 8 次正常——相關證據不強，但攤平爆量同時有第二個好處：
   WAF token 硬 TTL 240s（`waf.py:27`），小分片一顆 token 就夠，重取次數下降。
2. **「壞了 13 小時」大部分不是被擋，是筆電睡眠＋一次 Playwright 啟動逾時**：
   08-10 21:00-22:30 的時段連「本輪跳過」都沒寫（鎖被佔時 `run_daily.sh:55-60`
   必寫這行）→ 機器睡著，launchd 醒來 23:00:44 補發一次；該輪 python 又跨夜
   被凍結到 07:33 才收尾（`daily-20260810.log:450`、`:551`，exit=0）。
   真被 WAF 擋的時間 ≈ 1 輪。
3. **`BrowserType.launch` 逾時 180s 後，同輪還會再燒最多 3 次重取**
   （`daily-20260810.log:453-465`）；且 launch 失敗後遺留的 chromium
   （`<launched> pid=59906`）沒有人負責回收。
4. **漏跑的時段完全無聲**——「機器睡了」「鎖卡住」「launchd 掉單」三種成因
   外顯一模一樣，全部長得像「今天沒好貨」。這是專案第五節的頭號敵人。

## 全域約束（每個 task 都適用）

- **本計畫不碰任何過濾／解析規則**（不動 parsers/、不動排除字、不動 grade 判定），
  所以**不需要跑 corpus-diff**。若實作中發現必須動到，停下來回報，不要順手改。
- 測試零網路零 Telegram（工程原則 4）；conftest 已有防線，新測試不得繞過。
- 基線：`make test` 目前 1738 passed（2026-08-11 實測）。每個 task 結束時
  全量測試必須 ≥ 基線且無新失敗。
- 每個 task 一個 commit，訊息格式照 repo 慣例（中文、`fix:`/`feat:` 前綴）。
- 檔案錨點（行號）以計畫撰寫當下為準；**動手前先 Read 目標段落**，行號漂移就以
  實際內容定位。
- 派工註記：實作 task 用 sonnet（省略 model 即可）；純 read-back 驗證用 haiku。

## 不做什麼（明確排除）

- 不合併「商品／賣家／狙擊」三模組的請求（賣家頁與搜尋頁是不同頁面種類，
  合併＝靜默漏掉賣家的非關鍵字商品，踩專案 CLAUDE.md 第一節紅線）。
- 不修 `parse_grade` 的 PSA/ARS 誤判（CLAUDE.md 第二節第 5 項）——獨立成案。
- 不動 plist 的排程時段、不加自動出價、不加自動調參。

---

### Task 1: CompsEngine 已售出查詢分片（取代整批節流）

**目的**：`every_n_runs=12` 的語意從「每 12 輪跑一次全量」改為「12 輪走完一整份」。
每輪跑 `ceil(總數/12)` 個查詢（88 個 → 每輪 8 個 × ≤4 頁 ≈ 32 請求），游標存
store meta 跨行程推進。游標**只在該分片至少一條查詢成功時**推進（transient 失敗
重試同一片，semantic 上限 3 輪後強制推進並出聲——工程原則 2）。

**Files:**
- Modify: `src/ygo_sniper/comps.py`（`claim_sold_run` 在 :602-635，`SOLD_RUN_COUNTER_KEY` 在 :307）
- Test: `tests/test_comps_queries.py`（既有節流測試要遷移）

- [ ] **Step 1: 盤點既有引用**

Run: `grep -rn "claim_sold_run\|SOLD_RUN_COUNTER_KEY" src/ tests/`
Expected: `comps.py`（定義）、`pipeline.py:268`（呼叫）、`tests/test_comps_queries.py`（import＋節流測試）。
把命中的測試名記下來——Step 2 要改寫它們，Task 2 才改 pipeline。

- [ ] **Step 2: 寫失敗測試（追加到 tests/test_comps_queries.py 尾端）**

沿用檔內既有的 `_engine` helper、`_SoldSource`、`cfg` fixture 與 Store 建構寫法
（先看檔內既有「跨行程節流」測試怎麼建 Store，照抄同一種建法）：

```python
# ---------------------------------------------------------------------------
# 3. 已售出查詢分片（游標輪替，取代整批節流）
# ---------------------------------------------------------------------------
def _sold_sources():
    return {"buyee_mercari": _SoldSource("buyee_mercari")}


def _kw(shard):
    return [k for _, k in shard.queries]


def test_sold_shard_walks_list_and_wraps(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    eng = _engine(
        cfg,
        {"extra": ["kw0", "kw1", "kw2", "kw3", "kw4"], "every_n_runs": 2},
        store=store,
    )
    s1 = eng.sold_shard(_sold_sources())
    assert _kw(s1) == ["kw0", "kw1", "kw2"]  # ceil(5/2)=3
    eng.commit_sold_shard(s1, any_success=True)
    s2 = eng.sold_shard(_sold_sources())
    assert _kw(s2) == ["kw3", "kw4"]
    eng.commit_sold_shard(s2, any_success=True)
    assert _kw(eng.sold_shard(_sold_sources())) == ["kw0", "kw1", "kw2"]  # 繞回


def test_sold_shard_cursor_survives_process_restart(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=True)
    # 新行程 = 新 engine，同一個 store
    eng2 = _engine(cfg, {"extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    assert _kw(eng2.sold_shard(_sold_sources())) == ["c", "d"]


def test_sold_shard_does_not_advance_on_total_failure_then_force_advances(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    first = eng.sold_shard(_sold_sources())
    eng.commit_sold_shard(first, any_success=False)
    assert _kw(eng.sold_shard(_sold_sources())) == _kw(first)  # 第 1 次失敗：原地重試
    eng.commit_sold_shard(first, any_success=False)
    assert _kw(eng.sold_shard(_sold_sources())) == _kw(first)  # 第 2 次失敗：仍原地
    eng.commit_sold_shard(first, any_success=False)            # 第 3 次：強制推進
    assert _kw(eng.sold_shard(_sold_sources())) != _kw(first)


def test_sold_shard_force_returns_full_list_and_resets_cursor(cfg, tmp_path):
    store = Store(tmp_path / "comps.db")
    eng = _engine(cfg, {"extra": ["a", "b", "c", "d"], "every_n_runs": 2}, store=store)
    eng.commit_sold_shard(eng.sold_shard(_sold_sources()), any_success=True)  # 游標→2
    forced = eng.sold_shard(_sold_sources(), force=True)
    assert _kw(forced) == ["a", "b", "c", "d"]
    eng.commit_sold_shard(forced, any_success=True)
    assert _kw(eng.sold_shard(_sold_sources())) == ["a", "b"]  # 游標已歸零


def test_sold_shard_without_store_runs_everything(cfg):
    eng = _engine(cfg, {"extra": ["a", "b", "c"], "every_n_runs": 2}, store=None)
    shard = eng.sold_shard(_sold_sources())
    assert _kw(shard) == ["a", "b", "c"]
    eng.commit_sold_shard(shard, any_success=True)  # 不得炸
```

- [ ] **Step 3: 跑新測試確認失敗**

Run: `.venv/bin/pytest tests/test_comps_queries.py -k sold_shard -v`
Expected: FAIL，`AttributeError: 'CompsEngine' object has no attribute 'sold_shard'`

- [ ] **Step 4: 實作（comps.py）**

把 `:305-307` 的常數區改成：

```python
#: 分片游標：下一輪從展開後清單的第幾個 (source, keyword) 開始跑。
#: 存 store meta（跨行程、跨重啟都要記得住，用記憶體變數等於沒節流）。
SOLD_CURSOR_KEY = "comps_sold_cursor"
#: 同一分片連續整片失敗的次數；達 3 次就強制推進游標並出聲，避免壞片卡死輪替。
SOLD_STALL_KEY = "comps_sold_stall"
SOLD_STALL_LIMIT = 3
```

（`SOLD_RUN_COUNTER_KEY` 刪除；store 裡的舊值留著無害。）

在 `IngestReport` 附近新增 dataclass：

```python
@dataclass(slots=True)
class SoldShard:
    """一輪要跑的已售出查詢分片。`next_cursor is None` = 不推進游標
    （force 全量、無 store、every_n_runs<=1 的「每輪全跑」情境）。"""

    queries: list[tuple[str, str]]
    label: str
    next_cursor: int | None
```

把 `claim_sold_run`（:602-635）整個換成：

```python
    def sold_shard(self, sources, *, force: bool = False) -> SoldShard:
        """這一輪要跑哪一片已售出查詢。

        舊制是「每 every_n_runs 輪跑一次全量」——一口氣 88 查詢 × 4 頁
        ≈ 352 請求，是全 log 唯一與硬 blocked 同輪出現過的批次形態
        （daily-20260810.log:450-477）。改成每輪走 ceil(N/every) 個，
        every 輪走完一整份：對方看到的是穩定小流量，每查詢的更新頻率不變，
        而且一片塞得進一顆 WAF token 的 240s 預算。

        游標推進在 `commit_sold_shard`（呼叫端跑完才知道成敗）；
        `force=True` 回全量並在 commit 時把游標歸零（人工逃生門）。
        """
        spec = self.cfg.watchlist.get("comps_queries") or {}
        every = int(spec.get("every_n_runs", 1) or 1)
        all_q = self.sold_queries(sources)
        if not all_q:
            return SoldShard([], "", None)
        if force:
            return SoldShard(
                all_q, "force：整份全跑，游標歸零",
                0 if self.store is not None else None,
            )
        if every <= 1 or self.store is None:
            return SoldShard(all_q, "", None)
        try:
            cursor = int(self.store.get_meta(SOLD_CURSOR_KEY) or 0)
        except (TypeError, ValueError):
            cursor = 0
        cursor %= len(all_q)
        size = -(-len(all_q) // every)  # ceil
        shard = all_q[cursor : cursor + size]
        nxt = (cursor + len(shard)) % len(all_q)
        n_shards = -(-len(all_q) // size)
        label = (
            f"分片 {cursor // size + 1}/{n_shards}"
            f"（游標 {cursor}→{nxt}，全份 {len(all_q)} 查詢）"
        )
        return SoldShard(shard, label, nxt)

    def commit_sold_shard(self, shard: SoldShard, *, any_success: bool) -> None:
        """跑完一片之後推進游標。**整片全失敗不推進**（transient 下一輪原地
        重試），連續 SOLD_STALL_LIMIT 輪整片失敗就強制推進並出聲——
        不然一個壞掉的查詢會永遠卡住整個輪替（工程原則 2：transient 重試、
        semantic 不重試，這裡用「連續失敗上限」區分兩者）。"""
        if shard.next_cursor is None or self.store is None:
            return
        if any_success:
            self.store.set_meta(SOLD_CURSOR_KEY, str(shard.next_cursor))
            self.store.set_meta(SOLD_STALL_KEY, "0")
            return
        try:
            stall = int(self.store.get_meta(SOLD_STALL_KEY) or 0) + 1
        except (TypeError, ValueError):
            stall = 1
        if stall >= SOLD_STALL_LIMIT:
            print(
                f"[comps] ⚠️ 同一分片連續 {stall} 輪整片失敗，強制推進游標"
                "（跳過這片；若持續發生代表來源整體壞了，看來源告警）"
            )
            self.store.set_meta(SOLD_CURSOR_KEY, str(shard.next_cursor))
            self.store.set_meta(SOLD_STALL_KEY, "0")
        else:
            self.store.set_meta(SOLD_STALL_KEY, str(stall))
```

- [ ] **Step 5: 遷移舊節流測試**

Step 1 記下的既有 `claim_sold_run`／`SOLD_RUN_COUNTER_KEY` 測試：行為已被上面五個
新測試覆蓋者直接刪除；測「force 也消耗配額」這類已無對應語意的，刪除並在 commit
訊息註明。`from ygo_sniper.comps import` 行同步改（`SOLD_RUN_COUNTER_KEY` →
`SOLD_CURSOR_KEY`，若測試還要用）。

- [ ] **Step 6: 跑測試**

Run: `.venv/bin/pytest tests/test_comps_queries.py -v`
Expected: 全綠（此時 pipeline.py 還在叫 `claim_sold_run`，全量測試會紅——Task 2 修，
所以**這個 task 先不跑 make test、不 commit**，與 Task 2 合併為一個 commit）。

---

### Task 2: pipeline 接上分片

**Files:**
- Modify: `src/ygo_sniper/pipeline.py:252-303`（`refresh_comps`）

- [ ] **Step 1: 改寫 refresh_comps**

`:268-277` 的節流閘門＋全量取查詢，換成：

```python
        shard = self.comps.sold_shard(self.sources, force=force)
        if not shard.queries:
            print("[comps] 已售出查詢：展開後為空，跳過")
            self.comps.load_from_store()
            return 0

        pages = self.comps.sold_pages
        suffix = f"（{shard.label}）" if shard.label else ""
        print(f"[comps] 跑 {len(shard.queries)} 個已售出查詢 × 最多 {pages} 頁{suffix}")
```

`:282-291` 的迴圈改為記錄成敗（`queries` → `shard.queries`）：

```python
        any_success = False
        for source_name, keyword in shard.queries:
            src = self.sources[source_name]
            try:
                sold = src.search(keyword, sold=True, pages=pages)
            except Exception as exc:  # noqa: BLE001 - 隔離是刻意的，見 docstring
                print(
                    f"[warn] comps {source_name} 「{keyword}」失敗，跳過："
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            any_success = True
```

迴圈結束後、`self.comps.load_from_store()` 之前加：

```python
        self.comps.commit_sold_shard(shard, any_success=any_success)
```

docstring 的「先過節流閘門」段落改寫成分片語意（每輪一小片、every_n_runs 輪走完
一整份、游標成功才推進）。

- [ ] **Step 2: 全量測試**

Run: `make test`
Expected: ≥ 1738 passed（新增 6 個分片測試），0 failed。若有 pipeline 相關測試
引用舊訊息字串（「跳過已售出查詢」），依新行為更新斷言。

- [ ] **Step 3: Commit**

```bash
git add src/ygo_sniper/comps.py src/ygo_sniper/pipeline.py tests/test_comps_queries.py
git commit -m "feat(comps): 已售出查詢分片輪替取代整批節流——每輪 ≤1/12 份、游標成功才推進"
```

---

### Task 3: waf.py chromium 啟動逾時斷路器

**目的**：`BrowserType.launch` 預設 180s 逾時；08-10 實測失敗後同輪又重試 3 次
（`daily-20260810.log:453-465`）。降為 60s、失敗一次就整輪斷路——啟動失敗是
本機環境問題（semantic for this run），重試只會燒時間。

**Files:**
- Modify: `src/ygo_sniper/sources/waf.py`（`_acquire` :59-95、`_refresh` :98-128、`__init__` :47-56）
- Test: `tests/test_waf_launch.py`（新檔；若已有 test_waf*.py 就併入該檔）

- [ ] **Step 1: 寫失敗測試**

```python
"""chromium 啟動失敗的斷路器：一輪內失敗一次就不再嘗試開瀏覽器。零網路。"""

import pytest

from ygo_sniper.sources.waf import BrowserLaunchError, WafSession
from ygo_sniper.sources.base import BlockedError


def test_launch_failure_trips_circuit_breaker(cfg, monkeypatch):
    ws = WafSession(cfg)
    calls = {"n": 0}

    def boom(seed_url):
        calls["n"] += 1
        raise BrowserLaunchError("chromium 啟動失敗（TimeoutError: 180000ms）")

    monkeypatch.setattr(ws, "_acquire", boom)
    with pytest.raises(BlockedError):
        ws._refresh("https://buyee.jp/x")
    with pytest.raises(BlockedError, match="稍早"):
        ws._refresh("https://buyee.jp/y")
    assert calls["n"] == 1  # 第二次沒有再開瀏覽器


def test_non_launch_blocked_does_not_trip_breaker(cfg, monkeypatch):
    ws = WafSession(cfg)
    calls = {"n": 0}

    def no_cookie(seed_url):
        calls["n"] += 1
        raise BlockedError("沒拿到 aws-waf-token cookie", url=seed_url)

    monkeypatch.setattr(ws, "_acquire", no_cookie)
    with pytest.raises(BlockedError):
        ws._refresh("https://buyee.jp/x")
    with pytest.raises(BlockedError):
        ws._refresh("https://buyee.jp/y")
    assert calls["n"] == 2  # 「拿不到 cookie」是 WAF 側的事，照舊逐次重試
```

（`cfg` fixture 沿用 conftest 既有寫法；若 conftest 無此 fixture，照
test_comps_queries.py 取得 cfg 的方式建。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_waf_launch.py -v`
Expected: FAIL，`ImportError: cannot import name 'BrowserLaunchError'`

- [ ] **Step 3: 實作**

模組層加常數與例外（放 `MAX_REFRESHES_PER_RUN` 附近）：

```python
#: chromium 啟動逾時。預設 180s 太久：排程輪 30 分一班，180s × 4 次重取
#: 就吃掉 12 分鐘。啟動失敗是本機問題（記憶體、殘骸行程），60s 起不來就不會起來了。
LAUNCH_TIMEOUT_MS = 60_000


class BrowserLaunchError(BlockedError):
    """chromium 本體起不來（≠ WAF 擋）。一輪內發生一次就斷路：
    後續重取直接失敗，不再各燒一次啟動逾時。"""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message, url=url)
```

`__init__` 加一行：

```python
        self._launch_failed = False
```

`_acquire` 的 launch 行（:70）改成：

```python
            try:
                browser = pw.chromium.launch(headless=True, timeout=LAUNCH_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001 - 啟動失敗的型別來自 playwright 深處，統一收斂
                raise BrowserLaunchError(
                    f"chromium 啟動失敗（{type(exc).__name__}: {exc}）", url=seed_url
                ) from exc
```

（注意：這行原本就在 `with sync_playwright() as pw:` 裡、`try/finally browser.close()`
之外——保持這個結構，`with` 的 `__exit__` 仍負責關 driver。）

`_refresh` 的開頭（上限檢查之後、年齡計算之前）加斷路檢查；`_acquire` 呼叫處
接住 `BrowserLaunchError`：

```python
        if self._launch_failed:
            raise BlockedError(
                "本輪稍早 chromium 啟動失敗，跳過後續 token 重取（斷路器）",
                url=seed_url,
            )
```

```python
        try:
            token, html = self._acquire(seed_url)
        except BrowserLaunchError:
            self._launch_failed = True
            raise
        except ImportError as exc:
            # （原有的 ImportError 分支保持不動）
            raise BlockedError(_PLAYWRIGHT_HINT, url=seed_url) from exc
```

- [ ] **Step 4: 跑測試**

Run: `.venv/bin/pytest tests/test_waf_launch.py -v && make test`
Expected: 新測試 2 passed；全量 ≥ Task 2 的通過數，0 failed。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/sources/waf.py tests/test_waf_launch.py
git commit -m "fix(waf): chromium 啟動逾時降 60s＋斷路器——啟動失敗一次就整輪停止重取"
```

---

### Task 4: 行程看門狗（supervisor 殺整棵行程樹）

**目的**：08-10 的 python 行程在工作全部完成後仍存活數小時（`daily-20260810.log:551`，
夾雜睡眠凍結）。醒著卡死不得超過 25 分鐘：supervisor 把 daily 放進新 session
（獨立 process group），超時 TERM → 30s 後 KILL 整組——Playwright 殭屍、node driver、
chromium 全在同一組裡，一次收乾淨。macOS 的 `time.monotonic` 睡眠時停走，
所以筆電睡眠**不會**誤觸發。

**Files:**
- Create: `scripts/run_with_timeout.py`
- Modify: `scripts/run_daily.sh:88`（`ygo-sniper daily` 那行）與 :90-101（失敗通知分支）
- Test: `tests/test_run_with_timeout.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""supervisor 的行為測試：真開子行程（sh/sleep），但零網路。"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_with_timeout.py"


def _run(timeout: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), timeout, *cmd],
        capture_output=True, text=True, timeout=30,
    )


def test_exit_code_passthrough():
    assert _run("30", "sh", "-c", "exit 7").returncode == 7


def test_timeout_returns_124_and_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "pid"
    r = _run("1", "sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait")
    assert r.returncode == 124
    assert "watchdog" in r.stdout
    gpid = int(pidfile.read_text())
    time.sleep(0.3)  # 給 TERM 一點傳遞時間
    with pytest.raises(ProcessLookupError):
        os.kill(gpid, 0)  # 孫行程（sleep 60）也必須死
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_run_with_timeout.py -v`
Expected: FAIL（腳本不存在）

- [ ] **Step 3: 寫 supervisor**

`scripts/run_with_timeout.py`（純 stdlib，不 import 專案任何東西——它要在
任何 venv 狀態下都能跑）：

```python
#!/usr/bin/env python3
"""用法: run_with_timeout.py <timeout_seconds> <cmd> [args...]

把 cmd 放進自己的新 session（＝新 process group），超時先 SIGTERM 整組、
30s 內不退再 SIGKILL 整組。比 shell 背景工作可靠的原因：Playwright 的
node driver 與 chromium 是 cmd 的子孫，同組一次殺乾淨，不會留殭屍
佔著 log fd 或記憶體（2026-08-10 事故：launch 逾時後 pid=59906 沒人收）。

exit code：子行程正常結束就沿用；被 watchdog 終止回 124（比照 GNU timeout，
run_daily.sh 據此發「watchdog 終止」而非一般「掃描失敗」通知）。

計時用 subprocess.wait(timeout)（底層 time.monotonic）：macOS 睡眠時停走，
筆電闔蓋不會吃掉預算——watchdog 只抓「醒著卡死」。
"""
import os
import signal
import subprocess
import sys

GRACE_SECONDS = 30


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: run_with_timeout.py <timeout_seconds> <cmd> [args...]", file=sys.stderr)
        return 2
    timeout = float(sys.argv[1])
    proc = subprocess.Popen(sys.argv[2:], start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    print(
        f"🚨 [watchdog] 超過 {timeout:.0f}s 未結束，強制終止整個行程樹"
        f"（pgid={proc.pid}）——醒著卡死，可能是 Playwright 殭屍或網路黑洞",
        flush=True,
    )
    _killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    return 124


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑測試**

Run: `.venv/bin/pytest tests/test_run_with_timeout.py -v`
Expected: 2 passed（第二個測試 ~2s 內完成）

- [ ] **Step 5: 接進 run_daily.sh**

`scripts/run_daily.sh:88` 的

```bash
ygo-sniper daily >> "$LOG_FILE" 2>&1
```

改成：

```bash
# watchdog：醒著卡死不得超過 25 分（睡眠凍結不計入，見 run_with_timeout.py）。
# 124 = 被 watchdog 終止，下面的失敗通知會用不同文案。
python scripts/run_with_timeout.py "${YGO_CYCLE_TIMEOUT:-1500}" \
    ygo-sniper daily >> "$LOG_FILE" 2>&1
```

失敗通知分支（:90-101）的 `-d "text=⚠️ …"` 改成先組訊息再送：

```bash
if [ $STATUS -ne 0 ]; then
    echo "[$(date '+%H:%M:%S')] 掃描失敗，exit=$STATUS" >> "$LOG_FILE"
    if [ "$STATUS" -eq 124 ]; then
        MSG="🚨 ygo-sniper 本輪被 watchdog 強制終止（超過 ${YGO_CYCLE_TIMEOUT:-1500}s），可能 Playwright 卡死，請看 data/logs/"
    else
        MSG="⚠️ ygo-sniper 掃描失敗 (exit=${STATUS})，請看 data/logs/"
    fi
    # 失敗一定要主動告訴你。沉默的失敗是這類排程工具最大的坑：
    # 你會連續三週以為市場沒好貨，其實是 parser 早就掛了。
    if [ -f .env ]; then
        # shellcheck disable=SC1091
        source .env
        curl -s -X POST \
            "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            -d "text=${MSG}" \
            > /dev/null
    fi
fi
```

- [ ] **Step 6: 驗證 shell 與 supervisor 實跑**

Run: `bash -n scripts/run_daily.sh && python scripts/run_with_timeout.py 2 sleep 30; echo "exit=$?"`
Expected: 無語法錯誤；~2s 後印出 watchdog 訊息、`exit=124`。
（**不要**實跑 run_daily.sh 本體——它會打真網路、可能發真 Telegram。）

- [ ] **Step 7: Commit**

```bash
git add scripts/run_with_timeout.py scripts/run_daily.sh tests/test_run_with_timeout.py
git commit -m "feat(schedule): watchdog supervisor——醒著卡死 25 分強制殺整棵行程樹，exit=124 專屬告警"
```

---

### Task 5: 排程空窗偵測（漏跑要出聲）

**目的**：機器睡眠、鎖卡死、launchd 掉單——漏跑的時段目前零痕跡。每輪開始時
比對「上一輪開始時間」與排程表的預期間隔，超過就大聲印出＋推播；上一輪
「有開始沒結束」（當機／watchdog 殺掉）也在這裡報。偵測是**邊緣觸發**
（比對後立即更新基準），天然不重複告警，不需要 dedup 機制。

**Files:**
- Create: `src/ygo_sniper/schedule_watch.py`
- Modify: `src/ygo_sniper/pipeline.py`（`scan()` 的開頭與結尾，:754 附近起）
- Modify: `src/ygo_sniper/cli.py`（daily 的告警送出段，:240-252 附近）
- Test: `tests/test_schedule_watch.py`

- [ ] **Step 1: 對齊排程表**

Run: `grep -n -A3 "Hour\|Minute" scripts/com.jim.ygosniper.plist | head -80`
Expected: 09:30-17:30 每 2 小時＋18:00-22:30 每 30 分（與 CLAUDE.md 第九節一致）。
若實際 plist 不同，以 plist 為準調整 Step 2 的 `_WINDOWS`。

- [ ] **Step 2: 寫失敗測試**

```python
"""排程空窗偵測：純函式、假時間，零網路零 store。"""

from datetime import datetime

from ygo_sniper.schedule_watch import expected_next_gap_minutes, gap_alert


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def test_expected_gap_daytime_is_two_hours():
    assert expected_next_gap_minutes(_dt("2026-08-11T09:30:00")) == 120


def test_expected_gap_evening_is_thirty_minutes():
    assert expected_next_gap_minutes(_dt("2026-08-11T19:00:00")) == 30


def test_expected_gap_last_evening_slot_spans_overnight():
    # 22:30 之後的下一班是隔天 09:30 = 660 分鐘
    assert expected_next_gap_minutes(_dt("2026-08-11T22:30:00")) == 660


def test_no_alert_on_normal_cadence():
    assert gap_alert(
        "2026-08-11T19:00:00", "2026-08-11T19:03:00", _dt("2026-08-11T19:30:02")
    ) is None


def test_no_alert_on_overnight_window():
    assert gap_alert(
        "2026-08-11T22:30:00", "2026-08-11T22:35:00", _dt("2026-08-12T09:30:05")
    ) is None


def test_alert_when_evening_slots_were_skipped():
    # 08-10 事故的形狀：20:49 之後直接跳到 23:00（21:00-22:30 四班消失）
    msg = gap_alert(
        "2026-08-10T20:00:00", "2026-08-10T20:49:00", _dt("2026-08-10T23:00:44")
    )
    assert msg is not None and "空窗" in msg


def test_alert_when_previous_run_never_finished():
    msg = gap_alert(
        "2026-08-11T19:00:00", "2026-08-11T18:33:00", _dt("2026-08-11T19:30:00")
    )
    assert msg is not None and "收尾" in msg


def test_first_run_has_no_baseline():
    assert gap_alert(None, None, _dt("2026-08-11T09:30:00")) is None
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_schedule_watch.py -v`
Expected: FAIL（模組不存在）

- [ ] **Step 4: 實作 schedule_watch.py**

```python
"""排程空窗偵測：「該跑的時段沒跑」要出聲。

漏跑的三種成因（機器睡眠、鎖被殘留行程卡住、launchd 掉單）外顯完全相同——
log 裡就是少幾行，與「今天沒好貨」無法區分。唯一可靠的偵測點是
**下一次成功跑起來的那一輪**：拿上一輪的開始時間對照排程表，超過預期就報。

邊緣觸發：報完立刻更新基準，同一個空窗只會被報一次，不需要 dedup。
預期間隔刻意抓寬（窗尾直接跳到下一個窗頭），寧可漏報小漂移，
不要每次 launchd 晚 15 分就叫——實測喚醒漂移 ~15 分（08-10 15:45:30）。
"""

from __future__ import annotations

from datetime import datetime

RUN_STARTED_KEY = "schedule_run_started_at"
RUN_FINISHED_KEY = "schedule_run_finished_at"

#: 排程表（與 scripts/com.jim.ygosniper.plist 的 StartCalendarInterval 對齊；
#: 改 plist 時段時這裡要一起改——兩處不同步的症狀是空窗告警亂叫或不叫）。
#: (窗起分鐘, 窗迄分鐘, 間隔分鐘)；22:30 之後到隔日 09:30 是刻意的夜間空窗。
_WINDOWS = [(9 * 60 + 30, 17 * 60 + 30, 120), (18 * 60, 22 * 60 + 30, 30)]

#: launchd 喚醒漂移的寬限。
_SLACK_MINUTES = 20


def expected_next_gap_minutes(prev: datetime) -> int:
    """上一輪在 prev 開始，下一輪「最晚」該在幾分鐘內開始（不含寬限）。"""
    m = prev.hour * 60 + prev.minute
    for lo, hi, step in _WINDOWS:
        if lo <= m < hi and m + step <= hi:
            return step
    starts = sorted(lo for lo, _hi, _step in _WINDOWS)
    for lo in starts:
        if m < lo:
            return lo - m
    return (24 * 60 - m) + starts[0]  # 跨夜到明天第一班


def gap_alert(
    prev_started: str | None, prev_finished: str | None, now: datetime
) -> str | None:
    """回傳要出聲的訊息，或 None（一切正常／沒有基準）。"""
    if not prev_started:
        return None
    try:
        prev = datetime.fromisoformat(prev_started)
    except ValueError:
        return None

    msgs: list[str] = []

    finished_ok = False
    if prev_finished:
        try:
            finished_ok = datetime.fromisoformat(prev_finished) >= prev
        except ValueError:
            pass
    if not finished_ok:
        msgs.append(
            f"上一輪（{prev:%m-%d %H:%M} 開始）沒有正常收尾——"
            "當機、被 kill 或 watchdog 終止，請看 data/logs/"
        )

    expected = expected_next_gap_minutes(prev)
    actual_min = (now - prev).total_seconds() / 60
    if actual_min > expected + _SLACK_MINUTES:
        msgs.append(
            f"排程空窗 {actual_min / 60:.1f} 小時（上輪 {prev:%m-%d %H:%M}，"
            f"預期 {expected} 分內接棒）——可能筆電睡眠漏跑或鎖被卡住"
        )

    return "🚨 排程監督：" + "；".join(msgs) if msgs else None
```

- [ ] **Step 5: 跑測試**

Run: `.venv/bin/pytest tests/test_schedule_watch.py -v`
Expected: 8 passed

- [ ] **Step 6: 接進 pipeline.scan（記帳＋印出）**

先 Read `pipeline.py` 的 `scan()` 開頭（:754 附近）與結尾（組 report/return 處）。
開頭在 `dry_run` 判定可用之後加：

```python
        from .schedule_watch import RUN_FINISHED_KEY, RUN_STARTED_KEY, gap_alert

        self._schedule_alert: str | None = None
        if not dry_run and self.store is not None:
            self._schedule_alert = gap_alert(
                self.store.get_meta(RUN_STARTED_KEY),
                self.store.get_meta(RUN_FINISHED_KEY),
                datetime.now(),
            )
            if self._schedule_alert:
                print(self._schedule_alert)
            self.store.set_meta(RUN_STARTED_KEY, datetime.now().isoformat())
```

scan 正常結束、組完 report 之後（return 前）加：

```python
        if not dry_run and self.store is not None:
            self.store.set_meta(RUN_FINISHED_KEY, datetime.now().isoformat())
```

（`datetime` 若未 import，補 `from datetime import datetime`。）

- [ ] **Step 7: daily 推播接線**

先 Read `src/ygo_sniper/alerts.py:58-90`（`Alert.__new__` 的實際簽名）與
`src/ygo_sniper/notify.py` 的 `send_alert`。目標行為：daily 的告警送出段
（cli.py:240-252 附近）在來源告警之外，若 `pipe._schedule_alert` 非 None
就用與來源告警**同一條送出路徑**推播一則。以 Read 到的實際簽名建構 Alert；
若 `send_alert` 實際上只吃字串內容（`Alert` 是 str 子類），直接傳即可。
scan 指令維持只印不發（與「告警預覽（daily 會送出、scan 不發）」慣例一致）。

- [ ] **Step 8: 全量測試**

Run: `make test`
Expected: ≥ Task 4 的通過數 + 8，0 failed

- [ ] **Step 9: Commit**

```bash
git add src/ygo_sniper/schedule_watch.py src/ygo_sniper/pipeline.py src/ygo_sniper/cli.py tests/test_schedule_watch.py
git commit -m "feat(schedule): 排程空窗與上輪未收尾偵測——漏跑的時段下一輪開口出聲"
```

---

### Task 6（可選，建議做）: 逐請求時間戳記錄

**目的**：目前 log 只有輪級 banner，回答不了「兩個請求間隔多久」「對方回什麼
status」。在 `CachedFetcher`（工程原則 5 的單一 resilience boundary）加一行
`[req]` log，之後「是不是被節流」就有逐請求證據，不用再猜。

**Files:**
- Modify: `src/ygo_sniper/sources/base.py`（`CachedFetcher.get` :212 起、`_check` :170 起）
- Test: `tests/test_fetcher.py`（追加）

- [ ] **Step 1: Read `src/ygo_sniper/sources/base.py:149-288` 全段**，
  確認 `get` 的重試迴圈結構與 `tests/test_fetcher.py` 的假 transport 寫法。

- [ ] **Step 2: 寫失敗測試（追加到 test_fetcher.py，沿用檔內既有 fetcher fixture）**

```python
def test_request_log_line_per_network_attempt(capsys, ...):  # fixture 照檔內慣例
    # 安排一次成功的網路請求（非快取命中）
    ...
    out = capsys.readouterr().out
    assert "[req] " in out          # 有逐請求行
    assert "ms" in out              # 有耗時
    # 快取命中不記：再取一次同 URL，[req] 行數不變
```

（`...` 處依 test_fetcher.py 既有的建構方式補齊——該檔已有完整的假 transport
與 cfg fixture，照抄鄰近測試的 arrange 段即可；這是唯一允許「照檔內慣例補」的
地方，因為 fixture 形狀必須以檔內現況為準。）

- [ ] **Step 3: 實作**

`base.py` 模組層：

```python
#: 逐請求 log。預設開（它是「被節流了嗎」唯一的一手證據）；
#: YGO_REQ_LOG=0 可關。只記真的出網的請求，快取命中不記。
_REQ_LOG = os.environ.get("YGO_REQ_LOG", "1") != "0"


def _log_request(url: str, outcome: object, started: float) -> None:
    if not _REQ_LOG:
        return
    parts = urlsplit(url)
    ms = (time.monotonic() - started) * 1000
    q = f"?{parts.query[:80]}" if parts.query else ""
    print(
        f"[req] {datetime.now().isoformat(timespec='seconds')} "
        f"{parts.netloc} {outcome} {ms:.0f}ms {parts.path}{q}",
        flush=True,
    )
```

（`os`／`urlsplit`／`datetime` 依檔內既有 import 情況補。）
在 `get` 的重試迴圈裡，**每一次實際發出的請求**拿到 response 後呼叫
`_log_request(url, resp.status_code, t0)`；每個 except 分支（重試前）呼叫
`_log_request(url, type(exc).__name__, t0)`；`t0 = time.monotonic()` 在發出前取。
快取命中路徑不呼叫。

- [ ] **Step 4: 跑測試 ＋ 全量**

Run: `.venv/bin/pytest tests/test_fetcher.py -v && make test`
Expected: 全綠

- [ ] **Step 5: 確認 eBay 路徑的涵蓋**

Run: `grep -n "CachedFetcher\|httpx" src/ygo_sniper/sources/ebay.py | head -10`
若 eBay 走自己的 httpx client（不經 CachedFetcher），在其請求點加同一個
`_log_request` 呼叫（from .base import）；若走 CachedFetcher 則已涵蓋，不用動。
**注意**：eBay 的 Authorization 在 header 不在 URL，`_log_request` 只印
netloc/path/query，不印 header——確認新增呼叫點沒有把 token 帶進 log。

- [ ] **Step 6: Commit**

```bash
git add src/ygo_sniper/sources/base.py src/ygo_sniper/sources/ebay.py tests/test_fetcher.py
git commit -m "feat(fetch): 逐請求 [req] log——時間戳/host/status/耗時，被節流與否從此有一手證據"
```

---

## 最終驗收（主對話親自執行，不委派）

1. `make test` 全綠、通過數 ≥ 1738＋新增測試數，貼輸出末尾。
2. `git log --oneline -6` 確認 5-6 個 commit 落地。
3. `python scripts/run_with_timeout.py 2 sleep 30; echo $?` 親眼看到 124。
4. 觀察期（使用者）：下一個排程日 `make logs` 應看到每輪
   `[comps] 跑 N 個已售出查詢 × 最多 4 頁（分片 i/12…）`；故意闔蓋跳過一班後，
   下一輪應出現 `🚨 排程監督：排程空窗…`。

## 風險與回滾

- 分片游標壞掉的最壞情況＝某些查詢延後最多一輪才跑到（行情資料是累積制，
  晚到不丟失）；回滾＝revert Task 1+2 的 commit。
- watchdog 誤殺的最壞情況＝一輪掃描被砍（下一班 30 分後重來，upsert 冪等）；
  `YGO_CYCLE_TIMEOUT` 可加大。observation batch 是輪末整批寫入，被砍的輪
  沒有寫入就沒有「缺席證據」，不會造成誤判離場（healthy 旗標機制，
  `pipeline.py:809-813`）。
- 空窗告警誤叫＝多收一則 Telegram，邊緣觸發不會連環叫；閾值在
  `_SLACK_MINUTES` 一處可調。
