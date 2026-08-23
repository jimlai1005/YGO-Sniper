"""watchlist 的 `queries:` 解析成結構化的查詢設定。

**為什麼要有這一層**（2026-08-03 覆蓋率事故）：
在架掃描的每一條 query 都以「遊戯王」開頭，而 Mercari／PayPay 的搜尋是
AND 語意——標題沒寫「遊戯王」的標的**永遠**撈不到。實例（使用者自己找到並
買下的兩張，我們從來沒撈到過）：

    【PSA9】カエルスライム 初期 プレミアムパック
    【PSA9】大砲だるま 初期 プレミアムパック

解析端完全沒問題（`parse_card` 得到 PSA9、`ev=['jp_kw:初期']`、
`is_candidate=True`），純粹是**沒有被搜到**。補救辦法是「弱關鍵字 ＋ 分類 ID」：
分類把範圍收在遊戲王卡，關鍵字只留鑑定機構那一段。所以查詢設定必須帶得動
分類，而分類 ID **逐來源不同**（Buyee 的 1152 與 Yahoo 的 2084005059 是兩個
完全不相干的編號系統），因此：

- watchlist 的 `categories:` 是「來源 → 別名 → 該來源的分類值」兩層表；
- 每條 query 只寫**別名**（`category: yugioh`），各來源自己查自己的值；
- 某來源沒有這個別名 → 那條 query 對它就是純關鍵字搜尋（不是錯誤：
  各站的分類樹本來就不對齊，硬要求每站都有值只會逼人填假資料）。

分類值一律以**字串**在管線裡流動，由各 source 自己轉成它的參數型別
（Buyee `category_id=1152`、Yahoo `auccat=2084005059`、
PayPay `categoryIds=2511,2420`、eBay `category_ids=183454`）。
統一成 int 會讓 PayPay 的 `2511,2420` 這種路徑值無處可放。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 別名沒指定時，一條 query 對任何來源都不帶分類。
#: **刻意沒有「預設分類」**：舊版在 `pipeline._scan_source` 裡對 Buyee 硬套
#: `categories.buyee_mercari.yugioh_ocg`，而那個值一路被 `run_source_search`
#: 丟掉（它只把 category 傳給舊介面，Buyee 走的是 `search_detailed`）——
#: 設定看起來有效、實際上從未生效，而且沒有任何外顯症狀。
#: 現在要帶分類就得在 query 上寫出來，看得見才查得出來。


@dataclass(frozen=True, slots=True)
class QuerySpec:
    """一條在架掃描查詢。

    `keyword` 允許是空字串——「純分類、不帶關鍵字」是這次覆蓋率研究要實測的
    設定之一（分類已經把範圍收在遊戲王卡了，關鍵字反而是漏掉標的的原因）。
    """

    name: str
    keyword: str
    sources: tuple[str, ...]
    #: 分類別名（在 `categories:` 表裡逐來源查值）。None = 不帶分類。
    category: str | None = None

    @property
    def label(self) -> str:
        cat = f"+{self.category}" if self.category else ""
        return f"{self.name}{cat}"


def load_queries(watchlist: dict[str, Any]) -> list[QuerySpec]:
    """`watchlist['queries']` → `QuerySpec` 清單。壞掉的列印警告並跳過。

    跳過而不是拋例外：一條寫壞的 query 不該讓整輪掃描沒有產出，但**必須
    印出來**——安靜地少跑一條查詢，外顯與「今天沒貨」一模一樣。
    """
    out: list[QuerySpec] = []
    for raw in watchlist.get("queries") or []:
        if not isinstance(raw, dict):
            print(f"[warn] watchlist query 不是對映表，跳過：{raw!r}")
            continue
        sources = raw.get("sources") or []
        if not isinstance(sources, list) or not sources:
            print(f"[warn] watchlist query {raw.get('name')!r} 沒有 sources，跳過")
            continue
        keyword = raw.get("keyword")
        if keyword is None:
            print(f"[warn] watchlist query {raw.get('name')!r} 沒有 keyword，跳過")
            continue
        category = raw.get("category")
        out.append(
            QuerySpec(
                name=str(raw.get("name") or keyword or "(未命名)"),
                keyword=str(keyword),
                sources=tuple(str(s) for s in sources),
                category=str(category) if category else None,
            )
        )
    return out


def resolve_category(
    watchlist: dict[str, Any], source: str, alias: str | None
) -> str | None:
    """(來源, 別名) → 該來源的分類值字串。查不到回 None。

    查不到**不是錯誤**：各站分類樹不對齊，`categories.ebay` 沒有 `yugioh_ocg`
    這個別名是完全合理的，那條 query 對 eBay 就退化成純關鍵字搜尋。
    但別名整個表都不存在時要印警告——那是打錯字，不是設計。
    """
    if not alias:
        return None
    table = (watchlist.get("categories") or {}).get(source) or {}
    value = table.get(alias)
    if value is None:
        known = {
            a
            for spec in (watchlist.get("categories") or {}).values()
            if isinstance(spec, dict)
            for a in spec
        }
        if alias not in known:
            print(
                f"[warn] 分類別名 {alias!r} 在 categories: 表裡完全不存在"
                f"（已知：{', '.join(sorted(known)) or '無'}）——這條查詢不會帶分類"
            )
        return None
    return str(value)


def load_high_band_queries(
    watchlist: dict[str, Any],
) -> tuple[float | None, list[QuerySpec]]:
    """`watchlist['high_band']` → (max_price_jpy, QuerySpec 清單)。

    區塊不存在 → (None, [])——高價帶是選配，不裝就是沒有。
    `max_price_jpy` 缺失或非正數 → 印警告並回 (None, [])：沒有上限的高價帶
    等於整個市場，那不是這個功能的語意，寧可整段不跑並大聲說。
    查詢列的解析與 `load_queries` 同一套容錯（壞列印警告跳過）。
    """
    block = watchlist.get("high_band")
    if not isinstance(block, dict):
        return None, []
    max_price = block.get("max_price_jpy")
    if not isinstance(max_price, (int, float)) or isinstance(max_price, bool) or max_price <= 0:
        print(
            f"[warn] watchlist high_band.max_price_jpy 缺失或非正數（{max_price!r}）"
            "——高價帶沒有上限就等於整個市場，本輪不跑"
        )
        return None, []
    queries = load_queries(block)
    return float(max_price), queries


__all__ = ["QuerySpec", "load_queries", "load_high_band_queries", "resolve_category"]
