# 估價快取＋略過樂觀更新 Implementation Plan

> **For agentic workers:** 逐 task 派工執行（builder／@inline）。主線程逐 task 親跑驗收指令後才派下一個。步驟用 checkbox 追蹤。

**Goal:** dashboard 按「略過」從「秒級等待」變成即時：估價改成掃描收尾算一次、落庫快取，`/api/signals` 只讀不算；前端狀態變更改樂觀更新，不再整頁重載。

**Architecture:** 新表 `signal_valuations` 存每列的 P 值／公允價／轉賣路徑 JSON。寫入端只有三個：`Pipeline.scan()` 收尾（四個掃描入口共用這一個方法：CLI `daily`／`daily-high`、dashboard 兩顆掃描按鈕）、與手動 `ygo-sniper revalue`。讀取端（`/api/signals`）永遠只讀。前端 `setState` 改為本地移除＋非同步 POST，失敗才回頭向後端對帳。

**Tech Stack:** Python 3.12 / FastAPI / SQLite（WAL）/ 原生 JS（index.html 單檔）/ pytest（前端純函式沿用「抽標記區塊丟 node 實跑」慣例）。

---

## 背景與量測（2026-08-24，主線程實測）

| 項目 | 數字 |
|---|---|
| `POST /api/signals/{key}/state`（略過本身） | 0.004s |
| `/api/signals` 30 列（估價模型暖） | 0.08s |
| `/api/signals` 30 列（模型冷、重建） | 0.74s |
| `/api/signals` 1000 列 | 3.04s，回應 6.5MB（≈6.5KB/列） |
| 其他五支 loadAll API | 各 <0.01s |

慢的機制：前端 `setState`（index.html:1318）POST 後呼叫 `loadAll()` 全量重載；
`/api/signals`（web/app.py:173）對每列現算 `estimate_signal_row` ＋ `_resale_for_row`
（≈3ms/列）；估價模型以 comps 筆數當快取鍵，掃描期間幾乎每次點擊都重建（0.65s+）。

## 使用者裁決（2026-08-24，全文照錄語意）

1. **快取只在掃描時更新**（立即掃描／高價掃描／排程掃描；另給手動 `revalue`）。
   切分頁、略過等讀取操作**永不觸發重算**。「切換分頁去找別的內容，一回頭東西
   就不見了」是使用者明確不要的，所以讀取端連 lazy 補算都不做。
2. **估價與讀取非同步分離**：資料沒變就沒有理由重算；算一次快取下來，讀取最快、
   系統負擔最輕。P 值凍結到下一輪掃描是**刻意的取捨**，不是 bug——web/app.py
   既有註解「落庫等於把會過期的機率凍起來」的立場已被此裁決推翻，改註解時註明日期。
3. **略過改樂觀更新**：前端先把卡片移掉（可加小動畫），非同步送 POST；
   體感最順、可以連續略過不必等。

## 紅線（動手前讀專案 CLAUDE.md）

- 本 plan **不動任何過濾／解析規則**，不需要 corpus-diff。
- 比較同源：resale 與 P 值必須出自**同一個 valuator、同一時刻**算好一起落庫
  （維持現狀的同源性，只是搬到掃描時點）。
- 靜默失敗是頭號敵人：快取重算壞掉不准毀掉整輪掃描（推播、排程基準在後面），
  但必須 console 大聲印＋寫 meta，讓 dashboard 的 `valuation_error` 橫條顯示病名。
- 測試絕不能碰正式庫 `data/sniper.db`（TestClient fixture 照抄
  `tests/test_expiry_clear.py:970-1000`，含那條承重斷言）。
- `make test` 不要自己加 `-q`（pyproject 已有一個，疊成 `-qq` 會吃掉綠字）。

---

### Task 1 @inline：store——`signal_valuations` 表＋整批 upsert＋`list_signals` JOIN

**Files:**
- Modify: `src/ygo_sniper/store.py`（`_SCHEMA` 約 :24 起；`list_signals` :836-874）
- Test: `tests/test_valuation_cache_store.py`（新檔）

- [ ] **Step 1: 寫紅燈測試**

```python
"""signal_valuations 快取表：整批 upsert、list_signals 帶回 val_ 欄位。

signal 的塞法照抄 tests/test_expiry_clear.py::_signal_for（Signal 八欄必填、
key 必須是 site:external_id 形狀，理由見該函式 docstring）。
"""
from __future__ import annotations

import pytest

from ygo_sniper.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _signal_for(key: str, *, site: str = "buyee_yahoo"):
    # 照抄 tests/test_expiry_clear.py 的同名 helper（含 listing.key 斷言）。
    ...


def _val_row(key: str, **over):
    row = {
        "key": key, "p_worth_buying": 0.42, "fair_twd": 1234.0,
        "est_level_label": "L1", "resale_json": '{"ok": false, "reason": "stub"}',
        "comps_n": 100, "computed_at": "2026-08-24T00:00:00+00:00",
    }
    row.update(over)
    return row


def test_upsert_then_list_signals_carries_val_columns(store):
    store.upsert_signal(_signal_for("buyee_yahoo:a1"))
    store.upsert_valuations([_val_row("buyee_yahoo:a1")])
    rows = store.list_signals(state="all", limit=10)
    r = {x["key"]: x for x in rows}["buyee_yahoo:a1"]
    assert r["val_p_worth_buying"] == 0.42
    assert r["val_fair_twd"] == 1234.0
    assert r["val_level_label"] == "L1"
    assert r["val_resale_json"] == '{"ok": false, "reason": "stub"}'
    assert r["val_computed_at"] == "2026-08-24T00:00:00+00:00"


def test_upsert_overwrites_same_key(store):
    store.upsert_signal(_signal_for("buyee_yahoo:a1"))
    store.upsert_valuations([_val_row("buyee_yahoo:a1")])
    store.upsert_valuations([_val_row("buyee_yahoo:a1", p_worth_buying=None, fair_twd=None)])
    r = store.list_signals(state="all", limit=10)[0]
    assert r["val_p_worth_buying"] is None  # 新一輪算不出來就要照實蓋掉，不留舊值


def test_signal_without_cache_row_yields_nulls(store):
    store.upsert_signal(_signal_for("buyee_yahoo:nocache"))
    r = store.list_signals(state="all", limit=10)[0]
    assert r["val_p_worth_buying"] is None
    assert r["val_resale_json"] is None
```

