"""平台研究：PayPay 到底是不是「比較便宜買得到」？

這個模組回答三個**互相牽制**的問題。它們必須放在一起看，因為單獨任何一個
都能得到相反的結論——那正是這份研究存在的理由。

    Q1  同規格的卡，哪個平台「現在就買得到的價格」最低？   → 在架價分層比較
    Q2  成交價的平台係數（實測 PayPay/Yahoo = 2.57×）是真價差還是選擇偏差？
        → 同一個平台自己的「在架 vs 成交」分布比較
    Q3  PayPay 的賣得掉率如何？（便宜但賣不掉 = 便宜是有代價的）
        → 在架觀測帳（store.listing_obs）的離場統計

── Q1 的方法論紅線（不處理就沒有結論可言）─────────────────────────
三個平台的「在架價」**語意不同**，直接比是工程原則 1 的混源比較：

    Yahoo      競價拍賣。我們只收**即決価格**（`include_live_auctions: false`），
               而即決價是賣家開的「不想等就付這個」溢價；純競標標的的「現在価格」
               只是當下出價，還會漲，**不是可成交價**。
    Mercari    定價出售 → 掛牌價 = 成交價。
    PayPay     定價出售，同上。

處理方式（本模組的實作）：
1. Yahoo 拆成**兩個序列**：`buyee_yahoo`（即決，可立即成交）與
   `buyee_yahoo_bid`（現在価格，競標中，**只供參考、不進主結論**）。
2. 主結論一律用「可立即成交的價格」對齊：Yahoo 即決 vs Mercari 定價 vs PayPay 定價。
3. **偏差方向必須寫在結論旁邊**：Yahoo 的競標尾盤常以低於即決價成交，
   所以 Yahoo 的真實可得價格**比這裡的即決價更低**。也就是說——
   這份比較**系統性地高估 Yahoo、有利於 PayPay**。任何「PayPay 比較便宜」
   的結論都要先扣掉這個順風，才是它真正的 edge。

── 為什麼是分層比較，不是整體中位數 ─────────────────────────────
各平台賣的東西組成不同（Yahoo 的低稀有度／低分數本來就偏多）。不分層就會把
「賣的東西不一樣」算成「平台便宜」。分層維度與 valuation._fit_venue_curve
**刻意完全相同**（稀有度 × 機構 × 分數）——兩邊量的必須是同一個東西，
不然「在架比較」與「成交係數」對不起來，而那正是本研究要對照的兩個數字。

── 樣本不足時的行為（唯一正確的答案是「不知道」）───────────────
每個分層每個平台至少 `min_per_cell` 筆、可比分層至少 `min_strata` 個，
否則回 `verdict="insufficient"` 並說明差多少。**不外插、不放寬、不給數字**：
使用者拿這份結論決定把錢投到哪個平台，一個沒有樣本支撐的倍率比沒有答案危險。
"""

from __future__ import annotations

import statistics
import time
from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .domain import Listing
from .parsers import is_candidate, parse_card

#: 一個 (平台, 分層) 格至少要幾筆才拿來比。4 是 scoring.min_comps 的同一個數字：
#: 中位數在 n<4 時基本上就是「隨便挑一筆」。
MIN_PER_CELL = 4
#: 可比分層至少要幾個才敢給倍率。1 個分層的比值是軼事，不是量測。
MIN_STRATA = 3

#: 可立即成交的三個平台（主結論只看這三個）。
VENUES: tuple[str, ...] = ("buyee_yahoo", "buyee_mercari", "buyee_paypay")
#: Yahoo 競標中的「現在価格」序列。**參考用，不可與上面三個直接比較。**
YAHOO_BID_VENUE = "buyee_yahoo_bid"
#: 比值的分母。與 valuation 的基準平台一致（Yahoo 樣本最多）。
BASE_VENUE = "buyee_yahoo"

VENUE_STUDY_META_KEY = "venue_study"

_LABEL = {
    "buyee_yahoo": "Yahoo 即決（可立即成交）",
    "buyee_mercari": "Mercari 定價",
    "buyee_paypay": "PayPay 定價",
    YAHOO_BID_VENUE: "Yahoo 現在価格（競標中・不可比）",
}


