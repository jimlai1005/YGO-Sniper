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

from .selling import (
    best_round_trip,
    listing_from_signal_row,
    location_label,
    round_trips_for,
    venue_estimator_for_row,
)

#: dashboard 讀這兩把 meta 鑰匙：橫條顯示病名／清單行顯示快取時間。
VALUATION_CACHE_AT_KEY = "valuation_cache_at"
VALUATION_CACHE_ERROR_KEY = "valuation_cache_error"


def resale_for_row(valuator, cfg, fx, row: dict, raw_payload: str | None) -> dict:
    """這一列「若要轉賣」的答案。**不可行時明確寫不可行，不給數字。**

    這裡不自己開估價模型、也不自己算費率——全部走 `selling`，與 CLI 的
    `ygo-sniper spread` 是同一支。dashboard 自己再算一份的話，畫面上的
    淨利與指令跑出來的淨利會安靜地分岔（工程原則 1）。
    """
    lst = listing_from_signal_row({**row, "payload": raw_payload})
    if lst is None:
        return {"ok": False, "reason": "payload 殘缺，無法還原標的，拒絕估轉賣淨利"}

    trips = round_trips_for(
        lst, cfg, fx, estimate_for=venue_estimator_for_row(valuator, {**row, "payload": raw_payload})
    )
    best = best_round_trip(trips)
    # 不可行的理由要去重（同一個原因會在每條買進路徑各出現一次），
    # 但**必須留著**：「Mercari JP 賣得比較貴」的正確下文是「但你到不了那裡，
    # 因為 X」，把它濾掉使用者只會反覆自己重新想一次。
    seen: set[str] = set()
    blocked = []
    for t in trips:
        if t.ok or t.reason in seen:
            continue
        seen.add(t.reason)
        blocked.append({"venue_label": t.sell_venue_label, "route_label": t.buy_route_label,
                        "reason": t.reason})
    out = {
        "ok": best is not None,
        "blocked": blocked[:6],
        "tax_note": cfg.resale.tax_note,
        "jp_presence": cfg.resale.jp_presence,
    }
    if best is not None:
        out["best"] = best.to_dict()
        out["best"]["holding_label"] = location_label(best.holding)
    else:
        out["reason"] = "沒有任何可行的轉賣組合（原因見下）"
    return out


def refresh_valuation_cache(cfg, store, fx, index=None, *, valuator=None) -> dict:
    """整顆 signals 表重算一輪估價，整批落庫。回傳 {rows, errors, seconds, comps_n}。

    `valuator` 可傳入已建好的（pipeline 掃描中已經建過一次就重用，
    不要第二份）；不傳就自建。P 值與 resale 出自**同一個 valuator**
    ——這是既有的同源不變式，搬到掃描時點也要守住。

    逐列的失敗**不中斷**整批：該列各值寫 NULL（讀取端誠實顯示無 P），
    計數回報並大聲印前三個病名。valuator 建不起來這種整批性的失敗
    直接往外拋，由呼叫端（pipeline 掛勾）落 meta。
    """
    from datetime import UTC, datetime

    from .valuation import build_valuator, estimate_signal_row

    t0 = time.perf_counter()
    comps_n = int(store.stats().get("comps") or 0)
    if valuator is None:
        valuator = build_valuator(cfg, store, index)
    rows = store.list_signals(state="all", limit=1_000_000)
    now = datetime.now(UTC).isoformat()
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