- [ ] **Step 2: 跑一次確認紅**（`upsert_valuations` 不存在）

Run: `.venv/bin/pytest tests/test_valuation_cache_store.py -v`
Expected: FAIL（AttributeError: upsert_valuations）

- [ ] **Step 3: 實作**

`_SCHEMA` 追加（放在既有表定義後面，慣例是 `CREATE TABLE IF NOT EXISTS`，
新表不需要 `_migrate_*`）：

```sql
CREATE TABLE IF NOT EXISTS signal_valuations (
    key             TEXT PRIMARY KEY,   -- signals.key；估價是衍生資料，signal 刪不掉所以不設 FK
    p_worth_buying  REAL,               -- NULL = 這一輪算不出來（誠實留白，不是 0）
    fair_twd        REAL,
    est_level_label TEXT,
    resale_json     TEXT,               -- resale_for_row 的整包 dict（JSON 字串）
    comps_n         INTEGER NOT NULL,   -- 算這一批時的 comps 筆數（audit 用，讀取端不比對）
    computed_at     TEXT NOT NULL
);
```

`Store` 新方法（放 `list_signals` 附近）：

```python
def upsert_valuations(self, rows: list[dict[str, Any]]) -> None:
    """整批覆寫估價快取（單一交易）。

    **蓋掉就是蓋掉**：新一輪某列算不出來（值為 NULL）也照寫——留舊值等於
    拿舊 comps 的答案冒充新 comps 的答案（工程原則 1 的混源）。
    """
    with self._conn() as c:
        c.executemany(
            "INSERT INTO signal_valuations (key, p_worth_buying, fair_twd,"
            " est_level_label, resale_json, comps_n, computed_at)"
            " VALUES (:key, :p_worth_buying, :fair_twd, :est_level_label,"
            " :resale_json, :comps_n, :computed_at)"
            " ON CONFLICT(key) DO UPDATE SET"
            " p_worth_buying=excluded.p_worth_buying, fair_twd=excluded.fair_twd,"
            " est_level_label=excluded.est_level_label, resale_json=excluded.resale_json,"
            " comps_n=excluded.comps_n, computed_at=excluded.computed_at",
            rows,
        )
```

`list_signals` 的查詢字串加 JOIN（照 `listing_obs` 的前綴慣例，理由同 :849-852
的註解——兩表同名欄位不加前綴會被靜默覆蓋）：SELECT 清單加

```
"v.p_worth_buying AS val_p_worth_buying, v.fair_twd AS val_fair_twd, "
"v.est_level_label AS val_level_label, v.resale_json AS val_resale_json, "
"v.computed_at AS val_computed_at "
```

FROM 子句改成

```
"FROM signals s LEFT JOIN listing_obs o ON o.key = s.key "
"LEFT JOIN signal_valuations v ON v.key = s.key "
```

- [ ] **Step 4: 跑綠**

Run: `.venv/bin/pytest tests/test_valuation_cache_store.py -v`
Expected: 3 passed

- [ ] **Step 5: 全量回歸＋commit**

Run: `make test`（`list_signals` 的既有使用者不少，JOIN 不能弄壞任何一個）
Expected: 全綠（基線 1855 passed 起跳）

```bash
git add src/ygo_sniper/store.py tests/test_valuation_cache_store.py
git commit -m "feat(store): signal_valuations 估價快取表＋list_signals 帶 val_ 欄位"
```

---

### Task 2 @inline：`valuation_cache.py`——resale 搬家＋整批重算函式

**Files:**
- Create: `src/ygo_sniper/valuation_cache.py`
- Modify: `web/app.py`（刪 `_resale_for_row` :133-170；`/api/signals` 對它的呼叫
  本 task **先不動**——Task 4 才改端點，所以本 task 讓 web 暫時 import 新位置：
  `from ygo_sniper.valuation_cache import resale_for_row`，呼叫點改
  `resale_for_row(valuator, cfg, fx, r, raw)`）
- Test: `tests/test_valuation_cache.py`（新檔）

- [ ] **Step 1: 建模組（先搬 resale，一字不改邏輯）**

```python
"""掃描後的估價快取。

dashboard 的 /api/signals 過去對每一列現算 P 值與轉賣路徑（≈3ms/列），
清單一長、每按一次略過就整份重算（實測 1000 列 3.0 秒＋6.5MB 回應）。
2026-08-24 使用者裁決：估價改「掃描收尾算一次、落庫快取」，讀取端永遠只讀
不算，連 lazy 補算都不做（切分頁不可以改變資料）。寫入端只有三個：
CLI 排程掃描（daily／daily-high）、dashboard 掃描按鈕（同一個
Pipeline.scan() 入口）、手動 `ygo-sniper revalue`。

**取捨（刻意的）**：P 值比較的是「上次掃描的到手成本」與「快取當下那批
comps 撐出的公允價」，快取讓它凍結到下一輪掃描。CLI `ygo-sniper value`
仍是現算——兩者短暫分岔的量就是快取之後 comps 的成長，屬設計取捨
（使用者核可 2026-08-24）。
"""
from __future__ import annotations

import json
import time
from typing import Any

#: dashboard 讀這兩把 meta 鑰匙：橫條顯示病名／清單行顯示快取時間。
VALUATION_CACHE_AT_KEY = "valuation_cache_at"
VALUATION_CACHE_ERROR_KEY = "valuation_cache_error"


def resale_for_row(valuator, cfg, fx, row: dict, raw_payload: str | None) -> dict:
    # 本體自 web/app.py::_resale_for_row 原樣搬入（含 docstring），
    # 只把原本閉包引用的 cfg、fx 改成參數。不改任何邏輯。
    ...
```

`refresh_valuation_cache`（同檔）：

