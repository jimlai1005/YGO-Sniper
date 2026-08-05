"""關鍵字搜尋：給一個卡名，回「最適合入手的賣場清單」。

與 `appraise.py` 的分工：appraise 是「你已經找到一個標的，幫我判」（單一網址），
這裡是「我還沒找到，幫我找」（關鍵字 → 三個平台的在架標的）。
**判準必須是同一套**——同一個標的在兩個入口得到不同判決，兩個數字就都不能信了。
所以判決一律呼叫 `appraise.decide_verdict()`，可比樣本一律呼叫
`appraise.collect_comparables()`，估價一律 `valuation.Valuator.estimate()`。
這個模組自己**不定義任何門檻**（沒有自己的 P 門檻、沒有自己的樣本數下限）。

── 三個刻意的決定 ──────────────────────────────────────────────────
1. **不套價格上限。** 掃描器會用「到手成本＝鑑定費」反推的上限讓平台先過濾，
   因為它在找撿漏。這裡是使用者主動查一張卡，預算是他自己給的，而且他明說
   要看得到預算外的東西——平台側先砍掉就再也救不回來。

2. **每一筆都算兩份估價**（有／無平台校正），一次回傳。
   平台校正不是排序選項，它會改變公允價本身（Yahoo 競價出清價與 Mercari
   定價零售價差 2 倍以上），連帶改變區間、P(值得買) 與判決。所以「切換校正
   不重抓」不可能只靠前端重排——兩份都算好，前端切的是「看哪一份」。
   成本是每筆多一次 estimate（純記憶體運算，實測整體 <1s）。

3. **丟掉的筆數要留下原因。** 「搜到 90 筆只列出 3 筆」如果沒有漏斗，
   使用者只會覺得工具很爛；有了「符合年代 3 筆、無年代證據 71 筆、
   未偵測到鑑定機構 16 筆」，他才知道那是**篩選器在工作**，不是搜尋壞了。
"""

from __future__ import annotations

import time
from typing import Any

from .appraise import collect_comparables, decide_verdict
from .bidding import is_live_auction, max_bid_jpy
from .cards import CardIndex, CardMatch
from .config import Config
from .costs import quote_all_routes
from .domain import CardInfo, Listing
from .parsers import is_candidate, parse_card
from .pipeline import run_source_search
from .sources.health import ParseHealth
from .valuation import Valuator, build_valuator, load_comps_rows, venue_label

#: dashboard 關鍵字搜尋要打的三條在架管道（eBay 不在內：它是英文站、
#: 而且未設 App ID 時整條是關掉的；yahoo_closed 是成交價來源不是在架來源）。
SEARCH_SOURCES: tuple[str, ...] = ("yahoo_direct", "buyee_mercari", "paypay_direct")

#: 兩種估價視角的鍵名。`venue` = 校正到標的自己的平台；`mixed` = 完全不校正
#: （混合平台水準）。名字寫死在這裡，前端與測試都引用同一組字串。
VIEW_VENUE = "venue"
VIEW_MIXED = "mixed"
VIEWS = (VIEW_VENUE, VIEW_MIXED)

_HEALTH_LABEL = {
    ParseHealth.OK: "正常",
    ParseHealth.EMPTY_CONFIRMED: "查無結果",
    ParseHealth.PARSER_BROKEN: "解析壞了",
    ParseHealth.BLOCKED: "被擋",
    ParseHealth.FETCH_FAILED: "連線失敗",
}

#: 在架價格的語意。這是本專案的紅線：Yahoo 的「現在価格」不是你付得出去的價格。
_PRICE_KIND_LABEL = {
    # eBay 的定價／Buy It Now 也是這一格（2026-08-03 起 eBay 也會標 price_kind），
    # 所以標籤不能只寫 Yahoo 的說法。
    "buyout": "即決／Buy It Now（可直接成交）",
    "current_bid": "⚠️ 競標中（現在価格，不是可成交價）",
    "fixed": "定價（可直接成交）",
}


def _price_kind(lst: Listing) -> str:
    """Buyee 系兩站都是定價出售；Yahoo 直抓會在 raw 標好 buyout / current_bid。"""
    return str((lst.raw or {}).get("price_kind") or "fixed")


def _card_dict(info: CardInfo, match: CardMatch | None, card_name: str | None) -> dict[str, Any]:
    return {
        "grader": info.grader.value,
        "grade": info.grade,
        "rarity": info.rarity,
        "in_era": info.in_era,
        "era_evidence": list(info.era_evidence),
        "set_code": info.set_code,
        "card_name": card_name,
        "matched_name": match.name_ja if match else None,
        "matched_in_era": match.in_era if match else None,
    }


def _named_rows(rows: list[dict[str, Any]], index: CardIndex | None) -> list[dict[str, Any]]:
    """把卡名先解一次，之後每筆標的的 `collect_comparables` 就不必重跑比對。

    語意與 `collect_comparables(index=...)` 那條路**完全相同**（同一支
    `index.match`、同一個 in_era 條件），只是把 O(標的數 × 成交數) 的比對
    降成 O(成交數)。不同源就會出現「報告列的樣本」與「模型用的樣本」不一致，
    所以這裡刻意複製的是呼叫方式，不是判斷邏輯。
    """
    if index is None or not index.available:
        return rows
    out = []
    for r in rows:
        m = index.match(r.get("title") or "")
        out.append({**r, "card_name": m.name_ja if (m and m.in_era) else None})
    return out


