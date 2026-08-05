"""召回率研究：一把量「這組查詢到底看得到多少貨」的尺。

**為什麼需要它**（2026-08-03）：使用者自己找到並買下兩張我們從來沒撈到過的卡，

    【PSA9】カエルスライム 初期 プレミアムパック
    【PSA9】大砲だるま 初期 プレミアムパック

標題裡沒有「遊戯王」，而五條日本站 query 每一條都以「遊戯王」開頭、
Mercari／PayPay 的搜尋是 AND 語意——這類標的**永遠**撈不到。解析端完全正常
（`parse_card` 給 PSA9、`ev=['jp_kw:初期']`、`is_candidate=True`），
純粹是沒被搜到。而「沒搜到」是這個工具最難察覺的失敗：它與「今天市場沒好貨」
外顯一模一樣，沒有任何錯誤訊息、沒有健康告警、沒有數字變化。

所以在改查詢之前要先有量尺。本模組提供兩層：

1. **純函式的集合運算**（`marginal_gains`／`sequential_net_new`／`greedy_order`），
   吃「每組設定 → 候選鍵集合」，不碰網路，可以用固定資料驗算。
2. **`run_study()`**：真的去打外網，每組設定跑一次，把 (1) 需要的集合做出來。

## 三個刻意的決定

**候選鍵用 `Listing.key`（site:external_id）**，與 store 主鍵、`dedupe_listings`
同一份定義（工程原則 1）。用「標題＋價格」之類的近似鍵會讓去重數字與真的
落庫數字對不起來，而整份研究就是在比數字。

**只數通過 `is_candidate` 的**。「解析出 100 筆」不是覆蓋率——那 100 筆可能
全是現代卡。覆蓋率的分子必須是「這個工具真的會留下來的東西」，否則加一條
撈垃圾的 query 也會讓數字變好看。雜訊率另外報，兩個數字分開看。

**邊際貢獻 = 拿掉它，聯集少多少**（不是「它自己有幾筆」）。一條 query 撈到
90 筆但全部與別條重疊，它的邊際貢獻是 0，該砍——這是唯一能讓「加法與減法」
用同一把尺衡量的定義。順序敏感的「淨增量」另外給（`sequential_net_new`），
因為它回答的是另一個問題：「照這個順序加，第 N 條還值不值得」。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .parsers import is_candidate, parse_card
from .pipeline import run_source_search
from .sources.health import ParseHealth

#: 這幾種健康碼代表「這一次觀測可以採信」。其餘（被擋／解析壞／連線失敗）
#: 是「什麼都不知道」——**不可以拿它去算覆蓋率**，那會把一次 WAF 挑戰
#: 記成「這組設定沒有貢獻」，然後據此把一條好 query 砍掉。
_OBSERVABLE = (ParseHealth.OK, ParseHealth.EMPTY_CONFIRMED)


@dataclass(frozen=True, slots=True)
class Variant:
    """一組要評估的查詢設定。

    `category` 是**已經解析好的分類值**（Buyee 的 `1152`、Yahoo 的
    `2084005059`、PayPay 的 `2511,2420`），不是 watchlist 的別名——研究階段
    要能測還沒寫進 watchlist 的值。
    """

    label: str
    source: str
    keyword: str
    category: str | None = None
    #: 分組（例如 `old` / `new`），用來做前後對照的聯集比較。
    group: str = ""
    pages: int = 1


@dataclass(slots=True)
class VariantOutcome:
    """一組設定跑完的結果。`keys` 是覆蓋率運算的唯一輸入。"""

    variant: Variant
    health: str
    observable: bool
    parsed: int
    listings: int
    #: 通過 `is_candidate` 的標的：去重鍵 → 標題。**標題要留著**——
    #: 這次事故的證據就是「標題裡沒有『遊戯王』」，只存鍵的話無從驗證
    #: 「新組合真的撈得到那一類標的」。
    items: dict[str, str] = field(default_factory=dict)
    rejects: dict[str, int] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    requests: int = 0
    url: str = ""
    detail: str = ""

    @property
    def keys(self) -> set[str]:
        return set(self.items)

    @property
    def candidates(self) -> int:
        return len(self.items)

    def titles_without(self, needle: str) -> list[str]:
        """候選裡**標題不含 needle** 的那些。

        `needle="遊戯王"` 就是本次事故的直接證據：舊組合每一條都以「遊戯王」
        開頭、搜尋是 AND 語意，所以這個清單裡的每一筆都是舊組合結構上撈不到的。
        """
        return sorted(t for t in self.items.values() if needle not in t)

    @property
    def noise_rate(self) -> float | None:
        """雜訊率 = 1 − 候選數/回傳筆數。回傳 0 筆時是 None（不是 0）。

        None 與 0 必須分開：0 代表「回來的每一筆都是候選」，None 代表
        「沒有東西可以算」——把後者記成 0 會讓一條什麼都撈不到的 query
        在雜訊率那一欄看起來完美。
        """
        if self.listings <= 0:
            return None
        return round(1.0 - self.candidates / self.listings, 4)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.variant.label,
            "group": self.variant.group,
            "source": self.variant.source,
            "keyword": self.variant.keyword,
            "category": self.variant.category,
            "pages": self.variant.pages,
            "health": self.health,
            "observable": self.observable,
            "parsed": self.parsed,
            "listings": self.listings,
            "candidates": self.candidates,
            "noise_rate": self.noise_rate,
            # 去重鍵要存下來，落檔的報告才能**離線重算**任何集合運算
            # （換一組子集重算邊際貢獻、跟上一份報告比對），不必再打一次外網。
            "candidate_keys": sorted(self.items),
            "candidate_titles": sorted(self.items.values()),
            "candidates_without_yugioh": self.titles_without("遊戯王"),
            "rejects": dict(sorted(self.rejects.items(), key=lambda kv: -kv[1])),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "requests": self.requests,
            "url": self.url,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# 純函式：集合運算。**不碰網路，可以用固定資料驗算。**
# ---------------------------------------------------------------------------
def union_size(sets: Iterable[set[str]]) -> int:
    out: set[str] = set()
    for s in sets:
        out |= s
    return len(out)


def marginal_gains(sets: Mapping[str, set[str]]) -> dict[str, int]:
    """每組的邊際貢獻：`|全部聯集| − |少了它的聯集|`。

    這是「該不該砍掉這一條」的判準：0 代表它撈到的東西別條全都撈得到，
    砍掉不損失任何覆蓋。**與「它自己有幾筆」是兩回事**——一條撈 90 筆但
    完全重疊的 query，自身筆數很好看，邊際貢獻是 0。
    """
    total = union_size(sets.values())
    out: dict[str, int] = {}
    for name in sets:
        others = [s for k, s in sets.items() if k != name]
        out[name] = total - union_size(others)
    return out


def sequential_net_new(order: Sequence[str], sets: Mapping[str, set[str]]) -> dict[str, int]:
    """照給定順序累加時，每一組帶來的**新增**筆數（順序敏感）。

    與 `marginal_gains` 回答不同的問題：這裡是「已經有前面那些了，再加這條
    值不值」，適合用來排「第 N 條 query 的邊際效益」；`marginal_gains` 是
    「整組都在的前提下，抽掉它會少多少」，適合用來決定砍誰。
    兩個都要看：一條 query 可能在順序上淨增 0（前面已經涵蓋），
    但邊際貢獻 > 0（因為前面那條也可能被砍）。
    """
    seen: set[str] = set()
    out: dict[str, int] = {}
    for name in order:
        s = sets.get(name, set())
        out[name] = len(s - seen)
        seen |= s
    return out


def greedy_order(sets: Mapping[str, set[str]], k: int | None = None) -> list[tuple[str, int]]:
    """貪婪最大覆蓋：每次挑「現在能多帶最多新東西」的那一組。

    回傳 `[(名稱, 該步的新增數), ...]`。這是請求預算是硬約束時的選法——
    query 數有上限（每輪每條都要打請求），所以問題是「選 k 條最大化聯集」，
    而不是「哪幾條看起來合理」。

    ⚠️ 貪婪不保證最佳解（最大覆蓋是 NP-hard），但有 1−1/e 的近似保證，
    而且它的**每一步都看得見**——選誰、為什麼選、多帶了幾筆，全部在回傳值裡。
    平手時取名稱排序，讓結果可重現。
    """
    remaining = dict(sets)
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    limit = len(remaining) if k is None else min(k, len(remaining))
    for _ in range(limit):
        best_name, best_gain = None, -1
        for name in sorted(remaining):
            gain = len(remaining[name] - seen)
            if gain > best_gain:
                best_name, best_gain = name, gain
        if best_name is None:
            break
        out.append((best_name, best_gain))
        seen |= remaining.pop(best_name)
    return out


def group_union(outcomes: Sequence[VariantOutcome], group: str) -> set[str]:
    """某一組（例如舊查詢組合）的聯集候選鍵。

    **只算可觀測的那幾筆**：被擋的那一次不是「沒有貢獻」，是「不知道」。
    """
    out: set[str] = set()
    for o in outcomes:
        if o.variant.group == group and o.observable:
            out |= o.keys
    return out


def coverage_report(outcomes: Sequence[VariantOutcome]) -> dict[str, Any]:
    """整份研究的覆蓋率結論（純運算，吃 `run_study()` 的輸出）。"""
    sets = {o.variant.label: o.keys for o in outcomes if o.observable}
    unobservable = [o.variant.label for o in outcomes if not o.observable]
    marg = marginal_gains(sets)
    order = [o.variant.label for o in outcomes if o.observable]
    groups = sorted({o.variant.group for o in outcomes if o.variant.group})
    return {
        "union_candidates": union_size(sets.values()),
        "variants": len(sets),
        "unobservable": unobservable,
        "marginal_gains": dict(sorted(marg.items(), key=lambda kv: -kv[1])),
        "sequential_net_new": sequential_net_new(order, sets),
        "greedy_order": [
            {"label": name, "net_new": gain} for name, gain in greedy_order(sets)
        ],
        "groups": {g: _group_block(outcomes, g) for g in groups},
    }


def _group_block(outcomes: Sequence[VariantOutcome], group: str) -> dict[str, Any]:
    """單一分組的覆蓋率。

    **邊際貢獻在組內算，不在全體算**：把 old 與 new 混在一起算的話，new 裡
    一條好 query 的邊際貢獻會被 old 裡的重複條目吃掉，看起來像 0——然後你會
    照著那個 0 把它砍掉，而它其實正是要拿來取代 old 那條的。同一個詞在不同
    參考集合下是不同的量，報告要把參考集合講清楚。
    """
    mine = [o for o in outcomes if o.variant.group == group and o.observable]
    sets = {o.variant.label: o.keys for o in mine}
    return {
        "labels": [o.variant.label for o in outcomes if o.variant.group == group],
        "union_candidates": union_size(sets.values()),
        "requests": sum(o.requests for o in outcomes if o.variant.group == group),
        "marginal_gains": dict(
            sorted(marginal_gains(sets).items(), key=lambda kv: -kv[1])
        ),
        "greedy_order": [
            {"label": n, "net_new": g} for n, g in greedy_order(sets)
        ],
    }


# ---------------------------------------------------------------------------
# 實跑
# ---------------------------------------------------------------------------
def load_variants(spec: Mapping[str, Any]) -> list[Variant]:
    """研究設定檔（YAML/JSON）→ `Variant` 清單。缺欄位就是設定錯，直接拋。

    這裡**刻意不容錯**：研究要花真實請求，一條設定寫錯卻被靜默跳過的話，
    產出的覆蓋率表會少一列而沒有人會發現，然後拿它去砍 query。
    """
    out: list[Variant] = []
    for i, raw in enumerate(spec.get("variants") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"variants[{i}] 不是對映表：{raw!r}")
        for key in ("label", "source"):
            if not raw.get(key):
                raise ValueError(f"variants[{i}] 缺 {key}")
        if "keyword" not in raw:
            raise ValueError(f"variants[{i}] 缺 keyword（空字串是合法值，但必須寫出來）")
        cat = raw.get("category")
        out.append(
            Variant(
                label=str(raw["label"]),
                source=str(raw["source"]),
                keyword=str(raw["keyword"]),
                category=str(cat) if cat not in (None, "") else None,
                group=str(raw.get("group") or ""),
                pages=max(1, int(raw.get("pages", 1))),
            )
        )
    labels = [v.label for v in out]
    dupes = {x for x in labels if labels.count(x) > 1}
    if dupes:
        raise ValueError(f"variants 的 label 重複：{sorted(dupes)}——覆蓋率表會對不上")
    return out


def run_variant(
    cfg: Config,
    registry: Mapping[str, Any],
    variant: Variant,
    *,
    max_price: float | None = None,
) -> VariantOutcome:
    """跑一組設定。來源之間互相隔離（`run_source_search` 保證不往外拋）。"""
    src = registry.get(variant.source)
    if src is None:
        return VariantOutcome(
            variant=variant, health="missing", observable=False,
            parsed=0, listings=0, detail="registry 沒有這個來源",
        )
    t0 = time.perf_counter()
    res = run_source_search(
        variant.source, src, variant.keyword,
        pages=variant.pages, max_price=max_price, category=variant.category,
    )
    elapsed = time.perf_counter() - t0

    items: dict[str, str] = {}
    rejects: dict[str, int] = {}
    for lst in res.listings:
        info = parse_card(lst.title, cfg.watchlist)
        ok, why = is_candidate(info, cfg.watchlist)
        if ok:
            items[lst.key] = lst.title
        else:
            rejects[why] = rejects.get(why, 0) + 1

    return VariantOutcome(
        variant=variant,
        health=res.health.value,
        observable=res.health in _OBSERVABLE,
        parsed=res.parsed_count,
        listings=len(res.listings),
        items=items,
        rejects=rejects,
        elapsed_seconds=elapsed,
        # 一組設定的請求數 = 實際翻到的頁數（抓取層的節流是逐請求的）
        requests=max(res.pages_fetched, 0) or variant.pages,
        url=res.url,
        detail=res.detail,
    )


def run_study(
    cfg: Config,
    registry: Mapping[str, Any],
    variants: Sequence[Variant],
    *,
    max_price_for: Any = None,
    progress: Any = None,
) -> dict[str, Any]:
    """跑完整份研究，回傳可落檔的 dict。

    `max_price_for` 是 `(site) -> float | None`，讓研究能用**與實際掃描相同**
    的價格上限（同源同基準：不套上限量到的覆蓋率，拿去推論實際掃描會看到什麼
    就是換了一把尺）。傳 None = 完全不套上限。
    """
    outcomes: list[VariantOutcome] = []
    for v in variants:
        src = registry.get(v.source)
        cap = None
        if max_price_for is not None and src is not None:
            cap = max_price_for(src.site)
        out = run_variant(cfg, registry, v, max_price=cap)
        outcomes.append(out)
        if progress is not None:
            progress(out)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "total_requests": sum(o.requests for o in outcomes),
        "variants": [o.to_dict() for o in outcomes],
        "coverage": coverage_report(outcomes),
    }


def save_report(report: Mapping[str, Any], path: Path) -> Path:
    """落檔（JSON）。前後對照要能比對，所以研究結果一定要留得下來。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


__all__ = [
    "Variant",
    "VariantOutcome",
    "coverage_report",
    "greedy_order",
    "group_union",
    "load_variants",
    "marginal_gains",
    "run_study",
    "run_variant",
    "save_report",
    "sequential_net_new",
    "union_size",
]