```python
def refresh_valuation_cache(cfg, store, fx, index=None, *, valuator=None) -> dict:
    """整顆 signals 表重算一輪估價，整批落庫。回傳 {rows, errors, seconds, comps_n}。

    `valuator` 可傳入已建好的（pipeline 掃描中已經建過一次就重用，
    不要第二份）；不傳就自建。P 值與 resale 出自**同一個 valuator**
    ——這是既有的同源不變式，搬到掃描時點也要守住。

    逐列的失敗**不中斷**整批：該列各值寫 NULL（讀取端誠實顯示無 P），
    計數回報並大聲印前三個病名。valuator 建不起來這種整批性的失敗
    直接往外拋，由呼叫端（pipeline 掛勾）落 meta。
    """
    from datetime import datetime, timezone

    from .valuation import build_valuator, estimate_signal_row

    t0 = time.perf_counter()
    comps_n = int(store.stats().get("comps") or 0)
    if valuator is None:
        valuator = build_valuator(cfg, store, index)
    rows = store.list_signals(state="all", limit=1_000_000)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out, errors, first_errors = [], 0, []
    for r in rows:
        raw = r.get("payload")
        rec = {"key": r["key"], "p_worth_buying": None, "fair_twd": None,
               "est_level_label": None, "resale_json": None,
               "comps_n": comps_n, "computed_at": now}
        try:
            est = estimate_signal_row(valuator, {**r, "payload": raw})
            rec["p_worth_buying"] = est.p_worth_buying
            rec["fair_twd"] = est.fair_twd
            rec["est_level_label"] = est.level_label
            rec["resale_json"] = json.dumps(
                resale_for_row(valuator, cfg, fx, r, raw), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - 逐列失敗不中斷，但要留病名
            errors += 1
            if len(first_errors) < 3:
                first_errors.append(f"{r['key']}: {type(exc).__name__}: {exc}")
        out.append(rec)
    store.upsert_valuations(out)
    store.set_meta(VALUATION_CACHE_AT_KEY, now)
    # 部分失敗也要上 dashboard 橫條；全成功時清空舊病名。
    store.set_meta(
        VALUATION_CACHE_ERROR_KEY,
        f"{errors} 列估價失敗（首例：{first_errors[0]}）" if errors else "",
    )
    for line in first_errors:
        print(f"[value-cache] 列失敗：{line}")
    return {"rows": len(out), "errors": errors,
            "seconds": time.perf_counter() - t0, "comps_n": comps_n}
```

注意：`datetime` 時間戳若與 store 既有慣例（看 `begin_scan` 怎麼寫 `started_at`）
不同形，**以既有慣例為準**改這裡，不要讓同一顆 db 出現兩種時間格式。

- [ ] **Step 2: web/app.py 改 import（行為不變）**

刪 `_resale_for_row`（web/app.py:133-170），檔頭 import 加
`from ygo_sniper.valuation_cache import resale_for_row`，
`/api/signals` 內原呼叫 `r["resale"] = _resale_for_row(valuator, r, raw)` 改
`r["resale"] = resale_for_row(valuator, cfg, fx, r, raw)`。
驗證只有一份定義：`grep -c "_resale_for_row" web/app.py` 應為 0。

- [ ] **Step 3: 寫測試（monkeypatch 掉重物）**

```python
"""refresh_valuation_cache：整批落庫、逐列失敗不中斷、meta 誠實。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ygo_sniper import valuation_cache as vc
from ygo_sniper.store import Store

# _signal_for 照抄 tests/test_expiry_clear.py（同 Task 1）


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _stub_est(**over):
    base = dict(p_worth_buying=0.4, fair_twd=999.0, level_label="L1")
    base.update(over)
    return SimpleNamespace(**base)


def test_refresh_writes_all_rows_and_meta(store, monkeypatch):
    for k in ("buyee_yahoo:a1", "buyee_yahoo:a2"):
        store.upsert_signal(_signal_for(k))
    monkeypatch.setattr(vc, "resale_for_row", lambda *a, **kw: {"ok": False, "reason": "stub"})
    monkeypatch.setattr("ygo_sniper.valuation.build_valuator", lambda *a, **kw: object())
    monkeypatch.setattr("ygo_sniper.valuation.estimate_signal_row", lambda v, r: _stub_est())
    summary = vc.refresh_valuation_cache(cfg=None, store=store, fx=None)
    assert summary["rows"] == 2 and summary["errors"] == 0
    rows = store.list_signals(state="all", limit=10)
    assert all(r["val_p_worth_buying"] == 0.4 for r in rows)
    assert store.get_meta(vc.VALUATION_CACHE_AT_KEY)
    assert store.get_meta(vc.VALUATION_CACHE_ERROR_KEY) == ""


def test_per_row_failure_writes_nulls_and_error_meta(store, monkeypatch):
    for k in ("buyee_yahoo:ok", "buyee_yahoo:boom"):
        store.upsert_signal(_signal_for(k))
    monkeypatch.setattr(vc, "resale_for_row", lambda *a, **kw: {"ok": False, "reason": "stub"})
    monkeypatch.setattr("ygo_sniper.valuation.build_valuator", lambda *a, **kw: object())

    def _est(v, r):
        if "boom" in r["key"]:
            raise ValueError("炸")
        return _stub_est()

    monkeypatch.setattr("ygo_sniper.valuation.estimate_signal_row", _est)
    summary = vc.refresh_valuation_cache(cfg=None, store=store, fx=None)
    assert summary["errors"] == 1
    rows = {r["key"]: r for r in store.list_signals(state="all", limit=10)}
    assert rows["buyee_yahoo:boom"]["val_p_worth_buying"] is None   # 誠實留白
    assert rows["buyee_yahoo:ok"]["val_p_worth_buying"] == 0.4      # 好的照寫
    assert "估價失敗" in store.get_meta(vc.VALUATION_CACHE_ERROR_KEY)
```

（monkeypatch 目標若因 `refresh_valuation_cache` 內部是 `from .valuation import …`
的區域 import 而打不中，改成 patch `ygo_sniper.valuation` 模組屬性即可——上面
寫的就是這個形；實作時保持區域 import 以免 web 啟動變慢。）

- [ ] **Step 4: 跑綠＋全量回歸＋commit**

Run: `.venv/bin/pytest tests/test_valuation_cache.py -v` → 2 passed
Run: `make test` → 全綠（web 的 import 搬家不能弄壞既有 web 測試）

```bash
git add src/ygo_sniper/valuation_cache.py web/app.py tests/test_valuation_cache.py
git commit -m "feat(valuation): 估價快取模組——resale 搬出 web、整批重算落庫"
```

---