def _estimate_view(
    *,
    valuator: Valuator,
    rows: list[dict[str, Any]],
    card_name: str | None,
    info: CardInfo,
    match: CardMatch | None,
    landed_twd: float,
    venue: str | None,
) -> tuple[dict[str, Any], Any]:
    """一個視角（有／無平台校正）的完整答案：估價＋可比樣本＋判決。

    回傳 `(視角 dict, Estimate 物件)`。**出價上限要吃 Estimate 本身**，
    而且只能吃有平台校正的那一份——競標上限用混合平台水準去算會高估 2 倍，
    高估的方向正好是「上限開太高」，是會真的付錢的那一邊。回傳物件而不是
    讓呼叫端重算一次，是為了保證上限與畫面上顯示的區間出自同一次估價。

    `venue=None` 就是 `Valuator.estimate` 的「不做平台校正」路徑，
    回傳的公允價是混合平台水準——這一點由 estimate 自己在 notes 講清楚，
    這裡不重寫警語（同一句話兩個地方寫，遲早會分岔）。
    """
    estimate = valuator.estimate(
        card_name=card_name,
        rarity=info.rarity,
        grade=info.grade,
        landed_twd=landed_twd,
        venue=venue,
    )
    comparables, stats = collect_comparables(
        rows, None, card_name=card_name, rarity=info.rarity, grade=info.grade, venue=venue
    )
    verdict, reasons, numbers = decide_verdict(
        landed_twd=landed_twd,
        estimate=estimate,
        comparables=comparables,
        comp_stats=stats,
        card=info,
        card_match=match,
    )
    return {
        "verdict": verdict,
        "verdict_reasons": reasons,
        "verdict_numbers": numbers,
        "estimate": estimate.to_dict(),
        "fair_twd": estimate.fair_twd,
        "lo_twd": estimate.lo_twd,
        "hi_twd": estimate.hi_twd,
        "p_worth_buying": estimate.p_worth_buying,
        "comparable_n": stats.get("n", 0),
        "comparable_tier": stats.get("tier"),
        "comparable_tier_label": stats.get("tier_label"),
        "comparable_median_twd": stats.get("median_twd"),
        "comparable_stats": stats,
    }, estimate


def sort_key(item: dict[str, Any], view: str) -> tuple[int, float, float]:
    """排序：預算內優先 → P(值得買) 降冪 → 到手成本升冪。

    超預算的**不丟掉**（使用者說想看看預算外有什麼），但一律排在後面：
    排序鍵第一位就是 over_budget，這樣「預算內的爛標的」也會排在
    「預算外的好標的」前面——那是刻意的，預算是使用者給的硬條件。
    P(值得買) 沒有值（校準集不足，模型不給區間）時當成 -1：不是 0，
    因為 0 是「模型算過、認為很不值得」，兩者不該排在一起。
    """
    p = item["views"][view].get("p_worth_buying")
    return (
        1 if item["over_budget"] else 0,
        -(p if p is not None else -1.0),
        item["landed_twd"],
    )


