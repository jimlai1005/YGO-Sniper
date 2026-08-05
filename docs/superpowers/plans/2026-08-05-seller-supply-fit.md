# Seller Supply Fit 實作計劃

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一個與 Seller Alpha **並存但永不相加**的「供給契合度（Supply Fit）」分數，讓監控名單的入選門檻從「這賣家便宜嗎」改成「這賣家值不值得盯」，打破「要有 alpha 才進名單、進名單才長得出 ask 觀測、有 ask 觀測才算得出 alpha」的循環。

**Architecture:** 新模組 `src/ygo_sniper/seller_supply.py` 只吃現成的 `SellerMetrics`（不碰 db、不碰網路、純函式），對每個維度回傳「值 or 不可得＋原因」，用**站內百分位**轉分（自動迴避跨站不可比），再按**可得維度的權重重新正規化**合成 0-100 的 `SupplyFit`。監控名單入選改為雙軌：`alpha.ok and total >= 25` **OR** `supply_fit >= 門檻`，入選理由必須寫進 `seller_watch.reason`。

**Tech Stack:** Python 3.12、dataclass(slots=True)、pytest、Typer + rich、SQLite。無新增第三方依賴。

---

## 給執行者的背景（你是 fresh context，這段不能跳過）

這個 repo 是遊戲王 1998-2004 鑑定卡的自用撿漏器。**先讀專案根目錄的 `CLAUDE.md`**，尤其第三節（同源比較）、第四節（不要拿自己的模型當尺）、第五節（靜默失敗）——本計劃的每個設計決策都直接對應那幾條。

### 為什麼要做這件事（實測數字，2026-08-05）

跑 `.venv/bin/ygo-sniper sellers --rank` 得到：

```
可比 463/1635 筆（28.3%）｜基準 {'sold': 439, 'ask': 24}
賣家：360 個有觀測，34 個達門檻、326 個證據不足
在架帳跨度 3.2 天 / 成交帳 182.1 天
```

`notify_log` 實查：`seller_new`（同儕相對的強路徑）**只送過 3 則**，`seller_unpriced`（弱路徑）38 則。

根因：`listing_obs` **全表只有 538 列**（427 有 seller_id、364 還在架）。同儕鍵要求同站×同卡×同版次×同稀有度×同分數且不同賣家——538 列攤到 360 個賣家上幾乎不可能撞在一起，所以 ask 側可比只有 24 筆。

**這不是規則太嚴，是分母太小。** 而唯一能把在架帳撐厚的機制是密集掃賣家庫存：手動加進名單的 `ebay:psa` 一個賣家就貢獻 96 筆 ask 觀測（佔全部有賣家 ask 觀測的 22%）。

### 這件事已經做好的部分（不要重做）

`SellerMetrics`（[seller_alpha.py:597-665](../../../src/ygo_sniper/seller_alpha.py)）**已經算出**本計劃需要的全部原始量，而且對零可比賣家一樣算得出來。實跑 `analyze()` 驗證（360 個賣家、181 個零可比）：

| 欄位 | 零可比賣家的可得率 |
|---|---|
| `grade_mix: dict[str,int]` | 181/181（100%） |
| `listing_hour_hist: dict[int,int]` | 177/181（98%） |
| `series_top1_share` / `series_herfindahl` / `series_known_n` | 66/181（36%） |
| `n_disappeared` / `n_window_exit` / `n_still_open` | 58/181（32%） |
| `observation_span_days > 0` | 36/181（20%） |
| `n_rows` / `n_ask` / `n_sold` | 100% |

**所以本計劃不需要新增任何爬蟲、不需要改任何 parser、不碰任何過濾規則。**（也因此不需要跑 `corpus-diff`——那是改過濾／解析規則時才必須做的。）

### 三條本計劃專屬的紅線

1. **Supply Fit 與 Alpha 永不相加、永不互相 fallback。** Alpha 回答「便宜嗎」，湊不到同儕就拒答；Supply Fit 回答「值得盯嗎」，跟便宜與否無關。Task 4 有一條結構性測試釘住這件事。
2. **維度不可得 ≠ 該維度 0 分。** 不可得就從權重分母裡拿掉並重新正規化，同時在輸出裡標明「N/5 維度」。給 0 分等於說「這賣家這方面很差」，但事實是「我們不知道」。這是 `SellerScore.total=None`（不是 0）同一個哲學，見 [seller_alpha.py:831-855](../../../src/ygo_sniper/seller_alpha.py)。
3. **一律站內百分位，不做跨站絕對比較。** eBay 拿不到歷史成交（Marketplace Insights 403）、Yahoo 拍賣沒有議價功能——直接跨站比大小會讓分數變成「站台的代理變數」，這是 CLAUDE.md 第三節 venue 混池事故的重演。