### Task 3 @inline：`Pipeline.scan()` 收尾掛勾＋CLI `revalue`

**Files:**
- Modify: `src/ygo_sniper/pipeline.py`（`scan()` :820 起；掛在
  `result = self._scan(...)` 成功之後、`self.store.finish_scan(...)` 之前）
- Modify: `src/ygo_sniper/cli.py`（新增 `revalue` 指令；輕量指令的建構方式
  照抄既有唯讀指令如 `health`／`coverage-groups` 怎麼拿 cfg/store/fx，
  不要為了跑 revalue 建整組 sources）
- Modify: `CLAUDE.md` 第九節指令表加一行 `ygo-sniper revalue`
- Test: `tests/test_valuation_cache.py` 追加

- [ ] **Step 1: pipeline 掛勾**

`Pipeline` 加方法：

```python
def _refresh_valuation_cache(self, result: dict) -> None:
    """掃描收尾的估價快取重算（2026-08-24 plan，estimate 快取）。

    壞掉**不可以**毀掉整輪掃描——推播與排程基準都在後面；但也絕不靜默
    （CLAUDE.md 第五節）：console 大聲印＋寫 meta，dashboard 的
    valuation_error 橫條會把病名顯示給使用者。
    """
    from .valuation_cache import VALUATION_CACHE_ERROR_KEY, refresh_valuation_cache

    try:
        summary = refresh_valuation_cache(self.cfg, self.store, self.fx)
        result["valuation_cached"] = summary["rows"]
        print(
            f"[value-cache] {summary['rows']} 列 {summary['seconds']:.1f}s"
            f"（{summary['errors']} 列失敗，comps={summary['comps_n']}）"
        )
    except Exception as exc:  # noqa: BLE001 - 大聲落 meta 後放行，理由見 docstring
        msg = f"{type(exc).__name__}: {exc}"
        print(f"[value-cache] 快取重算失敗：{msg}")
        self.store.set_meta(VALUATION_CACHE_ERROR_KEY, msg)
        result["valuation_cache_error"] = msg
```

`scan()` 裡、`self.store.finish_scan(started, result=...)` 那行**之前**插入：

```python
if not dry_run:
    self._refresh_valuation_cache(result)
```

（`dry_run` 的語意是「只掃不寫庫」，寫快取當然也不行。`watch_only` 照常重算：
它也是一種掃描，而且會新增 signals，不算的話新列會一直無 P。）

實作前先讀 `pipeline.py:241-260`：若 `Pipeline` 在 refresh_comps 之後已建有
valuator 或 CardIndex，透過 `refresh_valuation_cache(..., valuator=...)` 或
`index=...` 重用，不要建第二份；沒有就照上面原樣（自建）。

- [ ] **Step 2: CLI `revalue`**

```python
@app.command()
def revalue():
    """手動重算估價快取（首次部署回填、或想立刻反映最新 comps 時用）。

    正常情況不需要跑：每一輪掃描（daily／daily-high／dashboard 按鈕）
    收尾都會自動重算。
    """
    from .valuation_cache import refresh_valuation_cache
    # cfg/store/fx 的取得照本檔其他唯讀指令的慣例
    summary = refresh_valuation_cache(cfg, store, fx)
    print(
        f"[value-cache] {summary['rows']} 列 {summary['seconds']:.1f}s"
        f"（{summary['errors']} 列失敗，comps={summary['comps_n']}）"
    )
```

CLAUDE.md 第九節指令表（`backfill-sale-kind` 那行附近）加：

```
ygo-sniper revalue             # 手動重算估價快取（掃描收尾會自動跑；首次部署回填用）
```

- [ ] **Step 3: 測試**

`tests/test_valuation_cache.py` 追加（不建完整 Pipeline——它的 ctor 會建 sources；
測掛勾方法本體，用 stub self）：

```python
def test_pipeline_hook_swallows_failure_but_writes_meta(store, monkeypatch):
    """快取炸掉不准毀掉掃描，但病名必須落 meta（dashboard 橫條要看得到）。"""
    from ygo_sniper.pipeline import Pipeline

    stub = SimpleNamespace(cfg=None, store=store, fx=None)
    monkeypatch.setattr(
        "ygo_sniper.valuation_cache.refresh_valuation_cache",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("model 炸了")),
    )
    result: dict = {}
    Pipeline._refresh_valuation_cache(stub, result)   # 不能 raise
    assert "model 炸了" in store.get_meta(vc.VALUATION_CACHE_ERROR_KEY)
    assert "model 炸了" in result["valuation_cache_error"]


def test_pipeline_hook_success_records_count(store, monkeypatch):
    from ygo_sniper.pipeline import Pipeline

    stub = SimpleNamespace(cfg=None, store=store, fx=None)
    monkeypatch.setattr(
        "ygo_sniper.valuation_cache.refresh_valuation_cache",
        lambda *a, **kw: {"rows": 5, "errors": 0, "seconds": 0.1, "comps_n": 42},
    )
    result: dict = {}
    Pipeline._refresh_valuation_cache(stub, result)
    assert result["valuation_cached"] == 5
```

（monkeypatch 目標同樣要對準 `_refresh_valuation_cache` 內實際 import 的路徑；
上面假設方法內是 `from .valuation_cache import refresh_valuation_cache` 的區域
import——patch `ygo_sniper.valuation_cache.refresh_valuation_cache` 即可打中。）

`scan()` 的呼叫點另用一條輕量檢查釘住（防止日後重構把掛勾拔掉沒人發現）：

```python
def test_scan_source_contains_hook_call():
    """scan() 收尾必須掛快取重算。用原始碼釘：dry_run 守衛＋呼叫都在。"""
    import inspect
    from ygo_sniper.pipeline import Pipeline

    src = inspect.getsource(Pipeline.scan)
    assert "_refresh_valuation_cache" in src
    assert "if not dry_run" in src
```

- [ ] **Step 4: 跑綠＋回歸＋commit**

Run: `.venv/bin/pytest tests/test_valuation_cache.py -v` → 全綠
Run: `make test` → 全綠

```bash
git add src/ygo_sniper/pipeline.py src/ygo_sniper/cli.py CLAUDE.md tests/test_valuation_cache.py
git commit -m "feat(pipeline): 掃描收尾重算估價快取＋ygo-sniper revalue"
```

