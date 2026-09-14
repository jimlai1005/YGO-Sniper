# 市場漂移監視器（A）＋ 估價時間加權實驗（C） Implementation Plan

> **For agentic workers:** 逐 task 執行，TDD（先寫紅燈測試再實作）。每個 task 完成後主線程親跑驗收指令。
> Steps use checkbox (`- [ ]`) syntax for tracking. 每個 task 都標 `@sdd`（haiku 機械執行）：
> 檔案路徑、簽名、驗收指令全部給定，**照抄即可**；遇到本檔沒寫的判斷題→停下回報，不要自己決定。

**Goal:** (A) 每天用本地 comps 算「同卡重複成交」的 4 週價格倍率（分 PSA10/PSA9/PSA≤8/ARS10/ARS9/ARS≤8/ALL），
存進 meta、顯示在 `health` 與 dashboard，某帶跌 ≥10% 就推 Telegram（送達才消耗 pending）；另抓
yugioh-relief.com 的週次 PSA 浮雕指數當**外部對照**（只顯示，永不進計算）。
(C) 估價模型加「成交時間衰減權重」（半衰期天數，預設 `null`＝等權，行為與現況逐位元相同），並讓
`coverage-time` 能一次比較 等權／45／60／90 天，輸出下尾違反率，供預註冊判準決定是否上線。

**Architecture:** A 是新模組 `src/ygo_sniper/market_drift.py`（純函式＋一個 refresh 入口），資料只讀
`comps` 表中 `sold_at_is_ingest=0` 的列（真實成交日；Mercari 的入庫日列一律不進指數）。結果 JSON 存
`meta` 表（不建新表：指數可從 comps 隨時重算，comps 才是事實）。告警走既有「送達才清 pending」模式
（`cli._send_schedule_alert` 同款）。C 只改 `valuation.py` 的**池中位數與截距**（L1/L2/L3 與 base），
分數／稀有度／平台溢價曲線維持等權（結構性溢價變化慢，加權只該改「水準」不該改「結構」）。

**Tech Stack:** Python 3.12、pytest、typer、rich、FastAPI（web/app.py）、既有 `CachedFetcher`。

**背景（為什麼做，遇到歧義往這裡靠）：** 2026 年 6-7 月 PSA10 浮雕急漲（外站指數 123→169），8 月中起回吐
到 138；PSA9 只 −4%、PSA8 以下持平。估價模型的擬合**沒有任何時間視窗**（`build_valuator` →
`comps_by(limit=5000)` 不帶 `since_days`），等權吃下 2 月以來全部成交。市場若繼續跌破春季水準，等權模型會
系統性高估 → 出價上限開太高（本專案唯一會賠錢的方向）。A 讓這件事**看得見**；C 先做實驗不動生產。

**預註冊判準（C 的上線條件，寫在這裡就不能事後改）：** 見 Task 11。

**明確不做（YAGNI）：**
- 不把外部指數乘進估價（混源，CLAUDE.md 第三節）。
- 不改 `comps_window_days`、不改 conformal 分群、不改任何閘門參數。
- 不改 Mercari 的 `sold_at` 語意（入庫日）；它們在 A 裡被排除、在 C 裡按入庫日加權（已知限制，寫進 docstring）。
- 不做出價上限的「證據陳舊」閘門（使用者決定不做）。
- 不新建 DB 表。

**檔案總覽：**
- Create: `src/ygo_sniper/market_drift.py`、`tests/test_market_drift.py`、`tests/test_valuation_recency.py`
- Modify: `src/ygo_sniper/config.py`、`config/settings.yaml`、`src/ygo_sniper/store.py`、`src/ygo_sniper/cli.py`、
  `web/app.py`、`web/static/index.html`、`src/ygo_sniper/valuation.py`、`CLAUDE.md`
- 已存在（主線程 2026-09-14 從生產路徑實抓）：`tests/fixtures/relief_trends_snippet.html`

**共同慣例：**
- 測試指令一律 `.venv/bin/pytest tests/<file>.py -v`（**不要**自己加 `-q`）。全量：`make test`。
- commit message 格式照 git log：`feat(scope): 中文一句話`，結尾加一行
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- 每個 task 只 `git add` 該 task 列出的檔案。

---

## Part A：市場漂移監視器

### Task 1 @sdd：Config 加 `market_drift` 節

**Files:**
- Modify: `src/ygo_sniper/config.py`（`Config` dataclass 約 146-178 行；`load_config` 約 352-365 行）
- Modify: `config/settings.yaml`（在 `valuation:` 節結束、`fetch:` 節開始之前，約 386 行處插入）
- Test: `tests/test_market_drift.py`（新檔）

- [ ] **Step 1: 紅燈測試**（建立 `tests/test_market_drift.py`）

```python
"""市場漂移監視器：同卡重複成交倍率、外部指數解析、告警帳本。"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ygo_sniper.config import load_config
from ygo_sniper.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


class TestConfig:
    def test_market_drift_section_is_loaded(self):
        cfg = load_config()
        md = cfg.market_drift
        assert md["recent_days"] == 28
        assert md["base_days"] == 56
        assert md["alert_ratio"] == 0.90
        assert md["min_pairs"] == 8
        assert md["external_url"].startswith("https://yugioh-relief.com/")
        assert md["external_enabled"] is True
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: FAIL，`AttributeError: 'Config' object has no attribute 'market_drift'`

- [ ] **Step 3: 實作**

`src/ygo_sniper/config.py`，在 `Config` 的 `seller_alpha` 欄位後面加：

```python
    # 市場漂移監視器（settings.yaml 的 market_drift:）。缺區塊時 market_drift.py 走預設值。
    # 與 seller_alpha 同理：沒有這個欄位，yaml 裡打的區塊會被 load_config 丟掉而不報錯。
    market_drift: dict[str, Any] = field(default_factory=dict)