### 已知會採用的既有慣例（照抄，不要自創）

- 「證據不足」的表達：`ok: bool` + `total: float|None`（False 時必定 None）+ `missing: list[str]`（缺什麼**以及怎麼補**）+ `caveats: list[str]`（一律 `⚠️` 開頭）。見 [seller_alpha.py:831-855](../../../src/ygo_sniper/seller_alpha.py)。
- 每一項分數必須帶依據字串：`ScoreComponent.detail`，測試 `test_every_component_carries_its_own_evidence`（[test_seller_alpha.py:482-492](../../../tests/test_seller_alpha.py)）釘死「裸數字不准輸出」。本計劃的 `SupplyDimension` 沿用同樣的要求。
- 測試風格：純 function（無 class）、模組級 helper 造資料、檔頭 docstring 逐條列「這組測試守的是哪種病」。
- Store 新表：`CREATE TABLE IF NOT EXISTS` 直接寫進模組級 `_SCHEMA` 常數（[store.py:23-248](../../../src/ygo_sniper/store.py)）；**加欄位到既有表**才走 additive migration。
- 時間參數一律 `now: str | None = None` + `stamp = now or _now_iso()`。

---

## File Structure

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/ygo_sniper/seller_supply.py` | **建立** | 純函式：五個維度計算、站內百分位、可得性正規化、`SupplyFit` 合成。不 import store、不 import 任何 source。 |
| `tests/test_seller_supply.py` | **建立** | 全部 Task 的測試。 |
| `src/ygo_sniper/seller_watch.py` | 修改 `sync_auto_watch`（:305-330 附近） | 入選改雙軌，`reason` 標明是 alpha 還是 supply 入選。 |
| `src/ygo_sniper/cli.py` | 修改 `sellers`（:691-731） | 新增 `--supply` 旗標與輸出表。 |
| `web/app.py` | 修改 `/api/sellers`（:717 附近） | payload 加 `supply_fit` 區塊。 |
| `web/static/index.html` | 修改 👤 賣家視圖（:2055-2210） | 兩個分數並排，明確區分「不給分數」與「0 分」。 |
| `config/settings.yaml` | 修改 `seller_alpha` 區塊（:205-267） | 新增 `supply_fit` 參數；順手修過時的現況數字。 |

**新模組不放進 `seller_alpha.py`**：那個檔案已經 1226 行，而且 Supply Fit 的整個重點是「跟 alpha 是不同的東西」——放在同一個檔會讓下一個人很自然地把兩者相加。分檔本身就是紅線 1 的結構性防線。

---

## 五個維度的定義與依據

權重來自使用者提供的表，扣掉「平均低於市場價格幅度」（那是 Alpha 本身，不進 Supply Fit），並新增 D5「供給規模」。**這些權重是起點不是結論**——Task 7 會實測分佈後回頭校準。

| 維度 | 來源欄位 | 權重 | 可得條件 | 方向 |
|---|---|---|---|---|
| D1 `sold_depth` 歷史成交深度 | `n_sold` | 25 | `n_sold >= 1`；站台拿不到成交（eBay）→ 不可得 | 越大越好 |
| D2 `grade_profile` 分數輪廓 | `grade_mix` | 15 | `sum(grade_mix.values()) >= 3` | 8/9 分佔比越高越好 |
| D3 `series_focus` 系列集中度 | `series_top1_share` | 10 | `series_known_n >= 3` | 越集中越好 |
| D4 `listing_rhythm` 上架時段可預測性 | `listing_hour_hist` | 5 | `sum(hist.values()) >= 5` | 越集中越好 |
| D5 `supply_scale` 供給規模 | `n_rows` | 25 | 恆可得 | 越大越好 |

**D1 為什麼是 25**：使用者原表給「賣家歷史成交數」25%，保留。語意是「他是持續經營的專業戶，不是一次性清倉」。

**D2 為什麼是 8/9 分越高越好**（這是假設，要寫在 docstring 裡）：PSA10 溢價高、7 以下品相風險大，8/9 是性價比帶。判定用分數值而非機構：把 `grade_mix` 的 key（形如 `"PSA 8"` / `"ARS 9"` / `"PSA 10"`）拆出數值，計 `8.0 <= g <= 9.5` 的佔比。**不要只認 PSA**——`grade_mix` 裡 ARS 佔比很高（實測有賣家 121 筆全 ARS10）。

**D4 的語意是排程用途不是品質**：他固定在某個時段上架 → 我們可以在那之前排掃描。權重 5，訊號最弱，docstring 要註明「這個維度衡量的是可預測性，不是賣家好壞」。

**D5 是本計劃新增的**（使用者原表沒有）。理由：「值不值得盯」最直接的判準就是他會不會持續產出符合年代輪廓的貨。實測 `ebay:psa` 的 `n_rows=117`、中位數賣家只有 1-2 列——這個維度的區辨力最強。權重比照 D1。

**用 `n_rows` 而不是「件/週」的原因（必須寫進 docstring）**：在架帳全域跨度只有 3.2 天、20% 的零可比賣家 `observation_span_days` 是 0，除以一個接近 0 的跨度會產生垃圾速率。所以 Phase 1 用**累積觀測量**，並在 caveat 明說「這是累積量不是速率，速率要等在架帳長厚」。

**沒有納入的兩項**（Phase 2，見文末）：「上架後多久成交」需要 duration 而現在只有 counts，且「消失 ≠ 成交」；「是否接受議價」需要新抓 offer flag。

---

## Task 1: SupplyDimension 與可得性宣告

**Files:**
- Create: `src/ygo_sniper/seller_supply.py`
- Test: `tests/test_seller_supply.py`

- [ ] **Step 1: 寫失敗測試**

```python
"""Supply Fit 的測試。這組測試守的是這幾種病：
1. 維度不可得被當成 0 分（等於說「這賣家很差」，但事實是「我們不知道」）
2. Supply Fit 與 Alpha 被相加或互相 fallback（第四節紅線的變種）
3. 跨站絕對比較（eBay 拿不到成交數，直接比大小 = 分數變成站台代理變數）
4. 裸數字輸出（每個維度必須帶得出依據字串）
"""
from ygo_sniper.seller_supply import SupplyDimension