---

### Task 4 @inline：`/api/signals` 改成只讀快取

**Files:**
- Modify: `web/app.py`（`/api/signals` :173-262；`_valuator` 註解 :85-93）
- Test: `tests/test_valuation_cache_web.py`（新檔；client fixture 照抄
  `tests/test_expiry_clear.py:970-1000`，含「不准開正式庫」承重斷言）

- [ ] **Step 1: 紅燈測試**

```python
def test_signals_reads_cache_and_never_builds_valuator(client):
    """/api/signals 只讀快取。_shared_valuator 被打到就代表又在現算。"""
    tc, app_mod = client
    app_mod.store.upsert_signal(_signal_for("buyee_yahoo:a1"))
    app_mod.store.upsert_valuations([_val_row("buyee_yahoo:a1")])  # helper 同 Task 1

    def _boom():
        raise AssertionError("/api/signals 不准建 valuator")

    app_mod._shared_valuator = _boom
    res = tc.get("/api/signals?state=all&limit=10").json()
    it = res["items"][0]
    assert it["p_worth_buying"] == 0.42
    assert it["fair_twd"] == 1234.0
    assert it["est_level_label"] == "L1"
    assert it["resale"] == {"ok": False, "reason": "stub"}


def test_signals_without_cache_shows_honest_nulls(client):
    tc, app_mod = client
    app_mod.store.upsert_signal(_signal_for("buyee_yahoo:fresh"))
    res = tc.get("/api/signals?state=all&limit=10").json()
    it = res["items"][0]
    assert it["p_worth_buying"] is None and it["resale"] is None
    assert res["p_worth_known"] == 0


def test_signals_surfaces_cache_error_and_timestamp(client):
    tc, app_mod = client
    from ygo_sniper.valuation_cache import (
        VALUATION_CACHE_AT_KEY, VALUATION_CACHE_ERROR_KEY)
    app_mod.store.set_meta(VALUATION_CACHE_ERROR_KEY, "3 列估價失敗（首例：x）")
    app_mod.store.set_meta(VALUATION_CACHE_AT_KEY, "2026-08-24T12:00:00+00:00")
    res = tc.get("/api/signals?state=all&limit=10").json()
    assert "估價失敗" in res["valuation_error"]
    assert res["valuation_cached_at"] == "2026-08-24T12:00:00+00:00"
```

Run: `.venv/bin/pytest tests/test_valuation_cache_web.py -v` → FAIL（現行端點會去建 valuator）

- [ ] **Step 2: 改端點**

`/api/signals` 的估價段（:223-237 的 try/except 整段＋前面的
`raw_payloads` 準備）整個換成讀 JOIN 欄位：

```python
    for r in rows:
        r["flags"] = json.loads(r.get("flags") or "[]")
        r["payload"] = _with_overhead(json.loads(r.get("payload") or "{}"))
        r["expiry"] = expiry_status(r, gone_confidence=_GONE_CONFIDENCE).to_dict()
        r["triggered"] = is_triggered(r["flags"])
        r["shipping_alert"] = shipping_alert_for_row(r, cfg)
        # 估價一律來自掃描時算好的快取（signal_valuations），這裡**只讀不算**
        # ——連 lazy 補算都不做：讀取不可以改資料（2026-08-24 使用者裁決，
        # 詳見 valuation_cache.py 模組 docstring）。沒有快取列就誠實顯示無 P。
        r["p_worth_buying"] = r.pop("val_p_worth_buying", None)
        r["fair_twd"] = r.pop("val_fair_twd", None)
        r["est_level_label"] = r.pop("val_level_label", None)
        raw_resale = r.pop("val_resale_json", None)
        r["resale"] = json.loads(raw_resale) if raw_resale else None
        r.pop("val_computed_at", None)
```

回應 dict 的兩個欄位改／加：

```python
        "valuation_error": store.get_meta(VALUATION_CACHE_ERROR_KEY) or None,
        "valuation_cached_at": store.get_meta(VALUATION_CACHE_AT_KEY),
```

（檔頭 import `VALUATION_CACHE_AT_KEY, VALUATION_CACHE_ERROR_KEY`；
`resale_for_row` 的 import 若因此無人使用就刪掉。）

**必改的過時註解**（不改的話下一個 session 會照舊註解把快取拆掉）：
- `/api/signals` docstring :184-188「`p_worth_buying`…現算不落庫…落庫等於把
  一個會過期的機率凍起來」→ 改成「來自掃描收尾的估價快取（signal_valuations），
  凍結到下一輪掃描是刻意取捨——2026-08-24 使用者裁決，理由與取捨見
  valuation_cache.py」。
- `_valuator` 註解 :85-93 → 補一句「/api/signals 已改讀快取，這顆 valuator
  只服務 /api/bundle、/api/appraise、/api/search 這些單發互動端點」。

`_shared_valuator`、`_with_overhead` 都**保留**（bundle/appraise/search 還在用
前者；payload 的 route 序列化仍走後者）。

- [ ] **Step 3: 跑綠＋全量回歸**

Run: `.venv/bin/pytest tests/test_valuation_cache_web.py -v` → 3 passed
Run: `make test` → 全綠。**預期會紅的既有測試**：任何斷言 `/api/signals` 回
`p_worth_buying` 有值、或斷言 `valuation_error` 語意的舊測試。修法是把測試改成
「先 `upsert_valuations` 塞快取再打端點」——**不是**在端點裡偷偷補算。

- [ ] **Step 4: commit**

```bash
git add web/app.py tests/test_valuation_cache_web.py tests/<被修的既有測試>
git commit -m "feat(web): /api/signals 改讀估價快取——只讀不算，缺快取誠實留白"
```

---

### Task 5 @inline：前端樂觀更新＋競態守衛＋快取時間顯示

**Files:**
- Modify: `web/static/index.html`（`setState` :1318-1328、`setBucket` :1304-1316、
  `loadSignals` :1458-1487、`card()`／`auctionCard()` 模板根節點、CSS）
- Test: `tests/test_optimistic_state.py`（新檔，抽標記區塊丟 node 實跑，
  模式照抄 `tests/test_expiry_banner.py`）

- [ ] **Step 1: 純函式（含標記，供 node 測試抽取）**