```

`load_config` 的 `return Config(` 呼叫裡，緊接 `valuation=settings.get("valuation", {}),` 之後加一行：

```python
        market_drift=settings.get("market_drift", {}),
```

`config/settings.yaml`，在 `valuation:` 節最後一行（`    normal: 1.0`）之後、`fetch:` 之前插入：

```yaml

# ---------------------------------------------------------------------------
# 市場漂移監視器（market_drift.py）。只顯示、只告警，**永遠不進估價**。
# 2026-09-14 背景：6-7 月 PSA10 浮雕急漲後 8 月中起回吐（外站指數 169→138），
# 估價模型的擬合沒有時間視窗，市場繼續跌時會系統性高估——這一節讓它看得見。
# ---------------------------------------------------------------------------
market_drift:
  # 「近 recent_days 天」對「再往前 base_days 天」的同卡重複成交倍率。
  recent_days: 28
  base_days: 56
  # 某帶倍率 ≤ alert_ratio 且配對數 ≥ min_pairs 才告警（配對不足＝「讀不到」，不是「沒跌」）。
  alert_ratio: 0.90
  min_pairs: 8
  # 外部對照：yugioh-relief.com 的週次 PSA 浮雕指數（只涵蓋浮雕、只有 PSA/ARS，與本庫不同基準）。
  external_enabled: true
  external_url: "https://yugioh-relief.com/psa-market-daily-trends/"
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/config.py config/settings.yaml tests/test_market_drift.py
git commit -m "feat(config): 新增 market_drift 設定節（漂移監視器門檻與外部對照 URL）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2 @sdd：`Store.comps_for_drift()`

**Files:**
- Modify: `src/ygo_sniper/store.py`（緊接 `comps_by` 方法之後，約 1180 行）
- Test: `tests/test_market_drift.py`（append）

- [ ] **Step 1: 紅燈測試**（append 到 `tests/test_market_drift.py`）

```python
def _insert_comp(store, *, signature, sold_at, price, grader="PSA", grade=10.0,
                 ingest=0, era="1999", dup_of=None, currency="JPY", site="buyee_yahoo"):
    with store._conn() as c:
        c.execute(
            "INSERT INTO comps (signature, title, price_twd, price_native, currency, site, "
            "sold_at, grader, grade, era_evidence, sold_at_is_ingest, dup_of_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (signature, f"t {signature}", price * 0.22, price, currency, site,
             sold_at, grader, grade, era, ingest, dup_of),
        )


class TestCompsForDrift:
    def test_only_real_dated_era_verified_non_dup_rows(self, store):
        _insert_comp(store, signature="a", sold_at="2026-09-01T00:00:00+00:00", price=1000)
        _insert_comp(store, signature="b", sold_at="2026-09-01T00:00:00+00:00", price=1000, ingest=1)
        _insert_comp(store, signature="c", sold_at="2026-09-01T00:00:00+00:00", price=1000, era="")
        _insert_comp(store, signature="d", sold_at="2026-09-01T00:00:00+00:00", price=1000, dup_of=1)
        _insert_comp(store, signature="e", sold_at="2026-09-01T00:00:00+00:00", price=0)
        rows = store.comps_for_drift()
        assert [r["signature"] for r in rows] == ["a"]
        assert set(rows[0]) >= {"signature", "sold_at", "price_native", "currency", "grader", "grade"}
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v -k CompsForDrift`
Expected: FAIL，`AttributeError: 'Store' object has no attribute 'comps_for_drift'`

- [ ] **Step 3: 實作**（`store.py`，放在 `comps_by` 方法的 `return` 之後、下一個 `def` 之前，縮排與 `comps_by` 同層）

```python
    def comps_for_drift(self) -> list[dict[str, Any]]:
        """漂移指數的樣本：**只要真實成交日**（`sold_at_is_ingest = 0`）。

        Mercari／部分 PayPay 的 `sold_at` 是入庫時間（見 `load_comps` 警告），拿它們算
        「近 4 週 vs 前 8 週」量到的是我們的抓取順序，不是市場——所以一律排除，
        寧可指數樣本薄，也不要一個看起來正常但全部是錯的數字。
        同時排除去重副本（`dup_of_id`）與無年代證據列（與估價取樣同判準）。
        """
        q = (
            "SELECT signature, sold_at, price_native, currency, grader, grade, site "
            "FROM comps WHERE COALESCE(sold_at_is_ingest, 0) = 0 AND dup_of_id IS NULL "
            "AND price_native > 0 AND COALESCE(era_evidence, '') <> '' "
            "ORDER BY sold_at"
        )
        with self._conn() as c:
            return [dict(r) for r in c.execute(q).fetchall()]
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/store.py tests/test_market_drift.py
git commit -m "feat(store): comps_for_drift——漂移指數只取真實成交日、非重複、有年代證據的列

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3 @sdd：`market_drift.py` 純函式——分帶、重複成交倍率、序列、告警判定

**Files:**
- Create: `src/ygo_sniper/market_drift.py`
- Test: `tests/test_market_drift.py`（append）

- [ ] **Step 1: 紅燈測試**（append）

```python
from ygo_sniper.market_drift import (  # noqa: E402
    ALL_BAND,
    BANDS,
    DriftPoint,
    band_of,
    drift_series,
    evaluate_alert,
    repeat_sale_ratio,
)


def _row(sig, day, price, grader="PSA", grade=10.0):
    return {"signature": sig, "sold_at": f"2026-{day}T12:00:00+00:00", "price_native": price,
            "currency": "JPY", "grader": grader, "grade": grade, "site": "buyee_yahoo"}


AS_OF = datetime(2026, 9, 14, tzinfo=UTC)


class TestBandOf:
    def test_bands(self):
        assert band_of("PSA", 10.0) == "PSA10"
        assert band_of("psa", 9.0) == "PSA9"
        assert band_of("PSA", 8.0) == "PSA<=8"
        assert band_of("ARS", 10.0) == "ARS10"
        assert band_of("ARS", 9.5) == "ARS9"
        assert band_of("ARS", 7.0) == "ARS<=8"

    def test_unknown_grade_or_grader_is_excluded(self):
        assert band_of("PSA", None) is None
        assert band_of("BGS", 10.0) is None
        assert band_of(None, 10.0) is None


class TestRepeatSaleRatio:
    def test_pairs_same_signature_recent_vs_base(self):
        rows = [
            # 卡 a：前 56 天 1000、近 28 天 800 → ×0.8
            _row("a", "07-10", 1000), _row("a", "09-01", 800),
            # 卡 b：前 56 天 2000（兩筆中位 2000）、近 28 天 1800 → ×0.9
            _row("b", "07-20", 1900), _row("b", "08-01", 2100), _row("b", "09-05", 1800),
            # 卡 c：只有近期 → 不配對
            _row("c", "09-02", 500),
            # 卡 d：太舊（超過 84 天）→ 不進 base
            _row("d", "06-01", 100), _row("d", "09-03", 50),
        ]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        p = pts["PSA10"]
        assert p.n_pairs == 2
        assert p.ratio == pytest.approx(math.exp((math.log(0.8) + math.log(0.9)) / 2))
        assert p.n_recent == 4 and p.n_base == 3
        assert pts[ALL_BAND].n_pairs == 2
        assert p.as_of == "2026-09-14"

    def test_no_pairs_gives_none_ratio_not_one(self):
        rows = [_row("a", "09-01", 800)]
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].ratio is None
        assert pts["PSA10"].n_pairs == 0

    def test_every_band_is_reported_even_when_empty(self):
        pts = repeat_sale_ratio([], as_of=AS_OF)
        assert [p.band for p in pts] == list(BANDS) + [ALL_BAND]

    def test_non_jpy_and_nonpositive_rows_are_skipped(self):
        rows = [_row("a", "07-10", 1000), _row("a", "09-01", 800)]
        rows[1]["currency"] = "USD"
        pts = {p.band: p for p in repeat_sale_ratio(rows, as_of=AS_OF)}
        assert pts["PSA10"].n_pairs == 0


class TestDriftSeries:
    def test_series_is_weekly_ascending_and_ends_at_as_of(self):
        rows = [_row("a", "07-10", 1000), _row("a", "09-01", 800)]
        pts = drift_series(rows, as_of=AS_OF, weeks=3)
        as_ofs = sorted({p.as_of for p in pts})
        assert as_ofs == ["2026-08-31", "2026-09-07", "2026-09-14"]
        assert len(pts) == 3 * (len(BANDS) + 1)


class TestEvaluateAlert:
    def _pt(self, band, ratio, n):
        return DriftPoint(band=band, as_of="2026-09-14", ratio=ratio, n_pairs=n,
                          n_recent=n, n_base=n)

    def test_alert_when_ratio_at_or_below_threshold_with_enough_pairs(self):
        pts = [self._pt("PSA10", 0.85, 9), self._pt("PSA9", 0.97, 20)]
        text, bands = evaluate_alert(pts, alert_ratio=0.90, min_pairs=8)
        assert bands == ["PSA10"]
        assert "PSA10" in text and "×0.85" in text and "n=9" in text
        assert "PSA9" not in text

    def test_insufficient_pairs_never_alerts(self):
        pts = [self._pt("PSA10", 0.50, 7)]
        assert evaluate_alert(pts, alert_ratio=0.90, min_pairs=8) == (None, [])

    def test_none_ratio_never_alerts(self):
        pts = [self._pt("PSA10", None, 0)]
        assert evaluate_alert(pts, alert_ratio=0.90, min_pairs=8) == (None, [])

    def test_all_band_is_not_used_for_alert(self):
        pts = [self._pt(ALL_BAND, 0.5, 50)]
        assert evaluate_alert(pts, alert_ratio=0.90, min_pairs=8) == (None, [])
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'ygo_sniper.market_drift'`

- [ ] **Step 3: 實作**（建立 `src/ygo_sniper/market_drift.py`）

```python
"""市場漂移監視器：同卡重複成交倍率（本地）＋外部週次指數（對照）。

為什麼要有這個模組（2026-09-14）：估價模型的擬合沒有時間視窗，市場整體移動時
它只會慢慢跟；市場**跌**的時候等權模型系統性高估，出價上限就會開太高。
沒有這個監視器，市場跌三成與「最近沒好貨」外顯一模一樣（CLAUDE.md 第五節）。

兩條鐵律：
1. **只顯示、只告警，永遠不進估價。** 外部指數只涵蓋 PSA/ARS 浮雕（與本庫不同基準）；
   本地指數樣本薄。任何一個乘進估價都是混源比較（第三節）。
2. **配對不足＝讀不到，不是沒跌。** `ratio=None`／`n_pairs < min_pairs` 一律不告警，
   但要在 health 裡大聲印出「樣本不足」，不能靜默。
"""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

#: 告警與顯示用的分帶（順序即顯示順序）。ALL 只顯示不告警（混機構混分數，只當總覽）。
BANDS: tuple[str, ...] = ("PSA10", "PSA9", "PSA<=8", "ARS10", "ARS9", "ARS<=8")
ALL_BAND = "ALL"

#: meta 表的鍵。pending 只在 Telegram **送達後**才清（與 cli._send_schedule_alert 同一條規則）。
DRIFT_LATEST_KEY = "market_drift_latest"
RELIEF_LATEST_KEY = "relief_index_latest"
RELIEF_ERROR_KEY = "relief_index_error"
DRIFT_PENDING_KEY = "market_drift_alert_pending"
DRIFT_SENT_KEY = "market_drift_alert_sent"
DRIFT_COMPUTED_DATE_KEY = "market_drift_computed_date"


@dataclass(frozen=True, slots=True)
class DriftPoint:
    band: str
    #: 近期視窗的**排他**結束日（ISO 日期）。
    as_of: str
    #: exp(同卡「近期中位對數價 − 基期中位對數價」的中位數)；沒有配對就是 None，不是 1.0。
    ratio: float | None
    n_pairs: int
    n_recent: int
    n_base: int


def band_of(grader: str | None, grade: float | None) -> str | None:
    """PSA/ARS × {10, 9, ≤8}。分數不明或其他機構 → None（不進任何帶）。"""
    if grader is None or grade is None:
        return None
    g = grader.upper()
    if g not in ("PSA", "ARS"):
        return None
    if grade >= 10:
        return f"{g}10"
    if grade >= 9:
        return f"{g}9"
    return f"{g}<=8"