def venue_study_label(venue: str) -> str:
    return _LABEL.get(venue, venue)


# ---------------------------------------------------------------------------
# 純統計層（不碰網路、不碰 db，可用固定資料驗算）
# ---------------------------------------------------------------------------
def quartiles(values: list[float]) -> dict[str, Any] | None:
    """n / p25 / 中位 / p75 / min / max。空清單回 None（不是回 0）。

    只用中位與四分位、不用平均：二手卡價右尾極長，一筆離群值就能把平均
    拉到沒有意義的地方（與 valuation.ValuationModel 的同一個理由）。
    """
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return None
    return {
        "n": len(xs),
        "p25": _pct(xs, 25),
        "median": statistics.median(xs),
        "p75": _pct(xs, 75),
        "min": xs[0],
        "max": xs[-1],
    }


def _pct(sorted_xs: list[float], pct: float) -> float:
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    k = (len(sorted_xs) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def stratum_key(row: dict[str, Any]) -> tuple[str | None, str | None, float | None]:
    """(稀有度, 機構, 分數)。維度與 valuation._fit_venue_curve 完全相同。"""
    grade = row.get("grade")
    return (
        (row.get("rarity") or None),
        (row.get("grader") or None),
        float(grade) if grade is not None else None,
    )


def stratum_label(key: tuple[str | None, str | None, float | None]) -> str:
    rarity, grader, grade = key
    return f"{rarity or '稀有度不明'} × {grader or '機構不明'}{f' {grade:g}' if grade is not None else ''}"


def stratum_medians(
    rows: list[dict[str, Any]],
    *,
    min_per_cell: int = MIN_PER_CELL,
    price_key: str = "price_twd",
) -> dict[tuple, dict[str, Any]]:
    """分層 → 該層的價格統計。**樣本不足的分層直接不存在**（不是存一個 n 很小的值）。

    讓「不夠」在資料結構層面就消失，下游任何一條路徑都不可能不小心用到它
    ——這比在每個呼叫點記得檢查 n 可靠（工程原則的結構性守門）。
    """
    buckets: dict[tuple, list[float]] = {}
    for r in rows:
        price = r.get(price_key)
        if price is None or float(price) <= 0:
            continue
        buckets.setdefault(stratum_key(r), []).append(float(price))
    out: dict[tuple, dict[str, Any]] = {}
    for key, vals in buckets.items():
        if len(vals) < min_per_cell:
            continue
        q = quartiles(vals)
        if q:
            out[key] = q
    return out


def ratio_across_strata(
    base: dict[tuple, dict[str, Any]],
    other: dict[tuple, dict[str, Any]],
    *,
    min_strata: int = MIN_STRATA,
) -> dict[str, Any]:
    """在**共同分層**上比兩組中位數，回 `other / base` 的分層中位比值。

    配對比較（同一個分層內比），不是拿兩個整體中位數相除：後者會把
    「兩邊賣的東西不一樣」算成價差。可比分層不足 `min_strata` 時
    `verdict="insufficient"`，**且 `ratio` 是 None**——沒有數字可以被誤引用。
    """
    pairs: list[dict[str, Any]] = []
    for key in sorted(set(base) & set(other), key=stratum_label):
        b, o = base[key], other[key]
        if b["median"] <= 0:
            continue
        pairs.append({
            "stratum": stratum_label(key),
            "base_median": round(b["median"], 0),
            "base_n": b["n"],
            "other_median": round(o["median"], 0),
            "other_n": o["n"],
            "ratio": round(o["median"] / b["median"], 3),
        })
    if len(pairs) < min_strata:
        return {
            "n_strata": len(pairs),
            "ratio": None,
            "pairs": pairs,
            "verdict": "insufficient",
            "detail": (
                f"可比分層只有 {len(pairs)} 個（門檻 {min_strata}，"
                f"每層每邊需 ≥{MIN_PER_CELL} 筆）→ 不足以判定"
            ),
        }
    ratios = sorted(p["ratio"] for p in pairs)
    return {
        "n_strata": len(pairs),
        "ratio": round(statistics.median(ratios), 3),
        "ratio_min": ratios[0],
        "ratio_max": ratios[-1],
        # 「幾個分層站在哪一邊」比中位比值本身更能看出結論穩不穩：
        # 5 個分層 3:2 分裂的 1.1x 與 5 個分層 5:0 的 1.1x 是兩種不同的宣稱。
        "n_other_cheaper": sum(1 for r in ratios if r < 1.0),
        "pairs": pairs,
        "verdict": "ok",
        "detail": "",
    }


def stratum_table(
    rows_by_venue: dict[str, list[dict[str, Any]]],
    *,
    min_per_cell: int = MIN_PER_CELL,
    price_key: str = "price_twd",
) -> list[dict[str, Any]]:
    """給人看的分層對照表：每個分層一列，每個平台一格（含樣本不足的格）。

    這裡**刻意保留樣本不足的格**（標 `enough: False`）——報告要讓人看得見
    「這個分層 PayPay 只有 2 筆」，而不是安靜地少一欄。統計計算走
    `stratum_medians`，那條路才會把不足的格丟掉，兩者分工不混。
    """
    all_keys: set[tuple] = set()
    per_venue: dict[str, dict[tuple, list[float]]] = {}
    for venue, rows in rows_by_venue.items():
        buckets: dict[tuple, list[float]] = {}
        for r in rows:
            price = r.get(price_key)
            if price is None or float(price) <= 0:
                continue
            buckets.setdefault(stratum_key(r), []).append(float(price))
        per_venue[venue] = buckets
        all_keys |= set(buckets)

    table: list[dict[str, Any]] = []
    for key in all_keys:
        cells: dict[str, Any] = {}
        for venue, buckets in per_venue.items():
            q = quartiles(buckets.get(key, []))
            if q is None:
                cells[venue] = {"n": 0, "enough": False}
                continue
            cells[venue] = {
                "n": q["n"],
                "median": round(q["median"], 0),
                "p25": round(q["p25"], 0),
                "p75": round(q["p75"], 0),
                "enough": q["n"] >= min_per_cell,
            }
        enough = [v for v, c in cells.items() if c.get("enough") and v in VENUES]
        table.append({
            "stratum": stratum_label(key),
            "key": [key[0], key[1], key[2]],
            "cells": cells,
            "comparable_venues": sorted(enough),
            "comparable": len(enough) >= 2,
            "total_n": sum(c.get("n", 0) for c in cells.values()),
        })
    table.sort(key=lambda t: (-int(t["comparable"]), -t["total_n"]))
    return table


# ---------------------------------------------------------------------------
# 在架調查（會打外網）
# ---------------------------------------------------------------------------
#: 一次調查的預設關鍵字。取自 watchlist `comps_queries` 的同一組維度
#: （年代詞 × 稀有度詞 × 機構），但**刻意寫死一組短清單**：組合展開會乘成
#: 數十個查詢 × 三個平台，請求數直接失控。這組 8 條實測 keep 率都在 30/50 以上。
DEFAULT_SURVEY_KEYWORDS: tuple[str, ...] = (
    "遊戯王 初期 PSA",
    "遊戯王 初期 レリーフ PSA",
    "遊戯王 初期 ウルトラ PSA",
    "遊戯王 二期 PSA",
    "遊戯王 三期 PSA",
    "遊戯王 ARS 鑑定 初期",
    "遊戯王 旧アジア PSA",
    "遊戯王 バンダイ 鑑定",
)

#: 每個來源翻幾頁。差異來自**單頁容量與可用率**，不是偏好：
#: Mercari 一頁 100、PayPay 一頁 40、Yahoo 一頁 50 但**只有約一半的標的有即決價**
#: （純競標的現在価格不可比，只能進參考序列），再扣掉年代／機構篩選，
#: 實測 2 頁只剩 39 筆可比樣本——不夠撐起 3 個可比分層。所以 Yahoo 翻最多頁。
#: 多抓樣本不會讓任何平台的中位數變便宜（分層中位數對樣本數不敏感），
#: 它只決定「這一格有沒有資格進 ≥4 筆的門檻」。
DEFAULT_SURVEY_PAGES: dict[str, int] = {
    "yahoo_direct": 4,
    "buyee_mercari": 2,
    # 直抓一頁 100 筆（走 Buyee 鏡像時是 40，所以那時要 3 頁）
    "paypay_direct": 2,
}

#: 請求數硬上限。超過就停止並在報告裡說明剩下幾個查詢沒跑——
#: 「跑了一半」必須看得見，不能靜靜地變成「樣本比較少」。
DEFAULT_MAX_REQUESTS = 150

SURVEY_SOURCES: tuple[str, ...] = ("yahoo_direct", "buyee_mercari", "paypay_direct")


def _yahoo_with_live_auctions(cfg: Config, registry: dict[str, Any]) -> Any:
    """Yahoo source 的調查版：**打開純競標**，才拿得到「現在価格」序列。

    掃描器預設關掉它是對的（現在価格不是可成交價，混進成本模型會產生假
    FREE_CARD）。但這份研究要回答的正是「Yahoo 的真實可得價格比即決低多少」，
    所以這裡刻意打開，並在 row 上留 `price_kind`，讓兩個序列從頭到尾分得開。
    只覆寫這一個來源的設定、共用同一顆 fetcher（節流與快取都不重來）。
    """
    from .sources.yahoo import YahooAuctionSource

    src = registry.get("yahoo_direct")
    if src is None:
        return None
    spec = {**(cfg.sources.get("yahoo_direct") or {}), "include_live_auctions": True}
    patched = dc_replace(cfg, sources={**cfg.sources, "yahoo_direct": spec})
    return YahooAuctionSource(patched, getattr(src, "fetcher", None))


def _seller_feedback(raw: dict | None) -> tuple[int | None, float | None]:
    """從 raw 的 seller 物件抽 (feedback 數, 好評率)。**逐站鍵名不同**：

    eBay 是 `feedbackScore`（int）／`feedbackPercentage`（字串 "100.0"）；
    PayPay 是 `numRating`（int）／`goodRatio`（數值 %）。抽不到一律 None。
    語意逐站不同，只能同站比較——sellers 表以 site 為 key 的一半，結構上先擋。
    """
    s = (raw or {}).get("seller")
    if not isinstance(s, dict):
        return None, None
    score = s.get("feedbackScore", s.get("numRating"))
    pct = s.get("feedbackPercentage", s.get("goodRatio"))
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    try:
        pct = float(pct) if pct is not None else None
    except (TypeError, ValueError):
        pct = None
    return score, pct


def listing_row(
    lst: Listing, info: Any, *, source: str, fx: Any, price_kind: str
) -> dict[str, Any]:
    """在架標的 → 一列觀測。

    **`price_twd` 一律 `apply_markup=False`**：comps 就是這麼換算的
    （comps.py `ingest_sold`），在架與成交要比就必須同一把尺。加了刷卡手續費
    的那把尺是「到手成本」，另一個問題、另一個欄位（`landed_twd`）。

    `seller_id` 進 listing_obs（帳本欄位）；`seller_feedback_*` **不是**
    listing_obs 的欄位，只是隨 row 捎給 `record_listing_scan` 更新 sellers 表
    （content 欄位清單不認得的鍵會被忽略，不落 listing_obs）。
    """
    venue = lst.site.value
    if venue == "buyee_yahoo" and price_kind == "current_bid":
        venue = YAHOO_BID_VENUE
    fb_score, fb_pct = _seller_feedback(lst.raw)
    return {
        "key": lst.key,
        "source": source,
        "site": lst.site.value,
        "venue": venue,
        "title": lst.title,
        "url": lst.url,
        "price_native": lst.price,
        "currency": lst.currency.value,
        "price_twd": round(fx.to_twd(lst.price, lst.currency, apply_markup=False), 0),
        "rarity": info.rarity,
        "grader": info.grader.value,
        "grade": info.grade,
        "era_evidence": ",".join(info.era_evidence),
        "card_name": None,
        "price_kind": price_kind,
        "seller_id": lst.seller_id,
        "seller_feedback_score": fb_score,
        "seller_feedback_pct": fb_pct,
    }


def run_listing_survey(
    cfg: Config,
    *,
    registry: dict[str, Any],
    fx: Any,
    keywords: tuple[str, ...] | list[str] = DEFAULT_SURVEY_KEYWORDS,
    pages: dict[str, int] | None = None,
    max_requests: int = DEFAULT_MAX_REQUESTS,
    sources: tuple[str, ...] = SURVEY_SOURCES,
) -> dict[str, Any]:
    """跨平台在架價調查（同步、會打外網、吃 `fetch.delay_seconds` 節流）。

    每個 (來源, 關鍵字) 各自隔離（`run_source_search` 保證不往外拋），一條管道
    被擋不會讓另外兩邊的樣本消失，病名留在 `sources[].health`。
    請求數以**實際抓到的頁數**計（`SearchResult.pages_fetched`），達到上限就停，
    並回報還有幾個查詢沒跑——半份資料必須看得出是半份。
    """
    from .pipeline import run_source_search

    pages = pages or DEFAULT_SURVEY_PAGES
    t0 = time.perf_counter()
    live_yahoo = _yahoo_with_live_auctions(cfg, registry)

    rows: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    requests = 0
    skipped_queries = 0
    seen_keys: set[str] = set()

    for name in sources:
        src = live_yahoo if name == "yahoo_direct" and live_yahoo is not None else registry.get(name)
        if src is None:
            reports.append({"source": name, "health": "missing", "detail": "registry 沒有這個來源",
                            "parsed": 0, "listings": 0, "requests": 0})
            continue
        n_pages = max(1, int(pages.get(name, 1)))
        got = parsed = used = 0
        health, detail = "ok", ""
        for kw in keywords:
            if requests + n_pages > max_requests:
                skipped_queries += 1
                continue
            # max_price=None：研究要看的是整個價格分布，平台側先砍就再也救不回來
            res = run_source_search(name, src, kw, pages=n_pages, max_price=None)
            used += res.pages_fetched or 1
            requests += res.pages_fetched or 1
            parsed += res.parsed_count
            if res.health.value not in ("ok", "empty"):
                health, detail = res.health.value, res.detail
            for lst in res.listings:
                if lst.key in seen_keys:
                    continue
                seen_keys.add(lst.key)
                info = parse_card(lst.title, cfg.watchlist)
                ok, why = is_candidate(info, cfg.watchlist)
                if not ok:
                    rejected[why] = rejected.get(why, 0) + 1
                    continue
                kind = str((lst.raw or {}).get("price_kind") or "fixed")
                rows.append(listing_row(lst, info, source=name, fx=fx, price_kind=kind))
                got += 1
        reports.append({"source": name, "health": health, "detail": detail,
                        "parsed": parsed, "listings": got, "requests": used})

    return {
        "rows": rows,
        "requests": requests,
        "max_requests": max_requests,
        "skipped_queries": skipped_queries,
        "keywords": list(keywords),
        "pages": dict(pages),
        "elapsed_seconds": round(time.perf_counter() - t0, 1),
        "sources": reports,
        "funnel": {
            "candidates": len(rows),
            "rejected": dict(sorted(rejected.items(), key=lambda kv: -kv[1])[:8]),
        },
    }


def rows_from_listing_obs(obs_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把在架觀測帳的列轉成研究用的 row（`--no-survey` 走這條）。

    `listing_obs` 記的就是每輪掃描看到的候選，欄位與 `listing_row` 同一組
    ——這是刻意的：不跑新調查時，研究要能拿既有觀測回答同一個問題，
    而不是拿一份空資料覆蓋掉上一次的結論。
    唯一要補的是分析用的 `venue`（Yahoo 的競標序列要分家），規則與
    `listing_row` 同一份，不在兩個地方各寫一次。
    """
    out: list[dict[str, Any]] = []
    for r in obs_rows:
        venue = r.get("site")
        if venue == "buyee_yahoo" and r.get("price_kind") == "current_bid":
            venue = YAHOO_BID_VENUE
        out.append({**r, "venue": venue})
    return out


# ---------------------------------------------------------------------------
# 三個問題的組裝
# ---------------------------------------------------------------------------
def _by_venue(rows: list[dict[str, Any]], key: str = "venue") -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(str(r.get(key) or "unknown"), []).append(r)
    return out


def answer_q1(
    listing_rows: list[dict[str, Any]],
    *,
    min_per_cell: int = MIN_PER_CELL,
    min_strata: int = MIN_STRATA,
) -> dict[str, Any]:
    """Q1：同規格的卡，哪個平台「現在就買得到的價格」最低？

    只用可立即成交的序列（Yahoo **即決** vs Mercari 定價 vs PayPay 定價）。
    Yahoo 的現在価格另外算一份 `bid_reference`，**不進主結論**。
    """
    grouped = _by_venue(listing_rows)
    buyable = {v: grouped.get(v, []) for v in VENUES}
    medians = {v: stratum_medians(rows, min_per_cell=min_per_cell) for v, rows in buyable.items()}
    base = medians.get(BASE_VENUE, {})

    ratios = {
        v: ratio_across_strata(base, medians.get(v, {}), min_strata=min_strata)
        for v in VENUES if v != BASE_VENUE
    }
    # PayPay vs Mercari 也要有——兩個定價平台之間的比較沒有「即決溢價」污染，
    # 是這份研究裡語意最乾淨的一組數字。
    ratios["buyee_paypay_vs_mercari"] = ratio_across_strata(
        medians.get("buyee_mercari", {}), medians.get("buyee_paypay", {}), min_strata=min_strata
    )

    bid_rows = grouped.get(YAHOO_BID_VENUE, [])
    bid_ref = ratio_across_strata(
        base, stratum_medians(bid_rows, min_per_cell=min_per_cell), min_strata=min_strata
    )

    counts = {v: len(rows) for v, rows in grouped.items()}
    verdict, headline = _q1_verdict(ratios)
    return {
        "table": stratum_table({**buyable, YAHOO_BID_VENUE: bid_rows}, min_per_cell=min_per_cell),
        "ratios": ratios,
        "counts": counts,
        "bid_reference": bid_ref,
        "verdict": verdict,
        "headline": headline,
        "caveats": [
            "Yahoo 的在架價是**即決価格**（賣家開的「不想等就付這個」溢價）。競標尾盤"
            "常以低於即決價成交，所以 Yahoo 的真實可得價格比表中更低——這份比較"
            "**系統性有利於 PayPay/Mercari**，任何「PayPay 較便宜」的結論要先扣掉這個順風。",
            "在架價 ≠ 成交價。掛著的價格只證明賣家開這個價，不證明有人買得到"
            "（真正的對照是 Q2）。",
            "價格一律 apply_markup=False 換算成台幣，與 comps 同一把尺；"
            "不含運費與手續費（那是到手成本，另一個問題）。",
        ],
    }


def _q1_verdict(ratios: dict[str, dict[str, Any]]) -> tuple[str, str]:
    pp = ratios.get("buyee_paypay") or {}
    if pp.get("verdict") != "ok":
        return "insufficient", f"PayPay vs Yahoo 即決：{pp.get('detail') or '樣本不足'}"
    r = pp["ratio"]
    side = f"{pp['n_other_cheaper']}/{pp['n_strata']} 個分層 PayPay 較便宜"
    if r < 0.9:
        return "paypay_cheaper", f"PayPay 在架價是 Yahoo 即決的 {r:.2f}×（{side}）"
    if r > 1.1:
        return "paypay_pricier", f"PayPay 在架價是 Yahoo 即決的 {r:.2f}×（{side}）"
    return "no_difference", f"PayPay 與 Yahoo 即決在架價相當（{r:.2f}×，{side}）"


def answer_q2(
    listing_rows: list[dict[str, Any]],
    sold_rows: list[dict[str, Any]],
    *,
    min_per_cell: int = MIN_PER_CELL,
    min_strata: int = MIN_STRATA,
) -> dict[str, Any]:
    """Q2：成交價的平台係數是真價差，還是選擇偏差？

    **決定性檢定**：對同一個平台，比它自己的「目前在架」與「近期成交」。
    在架與成交來自同一個平台、同一組分層，所以平台結構差異被消掉了，
    剩下的就是「什麼樣的價格賣得掉」。

        成交 ≫ 在架  → 便宜的賣不掉、只有貴的成交 → **選擇偏差**，係數不可信
        兩者接近     → 係數反映真實市場水準

    ⚠️ Yahoo 這一列的語意與另外兩個不同，不可平行解讀：它的在架邊是
    **即決價**（賣家的 ask），成交邊是**競標出清價**。ratio < 1 是這個
    市場結構的必然結果，不構成任何選擇偏差的證據。Mercari/PayPay 兩邊
    都是定價，才是乾淨的對照。
    """
    listing_by_venue = _by_venue(listing_rows)
    sold_by_venue = _by_venue(sold_rows, key="site")
    out: dict[str, Any] = {}
    for venue in VENUES:
        listed = stratum_medians(listing_by_venue.get(venue, []), min_per_cell=min_per_cell)
        sold = stratum_medians(sold_by_venue.get(venue, []), min_per_cell=min_per_cell)
        res = ratio_across_strata(listed, sold, min_strata=min_strata)
        venue_sold = sold_by_venue.get(venue, [])
        # 「成交是過去 N 天的存量」這句話對 sold_at 是入庫時間的那批**不成立**——
        # 它們的時間是我們抓到的時間。逐平台把筆數報出來，讓讀的人知道
        # 這一列的「時間」有多少是真的（見 store 的 sold_at_is_ingest）。
        ingest_n = sum(1 for r in venue_sold if r.get("sold_at_is_ingest"))
        res.update({
            "listing_n": len(listing_by_venue.get(venue, [])),
            "sold_n": len(venue_sold),
            "sold_ingest_time_n": ingest_n,
            "listing_strata": len(listed),
            "sold_strata": len(sold),
            "reading": _q2_reading(venue, res),
        })
        out[venue] = res
    total_ingest = sum(v["sold_ingest_time_n"] for v in out.values())
    caveats = [
        "Yahoo 那一列不可與另外兩列平行解讀：在架邊是即決價（ask）、成交邊是"
        "競標出清價，ratio<1 是市場結構的必然，不是選擇偏差的證據。",
        "成交樣本本身就有存活偏差（賣不掉的永遠不會有成交價）——這正是這個"
        "檢定要量的東西，不是它的缺陷。",
        "在架是**此刻**的快照，成交是過去 N 天的存量：市場漂移會混進這個比值。",
    ]
    if total_ingest:
        caveats.append(
            f"⚠️ 成交樣本裡有 {total_ingest} 筆的 sold_at 是**入庫時間不是成交時間**"
            "（Buyee 系的已售出頁沒有成交時間），所以上一條的「過去 N 天」對它們"
            "無效——那批只知道「我們看到它時已經賣掉了」。逐平台筆數見 "
            "sold_ingest_time_n。"
        )
    return {"by_venue": out, "caveats": caveats}


def _q2_reading(venue: str, res: dict[str, Any]) -> str:
    if res.get("verdict") != "ok":
        return f"不足以判定（{res.get('detail', '')}）"
    r = res["ratio"]
    if venue == BASE_VENUE:
        return (
            f"成交/在架 = {r:.2f}×。Yahoo 的在架是即決 ask、成交是競標出清價，"
            "這個比值量的是「即決溢價」，不是選擇偏差。"
        )
    if r >= 1.5:
        return f"成交是在架的 {r:.2f}× → **強烈的選擇偏差訊號**：便宜的掛著沒賣掉，成交的都是貴的。"
    if r >= 1.15:
        return f"成交是在架的 {r:.2f}× → 有選擇偏差跡象，成交價偏貴。"
    if r <= 0.85:
        return f"成交是在架的 {r:.2f}× → 成交價低於掛牌，議價／降價後成交是常態。"
    return f"成交與在架接近（{r:.2f}×）→ 沒有明顯選擇偏差，掛牌價大致就是市場水準。"


def answer_q3(
    summary: dict[str, Any], *, min_decided: int = 30, min_events: int = 10
) -> dict[str, Any]:
    """Q3：賣得掉率。**目前幾乎必然回「不足以判定」**，理由要講清楚。

    proxy 是「標的從後續掃描中消失」，而本專案的在架掃描是新着降冪 + 只抓
    第 1 頁——多數標的是被新貨擠出觀測窗，不是賣掉。store 那層已經把兩者
    分開記（`disappeared_at` vs `window_exit_at`），這裡只用前者，
    並用 `revived`（判定為消失後又出現）當這條規則自己的錯誤率。

    **兩道門檻，缺一不可**：
      `min_decided`  已定案的觀測數（消失 + 仍在架）——分母要夠大。
      `min_events`   真的發生的消失事件數——分子是 0/40 時，比率的相對誤差
                     大到沒有意義，而「0%」看起來卻像一個確定的答案。
                     只有分母門檻的版本會在事件數為 0 時報出「賣得掉率 0%」，
                     那是用一個看起來很確定的數字說「我還不知道」。
    """
    by_site = {r.get("site"): r for r in summary.get("by_site", [])}
    rows = []
    for venue in VENUES:
        s = by_site.get(venue) or {}
        total = int(s.get("total") or 0)
        gone = int(s.get("disappeared") or 0)
        exited = int(s.get("window_exit") or 0)
        still = int(s.get("still_open") or 0)
        decided = gone + still
        ready = decided >= min_decided and gone >= min_events
        rows.append({
            "venue": venue,
            "total": total,
            "still_open": still,
            "disappeared": gone,
            "window_exit": exited,
            "revived": int(s.get("revived") or 0),
            "multi_seen": int(s.get("multi_seen") or 0),
            "decided": decided,
            "sell_through": round(gone / decided, 3) if ready and decided else None,
            "blocked_by": (
                "" if ready
                else f"已定案 {decided}/{min_decided}、消失事件 {gone}/{min_events}"
            ),
            "oldest": s.get("oldest"),
            "newest": s.get("newest"),
        })
    enough = [r for r in rows if r["sell_through"] is not None]
    return {
        "rows": rows,
        "min_decided": min_decided,
        "min_events": min_events,
        "verdict": "ok" if len(enough) >= 2 else "insufficient",
        "detail": (
            "" if len(enough) >= 2 else
            f"只有 {len(enough)} 個平台同時達到「已定案 ≥{min_decided} 筆」與"
            f"「消失事件 ≥{min_events} 筆」，不足以比較賣得掉率"
        ),
        "caveats": [
            "「消失」只是成交的 proxy：下架、賣家改標題、被平台隱藏都會長得一模一樣。",
            "抓取形態是新着降冪 + 第 1 頁，多數標的是被新貨擠出觀測窗（記為 "
            "window_exit，不算消失）。revived 欄是這條推論規則自己的錯誤率，"
            "它若不接近 0，賣得掉率就不可信。",
        ],
    }


def build_study(
    *,
    survey: dict[str, Any] | None,
    sold_rows: list[dict[str, Any]],
    listing_obs_summary: dict[str, Any],
    min_per_cell: int = MIN_PER_CELL,
    min_strata: int = MIN_STRATA,
) -> dict[str, Any]:
    """把三個答案組成一份可存、可上 dashboard 的報告（純函式，不碰網路）。"""
    rows = list((survey or {}).get("rows") or [])
    q1 = answer_q1(rows, min_per_cell=min_per_cell, min_strata=min_strata)
    q2 = answer_q2(rows, sold_rows, min_per_cell=min_per_cell, min_strata=min_strata)
    q3 = answer_q3(listing_obs_summary)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "params": {"min_per_cell": min_per_cell, "min_strata": min_strata},
        "survey": {k: v for k, v in (survey or {}).items() if k != "rows"},
        "listing_n": len(rows),
        "sold_n": len(sold_rows),
        "q1": q1,
        "q2": q2,
        "q3": q3,
    }


__all__ = [
    "BASE_VENUE",
    "DEFAULT_MAX_REQUESTS",
    "DEFAULT_SURVEY_KEYWORDS",
    "DEFAULT_SURVEY_PAGES",
    "MIN_PER_CELL",
    "MIN_STRATA",
    "VENUES",
    "VENUE_STUDY_META_KEY",
    "YAHOO_BID_VENUE",
    "answer_q1",
    "answer_q2",
    "answer_q3",
    "build_study",
    "listing_row",
    "quartiles",
    "ratio_across_strata",
    "rows_from_listing_obs",
    "run_listing_survey",
    "stratum_label",
    "stratum_medians",
    "stratum_table",
    "venue_study_label",
]