放在 `setState` 上方：

```js
/* ==== LOCAL-STATE-LOGIC:BEGIN ==== */
/** 樂觀更新的**唯一**判定：狀態改成 newState 之後，這張卡片還屬於目前分頁嗎。
    純函式、不碰 DOM——tests/test_optimistic_state.py 用 node 實跑這一段。
    回傳新的 items 陣列（不就地改）＋ removed 旗標。 */
function applyLocalState(items, key, newState, currentState){
  const idx = items.findIndex(i => i.key === key);
  if(idx < 0) return {items: items, removed: false};
  const stays = currentState === "all" || newState === currentState;
  if(stays){
    const next = items.slice();
    next[idx] = Object.assign({}, next[idx], {state: newState});
    return {items: next, removed: false};
  }
  return {items: items.slice(0, idx).concat(items.slice(idx + 1)), removed: true};
}
/* ==== LOCAL-STATE-LOGIC:END ==== */
```

- [ ] **Step 2: node 測試**

`tests/test_optimistic_state.py`（HARNESS 讀 stdin 的
`{items, key, newState, currentState}`，印 `applyLocalState(...)` 的 JSON；
extract_block 斷言 `function applyLocalState(` 在區塊內）：

```python
def test_skip_removes_item_from_state_tab():
    out = run_js(items=[{"key": "a", "state": "new"}, {"key": "b", "state": "new"}],
                 key="a", new_state="skipped", current_state="new")
    assert out["removed"] is True
    assert [i["key"] for i in out["items"]] == ["b"]


def test_all_tab_keeps_item_but_updates_state():
    out = run_js(items=[{"key": "a", "state": "new"}],
                 key="a", new_state="skipped", current_state="all")
    assert out["removed"] is False
    assert out["items"][0]["state"] == "skipped"


def test_same_state_stays():
    out = run_js(items=[{"key": "a", "state": "watching"}],
                 key="a", new_state="watching", current_state="watching")
    assert out["removed"] is False


def test_unknown_key_is_noop():
    out = run_js(items=[{"key": "a", "state": "new"}],
                 key="ghost", new_state="skipped", current_state="new")
    assert out["removed"] is False and len(out["items"]) == 1
```

Run: `.venv/bin/pytest tests/test_optimistic_state.py -v` → 先 FAIL（區塊不存在），
Step 1 落了之後轉綠。

- [ ] **Step 3: `setState` 改樂觀更新**

```js
/** 樂觀更新（2026-08-24 使用者裁決）：先動畫面、再非同步告訴後端。
    舊版是 POST 完 loadAll() 整頁重載——實測每按一次略過要付一次
    /api/signals 全列重算＋MB 級回應，使用者被迫等卡片消失才敢按下一張。
    失敗路徑不樂觀：toast 病名＋loadSignals() 回去對帳，後端才是事實。 */
async function setState(key, state){
  const res = applyLocalState(items, key, state, currentState);
  items = res.items;
  const node = document.querySelector(`#grid [data-key="${CSS.escape(key)}"]`);
  if(res.removed && node){
    node.classList.add("leaving");                 // 150ms 淡出，看得出是哪張走了
    setTimeout(renderSignals, 160);
  }else{
    renderSignals();
  }
  try{
    await api(`/api/signals/${encodeURIComponent(key)}/state`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({state}),
    });
    toast("已更新");
  }catch(e){
    toast("失敗：" + e.message);
    loadSignals();   // 樂觀錯了就認：重抓後端的事實，卡片會自己回來
  }
}
```

`card()` 與 `auctionCard()` 兩個模板的**根節點**都加
`data-key="${escapeHtml(it.key)}"`（key 來自 db，照本檔慣例 escape）。

CSS（放既有卡片樣式附近）：

```css
.leaving{opacity:0;transform:scale(.97);transition:opacity .15s ease,transform .15s ease;pointer-events:none}
```

- [ ] **Step 4: `setBucket` 改局部更新（不再 loadAll）**

```js
async function setBucket(key, bucket){
  try{
    const r = await api(`/api/signals/${encodeURIComponent(key)}/bucket`, {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({bucket: bucket || null}),
    });
    // 後端回的是**寫完讀回來的值**（state 可能被 new→watching 連動改掉），
    // 照實更新本地，不重打整頁。
    const it = items.find(i => i.key === key);
    if(it){ it.bucket = r.bucket; if(r.state) it.state = r.state; }
    // 分類分頁：改完分類後不再屬於這一頁的，本地移除（與後端過濾同義）。
    if(currentBucket && r.bucket !== currentBucket){
      items = items.filter(i => i.key !== key);
    }
    toast(!bucket ? "已清除分類"
      : `已標記 ${BUCKETS[bucket] || bucket}`
        + (r && r.state === "watching" ? "，並移入觀察中" : ""));
    renderSignals();
  }catch(e){ toast("失敗：" + e.message); }
}
```

- [ ] **Step 5: `loadSignals` 競態守衛＋快取時間顯示**

序號守衛（兩個併發 loadSignals 時，過期回應直接丟棄——沒有這條，慢的舊回應
晚到會把新狀態蓋掉，畫面上出現「略過的又回來了」）：

```js
let signalsSeq = 0;
async function loadSignals(){
  const seq = ++signalsSeq;
  // …既有 fetch…
  const res = await api(`/api/signals?state=${currentState}&limit=1000${bq}`);
  if(seq !== signalsSeq) return;   // 已有更新的請求在飛，這份是過期事實
  // …其餘照舊…
}
```

清單 meta 行（renderSignals 尾端組計數字串處）加估價快取時間——這是把
「你看到的 P 值是幾點算的」誠實地放在使用者眼前：

```js
const cachedAt = lastRes && lastRes.valuation_cached_at
  ? ` · 估價快取 ${new Date(lastRes.valuation_cached_at)
      .toLocaleTimeString("zh-TW", {hour: "2-digit", minute: "2-digit"})}`
  : ` · <span class="hid">估價快取：無（先跑一次掃描或 ygo-sniper revalue）</span>`;