def search_market(
    cfg: Config,
    keyword: str,
    *,
    registry: dict[str, Any],
    store: Any,
    fx: Any,
    index: CardIndex | None = None,
    budget_twd: float = 1200.0,
    sources: tuple[str, ...] = SEARCH_SOURCES,
    pages: int = 1,
    valuator: Valuator | None = None,
) -> dict[str, Any]:
    """關鍵字 → 可入手清單（同步、會打外網）。

    `registry` 是 `sources.build_sources()` 的結果，由呼叫端持有——web 層要
    跨請求共用同一顆 WAF token（重建一次要開瀏覽器約 20 秒）。
    來源之間互相隔離（`run_source_search` 保證不往外拋），所以 Mercari 被擋
    不會讓 Yahoo 那兩排結果消失，只會在 `sources[].health` 留下病名。
    """
    t_all = time.perf_counter()
    idx = index if index is not None else CardIndex.load()

    # --- 1. 抓（每個來源各自計時、各自隔離）--------------------------
    reports: list[dict[str, Any]] = []
    found: list[tuple[str, Listing]] = []
    for name in sources:
        src = registry.get(name)
        if src is None:
            reports.append({
                "source": name, "site": None, "health": "missing",
                "health_label": "來源不存在", "parsed_count": 0, "listings": 0,
                "elapsed_seconds": 0.0, "url": "", "detail": "registry 沒有這個來源",
            })
            continue
        t0 = time.perf_counter()
        # max_price=None：預算是使用者的判斷，不在平台側先砍（見模組 docstring）
        res = run_source_search(name, src, keyword, pages=pages, max_price=None)
        elapsed = time.perf_counter() - t0
        reports.append({
            "source": name,
            "site": res.site,
            "site_label": venue_label(res.site),
            "health": res.health.value,
            "health_label": _HEALTH_LABEL.get(res.health, res.health.value),
            "ok": res.health in (ParseHealth.OK, ParseHealth.EMPTY_CONFIRMED),
            # parsed_count 是「解析器解出幾個」，listings 是「商業篩選後剩幾個」——
            # 兩個都要露出來，使用者才分得出「這站沒貨」與「這站的貨我買不了」
            "parsed_count": res.parsed_count,
            "listings": len(res.listings),
            "elapsed_seconds": round(elapsed, 2),
            "url": res.url,
            "detail": res.detail,
        })
        found.extend((name, lst) for lst in res.listings)

    # --- 2. 篩（丟掉的要留原因）---------------------------------------
    rejected: dict[str, int] = {}
    kept: list[tuple[str, Listing, CardInfo]] = []
    for name, lst in found:
        info = parse_card(lst.title, cfg.watchlist)
        ok, why = is_candidate(info, cfg.watchlist)
        if ok:
            kept.append((name, lst, info))
        else:
            rejected[why] = rejected.get(why, 0) + 1

    # --- 3. 算（成本 → 估價 → 判決）------------------------------------
    val = valuator if valuator is not None else build_valuator(cfg, store, idx)
    rows = _named_rows(load_comps_rows(store), idx)

    items: list[dict[str, Any]] = []
    for name, lst, info in kept:
        routes = quote_all_routes(lst, cfg, fx)
        if not routes:
            rejected[f"{lst.site.value} 沒有可用 route"] = (
                rejected.get(f"{lst.site.value} 沒有可用 route", 0) + 1
            )
            continue
        best = routes[0]
        match = idx.match(lst.title) if idx.available else None
        card_name = match.name_ja if (match and match.in_era) else None

        venue_view, venue_estimate = _estimate_view(
            valuator=val, rows=rows, card_name=card_name, info=info, match=match,
            landed_twd=best.landed_twd, venue=lst.site.value,
        )
        mixed_view, _ = _estimate_view(
            valuator=val, rows=rows, card_name=card_name, info=info, match=match,
            landed_twd=best.landed_twd, venue=None,
        )
        views = {VIEW_VENUE: venue_view, VIEW_MIXED: mixed_view}
        kind = _price_kind(lst)
        # 競標標的的「到手成本」與判決是拿**會漲的價格**算的，本身沒有決策價值；
        # 真正能照著做的數字是出價上限。所以它只掛在競標標的上，
        # 而且一律用有平台校正的那份估價（見 `_estimate_view`）。
        ceiling = (
            max_bid_jpy(venue_estimate, cfg, fx, site=lst.site)
            if is_live_auction(lst)
            else None
        )
        items.append({
            "key": lst.key,
            "source": name,
            "site": lst.site.value,
            "site_label": venue_label(lst.site.value),
            "title": lst.title,
            "url": lst.url,                    # 購買端（Buyee），點了就能下單
            "origin_url": lst.origin_url,      # 發現端原生頁（Yahoo）
            "image_url": lst.image_url,
            "price": lst.price,
            "currency": lst.currency.value,
            "price_kind": kind,
            "price_kind_label": _PRICE_KIND_LABEL.get(kind, kind),
            "current_bid": (lst.raw or {}).get("current_bid"),
            "end_time": lst.end_time.isoformat() if lst.end_time else None,
            "bids": lst.bids,
            "bid_ceiling": ceiling.to_dict() if ceiling else None,
            "bid_headroom_jpy": ceiling.headroom_jpy(lst.price) if ceiling else None,
            "landed_twd": best.landed_twd,
            "route": best.route,
            "route_label": best.label,
            "routes": [
                {"route": q.route, "label": q.label, "landed_twd": q.landed_twd}
                for q in routes
            ],
            "over_budget": best.landed_twd > budget_twd,
            "card": _card_dict(info, match, card_name),
            "views": views,
        })

    # --- 4. 排（兩種視角各排一份，前端切換不重抓）-----------------------
    order = {
        view: [it["key"] for it in sorted(items, key=lambda x, v=view: sort_key(x, v))]
        for view in VIEWS
    }

    return {
        "keyword": keyword,
        "budget_twd": budget_twd,
        "elapsed_seconds": round(time.perf_counter() - t_all, 2),
        "sources": reports,
        "funnel": {
            # 「共抓 N 筆」是商業篩選後真的拿到手的筆數；解析器解出幾個看 sources[].parsed_count
            "fetched": len(found),
            "parsed": sum(r["parsed_count"] for r in reports),
            "candidates": len(kept),
            "listed": len(items),
            "rejected": dict(sorted(rejected.items(), key=lambda kv: -kv[1])),
        },
        "comps_n": len(val.rows),
        "calibration_n": val.calibration_n,
        "views": list(VIEWS),
        "order": order,
        "items": items,
    }


__all__ = ["SEARCH_SOURCES", "VIEWS", "VIEW_MIXED", "VIEW_VENUE", "search_market", "sort_key"]