def test_unavailable_dimension_has_no_score_and_says_what_is_missing():
    d = SupplyDimension.unavailable("sold_depth", "ebay 拿不到歷史成交（Insights API 403）")
    assert d.available is False
    assert d.raw is None
    assert d.score is None          # 不是 0.0
    assert "Insights" in d.missing


def test_available_dimension_must_carry_its_evidence():
    d = SupplyDimension.of("grade_profile", raw=0.62, detail="8/9 分佔 62%（31/50 筆）")
    assert d.available is True
    assert d.raw == 0.62
    assert d.detail                  # 不准是空字串
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_seller_supply.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ygo_sniper.seller_supply'`

- [ ] **Step 3: 最小實作**

```python
"""賣家供給契合度（Supply Fit）——回答「這賣家值不值得盯」。

**這個分數與 Seller Alpha 是兩件事，永不相加、永不互相 fallback。**
Alpha 回答「他比市場便宜多少」（同儕相對，湊不到就拒答）；
Supply Fit 回答「盯著他，未來會不會產出可買的機會」（跟便宜與否無關）。
把兩者加總會讓「賣很多 PSA9 又固定賣 Vol 系列」的賣家看起來像有 alpha，
那是 CLAUDE.md 第四節（不要拿自己的模型當尺）的變種。

維度不可得時一律 score=None 並從權重分母移除，**不是給 0 分**——
0 分的語意是「這方面很差」，但事實是「我們不知道」。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SupplyDimension:
    """一個維度的量測結果。不可得時 `raw` 與 `score` 都是 None，不是 0。"""

    name: str
    available: bool
    raw: float | None = None
    score: float | None = None      # 0-100 站內百分位，Task 3 才填
    detail: str = ""
    missing: str = ""

    @classmethod
    def of(cls, name: str, *, raw: float, detail: str) -> SupplyDimension:
        if not detail:
            raise ValueError(f"維度 {name} 沒有帶依據字串——裸數字不准輸出")
        return cls(name=name, available=True, raw=raw, detail=detail)

    @classmethod
    def unavailable(cls, name: str, missing: str) -> SupplyDimension:
        if not missing:
            raise ValueError(f"維度 {name} 不可得，但沒說缺什麼")
        return cls(name=name, available=False, missing=missing)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_seller_supply.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/seller_supply.py tests/test_seller_supply.py
git commit -m "feat(seller): SupplyDimension 可得性宣告"
```

---

## Task 2: 五個維度的計算

**Files:**
- Modify: `src/ygo_sniper/seller_supply.py`
- Test: `tests/test_seller_supply.py`

先在測試檔加一個造 `SellerMetrics` 的 helper（照 `tests/test_seller_alpha.py:46-77` 的 `row()` 風格）：