```

- [ ] **Step 6: 跑綠＋回歸＋commit**

Run: `.venv/bin/pytest tests/test_optimistic_state.py -v` → 4 passed
Run: `make test` → 全綠（`test_auction_view.py`／`test_expiry_banner.py` 抽的
區塊不能被這次編輯弄斷）

```bash
git add web/static/index.html tests/test_optimistic_state.py
git commit -m "feat(web): 略過改樂觀更新——本地先消失、非同步回報後端；loadSignals 競態守衛"
```

---

### Task 6：主線程親驗（不派工）

- [ ] `make test` 全綠（主線程親跑，看到 `N passed` 那行）。
- [ ] `ygo-sniper revalue` 實跑：`[value-cache]` 行印出列數與秒數；
      `sqlite3 data/sniper.db "select count(*) from signal_valuations"` 應等於
      signals 總列數。
- [ ] TestClient 計時複測：暖機後 `/api/signals?state=skipped&limit=1000` 應從
      3.0s 降到 <0.5s（不再逐列估價）；`_shared_valuator` monkeypatch 成炸彈時
      端點仍 200。
- [ ] `ygo-sniper serve` 起來，手動按一次略過：卡片 150ms 內消失、無整頁閃動；
      清單 meta 行顯示估價快取時間。
- [ ] `git log` 確認每個 task 一個 commit、無 plan 外改動（`git status` 乾淨）。

## 審查修正回合（Task 7 @inline，2026-08-24 reviewer findings 主線程裁決）

Reviewer 判定「需修正後複審」。主線程逐條複驗後裁決如下（每條都已親跑指令
確認屬實才列入；F5 的比較基準與 reviewer 建議不同，理由寫在該條）。

**Files:**
- Modify: `web/static/index.html`（setState / setBucket / renderSignals noP 行）
- Modify: `web/app.py`（/api/signals 的 resale 解析防護＋快取落後偵測；刪三連空行）
- Modify: `src/ygo_sniper/pipeline.py`（掛勾移進 try 內）
- Modify: `src/ygo_sniper/valuation_cache.py`（刪未用 import）
- Modify: `tests/test_valuation_cache.py`、`tests/test_valuation_cache_web.py`（I001 import 排序）
- Test: `tests/test_valuation_cache_web.py` 追加 F3／F5 案例

- [ ] **F1（Critical）：setState／setBucket 成功後補刷湊單籃與計數**

「加入湊單」過去靠 `loadAll()` 連帶刷 `loadBundle()`（湊單籃總額）與
`loadStats()`（分頁計數）；樂觀更新拿掉 `loadAll()` 後這兩塊變成舊值且畫面
不會說它是舊的（CLAUDE.md 第三節：不同基準的數字擺在一起）。兩支 API 實測
都 <10ms，補刷不傷體感。`setState` 與 `setBucket` 的 POST **成功路徑**各加：

```js
    // 湊單籃總額與分頁計數的資料源在後端（bundle 即時重算、stats 是 db 計數），
    // 本地不重算第二份（同源）。這兩支實測 <10ms，樂觀更新省的是 /api/signals
    // 那一支，不是它們。fire-and-forget：失敗只影響側欄數字，下一次任何
    // 重載都會補上，不值得為它擋住主流程。
    loadBundle(); loadStats();
```

- [ ] **F2（Warning）：本地變更必須作廢在飛的清單回應**

`signalsSeq` 只在新的 `loadSignals` 起飛時遞增；`pollScanStatus` 掃完自動
`loadAll()` 的 0.4s 飛行期間按「略過」，舊回應晚到會把剛略過的卡畫回來——
正是守衛註解宣稱要防的事。修法：`setState` 的樂觀變更起點與 `setBucket`
改動 `items` 之前各加一行：

```js
    signalsSeq++;   // 本地已經走在伺服器快照前面了，任何在飛的清單回應都過期
```

（被作廢的那次刷新不補跑：正確性 > 新鮮度，下一次任何重載都會補上。）

- [ ] **F3（Warning）：一列壞掉的 resale_json 不准 500 整個清單**

`web/app.py` `/api/signals` 的 `json.loads(raw_resale)` 裸奔；同檔
`_route_dict` 對壞 payload 的既有立場是「一列壞掉不打掉整個清單」。改成：

```python
        raw_resale = r.pop("val_resale_json", None)
        try:
            r["resale"] = json.loads(raw_resale) if raw_resale else None
        except ValueError:
            # 一列殘缺的快取（部分寫入、手動改庫）退化成「這列沒有轉賣答案」，
            # 不打掉整個清單——與 _route_dict 對壞 payload 的立場一致。
            r["resale"] = None
```

- [ ] **F4（Warning）：掛勾移進 scan() 的 try 內**

`_refresh_valuation_cache` 在 try/except BaseException 之外，重算要跑幾秒，
期間 Ctrl-C 會讓 `finish_scan` 不執行、scan_status 卡 running 直到逾時兜底，
破壞 docstring「掃爆了先落 finished 再拋」的不變式。把

```python
        if not dry_run:
            self._refresh_valuation_cache(result)
```

移到 try 區塊內、`result = self._scan(...)` 之後（`_refresh_valuation_cache`
自己吞 Exception 的行為不變；會穿透的只剩 KeyboardInterrupt／SystemExit，
正該走 except BaseException 落 finished(error) 再拋）。
`tests/test_valuation_cache.py` 的 `test_scan_source_contains_hook_call` 不用改
（inspect.getsource 仍找得到兩個字串）。

- [ ] **F5（Warning）：快取落後最後一輪掃描要說出來**

掃描在寫完 signals 之後、掛勾之前死掉（watchdog kill、_scan 後段例外），
快取會停在上一輪而 meta 病名是空的——同一張卡「新的到手成本」配「舊 comps
算的 P」，畫面上沒有任何東西說它是舊的。偵測放 `/api/signals`：

**比較基準用上一輪掃描的 `started_at`，不是 finished_at**——掛勾跑在
`finish_scan` 之前，`cached_at` 永遠略早於 `finished_at` 幾毫秒到幾秒，
用 finished_at 每一輪正常掃描都會誤報（CLAUDE.md 第三節：先問零點在哪）。
正常輪：cached_at（掛勾時寫）> started_at → 不報。掛勾前死掉：cached_at
是上一輪的 < started_at → 報。掃描進行中（running）不比——掛勾本來就
還沒跑到。dry_run 不寫庫也不重算，跳過。時間戳解析失敗一律不報
（讀不到 ≠ 落後，與「讀不到錢 ≠ 錢虧光」同一條）。

```python
def _valuation_lag_warning() -> str | None:
    """快取落後偵測。回 None = 沒事；回字串 = 給 valuation_error 橫條的病名。"""
    from datetime import datetime

    cached_at = store.get_meta(VALUATION_CACHE_AT_KEY)
    st = store.scan_status()
    started_at = st.get("started_at")
    if not cached_at or not started_at or st.get("running") or st.get("dry_run"):
        return None
    try:
        lagging = datetime.fromisoformat(cached_at) < datetime.fromisoformat(started_at)
    except ValueError:
        return None          # 讀不到 ≠ 落後：解析不了就不指控
    if not lagging:
        return None
    return (
        f"估價快取落後最後一輪掃描（掃描 {started_at} 起跑，快取仍是 {cached_at}）"
        "——P 值可能與到手成本不同輪；等下一輪掃描，或跑 ygo-sniper revalue"
    )
