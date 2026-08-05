"""歷史成交挖掘：把「從今天開始累積帳本」換成「把平台的歷史挖出來」。

---------------------------------------------------------------------------
## 這個模組修的是一個方法論錯誤

Seller Alpha 原本的設計是：每小時掃一次在架，把觀測寫進 `listing_obs`，
四週之後才有足夠的時間跨度談「持續性」。那個設計把一件**已經發生過的事**
當成**還沒發生的事**在等——每個平台都有歷史成交紀錄可以挖：

| 來源 | 歷史深度（2026-08-04 實測） | 帶賣家 | 帶真實成交時間 |
|---|---|---|---|
| Yahoo 落札相場（`yahoo_closed`） | 約 180 天 archive | ✅ 100% | ✅ `endTime` |
| Yahoo!フリマ 賣家頁（`paypay_direct`） | 實測單頁橫跨 178 天 | ✅ | ✅ `endTime` |
| eBay | **無**（見下方「eBay 的誠實結論」） | — | — |

所以「持續性」不必等：一次回填就能拿到半年的賣家行為。

## 兩條路，兩種形狀

1. **廣掃**（`run_yahoo_backfill`）：`comps_queries` 展開出來的關鍵字，
   對 `yahoo_closed` 逐個深挖。Yahoo 的 `?seller=` 參數**實測無效**
   （回全部賣家），所以這條路只能廣掃後**依賣家分組**，不能逐賣家查詢。
2. **點射**（`mine_paypay_seller`）：Yahoo!フリマ 有真正的賣家頁
   （`/user/{id}`），一個請求就能拿回那個賣家最近 100 筆（實測 73 筆是
   已售出、橫跨半年）。已知賣家逐一挖，成本是每人 1 個請求。

## 為什麼要記帳（`meta` 裡的續跑帳本）

回填是一次性的大量請求（幾十到上百個），而它會被 Ctrl-C、被網路中斷、
被請求預算截斷。沒有帳本的話，續跑只能從頭再來一次——那些請求換回來的是
**逐位相同的資料**（`comps` 的 `UNIQUE(signature, url)` 會全部 IGNORE 掉）。
帳本落 `meta` 而不是記憶體：CLI 每次都是全新的行程（與
`comps.claim_sold_run`、`seller_watch` 的輪替帳同一套理由）。

帳本記的是**頁碼**不是「做過了」：同一個關鍵字第一次翻 4 頁、第二次想翻 9 頁，
續跑要從第 5 頁接下去，不是重抓前 4 頁。`archive_exhausted` 的查詢直接跳過
——那是「翻完了」，不是「還沒翻」。

## eBay 的誠實結論（2026-08-04 實測，兩條路都試過）

- **Marketplace Insights API**（唯一的官方已售出資料）：用現有的
  client credentials 打 `/buy/marketplace_insights/v1_beta/item_sales/search`
  回 **HTTP 403 `Access denied / Insufficient permissions`**；直接向
  `identity/v1/oauth2/token` 申請
  `api_scope/buy.marketplace.insights` 這個 scope 回
  **HTTP 400 `invalid_scope`**——也就是這組憑證根本拿不到那個 scope，
  它需要 eBay 逐案審核的合作夥伴權限，不是設定問題。
- **賣家頁的 sold 篩選**（`/sch/i.html?_ssn=…&LH_Sold=1&LH_Complete=1`）：
  純 httpx 回 **HTTP 403**（bot 防護），回應 1.8KB 是攔截頁不是結果頁。

所以 eBay 的賣家**只有在架價（ask basis）**可用，沒有成交價。這不是暫時的
缺口而是資料權限的邊界，記成 open item：要嘛去申請 Insights 權限，要嘛
承認 eBay 賣家的「持續性」只能靠我們自己的在架帳本慢慢累積。
**絕不用在架價冒充成交價**——那正是 `seller_alpha` 基準對齊規則擋的事。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 續跑帳本的 meta 鍵。
HISTORY_META_KEY = "comps_history_backfill"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# 參數與報告
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class HistoryParams:
    """一次回填的預算。**每個值都是請求數的直接乘數**，所以都要說得出依據。"""

    #: 每個查詢最多翻幾頁。實測 `遊戯王 初期 PSA`（pool 855）一頁 100 筆
    #: 涵蓋約 25 天，9 頁就把 180 天 archive 翻完。
    pages: int = 9
    #: 這一輪的請求硬上限。**這是唯一擋得住組合爆炸的東西**：78 個查詢 × 9 頁
    #: ＝ 702 個請求。超過就停下來記帳，下次續跑（不是靜默截斷尾端）。
    max_requests: int = 120
    #: 已經翻完（`archive_exhausted`）的查詢要不要重來。
    redo_exhausted: bool = False


@dataclass(slots=True)
class QueryOutcome:
    """一個關鍵字這一輪的結果。"""

    query: str
    pages_fetched: int = 0
    from_page: int = 1
    found: int = 0          # 解析出的成交筆數
    kept: int = 0           # 過 parse_card + is_candidate 之後真的入庫的
    rejected: int = 0
    with_seller: int = 0
    exhausted: bool = False
    health: str = "ok"
    detail: str = ""
    oldest: str | None = None
    newest: str | None = None


@dataclass(slots=True)
class HistoryReport:
    """一次回填的完整答案。**請求數與涵蓋範圍必須說得出來**。"""

    requests: int = 0
    outcomes: list[QueryOutcome] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budget_hit: bool = False
    dry_run: bool = False

    @property
    def kept(self) -> int:
        return sum(o.kept for o in self.outcomes)

    @property
    def found(self) -> int:
        return sum(o.found for o in self.outcomes)

    @property
    def with_seller(self) -> int:
        return sum(o.with_seller for o in self.outcomes)

    def span(self) -> tuple[str | None, str | None]:
        stamps = [s for o in self.outcomes for s in (o.oldest, o.newest) if s]
        return (min(stamps), max(stamps)) if stamps else (None, None)

    def summary(self) -> str:
        lo, hi = self.span()
        window = f"{(lo or '—')[:10]} → {(hi or '—')[:10]}" if lo else "—"
        return (
            f"查詢 {len(self.outcomes)} 個、請求 {self.requests} 個 → "
            f"成交 {self.found} 筆（入庫 {self.kept}、帶賣家 {self.with_seller}），"
            f"成交時間跨度 {window}"
            + ("；**請求預算用完，已記帳，可續跑**" if self.budget_hit else "")
        )


# ---------------------------------------------------------------------------
# 續跑帳本
# ---------------------------------------------------------------------------
def load_ledger(store: Any) -> dict[str, dict[str, Any]]:
    """讀續跑帳本。壞掉的 JSON 一律當空帳本（**不拋**）：一個手改壞的
    meta 值不該讓回填整支癱掉，重跑一次的代價只是重複請求。"""
    raw = store.get_meta(HISTORY_META_KEY)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        print("[warn] 歷史回填帳本不是合法 JSON，當成空帳本重新開始")
        return {}
    if not isinstance(val, dict):
        return {}
    return {str(k): v for k, v in val.items() if isinstance(v, dict)}


def save_ledger(store: Any, ledger: dict[str, dict[str, Any]]) -> None:
    store.set_meta(HISTORY_META_KEY, json.dumps(ledger, ensure_ascii=False))


def reset_ledger(store: Any) -> None:
    """清空帳本（人工「我要重挖一次」）。"""
    store.set_meta(HISTORY_META_KEY, "")


def next_page_for(ledger: dict[str, dict[str, Any]], query: str) -> int:
    """這個查詢下次該從第幾頁開始。沒有紀錄就是第 1 頁。"""
    entry = ledger.get(query) or {}
    try:
        done = int(entry.get("pages_done") or 0)
    except (TypeError, ValueError):
        done = 0
    return max(1, done + 1)


# ---------------------------------------------------------------------------
# Yahoo 落札相場：廣掃深挖
# ---------------------------------------------------------------------------
def _sold_span(listings: list[Any]) -> tuple[str | None, str | None]:
    """挖回來這批成交的時間跨度（最早, 最晚）。

    這裡讀的是 `lst.raw["sold_at"]`——**來源直接給的成交日**，不是 comps 那個
    可能被 `now()` 蓋過的欄位。只有 Yahoo 落札相場走這條路，而它是唯一有真實
    成交日的來源（2026-08-06 實測：comps 裡 buyee_yahoo 957 筆全部是真時間），
    所以這個跨度是真的。Buyee 系（Mercari／PayPay）的頁面沒有日期，
    `raw` 裡也就沒有這個鍵，不會混進來。
    """
    stamps = sorted(
        s for lst in listings if (s := (getattr(lst, "raw", None) or {}).get("sold_at"))
    )
    return (stamps[0], stamps[-1]) if stamps else (None, None)


def run_yahoo_backfill(
    *,
    store: Any,
    comps: Any,
    source: Any,
    queries: list[str],
    params: HistoryParams | None = None,
    dry_run: bool = False,
) -> HistoryReport:
    """對 `queries` 逐個深挖 Yahoo 落札相場，成交筆數餵進 comps。

    **冪等**：`comps` 的 `UNIQUE(signature, url)` 讓重複入庫變成 no-op，而
    `save_comps` 會把「這次帶著賣家的同一筆」的 `seller_id` 補進舊列的 NULL
    ——所以重跑不會產生重複行情，只會補齊賣家（那正是我們要的：庫裡 796 筆
    Yahoo 成交只有 43 筆帶賣家，因為它們是賣家欄位上線前入庫的）。

    **可續跑**：每個查詢做完就寫帳本（頁碼、是否翻完），請求預算用完就停。

    `dry_run=True` 只印會做什麼，不打外網、不入庫、不記帳。
    """
    p = params or HistoryParams()
    ledger = load_ledger(store)
    report = HistoryReport(dry_run=dry_run)

    for query in queries:
        entry = ledger.get(query) or {}
        if entry.get("exhausted") and not p.redo_exhausted:
            report.skipped.append((query, f"存量已翻完（{entry.get('pages_done')} 頁）"))
            continue
        start = next_page_for(ledger, query)
        if start > p.pages:
            report.skipped.append(
                (query, f"已翻到第 {start - 1} 頁，未超過本輪目標 {p.pages} 頁")
            )
            continue
        want = p.pages - start + 1
        remaining = p.max_requests - report.requests
        if remaining <= 0:
            report.budget_hit = True
            report.skipped.append((query, "本輪請求預算已用完（下次續跑）"))
            continue
        want = min(want, remaining)

        outcome = QueryOutcome(query=query, from_page=start)
        if dry_run:
            outcome.pages_fetched = want
            report.outcomes.append(outcome)
            report.requests += want
            continue

        try:
            result = source.search_detailed(query, pages=want, first_page=start)
        except Exception as exc:  # noqa: BLE001 - 單一查詢失敗不得讓整輪停擺
            report.errors.append(f"{query}：{type(exc).__name__}: {exc}")
            continue

        outcome.pages_fetched = result.pages_fetched
        outcome.health = getattr(result.health, "value", str(result.health))
        outcome.detail = result.detail
        outcome.exhausted = bool(result.archive_exhausted)
        outcome.found = len(result.listings)
        outcome.with_seller = sum(1 for lst in result.listings if lst.seller_id)
        outcome.oldest, outcome.newest = _sold_span(result.listings)
        report.requests += result.pages_fetched

        if result.listings:
            # 每個查詢用**乾淨的索引**入庫：`CompsEngine._index` 是累積的，
            # `save_comps` 每次寫整份索引——不清掉的話 78 個查詢會退化成
            # O(n²) 次寫入（結果一樣，但時間是幾十倍）。
            comps._index = {}
            ing = comps.ingest_sold(result.listings)
            outcome.kept, outcome.rejected = ing.kept, ing.rejected

        ledger[query] = {
            "pages_done": start + result.pages_fetched - 1,
            "exhausted": outcome.exhausted,
            "at": _now_iso(),
            "found": int(entry.get("found") or 0) + outcome.found,
            "kept": int(entry.get("kept") or 0) + outcome.kept,
        }
        save_ledger(store, ledger)
        report.outcomes.append(outcome)

        if report.requests >= p.max_requests:
            report.budget_hit = True

    return report


# ---------------------------------------------------------------------------
# Yahoo!フリマ：賣家頁歷史
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SellerMineOutcome:
    seller_key: str
    ok: bool = False
    requests: int = 0
    found: int = 0
    kept: int = 0
    rejected: int = 0
    oldest: str | None = None
    newest: str | None = None
    note: str = ""

    @property
    def span_days(self) -> float:
        if not (self.oldest and self.newest):
            return 0.0
        try:
            lo = datetime.fromisoformat(self.oldest)
            hi = datetime.fromisoformat(self.newest)
        except (TypeError, ValueError):
            return 0.0
        return (hi - lo).total_seconds() / 86400.0


def mine_paypay_seller(
    *, comps: Any, source: Any, seller_id: str, dry_run: bool = False
) -> SellerMineOutcome:
    """一個 Yahoo!フリマ 賣家的歷史成交（1 個請求）。

    賣家頁與搜尋頁是同一個結果節點，所以 SOLD／OPEN 分流與真實 `endTime`
    的判準完全重用 `paypay._extract_listings`——判準只有一份。
    """
    out = SellerMineOutcome(seller_key=f"buyee_paypay:{seller_id}")
    if dry_run:
        out.requests = 1
        out.note = "dry-run：未打外網"
        return out
    try:
        result = source.search_seller(seller_id, sold=True)
    except Exception as exc:  # noqa: BLE001 - 一個賣家失敗不得讓整輪停擺
        out.note = f"{type(exc).__name__}: {exc}"
        return out
    out.requests = max(1, result.pages_fetched)
    out.found = len(result.listings)
    out.oldest, out.newest = _sold_span(result.listings)
    if result.listings:
        comps._index = {}
        ing = comps.ingest_sold(result.listings)
        out.kept, out.rejected = ing.kept, ing.rejected
    out.ok = True
    out.note = result.detail or getattr(result.health, "value", "")
    return out


#: site → 有沒有「賣家歷史成交」這條路，以及為什麼沒有。
#: **說得出「為什麼沒挖」比安靜跳過重要**：安靜跳過與「這個賣家沒賣過東西」
#: 外顯一模一樣。
SELLER_HISTORY_SOURCE: dict[str, str] = {"buyee_paypay": "paypay_direct"}

SELLER_HISTORY_UNSUPPORTED: dict[str, str] = {
    "buyee_yahoo": (
        "Yahoo 落札相場的 `?seller=` 參數實測無效（回全部賣家）——只能廣掃後"
        "依賣家分組，`mine-history` 那條路已經在做"
    ),
    "ebay": (
        "eBay 沒有已售出資料可抓：Marketplace Insights API 回 403（憑證拿不到"
        "該 scope，實測申請 scope 本身回 invalid_scope），賣家頁 LH_Sold=1 回 403"
        "（bot 防護）。這是資料權限的邊界，不是還沒做（open item）"
    ),
    "buyee_mercari": (
        "Mercari 已售出頁沒有任何成交時間（實測搜尋頁與商品頁皆然）——"
        "抓回來只能蓋上入庫時間，那正是這次要修掉的病"
    ),
    "mercari_tw": "Mercari 台灣賣家頁尚未實測",
    "ruten": "露天賣家頁尚未實測",
}


__all__ = [
    "HISTORY_META_KEY",
    "SELLER_HISTORY_SOURCE",
    "SELLER_HISTORY_UNSUPPORTED",
    "HistoryParams",
    "HistoryReport",
    "QueryOutcome",
    "SellerMineOutcome",
    "load_ledger",
    "mine_paypay_seller",
    "next_page_for",
    "reset_ledger",
    "run_yahoo_backfill",
    "save_ledger",
]