def parse_sold_at(value: Any) -> datetime | None:
    """ISO 字串 → aware datetime（naive 視為 UTC）。解析不出來回 None，不猜。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def repeat_sale_ratio(
    rows: list[dict[str, Any]], *, as_of: datetime, recent_days: int = 28, base_days: int = 56,
) -> list[DriftPoint]:
    """同卡重複成交倍率：近 `recent_days` 天 vs 再往前 `base_days` 天。

    同一個 signature 兩個視窗都有成交才配成一對；每對取「近期中位對數價 − 基期中位對數價」，
    再取所有配對的中位數。同卡對同卡，組成變化（這個月剛好賣的都是貴卡）不會被讀成漲跌。
    只吃 JPY 且價格 > 0 的列（三個 Buyee 系來源都是 JPY；別的幣別混進來就是混源）。
    """
    recent_start = as_of - timedelta(days=recent_days)
    base_start = recent_start - timedelta(days=base_days)
    # band -> signature -> ("recent"/"base") -> [log price]
    buckets: dict[str, dict[str, dict[str, list[float]]]] = {b: {} for b in (*BANDS, ALL_BAND)}
    counts: dict[str, dict[str, int]] = {b: {"recent": 0, "base": 0} for b in (*BANDS, ALL_BAND)}
    for r in rows:
        if (r.get("currency") or "JPY") != "JPY":
            continue
        price = r.get("price_native")
        if not price or price <= 0:
            continue
        d = parse_sold_at(r.get("sold_at"))
        if d is None:
            continue
        if recent_start <= d < as_of:
            window = "recent"
        elif base_start <= d < recent_start:
            window = "base"
        else:
            continue
        band = band_of(r.get("grader"), r.get("grade"))
        if band is None:
            continue
        sig = str(r.get("signature") or "")
        if not sig:
            continue
        for b in (band, ALL_BAND):
            buckets[b].setdefault(sig, {"recent": [], "base": []})[window].append(math.log(price))
            counts[b][window] += 1

    out: list[DriftPoint] = []
    for b in (*BANDS, ALL_BAND):
        diffs = [
            statistics.median(w["recent"]) - statistics.median(w["base"])
            for w in buckets[b].values()
            if w["recent"] and w["base"]
        ]
        out.append(DriftPoint(
            band=b,
            as_of=as_of.date().isoformat(),
            ratio=math.exp(statistics.median(diffs)) if diffs else None,
            n_pairs=len(diffs),
            n_recent=counts[b]["recent"],
            n_base=counts[b]["base"],
        ))
    return out


def drift_series(
    rows: list[dict[str, Any]], *, as_of: datetime, weeks: int = 12,
    recent_days: int = 28, base_days: int = 56,
) -> list[DriftPoint]:
    """每週一個 as_of（往回推 `weeks` 週），最舊在前。給 health／dashboard 畫趨勢用。"""
    out: list[DriftPoint] = []
    for k in range(weeks - 1, -1, -1):
        out.extend(repeat_sale_ratio(
            rows, as_of=as_of - timedelta(days=7 * k),
            recent_days=recent_days, base_days=base_days,
        ))
    return out


def evaluate_alert(
    points: list[DriftPoint], *, alert_ratio: float, min_pairs: int,
) -> tuple[str | None, list[str]]:
    """哪些帶該告警。回 (告警文字或 None, 觸發的帶)。ALL 帶不告警（只當總覽）。"""
    hits = [
        p for p in points
        if p.band in BANDS and p.ratio is not None and p.n_pairs >= min_pairs
        and p.ratio <= alert_ratio
    ]
    if not hits:
        return None, []
    lines = ["📉 市場漂移告警（同卡重複成交，近 4 週 vs 前 8 週）"]
    for p in hits:
        lines.append(f"{p.band} ×{p.ratio:.2f}（n={p.n_pairs}）")
    lines.append("只顯示、不進估價；出價上限仍由區間下緣反推，請人工留意估價可能偏高。")
    return "\n".join(lines), [p.band for p in hits]
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: 全綠（`13 passed`）

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/market_drift.py tests/test_market_drift.py
git commit -m "feat(market-drift): 同卡重複成交倍率、週序列與告警判定（純函式）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4 @sdd：外部指數解析與抓取

**Files:**
- Modify: `src/ygo_sniper/market_drift.py`（append）
- Test: `tests/test_market_drift.py`（append）
- Fixture（已存在，不要改）：`tests/fixtures/relief_trends_snippet.html`

fixture 是 2026-09-14 用生產路徑（httpx，無 JS）實抓的頁面片段。內嵌資料在
`<script type="application/json" class="yra-psa-market-data">{...}</script>`，JSON 頂層鍵是
`records`／`weeklyIndexRecords`／`weeklyIndexMeta`／`generatedAt`／`startDate`（**已對過真實 payload**）。
`weeklyIndexRecords` 每筆有 `weekStart, weekEnd, allIndex, psa10Index, psa9Index, psa8BelowIndex,
allUpdatedCount, allComponentCount, isBaseWeek`。

- [ ] **Step 1: 紅燈測試**（append）

```python
from ygo_sniper.market_drift import fetch_relief_index, parse_relief_weekly_index  # noqa: E402


class TestReliefIndex:
    def test_parse_real_snippet(self):
        html = (FIXTURES / "relief_trends_snippet.html").read_text(encoding="utf-8")
        parsed = parse_relief_weekly_index(html)
        weeks = parsed["weeks"]
        assert len(weeks) == 37
        assert weeks[0]["week_start"] == "2025-12-29" and weeks[0]["all"] == 100.0
        last = weeks[-1]
        assert last["week_start"] == "2026-09-07"
        assert last["all"] == 124.43 and last["psa10"] == 138.12
        assert last["psa9"] == 125.46 and last["psa8_below"] == 109.7
        assert last["updated"] == 72 and last["components"] == 1205
        assert parsed["meta"]["representativePrice"] == "weekly_median_carry_forward"

    def test_missing_block_raises(self):
        with pytest.raises(ValueError):
            parse_relief_weekly_index("<html><body>nothing</body></html>")

    def test_fetch_uses_fetcher_and_returns_last_12_weeks(self):
        html = (FIXTURES / "relief_trends_snippet.html").read_text(encoding="utf-8")

        class FakeFetcher:
            def __init__(self):
                self.calls = []

            def get(self, url, **kw):
                self.calls.append((url, kw))
                return html

        f = FakeFetcher()
        out = fetch_relief_index(f, "https://yugioh-relief.com/psa-market-daily-trends/")
        assert f.calls[0][0] == "https://yugioh-relief.com/psa-market-daily-trends/"
        assert f.calls[0][1] == {"use_cache": False}
        assert len(out["weeks"]) == 12
        assert out["weeks"][-1]["week_start"] == "2026-09-07"
        assert "fetched_at" in out
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v -k ReliefIndex`
Expected: FAIL，`ImportError: cannot import name 'fetch_relief_index'`

- [ ] **Step 3: 實作**（append 到 `market_drift.py`）

```python
# ---------------------------------------------------------------------------
# 外部對照：yugioh-relief.com 週次 PSA 浮雕指數
# ---------------------------------------------------------------------------
_RELIEF_DATA_RE = re.compile(
    r'<script type="application/json" class="yra-psa-market-data">(.*?)</script>', re.S
)


def parse_relief_weekly_index(html: str) -> dict[str, Any]:
    """從趨勢頁 HTML 抽內嵌 JSON 的 `weeklyIndexRecords`。

    鍵名 2026-09-14 對過真實 payload（tests/fixtures/relief_trends_snippet.html 就是那份）。
    找不到資料塊或鍵名變了 → 拋 ValueError（對方改版要大聲，不能回空清單假裝沒事）。
    """
    m = _RELIEF_DATA_RE.search(html)
    if not m:
        raise ValueError("relief trends page: 找不到 yra-psa-market-data 資料塊（對方改版？）")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise ValueError(f"relief trends page: 資料塊不是合法 JSON：{exc}") from exc
    records = data.get("weeklyIndexRecords")
    if not isinstance(records, list) or not records:
        raise ValueError("relief trends page: weeklyIndexRecords 缺失或為空（鍵名改了？）")
    weeks = []
    for r in records:
        weeks.append({
            "week_start": r["weekStart"],
            "week_end": r["weekEnd"],
            "all": float(r["allIndex"]),
            "psa10": float(r["psa10Index"]),
            "psa9": float(r["psa9Index"]),
            "psa8_below": float(r["psa8BelowIndex"]),
            "updated": int(r["allUpdatedCount"]),
            "components": int(r["allComponentCount"]),
        })
    return {"weeks": weeks, "meta": data.get("weeklyIndexMeta") or {}}


def fetch_relief_index(fetcher: Any, url: str, *, keep_weeks: int = 12) -> dict[str, Any]:
    """抓外部指數。一律 `use_cache=False`（要的是今天的頁，不是 cache_ttl 內的舊頁）。
    抓取失敗由 fetcher 拋 FetchError，這裡不吞。"""
    html = fetcher.get(url, use_cache=False)
    parsed = parse_relief_weekly_index(html)
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "source": url,
        "weeks": parsed["weeks"][-keep_weeks:],
        "meta": parsed["meta"],
    }
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: 全綠（`16 passed`）

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/market_drift.py tests/test_market_drift.py tests/fixtures/relief_trends_snippet.html
git commit -m "feat(market-drift): 解析 yugioh-relief.com 週次指數（實抓 fixture、鍵名已對真實 payload）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5 @sdd：refresh 入口＋meta 持久化＋告警 pending 帳本

**Files:**
- Modify: `src/ygo_sniper/market_drift.py`（append）
- Test: `tests/test_market_drift.py`（append）

- [ ] **Step 1: 紅燈測試**（append）

```python
from ygo_sniper.market_drift import (  # noqa: E402
    DRIFT_LATEST_KEY,
    DRIFT_PENDING_KEY,
    DRIFT_SENT_KEY,
    RELIEF_ERROR_KEY,
    RELIEF_LATEST_KEY,
    deliver_drift_alert,
    refresh_market_drift,
)
from ygo_sniper.sources.base import FetchError  # noqa: E402


class _Cfg:
    def __init__(self, external_enabled=True):
        self.market_drift = {
            "recent_days": 28, "base_days": 56, "alert_ratio": 0.90, "min_pairs": 1,
            "external_enabled": external_enabled,
            "external_url": "https://yugioh-relief.com/psa-market-daily-trends/",
        }


class _FetcherOK:
    def get(self, url, **kw):
        return (FIXTURES / "relief_trends_snippet.html").read_text(encoding="utf-8")


class _FetcherDown:
    def get(self, url, **kw):
        raise FetchError("boom", url=url, status=503)


class _Notifier:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    def send_alert(self, text):
        self.sent.append(text)
        return self.ok


def _seed_drop(store):
    """PSA10：a、b 兩張卡各跌到 0.8 → 帶倍率 0.8，配對 2。"""
    for sig in ("a", "b"):
        _insert_comp(store, signature=sig, sold_at="2026-07-20T00:00:00+00:00", price=1000)
        _insert_comp(store, signature=sig, sold_at="2026-09-01T00:00:00+00:00", price=800)


class TestRefreshMarketDrift:
    def test_persists_local_series_and_external(self, store):
        _seed_drop(store)
        res = refresh_market_drift(store, _FetcherOK(), _Cfg(), now=AS_OF)
        latest = json.loads(store.get_meta(DRIFT_LATEST_KEY))
        assert latest["as_of"] == "2026-09-14"
        assert latest["recent_days"] == 28 and latest["base_days"] == 56
        psa10 = [p for p in latest["latest"] if p["band"] == "PSA10"][0]
        assert psa10["ratio"] == pytest.approx(0.8) and psa10["n_pairs"] == 2
        assert len(latest["series"]) == 12 * 7
        assert latest["alert_bands"] == ["PSA10"]
        ext = json.loads(store.get_meta(RELIEF_LATEST_KEY))
        assert ext["weeks"][-1]["week_start"] == "2026-09-07"
        assert store.get_meta(RELIEF_ERROR_KEY, "") == ""
        assert res["alert_text"] is not None and "PSA10" in res["alert_text"]
        pending = json.loads(store.get_meta(DRIFT_PENDING_KEY))
        assert pending["bands"] == {"PSA10": "2026-09-14"}

    def test_external_failure_is_recorded_not_raised(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherDown(), _Cfg(), now=AS_OF)
        assert store.get_meta(RELIEF_LATEST_KEY) is None
        assert "boom" in store.get_meta(RELIEF_ERROR_KEY)
        assert store.get_meta(DRIFT_LATEST_KEY) is not None  # 本地指數不受外部失敗影響

    def test_external_disabled_skips_fetch(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherDown(), _Cfg(external_enabled=False), now=AS_OF)
        assert store.get_meta(RELIEF_ERROR_KEY, "") == ""

    def test_already_sent_this_week_does_not_re_pend(self, store):
        _seed_drop(store)
        store.set_meta(DRIFT_SENT_KEY, json.dumps({"PSA10": "2026-09-14"}))
        res = refresh_market_drift(store, _FetcherOK(), _Cfg(), now=AS_OF)
        assert res["alert_text"] is None
        assert store.get_meta(DRIFT_PENDING_KEY, "") == ""


class TestDeliverDriftAlert:
    def test_nothing_pending_is_noop_true(self, store):
        n = _Notifier()
        assert deliver_drift_alert(store, n) is True
        assert n.sent == []

    def test_delivered_clears_pending_and_records_sent(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherOK(), _Cfg(), now=AS_OF)
        n = _Notifier(ok=True)
        assert deliver_drift_alert(store, n) is True
        assert len(n.sent) == 1 and "PSA10" in n.sent[0]
        assert store.get_meta(DRIFT_PENDING_KEY, "") == ""
        assert json.loads(store.get_meta(DRIFT_SENT_KEY)) == {"PSA10": "2026-09-14"}

    def test_failed_send_keeps_pending(self, store):
        _seed_drop(store)
        refresh_market_drift(store, _FetcherOK(), _Cfg(), now=AS_OF)
        n = _Notifier(ok=False)
        assert deliver_drift_alert(store, n) is False
        assert store.get_meta(DRIFT_PENDING_KEY, "") != ""
        assert store.get_meta(DRIFT_SENT_KEY) is None
        # 下一輪重送成功才清
        n2 = _Notifier(ok=True)
        assert deliver_drift_alert(store, n2) is True
        assert store.get_meta(DRIFT_PENDING_KEY, "") == ""
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v -k "Refresh or Deliver"`
Expected: FAIL，`ImportError: cannot import name 'deliver_drift_alert'`

- [ ] **Step 3: 實作**（append 到 `market_drift.py`）

```python
# ---------------------------------------------------------------------------
# refresh 入口與告警帳本
# ---------------------------------------------------------------------------
def _week_key(as_of: datetime) -> str:
    """告警去重的粒度：as_of 的日期（每天算一次，同一 as_of 同一帶只推一次）。"""
    return as_of.date().isoformat()


def refresh_market_drift(store: Any, fetcher: Any, cfg: Any, *, now: datetime) -> dict[str, Any]:
    """算本地指數、抓外部指數、判告警並記進 pending 帳本。**不送 Telegram**——送與清帳
    在 `deliver_drift_alert`，分開是為了讓「送達才消耗」有單一出口。

    外部抓取失敗只記 `RELIEF_ERROR_KEY`、保留上一份好的 `RELIEF_LATEST_KEY`，不拋：
    對方站掛了不該讓 daily 或本地指數一起掛。本地部分任何例外照拋（那是我們自己的 bug）。
    """
    md = dict(getattr(cfg, "market_drift", None) or {})
    recent_days = int(md.get("recent_days", 28))
    base_days = int(md.get("base_days", 56))
    alert_ratio = float(md.get("alert_ratio", 0.90))
    min_pairs = int(md.get("min_pairs", 8))

    rows = store.comps_for_drift()
    series = drift_series(rows, as_of=now, weeks=12, recent_days=recent_days, base_days=base_days)
    latest = [p for p in series if p.as_of == now.date().isoformat()]
    alert_text, alert_bands = evaluate_alert(latest, alert_ratio=alert_ratio, min_pairs=min_pairs)

    store.set_meta(DRIFT_LATEST_KEY, json.dumps({
        "computed_at": now.isoformat(),
        "as_of": now.date().isoformat(),
        "recent_days": recent_days,
        "base_days": base_days,
        "alert_ratio": alert_ratio,
        "min_pairs": min_pairs,
        "n_rows": len(rows),
        "latest": [asdict(p) for p in latest],
        "series": [asdict(p) for p in series],
        "alert_bands": alert_bands,
    }, ensure_ascii=False))

    if bool(md.get("external_enabled", True)):
        url = str(md.get("external_url") or "https://yugioh-relief.com/psa-market-daily-trends/")
        try:
            ext = fetch_relief_index(fetcher, url)
        except Exception as exc:  # noqa: BLE001 - 外站失敗要記錄、不能拖垮本地指數
            store.set_meta(RELIEF_ERROR_KEY, f"{now.isoformat()} {type(exc).__name__}: {exc}")
        else:
            store.set_meta(RELIEF_LATEST_KEY, json.dumps(ext, ensure_ascii=False))
            store.set_meta(RELIEF_ERROR_KEY, "")

    # 去重：同一帶同一 as_of 已送達過就不再排 pending
    sent = json.loads(store.get_meta(DRIFT_SENT_KEY) or "{}")
    wk = _week_key(now)
    fresh = [b for b in alert_bands if sent.get(b) != wk]
    if fresh:
        text, _ = evaluate_alert(
            [p for p in latest if p.band in fresh], alert_ratio=alert_ratio, min_pairs=min_pairs
        )
        store.set_meta(DRIFT_PENDING_KEY, json.dumps(
            {"text": text, "bands": {b: wk for b in fresh}}, ensure_ascii=False
        ))
        alert_text = text
    else:
        alert_text = None
    return {"alert_text": alert_text, "alert_bands": fresh, "latest": latest}


def deliver_drift_alert(store: Any, notifier: Any) -> bool:
    """送 pending 告警；**送達才清 pending、才記 sent**。沒有 pending 回 True。
    送失敗回 False 並原樣留著，下一輪 daily 再送（與 cli._send_schedule_alert 同規則）。"""
    raw = store.get_meta(DRIFT_PENDING_KEY) or ""
    if not raw:
        return True
    pending = json.loads(raw)
    if not notifier.send_alert(pending["text"]):
        return False
    sent = json.loads(store.get_meta(DRIFT_SENT_KEY) or "{}")
    sent.update(pending.get("bands") or {})
    store.set_meta(DRIFT_SENT_KEY, json.dumps(sent))
    store.set_meta(DRIFT_PENDING_KEY, "")
    return True
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: 全綠（`23 passed`）

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/market_drift.py tests/test_market_drift.py
git commit -m "feat(market-drift): refresh 入口落 meta、外部失敗只記錄、告警 pending 送達才清

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6 @sdd：CLI——daily 掛鉤、`market-drift` 指令、`health` 區塊

**Files:**
- Modify: `src/ygo_sniper/cli.py`
  - `daily()`（約 46-64 行）：在 `_mine_snipes_daily(pipe)` 之後加一行
  - `_mine_snipes_daily` 之後（約 172 行）新增 `_refresh_market_drift_daily`
  - `health()`（約 2198-2278 行）：來源健康表印完之後、`rows = store.list_alerts()` 之前插一段
  - 新增 `market_drift` 指令（放在 `health` 指令之前）
- Test: `tests/test_market_drift.py`（append，只測純格式函式）

- [ ] **Step 1: 紅燈測試**（append）

```python
from ygo_sniper.cli import _drift_table_rows  # noqa: E402


class TestDriftTableRows:
    def test_rows_are_human_readable(self):
        latest = {
            "as_of": "2026-09-14", "recent_days": 28, "base_days": 56, "min_pairs": 8,
            "alert_bands": ["PSA10"],
            "latest": [
                {"band": "PSA10", "as_of": "2026-09-14", "ratio": 0.85, "n_pairs": 9, "n_recent": 20, "n_base": 40},
                {"band": "PSA9", "as_of": "2026-09-14", "ratio": None, "n_pairs": 0, "n_recent": 3, "n_base": 0},
                {"band": "ARS10", "as_of": "2026-09-14", "ratio": 1.02, "n_pairs": 4, "n_recent": 8, "n_base": 9},
            ],
        }
        rows = _drift_table_rows(latest)
        assert rows[0] == ("PSA10", "×0.85", "9", "20 / 40", "📉 告警")
        assert rows[1] == ("PSA9", "—", "0", "3 / 0", "樣本不足（讀不到≠沒跌）")
        assert rows[2] == ("ARS10", "×1.02", "4", "8 / 9", "配對 <8，僅供參考")
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v -k DriftTableRows`
Expected: FAIL，`ImportError: cannot import name '_drift_table_rows'`

- [ ] **Step 3: 實作**

(a) `cli.py` 檔頭 import 區（`from .schedule_watch import PENDING_ALERT_KEY` 那一行之後）加：

```python
from .market_drift import (
    DRIFT_COMPUTED_DATE_KEY,
    DRIFT_LATEST_KEY,
    RELIEF_ERROR_KEY,
    RELIEF_LATEST_KEY,
    deliver_drift_alert,
    refresh_market_drift,
)
```

(b) `daily()` 裡 `_mine_snipes_daily(pipe)` 的下一行加：

```python
        _refresh_market_drift_daily(pipe)
```

(c) 在 `_mine_snipes_daily` 函式結束之後、`_run_notifications` 之前新增：

```python
def _refresh_market_drift_daily(pipe) -> None:
    """每天算一次市場漂移指數並送（或重送）告警。**失敗只警告，絕不影響掃描與推播。**

    計算一天一次（meta `DRIFT_COMPUTED_DATE_KEY`），但**每一輪都嘗試送 pending**：
    昨天沒送出去的告警不能因為今天不重算就永遠卡住（送達才消耗，CLAUDE.md 第五節第 7 項）。
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    today = now.date().isoformat()
    if pipe.store.get_meta(DRIFT_COMPUTED_DATE_KEY) != today:
        try:
            res = refresh_market_drift(pipe.store, pipe.fetcher, pipe.cfg, now=now)
        except Exception as exc:  # noqa: BLE001 - 指數算壞不能拖垮 daily，但要大聲
            print(f"[warn] 市場漂移指數計算失敗：{type(exc).__name__}: {exc}")
        else:
            pipe.store.set_meta(DRIFT_COMPUTED_DATE_KEY, today)
            if res["alert_bands"]:
                console.print(f"[yellow]市場漂移告警：{', '.join(res['alert_bands'])}[/yellow]")
            err = pipe.store.get_meta(RELIEF_ERROR_KEY, "")
            if err:
                print(f"[warn] 外部指數抓取失敗（沿用上一份）：{err}")
    try:
        ok = deliver_drift_alert(pipe.store, pipe.notifier)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 市場漂移告警送出時例外：{type(exc).__name__}: {exc}")
        return
    if not ok:
        console.print("[red]市場漂移告警送出失敗——留在 pending 帳裡，下一輪會重送[/red]")
