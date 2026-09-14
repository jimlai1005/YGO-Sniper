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

    同卡＝卡名主檔身分×稀有度×分數；同機制；同站。
    每對取「近期中位對數價 − 基期中位對數價」，再取所有配對的中位數。
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
        name = r.get("card_name")
        if not name:
            continue
        kind = r.get("sale_kind")
        if kind not in ("auction", "fixed"):
            continue
        sig = f"{name}|{r.get('rarity') or ''}|{band}|{r.get('grade')}|{kind}|{r.get('site') or ''}"
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


_ALERT_HEADER = "📉 市場漂移告警（同卡重複成交，近 4 週 vs 前 8 週）"
_ALERT_FOOTER = "只顯示、不進估價；出價上限仍由區間下緣反推，請人工留意估價可能偏高。"


def _band_line(p: DriftPoint) -> str:
    return f"{p.band} ×{p.ratio:.2f}（n={p.n_pairs}，截至 {p.as_of}）"


def _compose_alert(lines: dict[str, str]) -> str:
    """依 BANDS 順序組告警文字；`lines` 是 帶 → 那一行。"""
    body = [lines[b] for b in BANDS if b in lines]
    return "\n".join([_ALERT_HEADER, *body, _ALERT_FOOTER])


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
    return _compose_alert({p.band: _band_line(p) for p in hits}), [p.band for p in hits]


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


# ---------------------------------------------------------------------------
# refresh 入口與告警帳本
# ---------------------------------------------------------------------------
def _week_key(as_of: datetime) -> str:
    """告警去重的粒度：as_of 的日期（每天算一次，同一 as_of 同一帶只推一次）。"""
    return as_of.date().isoformat()


def refresh_market_drift(store: Any, fetcher: Any, cfg: Any, *, index: Any | None = None, now: datetime) -> dict[str, Any]:
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
    idx = index
    if idx is None:
        from .cards import CardIndex  # noqa: PLC0415 - 延後匯入，測試可注入假索引
        idx = CardIndex.load()
    if idx is not None and getattr(idx, "available", False):
        for r in rows:
            m = idx.match(r.get("title") or "")
            r["card_name"] = m.name_ja if (m and m.in_era) else None
    else:
        for r in rows:
            r["card_name"] = None
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

    # 去重：同一帶同一 as_of 已送達過就不再排 pending。
    # **pending 是合併不是覆寫**（2026-09-14 審查回合 Critical 2）：昨天沒送出去的帶要留著，
    # 今天新增的帶加進去；同一帶以較新的 as_of 那一行取代自己的舊行。送達才整批清。
    sent = json.loads(store.get_meta(DRIFT_SENT_KEY) or "{}")
    wk = _week_key(now)
    fresh = [b for b in alert_bands if sent.get(b) != wk]
    existing = json.loads(store.get_meta(DRIFT_PENDING_KEY) or "null") or {}
    if existing and not existing.get("lines"):
        existing = {}   # 舊格式（沒有逐行內容）無法合併，丟棄；不能送出沒有任何帶的空身告警
    bands: dict[str, str] = dict(existing.get("bands") or {})
    lines: dict[str, str] = dict(existing.get("lines") or {})
    for p in latest:
        if p.band in fresh:
            bands[p.band] = wk
            lines[p.band] = _band_line(p)
    if bands and lines:
        store.set_meta(DRIFT_PENDING_KEY, json.dumps(
            {"text": _compose_alert(lines), "bands": bands, "lines": lines}, ensure_ascii=False
        ))
    else:
        store.set_meta(DRIFT_PENDING_KEY, "")
    return {"alert_text": _compose_alert(lines) if fresh else None, "alert_bands": fresh, "latest": latest}


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
