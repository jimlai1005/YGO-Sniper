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
    # band -> signature -> ("recent"/"base") -> [price]
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
            buckets[b].setdefault(sig, {"recent": [], "base": []})[window].append(price)
            counts[b][window] += 1

    out: list[DriftPoint] = []
    for b in (*BANDS, ALL_BAND):
        diffs = [
            math.log(statistics.median(w["recent"])) - math.log(statistics.median(w["base"]))
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