```

(d) 在 `def health(` 之前新增指令與兩個純函式：

```python
def _drift_table_rows(latest: dict) -> list[tuple[str, str, str, str, str]]:
    """把 meta 裡的 latest 轉成 health 表的列：(帶, 倍率, 配對數, 近期/基期筆數, 判讀)。"""
    min_pairs = int(latest.get("min_pairs", 8))
    alert_bands = set(latest.get("alert_bands") or [])
    rows = []
    for p in latest.get("latest") or []:
        ratio = p.get("ratio")
        n_pairs = int(p.get("n_pairs") or 0)
        if ratio is None:
            note = "樣本不足（讀不到≠沒跌）"
        elif p["band"] in alert_bands:
            note = "📉 告警"
        elif n_pairs < min_pairs:
            note = f"配對 <{min_pairs}，僅供參考"
        else:
            note = ""
        rows.append((
            p["band"],
            f"×{ratio:.2f}" if ratio is not None else "—",
            str(n_pairs),
            f"{p.get('n_recent', 0)} / {p.get('n_base', 0)}",
            note,
        ))
    return rows


def _print_market_drift(store: Store) -> None:
    raw = store.get_meta(DRIFT_LATEST_KEY)
    if not raw:
        console.print("[dim]市場漂移：還沒算過。跑一次 `ygo-sniper market-drift` 或等 daily。[/dim]")
        return
    latest = json.loads(raw)
    t = Table(
        title=f"市場漂移（同卡重複成交：近 {latest['recent_days']} 天 vs 前 {latest['base_days']} 天"
              f"｜as_of {latest['as_of']}｜樣本 {latest.get('n_rows', '?')} 筆真實成交日）"
    )
    for col in ("帶", "倍率", "配對", "近期/基期筆數", "判讀"):
        t.add_column(col, justify="right" if col in ("倍率", "配對") else "left")
    for row in _drift_table_rows(latest):
        t.add_row(*row)
    console.print(t)
    ext_raw = store.get_meta(RELIEF_LATEST_KEY)
    err = store.get_meta(RELIEF_ERROR_KEY, "")
    if ext_raw:
        ext = json.loads(ext_raw)
        weeks = ext.get("weeks") or []
        t2 = Table(title=f"外部對照：yugioh-relief.com 週次 PSA 浮雕指數（抓於 {ext.get('fetched_at', '')[:16]}"
                         "｜只涵蓋浮雕、carry-forward 中位數、**不進計算**）")
        for col in ("週", "全部", "PSA10", "PSA9", "PSA≤8", "更新/成分"):
            t2.add_column(col, justify="left" if col == "週" else "right")
        for w in weeks[-6:]:
            t2.add_row(w["week_start"], f"{w['all']:.1f}", f"{w['psa10']:.1f}", f"{w['psa9']:.1f}",
                       f"{w['psa8_below']:.1f}", f"{w['updated']}/{w['components']}")
        console.print(t2)
    if err:
        console.print(f"[red]外部指數最近一次抓取失敗：{err}[/red]")


@app.command(name="market-drift")
def market_drift(
    refresh: bool = typer.Option(True, "--refresh/--no-refresh", help="重算並抓外部指數；--no-refresh 只印 meta 裡的"),
):
    """市場漂移監視器：同卡重複成交倍率（本地）＋ yugioh-relief.com 週次指數（外部對照）。
    只顯示、只告警，永遠不進估價。告警由 daily 推播；這個指令不送 Telegram。"""
    from datetime import UTC, datetime

    from .sources.base import CachedFetcher

    cfg = load_config()
    store = Store(cfg.db_path)
    if refresh:
        res = refresh_market_drift(store, CachedFetcher(cfg), cfg, now=datetime.now(UTC))
        if res["alert_bands"]:
            console.print(f"[yellow]告警帶：{', '.join(res['alert_bands'])}（已排入 pending，下一輪 daily 推播）[/yellow]")
    _print_market_drift(store)
```

(e) `health()` 裡，來源健康表那個 `else:` 區塊結束（`console.print(t)`）之後、`rows = store.list_alerts()` 之前插入：

```python
    _print_market_drift(store)

```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_market_drift.py -v`
Expected: 全綠（`24 passed`）

Run: `.venv/bin/ygo-sniper market-drift`
Expected: 印出「市場漂移」表（7 列：PSA10…ALL）與「外部對照」表（6 週），無 traceback。

Run: `.venv/bin/ygo-sniper health`
Expected: 來源健康表之後多出上面兩張表。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/cli.py tests/test_market_drift.py
git commit -m "feat(cli): market-drift 指令、health 漂移區塊、daily 每日重算＋送達才清的告警

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7 @sdd：dashboard——`/api/market-drift` 與頂列 pill

**Files:**
- Modify: `web/app.py`（在 `@app.get("/api/stats")` 之後）
- Modify: `web/static/index.html`（第 377 行 `stat-comps` pill 之後加一顆；`loadStats()` 約 1969-1974 行）

- [ ] **Step 1: 實作 API**（`web/app.py`，緊接 `def stats(): return store.stats()` 之後）

```python
@app.get("/api/market-drift")
def market_drift():
    """漂移監視器的 meta 快取（daily 每天算一次；沒算過就是 null）。只顯示，不進估價。"""
    import json as _json

    from ygo_sniper.market_drift import DRIFT_LATEST_KEY, RELIEF_ERROR_KEY, RELIEF_LATEST_KEY

    raw = store.get_meta(DRIFT_LATEST_KEY)
    ext = store.get_meta(RELIEF_LATEST_KEY)
    return {
        "local": _json.loads(raw) if raw else None,
        "external": _json.loads(ext) if ext else None,
        "external_error": store.get_meta(RELIEF_ERROR_KEY, "") or None,
    }
```

- [ ] **Step 2: 實作前端**

`index.html` 第 377 行 `<span class="pill" id="stat-comps">行情 <b>–</b></span>` 之後加：

```html
    <span class="pill" id="stat-drift" title="市場漂移：同卡重複成交，近 4 週 vs 前 8 週（只顯示，不進估價）">漂移 <b>–</b></span>
```

`loadStats()` 函式最後一行（`document.getElementById("stat-comps")...`）之後加：

```javascript
  try {
    const d = await api("/api/market-drift");
    const el = document.getElementById("stat-drift");
    const loc = d.local;
    if (!loc) { el.innerHTML = "漂移 <b>未算</b>"; }
    else {
      const by = Object.fromEntries((loc.latest || []).map(p => [p.band, p]));
      const fmt = b => (by[b] && by[b].ratio != null) ? `×${by[b].ratio.toFixed(2)}` : "—";
      const alert = (loc.alert_bands || []).length > 0;
      el.innerHTML = `漂移 <b>全體 ${fmt("ALL")} ｜ PSA10 ${fmt("PSA10")} ｜ PSA9 ${fmt("PSA9")}</b>`
        + (alert ? ` <span style="color:var(--bad,#c33)">📉 ${loc.alert_bands.join(", ")}</span>` : "");
    }
  } catch (e) { /* 漂移 pill 壞了不能影響其他統計 */ }
```

- [ ] **Step 3: 驗收（主線程親跑）**

Run（背景起 server 再 curl）：
```bash
(.venv/bin/ygo-sniper serve >/tmp/serve.log 2>&1 &) ; sleep 3
curl -s http://127.0.0.1:8321/api/market-drift | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print(sorted(d), d['local'] and d['local']['as_of'])"
curl -s http://127.0.0.1:8321/ | grep -c 'stat-drift'
pkill -f "ygo-sniper serve"
```
Expected：第一行印 `['external', 'external_error', 'local'] 2026-09-14`（日期為當天）；第二行 `1`。

- [ ] **Step 4: Commit**

```bash
git add web/app.py web/static/index.html
git commit -m "feat(web): /api/market-drift 與頂列漂移 pill（只顯示，不進估價）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Part C：估價時間加權（預設關閉）

### Task 8 @sdd：`Params.half_life_days`、權重與加權中位數

**Files:**
- Modify: `src/ygo_sniper/valuation.py`（`Params` 253-288 行；`_median` 398 行附近）
- Modify: `config/settings.yaml`（`valuation:` 節，`group_conformal: true` 之後）
- Test: `tests/test_valuation_recency.py`（新檔）

- [ ] **Step 1: 紅燈測試**（建立 `tests/test_valuation_recency.py`）

```python
"""估價時間加權：半衰期 None 時行為必須與等權逐位元相同；有半衰期時近期成交主導。"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from ygo_sniper.valuation import Obs, Params, _weights, _wmedian

AS_OF = datetime(2026, 9, 14, tzinfo=UTC)


def _obs(price, days_ago, name="A", rarity="ultra", grade=9.0, key=None):
    sold = (AS_OF - timedelta(days=days_ago)).isoformat()
    return Obs(price, name, rarity, grade, key=key or f"{name}-{price}-{days_ago}", sold_at=sold)


class TestParams:
    def test_default_is_none(self):
        assert Params().half_life_days is None

    def test_from_config_reads_null_and_number(self):
        class C:
            valuation = {"half_life_days": None}
        assert Params.from_config(C()).half_life_days is None

        class D:
            valuation = {"half_life_days": 45}
        assert Params.from_config(D()).half_life_days == 45.0


class TestWeights:
    def test_none_half_life_is_all_ones(self):
        rows = [_obs(100, 0), _obs(100, 400)]
        assert _weights(rows, None, AS_OF) == [1.0, 1.0]

    def test_half_life_halves_per_period(self):
        rows = [_obs(100, 0), _obs(100, 45), _obs(100, 90)]
        w = _weights(rows, 45, AS_OF)
        assert w == pytest.approx([1.0, 0.5, 0.25])

    def test_undated_row_gets_full_weight(self):
        rows = [Obs(100.0, "A", "ultra", 9.0, key="x")]
        assert _weights(rows, 45, AS_OF) == [1.0]

    def test_future_dated_row_clamps_to_weight_one(self):
        rows = [_obs(100, -3)]
        assert _weights(rows, 45, AS_OF) == [1.0]


class TestWeightedMedian:
    def test_equal_weights_match_statistics_median_exactly(self):
        vals = [3.0, 1.0, 4.0, 1.5]
        assert _wmedian(vals, [1.0] * 4) == statistics.median(vals)
        assert _wmedian(vals, [0.3] * 4) == statistics.median(vals)

    def test_heavy_weight_dominates(self):
        assert _wmedian([1.0, 10.0], [0.1, 1.0]) == 10.0

    def test_exact_half_boundary_averages_neighbours(self):
        # 權重 1,1,2：累積到第二個剛好 = 一半（2/4）→ 取第二、第三平均
        assert _wmedian([1.0, 2.0, 3.0], [1.0, 1.0, 2.0]) == 2.5

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _wmedian([], [])
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_valuation_recency.py -v`
Expected: FAIL，`ImportError: cannot import name '_weights'`

- [ ] **Step 3: 實作**

(a) `Params` dataclass：在 `venue_premium_prior` 欄位之後加：

```python
    #: 成交時間衰減的半衰期（天）。None ＝ 等權（與 2026-09 之前的行為逐位元相同）。
    #: 只影響 predict_log 的池中位數與截距，不影響分數／稀有度／平台溢價曲線。
    #: ⚠️ Mercari／部分 PayPay 的 sold_at 是入庫日，加權對它們量到的是「我們何時抓到」。
    half_life_days: float | None = None
```

`from_config` 的 `return cls(` 裡，`venue_premium_prior={...},` 之後加：

```python
            half_life_days=(
                float(v["half_life_days"]) if v.get("half_life_days") not in (None, "", 0) else None
            ),
```

(b) `valuation.py` 檔頭 import 區目前**沒有** datetime。在 `from typing import Any`（約 164 行）之前加一行：

```python
from datetime import UTC, datetime
```

(c) `_median` 之後加：

```python
def _wmedian(vals: list[float], weights: list[float]) -> float:
    """加權中位數。**所有權重相等時直接回 `_median`**（等權路徑與舊行為逐位元相同）。
    累積權重剛好落在一半時取相鄰兩值平均（與 statistics.median 對偶數 n 的精神一致）。"""
    if not vals:
        raise ValueError("_wmedian: 空樣本")
    if len(set(weights)) == 1:
        return _median(vals)
    pairs = sorted(zip(vals, weights))
    half = sum(w for _, w in pairs) / 2.0
    acc = 0.0
    for i, (v, w) in enumerate(pairs):
        acc += w
        if acc > half:
            return v
        if acc == half:
            return (v + pairs[i + 1][0]) / 2.0 if i + 1 < len(pairs) else v
    return pairs[-1][0]


def _parse_obs_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def _weights(rows: list[Obs], half_life_days: float | None, as_of: datetime) -> list[float]:
    """2^(−年齡/半衰期)。半衰期 None → 全 1.0。沒有 sold_at 的樣本給 1.0（沒資訊不懲罰）；
    未來日期（時鐘偏差）夾到 1.0。"""
    if not half_life_days or half_life_days <= 0:
        return [1.0] * len(rows)
    out: list[float] = []
    for r in rows:
        d = _parse_obs_date(r.sold_at)
        if d is None:
            out.append(1.0)
            continue
        age = max(0.0, (as_of - d).total_seconds() / 86400.0)
        out.append(2.0 ** (-age / half_life_days))
    return out
```

(d) `config/settings.yaml` 的 `valuation:` 節，`group_conformal: true` 之後加：

```yaml
  # 成交時間衰減權重的半衰期（天）。null ＝ 等權（現況）。**改成數字之前必須跑**
  # `ygo-sniper coverage-time --real-time-only --test-fraction 0.11 --half-life 45 --half-life 60 --half-life 90`
  # 且通過 docs/plans/market-drift-recency.md Task 11 的預註冊判準（下尾違反率 ≤10% 且不劣於等權）。
  # 只影響 L1/L2/L3 池中位數與截距，不影響溢價曲線。Mercari 的 sold_at 是入庫日（已知限制）。
  half_life_days: null
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_valuation_recency.py -v`
Expected: `11 passed`

Run: `.venv/bin/pytest tests/test_valuation.py -v`
Expected: 全綠（既有行為不變）

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/valuation.py config/settings.yaml tests/test_valuation_recency.py
git commit -m "feat(valuation): Params.half_life_days、時間衰減權重與加權中位數（預設 null＝等權）

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9 @sdd：`ValuationModel`／`Valuator` 吃權重（只在池中位數與截距）

**Files:**
- Modify: `src/ygo_sniper/valuation.py`
  - `_shrink`（402 行）：`n: int` → `n: float`（型別註記，行為不變）
  - `ValuationModel.__init__`（417-435 行）
  - `ValuationModel._fit` 的截距（472-478 行）
  - `ValuationModel.predict_log`（670-724 行）
  - `Valuator.__init__`（778-812 行）
- Test: `tests/test_valuation_recency.py`（append）

- [ ] **Step 1: 紅燈測試**（append）

```python
from ygo_sniper.valuation import ValuationModel, Valuator  # noqa: E402

PRIOR = Params(
    confidence=0.80, shrink_k=4, min_calibration=10, calibration_fraction=0.4, min_cell=3,
    grade_premium_prior={10.0: 4.0, 9.0: 1.0, 8.0: 0.5, 7.0: 0.25},
    rarity_premium_prior={"ultimate": 4.7, "secret": 3.5, "ultra": 2.2, "normal": 1.0},
)


def _rows():
    return [
        _obs(1000.0, 200, "A"), _obs(1200.0, 100, "A"), _obs(4000.0, 150, "A", grade=10.0),
        _obs(600.0, 120, "B"), _obs(700.0, 90, "C", grade=8.0), _obs(900.0, 60, "D", grade=10.0),
        _obs(300.0, 30, "E", rarity="normal"), _obs(350.0, 10, "F", rarity="normal"),
    ]


class TestModelWeighting:
    def test_none_half_life_identical_to_unweighted(self):
        rows = _rows()
        base = ValuationModel(rows, PRIOR)
        same = ValuationModel(rows, PRIOR, as_of=AS_OF)
        assert same.predict_log("A", "ultra", 9.0) == base.predict_log("A", "ultra", 9.0)
        assert same.predict_log(None, "ultra", 10.0) == base.predict_log(None, "ultra", 10.0)
        assert same.base == base.base

    def test_recent_sale_dominates_l1_with_short_half_life(self):
        rows = [_obs(1000.0, 200, "A", key="old1"), _obs(1000.0, 190, "A", key="old2"),
                _obs(1000.0, 180, "A", key="old3"), _obs(500.0, 1, "A", key="new"),
                _obs(800.0, 100, "B"), _obs(900.0, 100, "C"), _obs(700.0, 100, "D")]
        import dataclasses
        p_short = dataclasses.replace(PRIOR, half_life_days=10.0)
        eq = ValuationModel(rows, PRIOR, as_of=AS_OF).predict_log("A", "ultra", 9.0)
        wt = ValuationModel(rows, p_short, as_of=AS_OF).predict_log("A", "ultra", 9.0)
        assert wt[0] < eq[0]                      # 近期 500 把估計往下拉
        assert wt[1] == "L1" and eq[1] == "L1"
        assert wt[2] < eq[2]                      # 有效樣本數是權重和，老樣本幾乎不算
        assert eq[2] == 4

    def test_n_eff_is_rounded_weight_sum(self):
        import dataclasses
        rows = [_obs(1000.0, 0, "A", key="a0"), _obs(1000.0, 45, "A", key="a45"),
                _obs(1000.0, 45, "A", key="a45b"), _obs(800.0, 100, "B")]
        p = dataclasses.replace(PRIOR, half_life_days=45.0)
        _, lvl, n = ValuationModel(rows, p, as_of=AS_OF).predict_log("A", "ultra", 9.0)
        assert lvl == "L1" and n == 2             # 1 + 0.5 + 0.5 = 2.0

    def test_valuator_passes_as_of_and_stays_deterministic(self):
        import dataclasses
        rows = _rows()
        p = dataclasses.replace(PRIOR, half_life_days=45.0)
        v1 = Valuator(rows, p, as_of=AS_OF)
        v2 = Valuator(rows, p, as_of=AS_OF)
        assert v1.model.predict_log("A", "ultra", 9.0) == v2.model.predict_log("A", "ultra", 9.0)
        assert v1.model.as_of == AS_OF
        assert v1.cal_model is None or v1.cal_model.as_of == AS_OF
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_valuation_recency.py -v -k ModelWeighting`
Expected: FAIL，`TypeError: ValuationModel.__init__() got an unexpected keyword argument 'as_of'`

- [ ] **Step 3: 實作**

(a) `_shrink` 簽名改為 `def _shrink(layer_value: float, parent: float, n: float, k: float) -> float:`（只改型別）。

(b) `ValuationModel.__init__`：

```python
    def __init__(self, rows: list[Obs], params: Params, as_of: datetime | None = None) -> None:
        self.params = params
        self.rows = [r for r in rows if r.price_twd > 0]
        #: 權重的「現在」。時間切分驗證要傳訓練集的最後成交日，不能用牆上時鐘（否則未來洩漏）。
        self.as_of = as_of or datetime.now(UTC)
        #: 與 self.rows 對齊的權重；半衰期 None 時全是 1.0。
        self.w = _weights(self.rows, params.half_life_days, self.as_of)
```
（其餘原本的屬性初始化與 `self._fit()` 保持不動，接在後面。）

(c) `_fit` 的截距段改為：

```python
        if self.rows:
            self.base = _wmedian([self._residual_base(r) for r in self.rows], self.w)
            venued = [(r, w) for r, w in zip(self.rows, self.w) if r.venue]
            self.base_venue = (
                _wmedian([self._residual_base(r) - self.v(r.venue) for r, _ in venued],
                         [w for _, w in venued])
                if venued else self.base
            )
```

(d) `predict_log` 的三層改為（`conv`、`est` 初值與 docstring 不動）：

```python
        # L3：同稀有度（任何卡、任何分數），把分數換算到目標分數
        pool3 = [(r, w) for r, w in zip(self.rows, self.w) if r.rarity == rarity]
        if pool3:
            n3 = sum(w for _, w in pool3)
            est = _shrink(_wmedian([conv(r) for r, _ in pool3], [w for _, w in pool3]), est, n3, k)
            level, n_eff = "L3", int(round(n3))

        if not card_name:
            return est, level, n_eff

        # L2：同卡同稀有度，跨分數換算
        pool2 = [(r, w) for r, w in pool3 if r.card_name == card_name]
        if pool2:
            n2 = sum(w for _, w in pool2)
            est = _shrink(_wmedian([conv(r) for r, _ in pool2], [w for _, w in pool2]), est, n2, k)
            level, n_eff = "L2", int(round(n2))

        # L1：同卡同稀有度同分數，只剩平台需要換算
        pool1 = [(r, w) for r, w in pool2 if r.grade == grade]
        if pool1:
            n1 = sum(w for _, w in pool1)
            vals = [
                (r.logp - self.v(r.venue) + target_v) if adjust else r.logp for r, _ in pool1
            ]
            est = _shrink(_wmedian(vals, [w for _, w in pool1]), est, n1, k)
            level, n_eff = "L1", int(round(n1))

        return est, level, n_eff
```

(e) `Valuator.__init__` 簽名加 `as_of: datetime | None = None`，並把兩處建模改為：
`self.model = ValuationModel(self.rows, self.params, as_of=as_of)` 與
`self.cal_model = ValuationModel(fit, self.params, as_of=self.model.as_of)`。

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_valuation_recency.py tests/test_valuation.py -v`
Expected: 全綠。既有 `test_valuation.py` 一個都不能變紅（等權路徑逐位元相同）。

Run: `make test`
Expected: 全綠，末行 `N passed`（N ≥ 1932 + 本 plan 新增數）。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/valuation.py tests/test_valuation_recency.py
git commit -m "feat(valuation): 池中位數與截距吃時間權重、有效 n 改權重和；等權路徑行為不變

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10 @sdd：`coverage_time_split` 加下尾違反率＋`as_of=訓練截止`；`coverage-time --half-life`

**Files:**
- Modify: `src/ygo_sniper/valuation.py`（`coverage_time_split` 1577-1648 行）
- Modify: `src/ygo_sniper/cli.py`（`coverage_time` 指令 2966-3060 行）
- Test: `tests/test_valuation_recency.py`（append）

- [ ] **Step 1: 紅燈測試**（append）

```python
from ygo_sniper.valuation import coverage_time_split  # noqa: E402


class TestCoverageTimeSplitTails:
    def _rows(self):
        out = []
        for i in range(60):
            out.append(_obs(1000.0 + (i % 7) * 50, 200 - i * 3, "A", key=f"a{i}"))
            out.append(_obs(600.0 + (i % 5) * 40, 200 - i * 3, "B", key=f"b{i}"))
        return out

    def test_reports_lower_and_upper_tail_and_as_of(self):
        res = coverage_time_split(self._rows(), PRIOR, test_fraction=0.25)
        assert 0.0 <= res["lower_tail"] <= 1.0 and 0.0 <= res["upper_tail"] <= 1.0
        assert res["empirical"] == pytest.approx(1.0 - res["lower_tail"] - res["upper_tail"])
        assert res["as_of"] == res["train_until"]

    def test_half_life_changes_result_but_not_split(self):
        import dataclasses
        base = coverage_time_split(self._rows(), PRIOR, test_fraction=0.25)
        wt = coverage_time_split(self._rows(), dataclasses.replace(PRIOR, half_life_days=30.0),
                                 test_fraction=0.25)
        assert wt["n_tested"] == base["n_tested"] and wt["train_until"] == base["train_until"]
        assert wt["half_life_days"] == 30.0 and base["half_life_days"] is None
```

- [ ] **Step 2: 跑，確認紅燈**

Run: `.venv/bin/pytest tests/test_valuation_recency.py -v -k CoverageTimeSplitTails`
Expected: FAIL，`KeyError: 'lower_tail'`

- [ ] **Step 3: 實作**

(a) `coverage_time_split`：`out` 初始 dict 加四個鍵（放在 `"note": ""` 之前）：

```python
        "lower_tail": None,
        "upper_tail": None,
        "as_of": None,
        "half_life_days": params.half_life_days,
```

`v = Valuator(train, params)` 那一行改為：

```python
    as_of = _parse_obs_date(out["train_until"])
    out["as_of"] = out["train_until"]
    v = Valuator(train, params, as_of=as_of)
```

測試迴圈裡 `covered += int(lo <= t.logp <= hi)` 之前先宣告計數器（放在 `covered = 0` 旁）：
`below = 0` 與 `above = 0`；迴圈內加：

```python
        below += int(t.logp < lo)
        above += int(t.logp > hi)
```

結尾 `out["empirical"] = ...` 之後加：

```python
    out["lower_tail"] = below / len(test) if test else None
    out["upper_tail"] = above / len(test) if test else None
```

(b) `cli.py` 的 `coverage_time` 指令：簽名加一個參數

```python
    half_life: list[float] = typer.Option(
        [], "--half-life", help="時間衰減半衰期（天），可重複；一律附等權對照。例：--half-life 45 --half-life 60"
    ),
```

在函式最後（既有的最後一段 `console.print(...)` 之後）加：

```python
    if half_life:
        import dataclasses

        t3 = Table(title="時間加權對照（平台內切早晚；權重的「現在」＝訓練集最後成交日）")
        for col in ("半衰期", "平台校正", "覆蓋率", "下尾違反", "上尾", "點估計中位誤差", "區間中位寬度", "校準集"):
            t3.add_column(col, justify="left" if col in ("半衰期", "平台校正") else "right")
        for h in [None, *half_life]:
            p = dataclasses.replace(params, half_life_days=h)
            for label, aware in (("無", False), ("有", True)):
                res = coverage_time_split(obs, p, test_fraction=test_fraction,
                                          venue_aware=aware, stratify_by_venue=True)
                t3.add_row(
                    "等權" if h is None else f"{h:g} 天", label,
                    "—" if res["empirical"] is None else f"{res['empirical']:.1%}",
                    "—" if res["lower_tail"] is None else f"{res['lower_tail']:.1%}",
                    "—" if res["upper_tail"] is None else f"{res['upper_tail']:.1%}",
                    "—" if not res["median_error_ratio"] else f"×{res['median_error_ratio']:.2f}",
                    "—" if not res["median_width_twd"] else f"NT${res['median_width_twd']:,.0f}",
                    str(res["calibration_n"]),
                )
        console.print(t3)
        console.print(
            "[dim]下尾違反＝真實成交低於區間下緣的比例（名目 10%）——出價上限由下緣反推，"
            "這一欄才是會賠錢的那一欄。上線判準見 docs/plans/market-drift-recency.md Task 11。[/dim]"
        )
```

- [ ] **Step 4: 跑，確認綠燈**

Run: `.venv/bin/pytest tests/test_valuation_recency.py -v`
Expected: 全綠（`16 passed`）

Run: `.venv/bin/ygo-sniper coverage-time --real-time-only --test-fraction 0.11 --half-life 45 --half-life 60 --half-life 90`
Expected: 原本兩張表之後多出「時間加權對照」表，8 列（等權/45/60/90 × 無/有），下尾欄有百分比。

- [ ] **Step 5: Commit**

```bash
git add src/ygo_sniper/valuation.py src/ygo_sniper/cli.py tests/test_valuation_recency.py
git commit -m "feat(coverage-time): 下尾／上尾違反率、權重現在＝訓練截止、--half-life 對照表

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Task 11（主線程）：實驗、verdict、文件

**不派工。** 主線程親跑，結果寫回本檔「實驗結果」節。

- [ ] 跑：
  ```bash
  ygo-sniper coverage-time --real-time-only --test-fraction 0.11 --half-life 45 --half-life 60 --half-life 90
  ygo-sniper coverage-time --real-time-only --test-fraction 0.25 --half-life 45 --half-life 60 --half-life 90
  ```
- [ ] **預註冊判準（候選 h 可上線的條件，全部要過）**，以「平台內切早晚 × 平台校正＝有」那列為準
  （生產的高價帶是 Mercari 目標平台，走 venue-aware）：
  1. test_fraction 0.11（≈ 8/15 之後的切片）：`下尾違反(h) ≤ 10%` **且** `≤ 下尾違反(等權)`。
  2. 同切片：`覆蓋率(h) ≥ 覆蓋率(等權) − 2 個百分點`。
  3. 同切片：`點估計中位誤差(h) ≤ 點估計中位誤差(等權)`。
  4. test_fraction 0.25（含急漲期）：`下尾違反(h) ≤ 下尾違反(等權) + 1 個百分點`（不能在漲勢裡變得更會開高）。
  5. 「平台校正＝無」那列的下尾違反不得比等權差超過 1 個百分點。
  多個 h 都過 → 取**最大**的 h（最保守的改動）。沒有任何 h 過 → 維持 `half_life_days: null`，記錄結果。
- [ ] 通過者**不自動上線**：改 `settings.yaml` 的 `half_life_days` 會直接改變出價上限（真錢），
  主線程把表格與判定呈給使用者，使用者點頭才改設定並 `ygo-sniper revalue`。
- [ ] 文件：
  - `CLAUDE.md` 第九節指令清單加 `ygo-sniper market-drift` 與 `coverage-time --half-life` 兩條；
    第七節末尾加一段「擬合沒有時間視窗；`half_life_days` 的上線判準在本 plan」。
  - 本檔補「實驗結果」與「verdict」節。
- [ ] `git commit` 文件。

---

## 實驗結果（2026-09-14 主線程親跑）

指令：`ygo-sniper coverage-time --real-time-only --test-fraction {0.11, 0.25} --half-life 45 --half-life 60 --half-life 90`
（真實成交日樣本 1,807 筆；0.11 切片＝測試集 198 筆、訓練到 2026-08-22；0.25 切片＝測試集 451 筆、訓練到 2026-07-23）

**test_fraction 0.11（≈ 8/15 之後，崩盤後切片）**

| 半衰期 | 平台校正 | 覆蓋率 | 下尾違反 | 上尾 | 點估計中位誤差 | 校準集 |
|---|---|---|---|---|---|---|
| 等權 | 無 | 75.8% | 10.1% | 14.1% | ×1.93 | 646 |
| 等權 | **有** | 76.3% | **9.1%** | 14.6% | ×1.93 | 646 |
| 45 天 | 無 | 79.8% | 9.1% | 11.1% | ×2.03 | 646 |
| 45 天 | **有** | 76.8% | **10.6%** | 12.6% | ×1.95 | 646 |
| 60 天 | 無 | 77.8% | 10.1% | 12.1% | ×1.97 | 646 |
| 60 天 | **有** | 76.8% | **11.6%** | 11.6% | ×1.87 | 646 |
| 90 天 | 無 | 76.3% | 11.1% | 12.6% | ×2.02 | 646 |
| 90 天 | **有** | 78.3% | **11.1%** | 10.6% | ×1.83 | 646 |

**test_fraction 0.25（含 6-7 月急漲期）**

| 半衰期 | 平台校正 | 覆蓋率 | 下尾違反 | 上尾 | 點估計中位誤差 | 校準集 |
|---|---|---|---|---|---|---|
| 等權 | 無 | 74.9% | 6.2% | 18.8% | ×2.01 | 529 |
| 等權 | 有 | 81.2% | 4.2% | 14.6% | ×2.03 | 529 |
| 45 天 | 無 | 78.3% | 4.0% | 17.7% | ×2.06 | 529 |
| 45 天 | 有 | 79.4% | 4.9% | 15.7% | ×2.03 | 529 |
| 60 天 | 無 | 77.8% | 4.9% | 17.3% | ×2.05 | 529 |
| 60 天 | 有 | 78.9% | 5.1% | 16.0% | ×2.03 | 529 |
| 90 天 | 無 | 78.0% | 5.5% | 16.4% | ×2.07 | 529 |
| 90 天 | 有 | 79.2% | 4.7% | 16.2% | ×1.99 | 529 |

判準逐條（以 0.11 切片、平台校正＝有 為準）：

| 候選 | 判準 1：下尾 ≤10% 且 ≤ 等權 9.1% | 判準 2：覆蓋 ≥ 74.3% | 判準 3：誤差 ≤ ×1.93 | 判準 4（0.25）：下尾 ≤ 5.2% | 判準 5（無校正列）：下尾 ≤ 11.1% |
|---|---|---|---|---|---|
| 45 天 | ✗ 10.6% | ✓ | ✗ ×1.95 | ✓ 4.9% | ✓ |
| 60 天 | ✗ 11.6% | ✓ | ✓ | ✓ 5.1% | ✓ |
| 90 天 | ✗ 11.1% | ✓ | ✓ | ✓ 4.7% | ✓ |

## Verdict

**沒有任何半衰期通過判準 1（會賠錢的那一欄）。維持 `valuation.half_life_days: null`（等權）。**

誠實標註：
- 測試集只有 198 筆，下尾 9.1% vs 10.6% 的差距是 18 vs 21 筆，**在雜訊範圍內**；這個實驗分不出「加權真的更會開高」還是「樣本太少」。
  預註冊判準的意義正是在分不出來時**不改動會碰錢的參數**。
- 「平台校正＝無」那列 45 天有改善（下尾 10.1%→9.1%、覆蓋 75.8%→79.8%），但生產的高價帶走 venue-aware 路徑，那列不是判準。
- 本地漂移指數（A）上線後實測配對數：PSA10 3、PSA9 4、PSA≤8 0、ALL 12——**遠低於 `min_pairs: 8`**，
  現階段本地指數只會顯示「僅供參考／樣本不足」，不會告警；實際能看漂移的是外部指數（PSA 浮雕限定）。
  這是誠實的結果，不是 bug：同卡在 4 週內重複成交本來就少。要讓本地告警有機會觸發，選項是
  把 `recent_days`／`base_days` 拉長（例如 56/112）或降 `min_pairs`，**都要使用者決定**，不要憑感覺調。
- 重跑本實驗的方式：本節開頭那兩條指令；資料每天在變，數字會漂，判準不變。
- 若 3 個月後市場繼續下跌、`coverage-time --test-fraction 0.11` 的等權下尾違反率明顯 >10%，再重開此案；
  屆時候選應包含「指數校正法」（用重複成交指數把舊成交換算到今天再入池），本輪未做。
