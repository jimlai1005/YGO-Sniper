"""Dashboard 後端。

刻意做得很薄 —— 它跟 CLI 讀同一顆 SQLite，沒有自己的業務邏輯。
唯一的例外是 /api/bundle 的即時重算，因為湊單張數是使用者當下的操作，
不可能預先算好。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ygo_sniper.appraise import UnsupportedUrlError, appraise  # noqa: E402
from ygo_sniper.bidding import (  # noqa: E402
    CARD_SPECIFIC_LEVELS,
    EBAY_PROXY_BID_FINDING,
    EVIDENCE_TIERS,
    HONESTY_NOTES,
    PROXY_BID_FINDING,
    EvidenceGate,
    auction_view_config,
    target_margin_from,
)
from ygo_sniper.cards import CardIndex  # noqa: E402
from ygo_sniper.config import load_config  # noqa: E402
from ygo_sniper.costs import breakeven_table, quote_route  # noqa: E402
from ygo_sniper.domain import (  # noqa: E402
    CardBucket,
    Currency,
    Listing,
    RouteQuote,
    Site,
    TriageState,
)
from ygo_sniper.expiry import expiry_status, gone_confidence_from_config  # noqa: E402
from ygo_sniper.fx import FxRates  # noqa: E402
from ygo_sniper.market_search import VIEW_MIXED, VIEW_VENUE, search_market  # noqa: E402
from ygo_sniper.scoring import (  # noqa: E402
    is_triggered,
    overhead_threshold,
    shipping_alert_for_row,
)
from ygo_sniper.seller_links import seller_page_url  # noqa: E402
from ygo_sniper.selling import (  # noqa: E402
    best_round_trip,
    listing_from_signal_row,
    location_label,
    round_trips_for,
    venue_estimator_for_row,
)
from ygo_sniper.sources import CachedFetcher, build_sources  # noqa: E402
from ygo_sniper.sources.base import BlockedError, FetchError  # noqa: E402
from ygo_sniper.store import Store  # noqa: E402
from ygo_sniper.venue_study import VENUE_STUDY_META_KEY, venue_study_label  # noqa: E402
from ygo_sniper.verify_departed import build_page_verifier  # noqa: E402

app = FastAPI(title="ygo-sniper")
cfg = load_config()
fx = FxRates(cfg)
store = Store(cfg.db_path)
#: 各來源的離場判定信心度（`config/settings.yaml` 的 `scan.gone_confidence`）。
#: 設定缺漏時 `gone_confidence_from_config` 會退回一律 low——不會因為沒設定就假裝有信心。
_GONE_CONFIDENCE = gone_confidence_from_config(cfg)

STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------------------
#: 估價模型（給清單算 P(值得買) 用）跨請求共用。實測建一次 0.14 秒、193 列
#: 估價 0.08 秒——不快取的話每次切分頁都重付一次。
#: 失效判準是 **comps 的筆數**：comps 長了才有新的成交樣本，模型才會變。
#: 用「筆數」而不是時間戳，因為時間到了資料沒變等於白重建，資料變了時間沒到
#: 又會給出過期的機率（工程原則 1：判準要對著真正會變的那個東西）。
_valuator = None
_valuator_key: int | None = None
_valuator_lock = threading.Lock()


def _with_overhead(payload: dict) -> dict:
    """讓 payload 裡每一條 route 都帶著 `overhead_twd`／`overhead_ratio`。

    這兩個值是 `RouteQuote` 的 property，`asdict()` 不會序列化它們，所以
    2026-08-02 之前落庫的列沒有這兩個欄位。前端自己算 `(fee+ship)/landed`
    是最糟的補法——那會變成第二份定義，成本模型哪天多一個欄位（關稅、保險），
    畫面上的佔比會安靜地少算（工程原則 1）。這裡把 dict 還原成 RouteQuote
    再序列化一次，舊列與新列走的是同一個 property。
    """
    for key in ("best_route", "all_routes"):
        value = payload.get(key)
        if isinstance(value, dict):
            payload[key] = _route_dict(value)
        elif isinstance(value, list):
            payload[key] = [_route_dict(q) for q in value if isinstance(q, dict)]
    return payload


def _route_dict(route: dict) -> dict:
    try:
        return RouteQuote.from_dict(route).to_dict()
    except (TypeError, KeyError):
        return route  # 欄位殘缺的舊列照原樣送出，不要讓一列壞掉的 payload 打掉整個清單


def _shared_valuator():
    from ygo_sniper.valuation import build_valuator

    global _valuator, _valuator_key
    comps_n = int(store.stats().get("comps") or 0)
    with _valuator_lock:
        if _valuator is None or _valuator_key != comps_n:
            _valuator = build_valuator(cfg, store, _shared_card_index())
            _valuator_key = comps_n
        return _valuator


def _resale_for_row(valuator, row: dict, raw_payload: str | None) -> dict:
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


@app.get("/api/signals")
def signals(
    state: str = "new", min_score: float = 0, limit: int = 1000, bucket: str = ""
):
    """清單。`triggered`、`p_worth_buying`、`shipping_alert` 全是後端算的。

    「有觸發旗標」的定義只有一份（`scoring.TRIGGER_FLAGS`），評分閘門與清單標記
    共用它。前端自己維護一份旗標清單的話，哪天加了新的 trigger 旗標，
    dashboard 會安靜地把它顯示成「只是符合條件」（工程原則 1）。

    `p_worth_buying`＝P(公允價 > 到手成本)，**現算不落庫**：它比較的是
    「這一列上次掃描時的到手成本」與「現在這批 comps 撐出來的公允價」，
    comps 每小時都在長，落庫等於把一個會過期的機率凍起來。算法完全重用
    `valuation.estimate_signal_row`（CLI 的 `ygo-sniper value` 用的同一支），
    dashboard 不自己開第二套估價。

    `shipping_alert` 走 `scoring.shipping_alert_for_row`，與掃描當下寫
    `Flag.HIGH_OVERHEAD` 的是同一個判準——舊資料沒有那個旗標也看得到告警。
    """
    # 未知的 bucket 要**大聲拒絕**，不能安靜地回一份空清單：那兩件事在畫面上
    # 長得一模一樣（「這個分類沒有卡」vs「參數打錯了」），而只有後者需要修。
    if bucket and bucket not in {b.value for b in CardBucket}:
        raise HTTPException(
            400, f"未知分類 {bucket}，可用：{sorted(b.value for b in CardBucket)}"
        )
    rows = store.list_signals(
        state=state, limit=limit, min_score=min_score, bucket=bucket or None
    )
    # 原始的 payload JSON **字串**要留著：`estimate_signal_row` 是拿字串
    # `json.loads` 去取「掃描當下解析好的稀有度」的，餵它一個已經 parse 過的
    # dict 會靜靜掉進「payload 壞掉」的 fallback（改從標題重抽稀有度）。
    # 2026-08-02 實測目前這 193 筆兩條路徑的稀有度剛好一致（P 值零差異），
    # 所以這不是在修一個看得見的 bug——是在修**同源**：稀有度的抽法一改，
    # dashboard 的 P 值就會跟 CLI 的 `ygo-sniper value` 無聲分岔（工程原則 1）。
    raw_payloads = [r.get("payload") for r in rows]
    for r in rows:
        r["flags"] = json.loads(r.get("flags") or "[]")
        r["payload"] = _with_overhead(json.loads(r.get("payload") or "{}"))
        # 在架狀態：判定只有 expiry.py 一份，前端不自己算
        # （前端算的話，CLI 與通知那兩條路徑就會拿到不同答案）。
        r["expiry"] = expiry_status(r, gone_confidence=_GONE_CONFIDENCE).to_dict()
        r["triggered"] = is_triggered(r["flags"])
        r["shipping_alert"] = shipping_alert_for_row(r, cfg)
        r["p_worth_buying"] = None
        r["fair_twd"] = None
        r["resale"] = None

    # 估價炸掉不該讓整個清單開不出來，但也**不准安靜地當作沒事**（工程原則 3）：
    # 病名回到前端，畫面上會出現「P 值這一輪算不出來」的告警，
    # 而不是所有標的的 P 都顯示成「–」讓人以為模型說了什麼。
    valuation_error = None
    try:
        from ygo_sniper.valuation import estimate_signal_row

        valuator = _shared_valuator()
        for r, raw in zip(rows, raw_payloads, strict=True):
            est = estimate_signal_row(valuator, {**r, "payload": raw})
            r["p_worth_buying"] = est.p_worth_buying
            r["fair_twd"] = est.fair_twd
            r["est_level_label"] = est.level_label
            # 「若要轉賣」與 P 值共用同一個 valuator（同一批 comps、同一份
            # 卡片屬性）。分開建的話畫面上的兩個數字會來自兩個模型。
            r["resale"] = _resale_for_row(valuator, r, raw)
    except Exception as exc:  # noqa: BLE001 - 清單本身比 P 值重要，但要說出病名
        valuation_error = f"{type(exc).__name__}: {exc}"

    return {
        "count": len(rows),
        "triggered": sum(1 for r in rows if r["triggered"]),
        "limit": limit,
        # 前端要能分辨「就是這麼多」與「被 LIMIT 切掉了」
        "truncated": len(rows) >= limit,
        # 篩選門檻的預設值是**判斷的一部分**，所以放後端（與 /api/bidding 同一個
        # 理由）：改了 settings.yaml 就該立刻反映在畫面上，不必再改一次前端。
        "filters": {
            "p_worth_hide_default": float(
                cfg.scoring.get("p_worth_hide_default", 0.30)
            ),
            "overhead_threshold": overhead_threshold(cfg),
        },
        "valuation_error": valuation_error,
        "p_worth_known": sum(1 for r in rows if r.get("p_worth_buying") is not None),
        # 競標視圖的梯隊門檻與文案（bidding.auction_view_config）。**跟著清單一起送**
        # 的理由：前端每次 render 都要用它排序，而 render 只在清單載入後才發生——
        # 掛在另一支 API 上就會有「還沒到就先畫了一次」的空窗，那一次會退回純時間序。
        # 前端拿不到這份設定時**不猜門檻**（退回舊的結標時間排序），與「沒有區間
        # 就不給上限」同一個立場：沒有依據就不要假裝有。
        "auction_view": auction_view_config(cfg),
        "items": rows,
    }


class StateUpdate(BaseModel):
    state: str
    note: str | None = None


@app.post("/api/signals/{key:path}/state")
def set_state(key: str, body: StateUpdate):
    valid = {s.value for s in TriageState}
    if body.state not in valid:
        raise HTTPException(400, f"未知狀態 {body.state}，可用：{sorted(valid)}")
    if not store.get_signal(key):
        raise HTTPException(404, "找不到這筆標的")
    store.update_state(key, body.state, body.note)
    return {"ok": True, "key": key, "state": body.state}


class BucketUpdate(BaseModel):
    #: `None` = 清除分類。刻意用 nullable 而不是空字串：清除是一個明確的意圖，
    #: 不該跟「欄位忘了填」共用同一個值。
    bucket: str | None = None


@app.post("/api/signals/{key:path}/bucket")
def set_bucket(key: str, body: BucketUpdate):
    """指派／清除卡片分類（與 state 正交，見 `domain.CardBucket`）。

    回傳的 bucket 與 state 是**寫完再讀回來的**，不是我們以為寫進去的值——
    state 有可能被 `update_bucket` 的 new→watching 連動改掉，前端要照實顯示。
    """
    valid = {b.value for b in CardBucket}
    if body.bucket is not None and body.bucket not in valid:
        raise HTTPException(
            400, f"未知分類 {body.bucket}，可用：{sorted(valid)}（null = 清除）"
        )
    if not store.get_signal(key):
        raise HTTPException(404, "找不到這筆標的")
    store.update_bucket(key, body.bucket)
    row = store.get_signal(key) or {}
    return {"ok": True, "key": key, "bucket": row.get("bucket"),
            "state": row.get("state")}


class ClearExpiredRequest(BaseModel):
    #: 要清哪個分頁。只接受 Store.CLEARABLE_STATES 裡的三個。
    state: str


@app.post("/api/signals/clear-expired")
def clear_expired(body: ClearExpiredRequest, tasks: BackgroundTasks):
    """把某個分頁裡已離場的標的移到 expired——gone 候選逐筆開商品頁驗證，
    只清拿到實證的（SOLD／DELISTED），所以走背景模式（驗 40 筆約 1-2 分鐘，
    同步回應會讓前端掛在 spinner 上直到 timeout）。

    骨架照 `/api/scan` 的 begin/finish/status 三件組：已 running 就不再排一次
    （回 200 + `started:false`——「已經在跑了」對使用者是正常回答，不是錯誤）；
    否則**在回應之前**先標 running（晚標的話前端問到「沒有在跑」的空窗，
    正是使用者會再按一次的時機）；內層 try/except 保證失敗也 finish，
    不讓狀態卡死到逾時為止。

    冪等：清完就不在原 state，重按第二次的 last_result 回 `cleared: 0`
    （工程原則二——非冪等寫入不可重試，所以這支刻意設計成冪等）。
    """
    try:
        # 這一步兼做 state 驗證（語意錯誤要在背景任務開跑之前回 400，
        # 不是安靜地回 cleared: 0 假裝成功，CLAUDE.md 第五節）
        # 與 total 預估（前端一開始就能畫 0/total）。
        total = len(
            store.departed_candidates(
                body.state, gone_confidence=_GONE_CONFIDENCE
            )["gone"]
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    st = store.verify_clear_status(timeout_seconds=cfg.scan_timeout_seconds)
    if st["running"]:
        return {
            "ok": True,
            "started": False,
            "running": True,
            "message": f"已經有一輪驗證清除在跑（{st.get('trigger') or '?'} 觸發），這次不重複啟動",
            "status": st,
        }

    started = store.begin_verify_clear(
        trigger="dashboard", state=body.state, total=total
    )

    def _run() -> None:
        try:
            verifier = build_page_verifier(cfg)
            done = 0
            try:
                def _counting(key: str, url: str):
                    nonlocal done
                    res = verifier(key, url)
                    done += 1
                    store.set_verify_clear_progress(done, total)
                    return res

                result = store.clear_expired_signals(
                    body.state,
                    gone_confidence=_GONE_CONFIDENCE,
                    verifier=_counting,
                )
            finally:
                verifier.close()
            store.finish_verify_clear(started, result=result)
        except Exception as exc:  # noqa: BLE001 - 見下
            # 建 verifier 就失敗（缺 playwright、db 壞掉…）或驗到一半炸掉時，
            # 這裡不接的話狀態會卡 running 到逾時為止，按鈕鎖住而且看不到原因
            # （工程原則 3：大聲失敗）。error 落地之後例外照樣往外傳。
            store.finish_verify_clear(started, error=f"{type(exc).__name__}: {exc}")
            raise

    tasks.add_task(_run)
    return {
        "ok": True,
        "started": True,
        "running": True,
        "total": total,
        "message": f"開始驗證 {total} 筆的商品頁，完成後清單會自動更新",
    }


@app.get("/api/signals/clear-expired/status")
def clear_expired_status():
    """「現在有沒有在驗」＋進度＋上一次的結果。前端輪詢這個。

    卡死防線在 store.verify_clear_status：開始超過逾時還沒回報完成，
    一律回 `running:false, stale:true`——驗證中途被 kill 的狀態不會讓按鈕永遠鎖住。
    """
    return store.verify_clear_status(timeout_seconds=cfg.scan_timeout_seconds)


# ---------------------------------------------------------------------------
@app.get("/api/bundle")
def bundle():
    """湊單籃：籃子裡有幾張，就用幾張重算攤提後的每張成本。

    這是這個工具真正比通知有用的地方 —— 你可以直接看到
    「再加一張，前面每一張都會便宜多少」。
    """
    rows = store.bundle()
    n = max(1, len(rows))
    items = []
    total_landed = 0.0
    total_single = 0.0

    for r in rows:
        payload = json.loads(r.get("payload") or "{}")
        lst_d = payload.get("listing", {})
        try:
            listing = Listing(
                site=Site(lst_d["site"]),
                external_id=lst_d["external_id"],
                title=lst_d["title"],
                url=lst_d["url"],
                price=float(lst_d["price"]),
                currency=Currency(lst_d["currency"]),
                image_url=lst_d.get("image_url"),
                shipping_cost=lst_d.get("shipping_cost"),
            )
        except (KeyError, ValueError):
            continue

        route_cfg = cfg.routes.get(r["route"])
        if not route_cfg:
            continue

        q_now = quote_route(listing, route_cfg, fx, bundle_size=n)
        q_alone = quote_route(listing, route_cfg, fx, bundle_size=1)

        total_landed += q_now.landed_twd
        total_single += q_alone.landed_twd
        items.append(
            {
                "key": r["key"],
                "title": r["title"],
                "url": r["url"],
                "image_url": r["image_url"],
                "route": r["route"],
                "landed_now": q_now.landed_twd,
                "landed_alone": q_alone.landed_twd,
                "item_twd": q_now.item_twd,
                "under_grading_fee": q_now.landed_twd < cfg.grading_fee_twd,
            }
        )

    return {
        "bundle_size": len(rows),
        "items": items,
        "total_landed": round(total_landed, 0),
        "total_if_separate": round(total_single, 0),
        "saving": round(total_single - total_landed, 0),
        "grading_fee_twd": cfg.grading_fee_twd,
    }


@app.get("/api/breakeven")
def breakeven(target: float | None = None):
    """破口表 ＋ **成本拆解要用到的匯率參數**。

    `card_markup`／`safety_buffer` 跟著出門的理由：使用者攤開一筆標的的成本
    拆解時，會看到「¥1,500 的卡怎麼變成 NT$328」——差額就是這兩個乘數。
    不把它們送出去，前端就得自己寫死 1.5%／2%，改了 settings.yaml 之後
    畫面上還是舊數字（工程原則 1）。
    """
    return {
        "target_twd": target or cfg.grading_fee_twd,
        "fx_source": fx.source,
        "rates": fx.rates,
        "card_markup": fx.card_markup,
        "safety_buffer": fx.safety_buffer,
        "grading_fee_twd": cfg.grading_fee_twd,
        "rows": breakeven_table(cfg, fx, target),
    }


@app.get("/api/stats")
def stats():
    return store.stats()


@app.get("/api/bidding")
def bidding_meta():
    """競標視圖的**說明文字與參數**，全部來自後端常數。

    為什麼不寫死在前端：這些句子是判斷的一部分（上限為什麼用區間下緣、
    為什麼會經常出價失敗、代理出價查到了什麼），前端自己維護一份的話，
    改了 `target_margin` 或重新查證 Buyee 之後，畫面上還會是舊的說法
    （工程原則 1：一個事實只有一份定義）。
    """
    gate = EvidenceGate.from_config(cfg)
    return {
        "target_margin": target_margin_from(cfg),
        "basis": "estimate.lo_twd",
        "basis_label": "conformal 80% 區間下緣（保守公允價，群組條件校準）",
        # 閘門設定也是「判斷的一部分」：使用者看到 31 筆競標只有 8 筆有上限時，
        # 必須查得到那 23 筆是被哪一條擋掉的，而不是以為工具壞了。
        "evidence_gate": {
            "require_known_grade": gate.require_known_grade,
            "require_card_specific_level": gate.require_card_specific_level,
            "min_calibration_samples": gate.min_calibration_samples,
            "min_effective_samples": gate.min_effective_samples,
            "card_specific_levels": list(CARD_SPECIFIC_LEVELS),
        },
        "evidence_tiers": dict(EVIDENCE_TIERS),
        "honesty_notes": list(HONESTY_NOTES),
        "proxy_bid": {
            **PROXY_BID_FINDING,
            "sources": list(PROXY_BID_FINDING["sources"]),
            "details": list(PROXY_BID_FINDING["details"]),
        },
        # eBay 原生 automatic bidding 的查證（與 Buyee 那份同構，2026-08-03）。
        # 兩個平台的代理出價是兩個不同的按鈕，前端引用哪一份要看標的的 site。
        "proxy_bid_ebay": {
            **EBAY_PROXY_BID_FINDING,
            "sources": list(EBAY_PROXY_BID_FINDING["sources"]),
            "details": list(EBAY_PROXY_BID_FINDING["details"]),
        },
    }


@app.get("/api/venue-study")
def venue_study():
    """平台研究的**唯讀**視圖：PayPay 是不是比較便宜買得到。

    這裡不重算、也不打外網——報告由 `ygo-sniper venue-study` 產生後存進 meta 表。
    理由是那份研究會發出上百個對外請求（間隔 2 秒、要開瀏覽器解 WAF），
    做成一個「按下去就跑」的按鈕，等於把一個數分鐘的爬蟲掛在 HTTP 請求上，
    而且每個重整的人都會再打一輪。判定邏輯全在 `venue_study.py`，
    CLI 與這裡讀的是同一份 JSON，不可能給出兩種結論（工程原則 1）。

    `listing_obs` 的現況則是即時查的：那是本地 SQL，而且它每小時都在長。
    """
    raw = store.get_meta(VENUE_STUDY_META_KEY)
    report = None
    if raw:
        try:
            report = json.loads(raw)
        except ValueError:
            report = None
    return {
        "report": report,
        "hint": "還沒跑過研究。執行 `ygo-sniper venue-study` 產生報告。" if not report else "",
        "listing_obs": store.listing_obs_summary(),
        # 平台名稱（含「即決／定價／競標中」的語意）只有一份定義，前端不自己維護
        "labels": {
            v: venue_study_label(v)
            for v in ("buyee_yahoo", "buyee_mercari", "buyee_paypay", "buyee_yahoo_bid")
        },
    }


# ---------------------------------------------------------------------------
# 鑑價：貼一個商品網址，回一份判決報告
# ---------------------------------------------------------------------------
#: Buyee 商品頁要開 Playwright 解 WAF 挑戰，實測約 22 秒（含 20s 的骨架等待逾時）。
#: 這個上限只是防呆（避免一個卡住的瀏覽器把連線佔住），不是預期耗時。
APPRAISE_TIMEOUT_SECONDS = 120

#: 卡片主檔 758KB，每次請求重讀＋重建索引要 0.2 秒以上。載一次就好。
#: 換主檔（refresh-cards）之後要重啟 web 才會生效——這是刻意的取捨：
#: 主檔是「1998-2004 發行過哪些卡」這種不會變的歷史事實，不需要熱重載。
_card_index: CardIndex | None = None
_card_index_lock = threading.Lock()


def _shared_card_index() -> CardIndex:
    global _card_index
    with _card_index_lock:
        if _card_index is None:
            _card_index = CardIndex.load()
        return _card_index


class AppraiseRequest(BaseModel):
    url: str


def _error(status: int, kind: str, message: str, **extra) -> JSONResponse:
    """結構化錯誤。

    前端要能分辨「你貼錯網址」（改一下就好）與「被擋／逾時」（等一下再試），
    所以 `kind` 與 `retryable` 是回應的一部分，不是塞在自由文字裡。
    """
    return JSONResponse(
        status_code=status,
        content={"ok": False, "error": {"kind": kind, "message": message, **extra}},
    )


@app.post("/api/appraise")
async def api_appraise(body: AppraiseRequest):
    """網址 → 判決報告。

    抓取是同步且可能很慢（Playwright），所以丟到 thread 跑並加逾時：
    卡住的請求要在**這裡**變成 504，而不是讓瀏覽器自己等到天荒地老。
    失敗一律沿用 FetchError／BlockedError 的分類語意回結構化錯誤，不吐 500 堆疊。
    """

    def _run():
        return appraise(
            cfg,
            body.url,
            store=store,
            fx=fx,
            index=_shared_card_index(),
        )

    try:
        report = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=APPRAISE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return _error(
            504,
            "timeout",
            f"抓取超過 {APPRAISE_TIMEOUT_SECONDS} 秒還沒完成。"
            "Buyee 商品頁要開瀏覽器解 WAF 挑戰，偶爾會卡住——稍後再試一次。",
            retryable=True,
        )
    except UnsupportedUrlError as exc:
        return _error(400, "unsupported_url", str(exc), retryable=False)
    except BlockedError as exc:
        return _error(
            502, "blocked", str(exc), url=exc.url, status=exc.status, retryable=False
        )
    except FetchError as exc:
        return _error(
            502 if not exc.transient else 503,
            "fetch_failed",
            str(exc),
            url=exc.url,
            status=exc.status,
            retryable=exc.transient,
        )
    except ValueError as exc:
        return _error(400, "bad_request", str(exc), retryable=False)
    except Exception as exc:  # noqa: BLE001 - 絕不讓鑑價把整個 dashboard 打成堆疊頁
        return _error(
            500, "internal", f"{type(exc).__name__}: {exc}", retryable=False
        )

    return {"ok": True, "report": report.to_dict()}


# ---------------------------------------------------------------------------
# 關鍵字搜尋：輸入卡名，回「最適合入手的賣場清單」
# ---------------------------------------------------------------------------
#: 三個來源實測合計約 6 秒（Yahoo 1s / Mercari 4.2s / PayPay 1s），但 Buyee 系
#: 冷啟動要開 Playwright 解 WAF 挑戰，第一次會多約 20 秒。90 秒是防呆上限
#: （避免卡住的瀏覽器把連線佔著），不是預期耗時。
SEARCH_TIMEOUT_SECONDS = 90

#: 抓取用的 source registry **跨請求共用**：Buyee 系兩條管道共用同一顆
#: aws-waf-token（TTL 約 5 分鐘、由 WafSession 自己續），每次請求重建
#: 等於每次都付一遍開瀏覽器的 20 秒。搭配 _search_lock 序列化，
#: 同一時間只有一個搜尋在用它——兩個並行的搜尋會互相搶 token 重取預算。
_search_registry: dict | None = None
_search_fetcher: CachedFetcher | None = None
_search_lock = threading.Lock()


def _shared_registry() -> dict:
    global _search_registry, _search_fetcher
    if _search_registry is None:
        _search_fetcher = CachedFetcher(cfg)
        _search_registry = build_sources(cfg, _search_fetcher)
    return _search_registry


class SearchRequest(BaseModel):
    keyword: str
    budget_twd: float = 1200
    #: 預設開：一個 Mercari 標的要跟 Mercari 的價格水準比。關掉是為了讓使用者
    #: 用「開／關的排序差異」判斷 PayPay 排前面是真的有 edge 還是係數在作用。
    venue_adjust: bool = True


@app.post("/api/search")
async def api_search(body: SearchRequest):
    """關鍵字 → 三個平台的在架清單（含到手成本、公允價、判決）。

    判決與可比樣本**完全重用 appraise 那一套**（`market_search` 只呼叫
    `decide_verdict`／`collect_comparables`，自己不定門檻）——同一個標的
    在 /api/appraise 與 /api/search 必須得到同一個判決。

    `venue_adjust` 只決定「預設先看哪一份」：兩份估價（有／無平台校正）
    都算好一起回傳，前端切換不必重抓（見 market_search 模組 docstring）。
    """
    keyword = body.keyword.strip()
    if not keyword:
        return _error(400, "bad_request", "關鍵字是空的。", retryable=False)

    def _run():
        # 序列化：兩個並行搜尋會搶同一顆 WAF token 的重取預算，
        # 結果是兩邊都被擋——寧可排隊也不要一起失敗。
        with _search_lock:
            return search_market(
                cfg,
                keyword,
                registry=_shared_registry(),
                store=store,
                fx=fx,
                index=_shared_card_index(),
                budget_twd=body.budget_twd,
            )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run), timeout=SEARCH_TIMEOUT_SECONDS
        )
    except TimeoutError:
        return _error(
            504,
            "timeout",
            f"搜尋超過 {SEARCH_TIMEOUT_SECONDS} 秒還沒完成。"
            "Buyee 系來源要開瀏覽器解 WAF 挑戰，冷啟動偶爾會卡住——稍後再試一次。",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 - 絕不讓搜尋把整個 dashboard 打成堆疊頁
        return _error(500, "internal", f"{type(exc).__name__}: {exc}", retryable=False)

    # 來源層的失敗不是整個請求的失敗（隔離是本專案的核心約束）：
    # Mercari 被擋時 Yahoo 的結果照樣要出得來，病名放在 sources[].health 裡。
    result["view"] = VIEW_VENUE if body.venue_adjust else VIEW_MIXED
    result["venue_adjust"] = body.venue_adjust
    return {"ok": True, "result": result}


# ---------------------------------------------------------------------------
# 賣家（Seller Alpha）：排行榜、監控名單、drill-down、一鍵加入／移出
# ---------------------------------------------------------------------------
#: 全量分析要掃 listing_obs ＋ comps（實測本庫約 2.6k 列、0.3 秒）並建卡名索引。
#: 切分頁／點 drill-down 每次重算會讓畫面卡住，所以跟 `_shared_valuator` 同一套
#: 快取策略：失效判準是**資料真的變了**（comps 筆數 ＋ listing_obs 筆數），
#: 不是時間到了（時間到而資料沒變等於白重算，資料變了時間沒到又會給過期的排行）。
_alpha_report_cache = None
_alpha_report_key: tuple[int, int] | None = None
_alpha_lock = threading.Lock()


def _shared_alpha_report(force: bool = False):
    from ygo_sniper.seller_alpha import analyze

    global _alpha_report_cache, _alpha_report_key
    key = (
        int(store.stats().get("comps") or 0),
        int(store.listing_obs_summary().get("total") or 0),
    )
    with _alpha_lock:
        if force or _alpha_report_cache is None or _alpha_report_key != key:
            # **不帶 valuator**：模型絕對值在賣家頁只是輔助欄位，而它是第二棒
            # 證明會製造假 alpha 的東西。少建一顆模型也少一個失敗點。
            _alpha_report_cache = analyze(store, cfg=cfg, index=_shared_card_index())
            _alpha_report_key = key
        return _alpha_report_cache


def _watch_params():
    from ygo_sniper.seller_watch import WatchParams

    return WatchParams.from_config(cfg)


#: 供給契合度回答「值不值得盯」，跟便不便宜無關，**不可與 Alpha 相加**——
#: 這句話一字不差抄自 CLI 的 `_print_supply_fit`（跟這句話配對的表頭警語同理），
#: dashboard 與 CLI 對同一份資料只能講一種話（工程原則 1）。
SUPPLY_FIT_NOTE = "供給契合度回答「值不值得盯」，不是「便不便宜」——這兩欄是兩把不同的尺，不可相加。"


def _fmt_alpha_for_supply(score) -> dict:
    """Supply Fit 排行榜裡附帶的 Alpha 欄。

    `ok=False` 或 `total is None` 一律送 `total: null`——**絕不送 0**。
    0 的語意是「算出來就是比同儕貴」，null 是「同儕湊不齊，不知道」，
    前端把兩者顯示成同一個東西就會讓人把「不知道」讀成「比同儕貴」
    （見 `cli._fmt_alpha_total` 同一份規則，這裡是 API 版本）。
    """
    if score is None or not score.ok or score.total is None:
        return {"ok": False, "total": None}
    return {"ok": True, "total": score.total}


def _supply_fit_dict(fit, alpha_score) -> dict:
    dims = {d.name: (d.raw if d.available else None) for d in fit.dimensions}
    return {
        "seller_key": fit.seller_key,
        # None ＝ 未知 site（見 seller_links.seller_page_url）：前端顯示純文字，
        # 不猜 URL——猜錯的連結點下去是 404，比沒有連結更糟。
        "url": seller_page_url(fit.seller_key),
        "site": fit.site,
        "ok": fit.ok,
        "reason": fit.reason,
        "total": fit.total,
        "n_dimensions_used": fit.n_dimensions_used,
        "n_dimensions_total": fit.n_dimensions_total,
        "dims": dims,
        "missing": list(fit.missing),
        "caveats": list(fit.caveats),
        "alpha": _fmt_alpha_for_supply(alpha_score),
    }


def _supply_fit_block(rep, *, limit: int) -> dict:
    """`/api/sellers` 的 `supply_fit` 區塊。走 `seller_supply.supply_fit_all`——
    跟 CLI 的 `ygo-sniper sellers --supply` 是同一個函式、同一份門檻，
    dashboard 不自己定第二套規則。
    """
    from collections import Counter

    from ygo_sniper.seller_supply import SupplyParams, supply_fit_all

    fits = supply_fit_all(list(rep.metrics.values()), params=SupplyParams())
    ok_fits = [f for f in fits.values() if f.ok]
    ranked_fits = sorted(ok_fits, key=lambda f: (-(f.total or 0.0), f.seller_key))[:limit]
    rejected_fits = [f for f in fits.values() if not f.ok]

    rejected_summary = None
    if rejected_fits:
        reason_counts = Counter(f.reason for f in rejected_fits)
        top_reason, top_count = reason_counts.most_common(1)[0]
        rejected_summary = {
            "count": len(rejected_fits),
            "top_reason": top_reason,
            "n": top_count,
        }

    return {
        "summary": {
            "scored": len(ok_fits),
            "total": len(fits),
            # 對照組：同一份 rep 算出來的 Alpha 達門檻數，跟 coverage 印的是同一個數字
            # （工程原則 1：同一份資料只能有一個「達門檻數」）。
            "alpha_scored": rep.coverage.get("sellers_scored"),
            "threshold_note": SUPPLY_FIT_NOTE,
        },
        "ranked": [
            _supply_fit_dict(f, rep.scores.get(f.seller_key)) for f in ranked_fits
        ],
        "rejected_summary": rejected_summary,
    }


def _score_dict(score) -> dict:
    return {
        "seller_key": score.seller_key,
        # None ＝ 未知 site（見 seller_links.seller_page_url）：前端顯示純文字，
        # 不猜 URL——猜錯的連結點下去是 404，比沒有連結更糟。
        "url": seller_page_url(score.seller_key),
        "ok": score.ok,
        # ⚠️ `ok=False` 時 total 一律 None（不是 0）——0 會被讀成「這個賣家很差」，
        # None 才是「證據不足，我不知道」。前端照這個差別顯示。
        "total": score.total,
        "reason": score.reason,
        "missing": list(score.missing),
        "caveats": list(score.caveats),
        "n_comparable": score.n_comparable,
        "n_distinct_cards": score.n_distinct_cards,
        "tier_label": score.tier_label,
        "components": [
            {"name": c.name, "label": c.label, "points": c.points,
             "max_points": c.max_points, "detail": c.detail}
            for c in score.components
        ],
    }


def _metrics_brief(m) -> dict:
    return {
        "site": m.site,
        "n_rows": m.n_rows,
        "n_ask": m.n_ask,
        "n_sold": m.n_sold,
        "n_bid_excluded": m.n_bid_excluded,
        # 成交型態查不出來的列（不進任何比較）。少掉的可比數要有名字，
        # 不然「可比只有 2 筆」看起來會像壞掉而不是像證據不足。
        "n_sale_kind_unknown": m.n_sale_kind_unknown,
        "sale_kind_counts": dict(m.sale_kind_counts),
        "n_comparable": m.n_comparable,
        "n_distinct_cards": m.n_distinct_cards,
        "discount_ratio_median": m.discount_ratio_median,
        "discount_ratio_p25": m.discount_ratio_p25,
        "discount_ratio_p75": m.discount_ratio_p75,
        "peer_seller_pool": m.peer_seller_pool,
        "peer_seller_top_share": m.peer_seller_top_share,
        "observation_span_days": m.observation_span_days,
        "persistence_note": m.persistence_note,
        "sold_through_note": m.sold_through_note,
        "feedback_score": m.feedback_score,
        "feedback_pct": m.feedback_pct,
        "risk_known": m.risk_known,
        "risk_notes": list(m.risk_notes),
        "tier_counts": dict(m.tier_counts),
        "model_ratio_median": m.model_ratio_median,
    }


@app.get("/api/sellers")
def sellers(limit: int = 50):
    """賣家排行榜 ＋ 覆蓋率 ＋ 監控名單狀態。

    排行榜、caveat、「不給分數」的理由全部照抄 `seller_alpha` 的輸出，
    dashboard 不自己定門檻、不自己算折價——CLI 的 `ygo-sniper sellers --rank`
    與這一頁必須對同一份資料講同一句話（工程原則 1）。
    """
    rep = _shared_alpha_report()
    params = _watch_params()
    from ygo_sniper.seller_watch import (
        SELLER_PAGE_SOURCE,
        SOURCE_PINNED,
        UNSUPPORTED_SITE_NOTE,
        rotation_state,
    )

    watch_rows = store.list_seller_watch(active_only=False)
    for r in watch_rows:
        r["url"] = seller_page_url(r["seller_key"])
        # 站台還掃不到的要在畫面上講清楚（灰字註記）——「釘了但永遠 0 筆」
        # 與「沒在掃」外顯一樣，差別必須看得見（CLAUDE.md 第五節）。
        site = str(r["seller_key"]).partition(":")[0]
        r["unsupported_note"] = (
            None if site in SELLER_PAGE_SOURCE
            else UNSUPPORTED_SITE_NOTE.get(site, f"{site} 沒有賣家頁列舉實作")
        )
    watch_active = {r["seller_key"]: r for r in watch_rows if r["active"]}
    # 名額計數不含 pinned（釘選不佔 30 名額）；畫面上的 N/30 必須跟
    # add_watch 的上限判準同源，混進 pinned 就是兩把尺。
    n_pinned = sum(1 for r in watch_active.values() if r["source"] == SOURCE_PINNED)
    ranked = [
        {**_score_dict(s), "metrics": _metrics_brief(m),
         "watch": watch_active.get(s.seller_key)}
        for s, m in rep.ranked()[:limit]
    ]
    rejected = [
        {**_score_dict(s), "metrics": _metrics_brief(m),
         "watch": watch_active.get(s.seller_key)}
        for s, m in rep.rejected()[:limit]
    ]
    return {
        "coverage": rep.coverage,
        "ranked": ranked,
        "rejected": rejected,
        "watch": watch_rows,
        "watch_active": len(watch_active) - n_pinned,
        "watch_pinned": n_pinned,
        "rotation": rotation_state(store),
        "params": {
            "enabled": params.enabled,
            "max_sellers": params.max_sellers,
            "batches": params.batches,
            "batch_interval_minutes": params.batch_interval_minutes,
            "per_seller_interval_minutes": params.per_seller_interval_minutes,
            "auto_min_score": params.auto_min_score,
        },
        # 另一把尺——「值不值得盯」，跟上面的 Alpha 排行榜永遠不相加（見 CLAUDE.md 第四節）。
        "supply_fit": _supply_fit_block(rep, limit=limit),
    }


@app.get("/api/sellers/{seller_key:path}")
def seller_detail(seller_key: str, items: int = 40, peers: int = 3):
    """單一賣家 drill-down：逐筆標的、同儕折價與同儕來源、分數逐項貢獻、caveat。"""
    rep = _shared_alpha_report()
    m = rep.metrics.get(seller_key)
    if m is None:
        raise HTTPException(404, f"沒有這個賣家的觀測：{seller_key}")
    score = rep.scores[seller_key]
    shown = sorted(
        m.items, key=lambda i: (not i.scoring, i.peer is None, i.ratio or 0)
    )[:items]
    from ygo_sniper.seller_alpha import basis_kind_label

    def _item(i):
        return {
            "key": i.row.key,
            "title": i.row.title,
            "url": i.row.url,
            "basis": i.row.basis,
            # ⚠️ 標籤帶著成交型態（「成交價（競標結標）」vs「成交價（定價成交）」）：
            # 同儕列表如果混了型態，使用者要**在畫面上看得出來**，不是靠相信我們。
            "sale_kind": i.row.sale_kind,
            "basis_label": basis_kind_label(i.row.basis, i.row.sale_kind),
            "price_twd": i.row.price_twd,
            "source_table": i.row.source_table,
            "scoring": i.scoring,
            "discount_pct": i.discount_pct,
            # ⚠️ 模型絕對法只是輔助欄位（承受模型分段偏誤），永遠不進分數。
            "model_ratio": i.model_ratio,
            "peer": None if i.peer is None else {
                "tier": i.peer.tier,
                "tier_label": i.peer.tier_label,
                "median_twd": i.peer.peer_median_twd,
                "n": i.peer.peer_n,
                "sellers": i.peer.peer_sellers,
                "unknown_seller_n": i.peer.peer_unknown_seller_n,
                "scoring": i.peer.scoring,
                "sources": [
                    {"price_twd": p.price_twd, "title": p.title,
                     "seller_key": p.seller_key, "table": p.source_table,
                     "basis_label": basis_kind_label(p.basis, p.sale_kind)}
                    for p in i.peer.sources[:peers]
                ],
            },
        }

    return {
        "seller_key": seller_key,
        # None ＝ 未知 site（見 seller_links.seller_page_url）：前端顯示純文字，
        # 不猜 URL——猜錯的連結點下去是 404，比沒有連結更糟。
        "url": seller_page_url(seller_key),
        "score": _score_dict(score),
        "metrics": _metrics_brief(m),
        "watch": store.get_seller_watch(seller_key),
        "items": [_item(i) for i in shown],
        "items_total": len(m.items),
    }


class WatchUpdate(BaseModel):
    action: str            # add | remove
    reason: str | None = None


@app.post("/api/sellers/{seller_key:path}/watch")
def set_seller_watch(seller_key: str, body: WatchUpdate):
    """一鍵加入／移出監控名單。

    **名單上限與淘汰規則不在這裡**：整條政策只有 `seller_watch.add_watch`
    一份，CLI 與 dashboard 共用（前端自己判斷「滿了沒」就是第二份規則）。
    加入一律記成 `manual`：從畫面上按下去的就是使用者的判斷，不是自動入選——
    所以它也不帶分數（不假裝手動加入的賣家有評分）。
    """
    from ygo_sniper.seller_watch import SOURCE_MANUAL, add_watch, remove_watch

    if body.action == "remove":
        ok = remove_watch(
            store, seller_key, reason=body.reason or "從 dashboard 移出名單"
        )
        return {"ok": True, "removed": ok, "seller_key": seller_key,
                "message": "已移出監控名單" if ok else "本來就不在名單上"}
    if body.action != "add":
        raise HTTPException(400, f"未知動作 {body.action}，可用：add / remove")
    res = add_watch(
        store, seller_key, source=SOURCE_MANUAL,
        reason=body.reason or "從 dashboard 手動加入", params=_watch_params(),
    )
    return {
        "ok": res.ok, "already": res.already, "seller_key": res.seller_key,
        "message": res.reason, "batch": res.batch, "evicted": res.evicted,
    }


class PinRequest(BaseModel):
    url: str
    reason: str | None = None


@app.post("/api/sellers/pin")
def pin_seller(body: PinRequest):
    """貼賣家頁 URL → 釘選（不佔名額、永不淘汰、每個輪替時段都掃）。

    URL 解析失敗回 **400＋SellerUrlError 的訊息原文**（訊息本身列出支援的
    URL 形式，前端照原文顯示即可）；也接受現成的 `site:id` 鍵（與 CLI 的
    `watch-seller pin` 同一條判準，兩邊共用 `seller_resolve.resolve_seller_target`）。
    eBay `/str/` 店鋪頁會**連網**解析出真實帳號（slug ≠ username，不猜），
    解析出處寫進 reason。政策只有 `seller_watch.add_watch` 一份，這裡不重複判斷名額。
    """
    from ygo_sniper.seller_links import SellerUrlError
    from ygo_sniper.seller_resolve import resolve_seller_target
    from ygo_sniper.seller_watch import (
        SELLER_PAGE_SOURCE,
        SOURCE_PINNED,
        UNSUPPORTED_SITE_NOTE,
        add_watch,
    )

    raw = (body.url or "").strip()
    try:
        key, store_slug = resolve_seller_target(raw, cfg)
    except SellerUrlError as exc:
        raise HTTPException(400, str(exc)) from exc
    reason_text = body.reason or "從 dashboard 釘選（貼賣家頁 URL）"
    if store_slug:
        # 店鋪頁是連網解析來的：出處進 reason，日後看名單知道這個鍵哪來的
        reason_text += f"（從店鋪頁 {store_slug} 解析）"
    res = add_watch(
        store, key, source=SOURCE_PINNED,
        reason=reason_text,
        params=_watch_params(),
    )
    if not res.ok:
        # add_watch 的拒絕（例如鍵格式錯誤）也是使用者貼錯東西——一樣 400
        # ＋原文，不包裝成 200 讓前端自己猜。
        raise HTTPException(400, res.reason)
    site = key.partition(":")[0]
    note = (
        None if site in SELLER_PAGE_SOURCE
        else UNSUPPORTED_SITE_NOTE.get(site, f"{site} 沒有賣家頁列舉實作")
    )
    message = res.reason
    if store_slug:
        message += f"；店鋪頁 {store_slug} → 帳號 {key.partition(':')[2]}"
    return {
        "ok": True, "already": res.already, "seller_key": res.seller_key,
        "message": message, "batch": res.batch,
        "unsupported_note": note,
    }


# ---------------------------------------------------------------------------
# 指定卡狙擊（card snipe）。**這一層只搬資料**：tier 判準、登錄、挖掘、檔案
# 組裝全部在 `ygo_sniper.card_snipe` —— CLI 的 `snipe` 群組與這幾條 route 共用
# 同一支政策，兩邊才不會對同一張卡講兩種話。
#
# 路徑不與 `/api/sellers/{seller_key:path}`（catch-all）相撞：那條吃的是
# `/api/sellers/…`，這裡整段前綴是 `/api/snipe`，第一段就分岔了。
class SnipeAddRequest(BaseModel):
    name_ja: str
    grader: str
    grade: str
    name_en: str = ""
    code: str = ""
    census_url: str = ""
    evidence_urls: list[str] = []
    note: str = ""
    #: 預設登錄當下就挖市場成交檔案（那是檔案的主要內容——我們自己的庫只有
    #: 181 天且是碰巧掃到的）。測試傳 False 免網路。
    mine: bool = True


@app.get("/api/snipe")
def snipe_list_api():
    """狙擊清單＋命中統計（輕量；完整檔案在 `/api/snipe/{id}`）。

    三個 tier 分開回，**不合成一個總數**：near（未鑑定／別家機構／現代重印）
    是「記帳但不推播」的那一批，與 🎯／👀 的意義不同，加起來只會讓人以為
    命中很多。
    """
    out = []
    for w in store.list_card_watch(active_only=True):
        hits = store.list_card_watch_hits(watch_id=int(w["id"]))
        counts = {t: sum(1 for h in hits if h["tier"] == t)
                  for t in ("exact", "partial", "near")}
        out.append({**w, "hit_counts": counts,
                    "recent_hits": [h for h in hits if h["tier"] != "near"][:10]})
    return {"watches": out}


@app.get("/api/snipe/{watch_id}")
def snipe_detail(watch_id: int):
    """完整檔案：census＋實證＋市場成交檔案＋本地歷史（現場重比對）＋命中帳。"""
    from ygo_sniper.card_snipe import build_dossier

    w = store.get_card_watch(watch_id)
    if w is None:
        raise HTTPException(404, f"沒有狙擊 #{watch_id}")
    d = build_dossier(store, w)
    return {
        "watch": d.watch, "census": d.census, "census_total": d.census_total,
        # 三個桶分開回，前端也分開畫——出處與分母都不同，不可相加
        "sales": d.sales, "local_history": d.local_history,
        "evidence": d.evidence, "hits": d.hits,
        "recommendation": d.recommendation,
    }


@app.post("/api/snipe/{watch_id}/mine")
def snipe_mine_api(watch_id: int):
    """重挖市場成交檔案（會連網，數秒）。

    挖不到要把問題**原文**回給前端顯示：「0 筆成交」與「被擋／連不上」外顯
    一模一樣，只有這條 `problems` 分得出來（CLAUDE.md 第五節）。
    """
    from ygo_sniper.card_snipe import WatchMatcher, mine_sold_archive

    w = store.get_card_watch(watch_id)
    if w is None:
        raise HTTPException(404, f"沒有狙擊 #{watch_id}")
    with CachedFetcher(cfg) as fetcher:
        res = mine_sold_archive(store, build_sources(cfg, fetcher),
                                WatchMatcher.from_row(w))
    return {"ok": res.ok, "summary": res.summary(), "new_sales": res.new_sales,
            "total_sales": res.total_sales, "problems": res.problems}


@app.post("/api/snipe")
def snipe_add_api(body: SnipeAddRequest):
    """與 CLI 的 `snipe add` 同一支政策（`card_snipe.add_card_watch`）——判準只有一份。

    會連網抓 census 與證據頁（幾秒），與釘選解析店鋪頁同一種等待。輸入格式錯
    （機構不認得、分數看不懂、卡號正規化失敗）是 semantic 失敗：400＋訊息原文，
    不入庫、不重試。
    """
    from ygo_sniper.card_snipe import add_card_watch

    try:
        with CachedFetcher(cfg) as fetcher:
            res = add_card_watch(
                store, fetcher,
                grader=body.grader, grade_input=body.grade, name_ja=body.name_ja,
                name_en=body.name_en, code=body.code, census_url=body.census_url,
                evidence_urls=body.evidence_urls, note=body.note,
                sources=build_sources(cfg, fetcher) if body.mine else None,
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "watch_id": res.watch_id, "messages": res.messages}


@app.post("/api/snipe/{watch_id}/remove")
def snipe_remove_api(watch_id: int):
    """軟刪除（命中帳、成交檔案與證據都留著——那是之後判斷的依據）。"""
    if not store.deactivate_card_watch(watch_id):
        raise HTTPException(404, f"#{watch_id} 不在清單上")
    return {"ok": True}


@app.get("/api/scan/status")
def scan_status():
    """「現在有沒有在掃」＋「上一次掃完是什麼時候」。前端輪詢這個。

    狀態落在 db 的 meta 表，所以它是**跨行程**的：CLI（launchd 的 daily）
    正在掃的時候，dashboard 一樣看得到「掃描中」，不會兩邊各按一次。

    卡死防線在 store.scan_status：開始超過 `scan.timeout_seconds` 還沒回報完成，
    一律回 `running:false, stale:true`。掃描中途被 kill 的狀態不會讓按鈕永遠鎖住。
    """
    return store.scan_status(timeout_seconds=cfg.scan_timeout_seconds)


@app.post("/api/scan")
def trigger_scan(tasks: BackgroundTasks):
    """從 dashboard 手動觸發一次掃描（不推播，結果直接進 db）。

    已經在掃就不再排一次：兩條 pipeline 同時打同一批來源只會更快被擋，
    而且兩邊都會寫 scan 狀態，先結束的那個會把還在跑的那個標成「已完成」。
    回 200 + `started:false` 而不是 4xx——「已經在跑了」對使用者是正常回答，
    不是錯誤。
    """
    st = store.scan_status(timeout_seconds=cfg.scan_timeout_seconds)
    if st["running"]:
        return {
            "ok": True,
            "started": False,
            "running": True,
            "message": f"已經有一輪掃描在跑（{st.get('trigger') or '?'} 觸發），這次不重複啟動",
            "status": st,
        }

    # **在回應之前**就把狀態標成 running。BackgroundTasks 是回應送出「之後」才跑，
    # 等 pipeline 自己去標的話，中間有一段前端問到「沒有在掃」的空窗——
    # 而那正是使用者會再按一次的時機。pipeline 進場後會用自己的 started_at
    # 覆寫這一筆，兩者都是 running，不影響前端。
    started = store.begin_scan(trigger="dashboard")

    def _run() -> None:
        from ygo_sniper.pipeline import Pipeline

        try:
            p = Pipeline(cfg)
            try:
                p.scan(trigger="dashboard")
            finally:
                p.close()
        except Exception as exc:  # noqa: BLE001 - 見下
            # 建 Pipeline 就失敗（缺 playwright、db 壞掉…）時 pipeline 自己那層
            # 的 finally 根本沒機會跑。這裡不接的話狀態會卡 running 到逾時為止，
            # 使用者半小時內按不了掃描而且看不到原因（工程原則 3：大聲失敗）。
            store.finish_scan(started, error=f"{type(exc).__name__}: {exc}")
            raise

    tasks.add_task(_run)
    return {
        "ok": True,
        "started": True,
        "running": True,
        "message": "掃描已在背景啟動，完成後清單會自動更新",
    }


@app.post("/api/scan-high")
def trigger_scan_high(tasks: BackgroundTasks):
    """從 dashboard 手動觸發一次**高價帶**掃描（¥8,624～50,000，只掛
    buyee_mercari；不推播，結果直接進 db）。

    骨架與 `/api/scan` 完全同款、**共用同一個全域 scan 狀態**
    （`store.begin_scan`/`finish_scan`/`scan_status`）：兩顆按鈕互斥，
    任一輪在跑時另一輪回 `started:false`——這不是偷懶省一個狀態表，是
    既有防線的正確延伸（高價帶 plan 全域紅線：Playwright 不該兩個並開；
    兩條 pipeline 同時打來源只會更快被擋）。差別只有 `trigger` 與
    `Pipeline.scan(high_band=True)`。
    """
    st = store.scan_status(timeout_seconds=cfg.scan_timeout_seconds)
    if st["running"]:
        return {
            "ok": True,
            "started": False,
            "running": True,
            "message": f"已經有一輪掃描在跑（{st.get('trigger') or '?'} 觸發），這次不重複啟動",
            "status": st,
        }

    started = store.begin_scan(trigger="dashboard-high")

    def _run() -> None:
        from ygo_sniper.pipeline import Pipeline

        try:
            p = Pipeline(cfg)
            try:
                p.scan(high_band=True, trigger="dashboard-high")
            finally:
                p.close()
        except Exception as exc:  # noqa: BLE001 - 見 trigger_scan 的同款註解
            store.finish_scan(started, error=f"{type(exc).__name__}: {exc}")
            raise

    tasks.add_task(_run)
    return {
        "ok": True,
        "started": True,
        "running": True,
        "message": "高價掃描已在背景啟動，完成後清單會自動更新",
    }