```python
from ygo_sniper.seller_alpha import SellerMetrics


def metrics(**kw) -> SellerMetrics:
    """只填測試關心的欄位，其餘吃 dataclass 預設。"""
    base = dict(seller_key="ebay:t", site="ebay", seller_id="t")
    base.update(kw)
    return SellerMetrics(**base)
```

- [ ] **Step 1: 寫失敗測試（五個維度各一條 + 三條可得性邊界）**

```python
from ygo_sniper.seller_supply import (
    dim_grade_profile, dim_listing_rhythm, dim_series_focus,
    dim_sold_depth, dim_supply_scale,
)


def test_grade_profile_counts_8_and_9_across_graders():
    """ARS 也算——grade_mix 裡 ARS 佔比很高，只認 PSA 會漏掉大半市場。"""
    m = metrics(grade_mix={"PSA 8": 3, "ARS 9": 2, "PSA 10": 5})
    d = dim_grade_profile(m)
    assert d.available is True
    assert d.raw == pytest.approx(0.5)          # (3+2)/10
    assert "5/10" in d.detail


def test_grade_profile_unavailable_below_three_samples():
    d = dim_grade_profile(metrics(grade_mix={"PSA 9": 2}))
    assert d.available is False
    assert d.score is None
    assert "3" in d.missing


def test_series_focus_needs_three_known_series_rows():
    ok = dim_series_focus(metrics(series_top1_share=0.8, series_known_n=5))
    assert ok.available is True and ok.raw == pytest.approx(0.8)
    thin = dim_series_focus(metrics(series_top1_share=1.0, series_known_n=2))
    assert thin.available is False      # 兩筆全同系列不算「固定賣某系列」


def test_sold_depth_unavailable_when_site_cannot_yield_sold_prices():
    """eBay 的 Marketplace Insights 是 403，成交數在這一站是「不知道」不是「0」。"""
    d = dim_sold_depth(metrics(site="ebay", n_sold=0))
    assert d.available is False
    assert "403" in d.missing or "Insights" in d.missing


def test_sold_depth_available_on_sites_with_history():
    d = dim_sold_depth(metrics(site="buyee_yahoo", n_sold=184))
    assert d.available is True and d.raw == 184.0


def test_supply_scale_is_always_available_and_says_it_is_cumulative():
    d = dim_supply_scale(metrics(n_rows=117, observation_span_days=3.2))
    assert d.available is True and d.raw == 117.0
    assert "累積" in d.detail            # 不是速率


def test_listing_rhythm_measures_concentration_not_quality():
    m = metrics(listing_hour_hist={20: 8, 21: 2})
    d = dim_listing_rhythm(m)
    assert d.available is True
    assert d.raw == pytest.approx(0.8)   # top1 時段佔比
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_seller_supply.py -v`
Expected: FAIL — `ImportError: cannot import name 'dim_grade_profile'`

- [ ] **Step 3: 實作五個維度**

要點（完整實作由執行者寫，但這些判斷不可改）：

- `SITES_WITHOUT_SOLD_HISTORY = frozenset({"ebay"})`，`dim_sold_depth` 對這些站一律 `unavailable("sold_depth", "ebay 拿不到歷史成交（Marketplace Insights API 403），成交數是未知不是 0")`。**這一條是站台可得性的核心，不要用 `n_sold == 0` 當判準**——那會把「拿不到」跟「真的沒賣過」混為一談。
- `dim_grade_profile`：解析 `grade_mix` 的 key。key 格式是 `"{機構} {分數}"`（實測值：`"PSA 8"`、`"ARS 10"`、`"PSA 9"`）。用 `key.rsplit(" ", 1)[-1]` 取分數並 `float()`，parse 不出來的 key 計入分母但不計入分子（並在 detail 註明有幾筆無法解析）。門檻 `total >= 3`。
- `dim_series_focus`：直接吃 `series_top1_share`，門檻 `series_known_n >= 3`；`series_top1_share is None` 也算不可得。
- `dim_supply_scale`：`raw = float(n_rows)`，detail 必須含「累積觀測量」與 `observation_span_days`，例如 `f"累積觀測 117 列（在架帳跨度 3.2 天——這是累積量不是速率）"`。
- `dim_listing_rhythm`：`max(hist.values()) / sum(hist.values())`，門檻 `sum >= 5`。

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_seller_supply.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(seller): Supply Fit 五個維度計算"
```

---

## Task 3: 站內百分位與可得性正規化合成

**Files:**
- Modify: `src/ygo_sniper/seller_supply.py`
- Test: `tests/test_seller_supply.py`

- [ ] **Step 1: 寫失敗測試**

```python
from ygo_sniper.seller_supply import SupplyParams, supply_fit_all