```

回應組裝處改成（meta 病名優先——掛勾失敗的病名比落後更具體）：

```python
        "valuation_error": store.get_meta(VALUATION_CACHE_ERROR_KEY)
        or _valuation_lag_warning(),
```

（注意既有的 `or None` 語意要保住：meta 空字串＋無落後 → 仍是 None。）

- [ ] **F6（Suggestions，全收）**

1. `src/ygo_sniper/valuation_cache.py` 刪未使用的 `from typing import Any`；
   `tests/test_valuation_cache.py`、`tests/test_valuation_cache_web.py` 修 I001
   import 排序——`make lint` 錯誤數回到基線 27（改前先跑一次記下數字）。
2. `web/app.py` 原 `_valuator` 區塊刪除後留下的三連空行縮成兩行。
3. `web/static/index.html` renderSignals 的「N 筆無 P 值」：
   `items.length - res.p_worth_known` 在樂觀移除後一新一舊（略過一張有 P 的卡
   會少報一筆）。改成兩個數字同源——都從當下的 `items` 數：

```js
  const noPn = items.filter(i => i.p_worth_buying == null).length;
  const noP = noPn
    ? ` · <span class="hid">${noPn} 筆無 P 值（不受 P 篩選影響）</span>` : "";
```

- [ ] **F7：測試（追加到 tests/test_valuation_cache_web.py）**

```python
def test_signals_survives_one_corrupt_resale_row(client):
    """一列殘缺的 resale_json 只犧牲那一列，不 500 整個清單（F3）。"""
    tc, app_mod = client
    app_mod.store.upsert_signal(_signal_for("buyee_yahoo:good"))
    app_mod.store.upsert_signal(_signal_for("buyee_yahoo:bad"))
    app_mod.store.upsert_valuations([
        _val_row("buyee_yahoo:good"),
        _val_row("buyee_yahoo:bad", resale_json="{truncated"),
    ])
    res = tc.get("/api/signals?state=all&limit=10")
    assert res.status_code == 200
    items = {i["key"]: i for i in res.json()["items"]}
    assert items["buyee_yahoo:good"]["resale"] == {"ok": False, "reason": "stub"}
    assert items["buyee_yahoo:bad"]["resale"] is None
    assert items["buyee_yahoo:bad"]["p_worth_buying"] == 0.42  # 其他欄位不陪葬


def test_signals_reports_cache_lagging_behind_last_scan(client):
    """掃描起跑晚於快取＝掛勾沒跑到，要說出來（F5）。"""
    tc, app_mod = client
    from ygo_sniper.valuation_cache import VALUATION_CACHE_AT_KEY
    app_mod.store.set_meta(VALUATION_CACHE_AT_KEY, "2026-08-24T00:00:00+00:00")
    started = app_mod.store.begin_scan(trigger="cli", dry_run=False)
    app_mod.store.finish_scan(started, result={})
    res = tc.get("/api/signals?state=all&limit=10").json()
    assert res["valuation_error"] and "落後" in res["valuation_error"]


def test_signals_no_lag_warning_while_scan_running_or_fresh(client):
    """掃描進行中不比（掛勾本來就還沒跑）；快取比掃描新也不報（F5）。"""
    tc, app_mod = client
    from ygo_sniper.valuation_cache import VALUATION_CACHE_AT_KEY
    # 進行中：begin 之後不 finish
    app_mod.store.set_meta(VALUATION_CACHE_AT_KEY, "2026-08-24T00:00:00+00:00")
    app_mod.store.begin_scan(trigger="cli", dry_run=False)
    assert tc.get("/api/signals?state=all&limit=10").json()["valuation_error"] is None
    # 掃完且快取較新（正常輪的形狀：掛勾在 finish 之前寫）
    started = app_mod.store.begin_scan(trigger="cli", dry_run=False)
    from datetime import UTC, datetime, timedelta
    app_mod.store.set_meta(
        VALUATION_CACHE_AT_KEY,
        (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
    )
    app_mod.store.finish_scan(started, result={})
    assert tc.get("/api/signals?state=all&limit=10").json()["valuation_error"] is None
```

- [ ] **F8：驗收**

Run: `.venv/bin/pytest tests/test_valuation_cache_web.py tests/test_valuation_cache.py tests/test_optimistic_state.py -v` → 全綠
Run: `make lint` → 錯誤數回到改前基線（27）
Run: `make test` → 全綠（基線 1908＋F7 新 3）
Commit 一個：`fix: 審查修正回合——湊單籃補刷、清單競態、resale 防護、快取落後偵測`

## Self-review 紀錄（寫完 plan 時檢查）

- 使用者三項裁決 → Task 1-3（快取只在掃描更新）、Task 4（讀寫分離）、
  Task 5（樂觀更新）逐項對應；`revalue` 是裁決 1 的「手動」補充，已在
  裁決文字外註明理由（首次部署回填）。
- 型別一致性：`upsert_valuations` 的欄位名（Task 1 SQL、Task 2 rec dict、
  Task 4 val_ 讀取）已互相核對；`VALUATION_CACHE_*_KEY` 兩把鑰匙在
  Task 2 定義、Task 3/4 引用同名。
- 無 placeholder 殘留：`_signal_for` 標明「照抄 tests/test_expiry_clear.py」
  是引用既有 helper，非 TBD。
