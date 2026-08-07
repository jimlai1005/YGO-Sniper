"""需求驅動的行情回補（refill）。

## 要解的病（2026-08-02 量化）

競標標的的 L3 裡，絕大多數是「卡名已成功比對、in_era、但該卡在 comps 庫裡
0 筆成交」——出價上限的證據閘門要求 L1/L2（同卡成交），所以這批全部拿不到
上限、不可行動。廣撒式的 `comps_queries`（稀有度 × 年代 × 機構組合展開）
已經到頂：它撈的是熱門屬性組合裡的熱門卡，冷門卡永遠輪不到。

這裡反過來走：**從掃到的競標標的出發**，找出「卡名比對得到、in_era、
但同卡成交 < `min_comps` 筆」的卡，用該卡的日文卡名（主檔 `name_ja`）
對已售出來源做針對性查詢。查的是需求端真的缺的卡，不是屬性空間的格子。

## 第二個需求來源：監控賣家的定價上架（2026-08-07）

逐筆重放 80 筆估不了的監控賣家上架：27 筆的卡在 comps 一筆成交都沒有。
根因：佇列只吃 live auction 標題，而監控賣家的上架 62/80 是定價
（paypay 48＋ebay 14），結構上永遠進不了佇列。所以 `run_refill` 多收一組
`watch_titles`（監控賣家掃出來的**定價**上架；競標的那部分走原本的 `titles`）。
兩組需求**先合併、再過同一道冷卻濾網與 max_cards 截斷**——新來源結構上
繞不過任何一道節流，每輪請求上限不變。一般關鍵字掃描的定價上架**刻意不進**
（會把佇列衝大，另議）；來源計數分開記在 `RefillCandidate.auction_n /
watch_fixed_n`，之後查「refill 有沒有在吃新來源」不用猜。

## 節流是硬約束，不是禮貌

- 每輪最多 `max_cards_per_run` 張卡（預設 10）。
- 每卡每來源固定 **1 頁**（`REFILL_PAGES`，刻意不做成 config：需求驅動查詢
  的價值在「查對卡」，不在「挖深」——要挖深是廣撒那條線的事）。
- 同一張卡回補後 `cooldown_days` 天內不重查（預設 7）：查過還是 0 筆代表
  市場上就是沒有近期成交，短期重查只是浪費請求。**查無結果也要記帳**，
  不然每輪都會把零結果的卡重查一遍。
- 節流帳落在 store 的 `comps_refill` 表（跨行程冪等）——CLI 每輪都是新行程,
  記憶體變數的節流在這個部署形態下等於沒有節流（與 `claim_sold_run` 同理）。
- 與廣撒式的 `claim_sold_run` 節流**互相獨立**：廣撒一天 1-2 次照舊，
  回補每輪掃描都可以跑（量小且有自己的節流）。

## 記帳的前提：至少一個來源真的完成觀測

被擋／斷線／解析壞是「什麼都不知道」，不是「市場沒貨」。全部來源都失敗
還記帳，等於把一次 WAF 挑戰當成「這張卡沒有行情」冷卻七天——
**讀不到 ≠ 沒有**（與 pipeline 的 `_OBSERVABLE`、store 的離場判定同一個立場）。
所以只有 OK / EMPTY_CONFIRMED 的觀測才算數；全滅的卡不落帳，下輪重試。

## 入庫走既有的 `ingest_sold`

同一組 `parse_card + is_candidate` 過濾、同一個 `UNIQUE(signature, url)` 去重
（工程原則 1：同源同基準）。針對性查詢撈回來的東西大多過不了濾網
（沒寫年代詞、沒有鑑定機構、分數低於門檻）——那正是濾網該做的事，
不另開後門。
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .cards import CardIndex
from .comps import CompsEngine
from .sources.health import ParseHealth, SearchResult

#: 每卡每來源固定 1 頁。這不是 config：需求驅動查詢的價值在「查對卡」不在挖深。
REFILL_PAGES = 1

#: config 預設值（watchlist.yaml 的 `refill:` 區塊可覆寫）。
DEFAULT_MIN_COMPS = 3
DEFAULT_MAX_CARDS_PER_RUN = 10
DEFAULT_COOLDOWN_DAYS = 7.0
#: yahoo_closed 為主（結標成交、有真實時間、純 httpx），paypay_direct 與
#: buyee_mercari 的 sold 為輔（前者產出率低但時間是真的；後者要 Playwright
#: 解 WAF 且頁面沒有成交時間——價格是真的，sold_at 會被標成入庫時間）。
DEFAULT_SOURCES: tuple[str, ...] = ("yahoo_closed", "paypay_direct", "buyee_mercari")

#: 「這一次觀測可以採信」的健康碼。ok（有貨）與 empty（確認沒貨）都是看過了；
#: 其餘是「什麼都不知道」，不能拿去記節流帳（見模組頂註）。
_OBSERVABLE = (ParseHealth.OK, ParseHealth.EMPTY_CONFIRMED)


def _int_setting(spec: Mapping, key: str, default: int) -> int:
    raw = spec.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"[warn] refill.{key}={raw!r} 不是整數，改用 {default}")
        return default
    if value < 0:
        print(f"[warn] refill.{key}={value} 是負數，改用 {default}")
        return default
    return value


@dataclass(slots=True, frozen=True)
class RefillParams:
    """回補的節流參數。非法值退回預設並印警告（不靜默）——
    一個被打錯的門檻會讓回補在使用者不知情的狀況下狂打請求。"""

    enabled: bool = True
    min_comps: int = DEFAULT_MIN_COMPS
    max_cards_per_run: int = DEFAULT_MAX_CARDS_PER_RUN
    cooldown_days: float = DEFAULT_COOLDOWN_DAYS
    sources: tuple[str, ...] = DEFAULT_SOURCES

    @classmethod
    def from_config(cls, cfg: Any) -> RefillParams:
        spec = dict((getattr(cfg, "watchlist", None) or {}).get("refill") or {})
        try:
            cooldown = float(spec.get("cooldown_days", DEFAULT_COOLDOWN_DAYS))
        except (TypeError, ValueError):
            print(
                f"[warn] refill.cooldown_days={spec.get('cooldown_days')!r} 不是數字，"
                f"改用 {DEFAULT_COOLDOWN_DAYS}"
            )
            cooldown = DEFAULT_COOLDOWN_DAYS
        raw_sources = spec.get("sources")
        sources = (
            tuple(str(s) for s in raw_sources if str(s).strip())
            if isinstance(raw_sources, (list, tuple)) else DEFAULT_SOURCES
        )
        return cls(
            enabled=bool(spec.get("enabled", True)),
            min_comps=_int_setting(spec, "min_comps", DEFAULT_MIN_COMPS),
            max_cards_per_run=_int_setting(
                spec, "max_cards_per_run", DEFAULT_MAX_CARDS_PER_RUN
            ),
            cooldown_days=cooldown,
            sources=sources,
        )


@dataclass(slots=True, frozen=True)
class RefillCandidate:
    """一張等著被回補的卡：庫裡幾筆成交、幾筆在架標的等著它。

    `listings_n` 是**兩個需求來源的合計**（排序、截斷都用它）；
    `auction_n`／`watch_fixed_n` 把來源分開記——「refill 有沒有在吃
    監控賣家這條新需求線」要能從帳上直接讀出來，不用猜。
    """

    card_name: str
    comps_n: int
    listings_n: int
    #: 需求來自幾筆競標中標的（原始來源，任何發現管道）。
    auction_n: int = 0
    #: 需求來自幾筆**監控賣家的定價上架**（2026-08-07 新增的來源）。
    watch_fixed_n: int = 0


def comps_count_by_card(rows: list[dict[str, Any]], index: CardIndex) -> dict[str, int]:
    """comps 列 → 每張卡（年代內）幾筆成交。

    **與估價的 L1/L2 池同一把尺**（工程原則 1）：卡名一律由 `index.match(title)`
    在查詢時決定，不讀 `card_name` 欄位（那是物化快取，可能是舊比對規則寫的）。
    這裡數出來的「n 筆」就是 `ValuationModel.predict_log` 的 pool2 上限——
    換一把尺數，就會出現「回補說夠了、估價說沒有」的分岔。
    """
    counts: Counter[str] = Counter()
    for r in rows:
        m = index.match(r.get("title") or "")
        if m is not None and m.in_era:
            counts[m.name_ja] += 1
    return dict(counts)


def select_refill_cards(
    titles: Iterable[str],
    index: CardIndex,
    comps_counts: Mapping[str, int],
    *,
    min_comps: int,
    max_cards: int,
    cooling: frozenset[str] | set[str] = frozenset(),
    watch_titles: Iterable[str] = (),
) -> tuple[list[RefillCandidate], list[RefillCandidate]]:
    """從標的標題選出要回補的卡。回傳 (入選, 因冷卻被跳過)。純函式，可單測。

    需求有兩個來源：`titles`（競標中標的）與 `watch_titles`（監控賣家的
    定價上架，見模組頂註）。**先合併需求、再過冷卻濾網與 max_cards 截斷**
    ——節流對兩個來源一視同仁，新來源結構上繞不過任何一道。

    入選判準（三個都要）：卡名比對得到、in_era、同卡成交 < `min_comps`。
    比不到卡名的標題**不選**——沒有卡名就沒有可查詢的關鍵字，那是
    卡名比對那條線的工作，不是回補能救的。

    排序決定性（同一份輸入永遠選同一批）：等著這張卡的標的越多越優先
    （兩來源合計）、庫裡樣本越少越優先、最後按卡名排 tie-break。
    截斷發生在尾端，max_cards 改小時砍掉的是最不缺的那幾張。
    """
    demand_auction: Counter[str] = Counter()
    demand_watch: Counter[str] = Counter()
    for bucket, batch in ((demand_auction, titles), (demand_watch, watch_titles)):
        for title in batch:
            m = index.match(title or "")
            if m is not None and m.in_era:
                bucket[m.name_ja] += 1

    selected: list[RefillCandidate] = []
    cooled: list[RefillCandidate] = []
    for name in {*demand_auction, *demand_watch}:
        n = int(comps_counts.get(name, 0))
        if n >= min_comps:
            continue
        auction_n = demand_auction.get(name, 0)
        watch_n = demand_watch.get(name, 0)
        cand = RefillCandidate(
            card_name=name, comps_n=n, listings_n=auction_n + watch_n,
            auction_n=auction_n, watch_fixed_n=watch_n,
        )
        (cooled if name in cooling else selected).append(cand)

    selected.sort(key=lambda c: (-c.listings_n, c.comps_n, c.card_name))
    cooled.sort(key=lambda c: (-c.listings_n, c.comps_n, c.card_name))
    if max_cards >= 0:
        selected = selected[:max_cards]
    return selected, cooled


@dataclass(slots=True)
class RefillReport:
    """一輪回補的完整帳。發了幾個查詢、收了幾筆、擋了幾筆、誰被冷卻跳過——
    跟 `IngestReport` 同一個立場：沒有這份帳，回補失效時外顯就是安靜的 0。"""

    selected: list[RefillCandidate] = field(default_factory=list)
    skipped_cooldown: list[RefillCandidate] = field(default_factory=list)
    #: (卡, 來源) 查詢：發出幾個、其中幾個完成觀測（ok/empty）
    queries: int = 0
    queries_ok: int = 0
    #: 對外請求數（各查詢實際抓的頁數合計；每查詢上限 REFILL_PAGES=1）
    requests: int = 0
    #: 來源回傳的已售出筆數（過濾前）
    fetched: int = 0
    kept: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    #: 每張卡的結果：{"found": 過濾前, "kept": 入庫, "observed": 有沒有記帳}
    per_card: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    def summary(self) -> str:
        top = ", ".join(
            f"{k}×{v}" for k, v in sorted(self.reasons.items(), key=lambda kv: -kv[1])[:3]
        )
        parts = [
            f"回補 {len(self.selected)} 張卡",
            f"查詢 {self.queries}（完成觀測 {self.queries_ok}）",
            f"請求 {self.requests}",
            f"收 {self.kept} 筆、擋 {self.rejected} 筆" + (f"（{top}）" if top else ""),
        ]
        # 新需求來源（監控賣家定價上架）有沒有在動，log 上要直接看得到。
        watch_cards = sum(1 for c in self.selected if c.watch_fixed_n)
        if watch_cards:
            parts.append(f"含監控定價需求 {watch_cards} 張")
        if self.skipped_cooldown:
            parts.append(f"冷卻中跳過 {len(self.skipped_cooldown)} 張")
        if self.errors:
            parts.append(f"來源失敗 {len(self.errors)} 次")
        return "｜".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected": [c.card_name for c in self.selected],
            # 每張入選卡的需求來源計數：auction（競標中標的）／
            # watch_fixed（監控賣家定價上架）。查「新來源有沒有被吃」用這格。
            "selected_origin": {
                c.card_name: {"auction": c.auction_n, "watch_fixed": c.watch_fixed_n}
                for c in self.selected
            },
            "skipped_cooldown": [c.card_name for c in self.skipped_cooldown],
            "queries": self.queries,
            "queries_ok": self.queries_ok,
            "requests": self.requests,
            "fetched": self.fetched,
            "kept": self.kept,
            "rejected": self.rejected,
            "reasons": dict(self.reasons),
            "errors": list(self.errors),
            "elapsed_seconds": self.elapsed_seconds,
            "dry_run": self.dry_run,
        }


def _sold_search(src: Any, source_name: str, keyword: str) -> SearchResult:
    """單一 (來源, 卡名) 的已售出搜尋。**任何情況都回 SearchResult，不往外拋**
    （與 `pipeline.run_source_search` 同一個隔離立場：一條管道壞掉只污染自己）。

    走 `search_detailed` 而不是 `search()`：回補要靠 health 分辨
    「確認沒有成交」（可記帳）與「被擋／解析壞」（不可記帳），
    `search()` 的空清單把兩者混在一起。
    """
    site_value = getattr(getattr(src, "site", None), "value", "") or ""
    try:
        if hasattr(src, "search_detailed"):
            return src.search_detailed(keyword, sold=True, pages=REFILL_PAGES)
        listings = src.search(keyword, sold=True, pages=REFILL_PAGES)
        res = SearchResult(
            source=source_name, site=site_value, query=keyword,
            listings=listings, parsed_count=len(listings), pages_fetched=REFILL_PAGES,
        )
        return res
    except Exception as exc:  # noqa: BLE001 - 隔離邊界，見 docstring
        health = ParseHealth.PARSER_BROKEN
        name = type(exc).__name__
        if name == "BlockedError":
            health = ParseHealth.BLOCKED
        elif name == "FetchError":
            health = ParseHealth.FETCH_FAILED
        return SearchResult(
            source=source_name, site=site_value, query=keyword,
            health=health, detail=f"{name}: {exc}",
        )


def run_refill(
    *,
    store: Any,
    sources: Mapping[str, Any],
    comps: CompsEngine,
    index: CardIndex,
    titles: Iterable[str],
    params: RefillParams,
    watch_titles: Iterable[str] = (),
    dry_run: bool = False,
) -> RefillReport:
    """跑一輪需求驅動回補。`titles` 是等著行情的競標標的標題；
    `watch_titles` 是監控賣家掃出來的**定價**上架標題（第二個需求來源，
    見模組頂註）。兩組合併後走同一套選卡與節流，只有來源計數分開記。

    `dry_run=True` 只做選卡（不打外網、不記帳、不入庫）——給「看看會查誰」用。

    每張卡逐來源查詢，(卡, 來源) 各自隔離：一條管道壞掉只影響它自己那格。
    一張卡**至少一個來源完成觀測**（ok/empty）才落節流帳；全滅的卡不落帳，
    下輪重試（讀不到 ≠ 沒有）。
    """
    report = RefillReport(dry_run=dry_run)
    if not params.enabled:
        return report
    if not index.available:
        # 沒有主檔就沒有卡名可查。安靜降級與 CardIndex 一致，但帳上要看得見。
        report.errors.append("卡片主檔不存在（refresh-cards 未跑過），回補無法選卡")
        return report

    comps_counts = comps_count_by_card(store.comps_by(limit=100000), index)
    cooling = store.refill_cooldown_active(params.cooldown_days)
    selected, cooled = select_refill_cards(
        titles, index, comps_counts,
        min_comps=params.min_comps,
        max_cards=params.max_cards_per_run,
        cooling=cooling,
        watch_titles=watch_titles,
    )
    report.selected = selected
    report.skipped_cooldown = cooled
    if dry_run or not selected:
        return report

    started = time.monotonic()
    target_sources: list[tuple[str, Any]] = []
    for name in params.sources:
        src = sources.get(name)
        if src is None or not getattr(src, "supports_sold", False):
            report.errors.append(f"來源 {name} 不在 registry 或不支援 sold，跳過")
            continue
        target_sources.append((name, src))

    for cand in selected:
        found = 0
        kept = 0
        observed = False
        for source_name, src in target_sources:
            res = _sold_search(src, source_name, cand.card_name)
            report.queries += 1
            report.requests += res.pages_fetched
            if res.health not in _OBSERVABLE:
                report.errors.append(
                    f"{source_name} 「{cand.card_name}」{res.health.value}：{res.detail}"
                )
                continue
            report.queries_ok += 1
            observed = True
            found += len(res.listings)
            ingest = comps.ingest_sold(res.listings)
            kept += ingest.kept
            report.rejected += ingest.rejected
            for reason, n in ingest.reasons.items():
                report.reasons[reason] = report.reasons.get(reason, 0) + n
        report.fetched += found
        report.kept += kept
        report.per_card[cand.card_name] = {
            "found": found, "kept": kept, "observed": observed,
        }
        if observed:
            # 查無結果也要記帳：0 筆代表市場上就是沒有，短期重查只是浪費。
            store.record_refill(cand.card_name, found=found, kept=kept)

    report.elapsed_seconds = round(time.monotonic() - started, 1)
    return report