def test_score_is_renormalised_over_available_dimensions_only():
    """eBay 賣家拿不到成交數（權重 25）→ 剩下 75 的權重要重新正規化到 100，
    不是讓他直接損失 25 分。"""
    ms = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i * 10,
                  grade_mix={"PSA 8": i, "PSA 10": 1},
                  listing_hour_hist={20: 5, 21: 1}) for i in range(1, 12)]
    out = supply_fit_all(ms, params=SupplyParams())
    top = out[ms[-1].seller_key]
    assert top.ok is True
    assert top.n_dimensions_used == 3        # sold_depth 與 series_focus 都不可得
    assert top.n_dimensions_total == 5
    assert 0.0 <= top.total <= 100.0
    assert any("sold_depth" in x for x in top.missing)


def test_site_with_too_few_sellers_is_not_ranked():
    """站內只有 3 個賣家時百分位沒有意義——寧可不給分數。"""
    ms = [metrics(seller_key=f"mercari:s{i}", site="buyee_mercari", n_rows=i)
          for i in range(3)]
    out = supply_fit_all(ms, params=SupplyParams())
    for fit in out.values():
        assert fit.ok is False
        assert fit.total is None             # 不是 0
        assert "站內賣家數" in fit.reason


def test_percentile_is_within_site_only():
    """eBay 最大的賣家與 yahoo 最大的賣家，各自在自己站內拿到高分——
    不會因為 yahoo 的絕對量大就把 eBay 全站壓成低分。"""
    ebay = [metrics(seller_key=f"ebay:s{i}", site="ebay", n_rows=i,
                    grade_mix={"PSA 8": 3}, listing_hour_hist={20: 5})
            for i in range(1, 12)]
    yahoo = [metrics(seller_key=f"y:s{i}", site="buyee_yahoo", n_rows=i * 100,
                     n_sold=i * 100, grade_mix={"PSA 8": 3},
                     listing_hour_hist={20: 5}) for i in range(1, 12)]
    out = supply_fit_all(ebay + yahoo, params=SupplyParams())
    assert out["ebay:s11"].total > 50        # 站內第一名，不因絕對量小而被壓低
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_seller_supply.py -k renormalis -v`
Expected: FAIL — `ImportError: cannot import name 'supply_fit_all'`

- [ ] **Step 3: 實作**

```python
@dataclass(slots=True)
class SupplyParams:
    weights: dict[str, float] = field(default_factory=lambda: {
        "sold_depth": 25.0,
        "supply_scale": 25.0,
        "grade_profile": 15.0,
        "series_focus": 10.0,
        "listing_rhythm": 5.0,
    })
    #: 站內賣家數少於這個值就不排名——百分位在小樣本裡沒有意義。
    min_site_sellers: int = 10
    #: 至少要有幾個維度算得出來才給分數。
    min_dimensions: int = 2


@dataclass(slots=True)
class SupplyFit:
    seller_key: str
    site: str
    ok: bool
    reason: str
    total: float | None = None          # ok=False 時必定 None，不是 0
    dimensions: list[SupplyDimension] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    n_dimensions_used: int = 0
    n_dimensions_total: int = 5
```

`supply_fit_all(metrics_list, *, params)` 的流程：

1. 按 `site` 分組。站內賣家數 `< params.min_site_sellers` → 該站全部 `ok=False`，`reason=f"站內賣家數只有 {n} 個（門檻 {min}），百分位沒有意義"`。
2. 對每個賣家算五個 `SupplyDimension`。
3. 對每個維度，在**站內**取所有 `available=True` 的 `raw` 值算百分位：`score = 100 * (在站內比我小的個數) / max(該維度站內可得賣家數 - 1, 1)`。
4. 可得維度數 `< params.min_dimensions` → `ok=False`，`reason` 說明只有幾個維度可得。
5. `total = sum(w_i * score_i) / sum(w_i)`，只加總可得的維度。
6. `missing` 收集每個不可得維度的說明；`caveats` 加上：可得維度 <3 時 `"⚠️ 只用了 N/5 個維度算出來"`、`observation_span_days < 7` 時 `"⚠️ 觀測跨度只有 X 天——這是橫斷面的供給規模，不是「持續」供給"`。

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/pytest tests/test_seller_supply.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(seller): Supply Fit 站內百分位與可得性正規化"
```

---

## Task 4: 結構性防線——兩個分數不能相加

**Files:**
- Test: `tests/test_seller_supply.py`

這一條是本計劃最重要的測試。它守的不是某個 bug，是防止未來有人「順手」把兩個分數合起來。

- [ ] **Step 1: 寫測試**

```python
import inspect

from ygo_sniper import seller_supply


def test_supply_module_never_imports_the_valuator_or_alpha_score():
    """Supply Fit 不准碰估值模型，也不准讀 Alpha 的分數。
    它只吃 SellerMetrics 的原始量。這是結構性的，不是靠人記得。"""
    src = inspect.getsource(seller_supply)
    for banned in ("valuator", "Valuator", "SellerScore", "discount_ratio", "model_ratio"):
        assert banned not in src, f"seller_supply 不該提到 {banned}"


def test_supply_fit_of_a_seller_with_zero_peers_is_still_scoreable():
    """整個計劃的重點：181 個零可比賣家必須算得出 Supply Fit。"""
    ms = [metrics(seller_key=f"y:s{i}", site="buyee_yahoo", n_rows=i, n_sold=i,
                  n_comparable=0,                      # 零可比
                  grade_mix={"PSA 8": 3}, listing_hour_hist={20: 5})
          for i in range(1, 12)]
    out = supply_fit_all(ms, params=SupplyParams())
    assert sum(1 for f in out.values() if f.ok) >= 10
```

- [ ] **Step 2: 跑測試**

Run: `.venv/bin/pytest tests/test_seller_supply.py -k "never_imports or zero_peers" -v`
Expected: 2 passed（如果 FAIL，代表實作偷用了 alpha 的東西——修實作，不要改測試）

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "test(seller): 釘死 Supply Fit 與 Alpha 不得相加"
```

---

## Task 5: 監控名單入選改雙軌

**Files:**
- Modify: `src/ygo_sniper/seller_watch.py`（`WatchParams` :124 附近、`sync_auto_watch` :305-330）
- Modify: `config/settings.yaml`（`seller_alpha` 區塊 :205-267）
- Test: `tests/test_seller_watch.py`

- [ ] **Step 1: 寫失敗測試（加到 `tests/test_seller_watch.py`）**

```python
from ygo_sniper.seller_alpha import AlphaReport, SellerScore
from ygo_sniper.seller_supply import SupplyFit
from ygo_sniper.seller_watch import WatchParams, sync_auto_watch

CHEAP = "buyee_yahoo:cheap"
BIG = "buyee_yahoo:bigsupply"


def _two_track_fixtures():
    """cheap 只有 Alpha、bigsupply 只有 Supply Fit——兩軌各一個代表。"""
    report = AlphaReport(scores={
        CHEAP: SellerScore(seller_key=CHEAP, ok=True, reason="", total=40.0),
        BIG: SellerScore(seller_key=BIG, ok=False, reason="證據不足", total=None),
    })
    supply = {
        CHEAP: SupplyFit(seller_key=CHEAP, site="buyee_yahoo", ok=True,
                         reason="", total=20.0, n_dimensions_used=3),
        BIG: SupplyFit(seller_key=BIG, site="buyee_yahoo", ok=True,
                       reason="", total=70.0, n_dimensions_used=3),
    }
    return report, supply


def test_seller_with_no_alpha_but_high_supply_fit_gets_watched(store):
    """打破雞生蛋：需要 alpha 才進名單、進名單才長得出 ask 觀測、
    有 ask 觀測才算得出 alpha。高供給的賣家必須進得去。"""
    report, supply = _two_track_fixtures()
    result = sync_auto_watch(store, report, WatchParams(), supply=supply)
    assert BIG in [a["seller_key"] for a in result["added"]]
    row = store.get_seller_watch(BIG)
    assert "供給" in row["reason"]          # 入選理由必須寫明是哪一軌


def test_watch_reason_distinguishes_the_two_tracks(store):
    """名單裡必須看得出來誰是「便宜」進來的、誰是「值得盯」進來的——
    否則使用者會以為名單上每個都是便宜賣家。"""
    report, supply = _two_track_fixtures()
    sync_auto_watch(store, report, WatchParams(), supply=supply)
    assert "Alpha" in store.get_seller_watch(CHEAP)["reason"]
    assert "供給" in store.get_seller_watch(BIG)["reason"]
```

> `store.get_seller_watch(seller_key) -> dict | None` 已存在（[store.py:1167](../../../src/ygo_sniper/store.py)）。相關方法：`list_seller_watch`（:1152）、`upsert_seller_watch`（:1174）、`deactivate_seller_watch`（:1202）、`mark_seller_watch_scanned`（:1215）。`SellerScore` 的欄位見 [seller_alpha.py:831-855](../../../src/ygo_sniper/seller_alpha.py)。

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/pytest tests/test_seller_watch.py -k "supply or two_tracks" -v`
Expected: FAIL — `sync_auto_watch() got an unexpected keyword argument 'supply'`

- [ ] **Step 3: 實作**

- `WatchParams` 新增 `supply_min_score: float = 60.0`（Task 7 會校準這個值），config key `seller_alpha.watch_supply_min_score`。
- `sync_auto_watch(store, report, params, *, supply=None, now=None)`：`supply` 是 `dict[str, SupplyFit]`，預設 `None`（保持向後相容，既有測試不會壞）。
- 判定改為：alpha 軌 `score.ok and score.total >= params.auto_min_score` → `reason = f"Alpha {total:.1f} 分（同儕相對折價）"`；supply 軌 `fit.ok and fit.total >= params.supply_min_score` → `reason = f"供給契合 {total:.1f} 分（{n_used}/5 維度）——尚未有 Alpha 證據，盯著累積 ask 觀測"`。
- **兩軌都過時 reason 兩者都寫。** 名單容量仍是 30、仍然只加不刪、manual 仍然不被擠掉——這些不要動。
- **淘汰順位**：既有邏輯是低分被高分擠掉。supply 軌入選的賣家在被擠掉時**優先於** alpha 軌（alpha 是實證的便宜，supply 只是假設），在 `sync_auto_watch` 的排序鍵裡體現。

- [ ] **Step 4: 跑測試**

Run: `.venv/bin/pytest tests/test_seller_watch.py -v`
Expected: 47 passed（原 45 + 新 2），**既有 45 條全部仍需通過**

- [ ] **Step 5: 接上 pipeline**

`src/ygo_sniper/pipeline.py:527` 呼叫 `sync_auto_watch` 的地方，補上 `supply=supply_fit_all(report.metrics.values(), params=...)`。

- [ ] **Step 6: 跑全測試 + commit**

Run: `make test`
Expected: 1220+ passed（不得有既有測試轉紅）

```bash
git add -A && git commit -m "feat(seller): 監控名單入選改雙軌（Alpha ∪ Supply Fit）"
```

---

## Task 6: CLI 與 dashboard 並排顯示

**Files:**
- Modify: `src/ygo_sniper/cli.py`（`sellers` 命令 :691-731）
- Modify: `web/app.py`（`/api/sellers` :717 附近）
- Modify: `web/static/index.html`（:2055-2210）

- [ ] **Step 1: CLI 新增 `--supply`**

在 `sellers` 命令加 `supply: bool = typer.Option(False, "--supply", help="供給契合度排行榜（值不值得盯，與 Alpha 是兩件事）")`。輸出 rich `Table`，欄位：`seller_key` / `供給分` / `維度` (`3/5`) / `n_rows` / `n_sold` / `8-9分佔比` / `系列集中` / `Alpha`（有就顯示，沒有顯示 `—`，**不要顯示 0**）。表頭副標必須寫一句話：`供給契合度回答「值不值得盯」，不是「便不便宜」——兩欄不可相加`。

- [ ] **Step 2: 端到端跑一次**

Run: `.venv/bin/ygo-sniper sellers --supply`
Expected: 印出表格，列數 > 100（因為零可比賣家現在也有分數了），且 Alpha 欄大量顯示 `—`

- [ ] **Step 3: API 與前端**

`/api/sellers` payload 加 `supply_fit` 陣列（欄位同 CLI）。前端 👤 賣家視圖改成兩個區塊並排，沿用既有「不給分數 vs 0 分」的區分樣式（[index.html:2055-2210](../../../web/static/index.html) 已有這個處理，照抄）。

- [ ] **Step 4: 端到端驗證**

Run: `.venv/bin/ygo-sniper serve` 然後開 `http://127.0.0.1:8321`，點 👤 賣家視圖
Expected: 兩個排行榜並排；供給榜的列數明顯多於 Alpha 榜；供給榜有賣家的 Alpha 欄顯示「證據不足」而非 0

> 注意 CLAUDE.md 第六節：`serve` 這個指令本身出過事（console script 的 `sys.path[0]` 是 `.venv/bin`）。**要跑真正的 `ygo-sniper serve`，不要用 `python -m` 或手動加 sys.path 繞過。**

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(seller): CLI 與 dashboard 並排顯示兩個分數"
```

---

## Task 7: 校準門檻（不憑直覺調）

**Files:**
- Modify: `config/settings.yaml`

CLAUDE.md 第七節：要調任何門檻之前，先實測分佈重新量一次，不要照直覺調。`supply_min_score: 60.0` 是 Task 5 隨手放的預設值，必須校準。

- [ ] **Step 1: 量分佈**

Run: `.venv/bin/ygo-sniper sellers --supply | head -60`

記錄：有多少賣家 `ok=True`、分數的 P50/P75/P90、各站分別多少。

- [ ] **Step 2: 定門檻**

判準：**名單上限是 30，manual 佔 1，alpha 軌現有 16——所以 supply 軌大約只有 10-13 個名額。** 把 `supply_min_score` 設在「站內大約各取前 3-4 名」的位置。門檻定好後，在 `config/settings.yaml` 的參數旁寫一行註解記錄**當時量到的分佈**（照 `settings.yaml:205-267` 既有的「參數依據」慣例）。

- [ ] **Step 3: 驗證入選結果**

Run: `.venv/bin/ygo-sniper sellers --sync-watch` 然後 `.venv/bin/ygo-sniper watch-seller list`
Expected: active 數量 ≤ 30；新入選的賣家 reason 寫著「供給契合」；原有的 alpha 軌賣家沒有被擠掉

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "chore(seller): 依實測分佈校準 supply_min_score"
```

---

## Task 8: 修過時的文件與註解

已實測過時（會誤導下一棒）：

- [ ] `config/settings.yaml:264`、`seller_watch.py:15-18`、`web/static/index.html:2148` 都還寫著「95 個賣家只有 5 個過門檻／auto 只選得出 1 個」，實際是 **360 個賣家、34 個過門檻、17 個在名單上**。
- [ ] `seller_alpha.py:81, 807, 1052` 寫著「Yahoo 賣家頁評價還沒抓（open item）」，但 `sources/yahoo.py:656-674` 已經在抓 goodRatio。
- [ ] `seller_alpha.py:793` 寫著「PayPay SOLD 尚未抓」，實際已在挖。
- [ ] `seller_alpha.py:310-311` 的註解說 `listing_obs.card_name` 是 `0/416`，實測現在是 `0/538`——數字更新即可，結論不變。

改法：每處改成當下實測值 + `<!-- 2026-08-05 實測 -->` 或 `# 2026-08-05 實測` 註記。

```bash
git add -A && git commit -m "docs(seller): 修正過時的現況數字與已完成的 open item"
```

---

## 驗收（交付前主對話必須親跑，subagent 的回報不算證據）

1. `make test` → 全綠，且測試數 ≥ 原本的 1220 + 新增數
2. `.venv/bin/ygo-sniper sellers --supply` → 有分數的賣家數 **顯著多於 34**（這是整個計劃的目的）
3. `.venv/bin/ygo-sniper watch-seller list` → active ≤ 30，且看得出誰是哪一軌進來的
4. `.venv/bin/ygo-sniper serve` → 真的跑得起來（不要用 `python -m` 繞過），賣家視圖兩榜並排
5. `grep -rn "valuator\|SellerScore" src/ygo_sniper/seller_supply.py` → **0 命中**

**不需要跑 `corpus-diff`**：本計劃不動任何過濾或解析規則。

---

## Phase 2（另開計劃，等 Phase 1 的分佈量出來再寫）

這三項需要新資料或新研究，不放進 Phase 1：

1. **「上架後多久成交」**（使用者原表 15%）。現在只有 `n_disappeared` / `n_window_exit` / `n_still_open` 三個計數，沒有 duration。**主要障礙是「消失 ≠ 成交」**——消失有三種原因：真的賣掉、賣家自己下架、我們掃描漏掉（`listing_obs.revived_count` 欄位的存在就證明有東西消失又回來）。把消失當成交會系統性高估成交速度，那是 CLAUDE.md 第五節的變種。做法：只在能對上 `comps` 成交記錄時才算「成交」，其餘記為 `disappeared_unknown` 且不計入分母；競標（必定結標）與定價（消失才有資訊）要分開處理。
2. **「是否接受議價」**（使用者原表 10%）。需要新抓 offer flag（eBay Best Offer、PayPay／Mercari 的値下げ交渉）。**Yahoo 拍賣沒有這個功能**，所以這個維度天生站台不均，必須走 `unavailable` 路徑而不是給 0。
3. **權重校準**。Phase 1 的權重直接沿用使用者提供的表，只做了可得性正規化。等 Phase 1 累積 4-6 週資料後，可以做真正的驗證：**被 supply 軌選進名單的賣家，後來有沒有真的長出 alpha？** 那才是這套分數唯一有意義的驗收——如果答案是否定的，該修的是維度定義，不是權重。

Phase 2 的進入條件：Phase 1 上線滿 4 週，且 `listing_obs` 列數 ≥ 3000（現在 538）。
