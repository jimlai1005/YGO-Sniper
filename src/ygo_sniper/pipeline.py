"""每日掃描主流程。

順序刻意設計成「先累積行情，再判斷折價」：
comps 是判斷的基礎，如果先掃在架標的再抓成交價，
第一天的訊號會全部因為 THIN_COMPS 被降權，那就白跑了。
"""

from __future__ import annotations

import json
from datetime import datetime

from .alerts import HEALTH_SEVERITY, Alert, AlertEngine
from .bidding import is_live_auction
from .comps import CompsEngine
from .config import Config, load_config
from .costs import max_item_price_jpy
from .domain import Site
from .fx import FxRates
from .notify import TelegramNotifier
from .parsers import is_candidate, parse_card
from .queries import load_queries, resolve_category
from .schedule_watch import (
    PENDING_ALERT_KEY,
    RUN_FINISHED_KEY,
    RUN_STARTED_KEY,
    resolve_alert,
    schedule_health,
    watchdog_message,
)
from .scoring import evaluate, is_triggered, seller_histogram
from .sources import CachedFetcher, build_sources
from .sources.base import BlockedError, FetchError
from .sources.health import ParseHealth, SearchResult
from .store import Store
from .valuation import build_valuator, estimate_listing
from .venue_study import listing_row

#: 「這一輪對這條管道的觀測可以採信嗎」。只有這兩種健康碼算數：
#: ok（有貨）與 empty（確認沒貨）都是**看過了**；被擋／解析壞／連線失敗是
#: 「什麼都不知道」，拿它去推論「標的不見了」就會把一次 WAF 挑戰記成整站賣光。
_OBSERVABLE = (ParseHealth.OK, ParseHealth.EMPTY_CONFIRMED)

#: 摘要聚合時「誰比較嚴重」的排序。一個管道跑多條 query 時，
#: 摘要顯示最嚴重的那次——被擋一次比其他 query 正常更值得被看見。
#: 定義只有一份（alerts.HEALTH_SEVERITY），摘要與告警共用，
#: 避免「摘要說 blocked、告警按 fetch_failed 處理」這種同源不同基的錯（工程原則 1）。
_HEALTH_SEVERITY = HEALTH_SEVERITY

#: canary 判定用的健康碼：這幾種是抓取層自己已經確診的病，
#: canary 不做二次判定（也不該重複告警）。
_CANARY_SKIP = {ParseHealth.BLOCKED, ParseHealth.FETCH_FAILED, ParseHealth.PARSER_BROKEN}


def canary_verdict(res: SearchResult, min_results: int) -> SearchResult | None:
    """canary 判定（純函式，可單測）：回傳「要追加的 SearchResult」或 None。

    canary 用一個必定大量命中的關鍵字（「遊戯王」）自檢，所以
    **解析數過少本身就是證據**——EMPTY_CONFIRMED 也算壞掉（這個字不可能真的沒貨）。
    這是唯一不會跟著對方改版一起壞的判準：解析器改版後往往「解析成功但 0 筆」，
    命中數交叉比對可能一起失效，canary 不會。

    ⚠️ **判定看 `parsed_count`（解析器解出幾個商品），不看 `len(listings)`。**
    2026-08-01 事故：原本數的是 listings，而那是**商業篩選後**的數量——
    Yahoo 的 `include_live_auctions=false` 會丟掉所有純競標標的，改新着排序後
    「遊戯王」最新上架的 50 筆大多是剛開的 ¥1 起標無即決，於是同一個查詢連續
    三次得到 22 / 18 / 1 筆（波動 22 倍），而解析器每次都健康地解出 50 個商品
    區塊。結果是 12 次假的 `yahoo_direct:parser_broken`。
    **量錯東西的指標比沒有指標更糟**：它把「市場今天長這樣」報成「工具壞了」，
    而假警報吵久了，真的壞掉那一次會被直接忽略——那正是這套健康判定要解的病。
    所以 `min_results`（`canary_min_results`）的語意是「解析器至少要解出幾個
    商品」，與「有幾筆我買得下去」無關。

    抓取層自己已確診的病（被擋／斷線／解析壞）原樣回傳，不做二次判定。
    """
    if res.health in _CANARY_SKIP:
        return res
    n = res.parsed_count
    if n >= min_results:
        return None
    return SearchResult(
        source=res.source,
        site=res.site,
        query=res.query,
        health=ParseHealth.PARSER_BROKEN,
        pages_fetched=res.pages_fetched,
        url=res.url,
        html_bytes=res.html_bytes,
        parsed_count=n,
        detail=(
            f"canary 「{res.query}」解析器只解出 {n} 個商品（門檻 {min_results}；"
            f"商業篩選後剩 {len(res.listings)} 筆，篩選數不參與判定）"
            f"——這個關鍵字不可能真的沒貨，解析器很可能已失效"
        ),
    )


def dedupe_listings(results: list[SearchResult]) -> list:
    """把多趟抓取的結果併成一份清單，用 `Listing.key` 去重（保序，先到者留）。

    為什麼要有這一步：Yahoo 現在跑兩趟（新着＋即將結標），同一個標的可能兩趟
    都出現（剛上架又剛好快結標的、或翻頁時的推廣位）。不去重的話它會被評分、
    落庫、算進「掃到幾筆」各兩次——`scanned` 這個數字會憑空膨脹，而
    `Store.upsert_signal` 會把第二次當成「又看到一次」。
    去重鍵用 `Listing.key`（site:external_id），與 store 的主鍵同一份定義，
    不另外拍一個「標題＋價格」之類的近似鍵（工程原則 1）。
    """
    seen: set[str] = set()
    out = []
    for res in results:
        for lst in res.listings:
            if lst.key in seen:
                continue
            seen.add(lst.key)
            out.append(lst)
    return out


def _optional_kwargs(
    source_name: str, src, *, category: str | None, sort: str | None
) -> dict:
    """只把來源真的支援的選用參數傳下去。

    `sort` 與 `category` 都不是每個 source 都有——無條件傳的話，不支援的
    source 會直接 TypeError，被隔離邊界包成 `parser_broken`，於是一個「設定
    寫了但這條管道用不到」的小事，外顯成「解析器壞了」。

    **但支援與否要用宣告的屬性判斷，不是靠 try/except 吞掉 TypeError。**
    2026-08-03 事故：舊版只把 `category_id` 傳給「沒有 `search_detailed` 的舊
    介面」，而 Buyee 有 `search_detailed`——分類 ID 在 watchlist 裡設好了、
    在 `_scan_source` 裡查出來了，然後在這一行被安靜地丟掉，一年都沒人發現，
    因為「分類沒生效」與「分類生效了但今天貨就是這些」外顯一模一樣。
    `sort` 的支援判斷維持原本的 `sort is not None`（只有 Yahoo 會被傳 sort，
    那是呼叫端 `_scan_source_passes` 明確決定的）。
    """
    out: dict = {}
    if sort is not None:
        out["sort"] = sort
    if category:
        if getattr(src, "supports_category", False):
            out["category"] = category
        else:
            print(
                f"[warn] {source_name} 不支援分類參數（supports_category 未宣告），"
                f"category={category!r} 這一條退化成純關鍵字搜尋"
            )
    return out


def run_source_search(
    source_name: str,
    src,
    keyword: str,
    *,
    pages: int,
    max_price: float | None = None,
    category: str | None = None,
    sort: str | None = None,
    seller: str | None = None,
) -> SearchResult:
    """單一 (source, query) 的抓取＋解析。**任何情況都回 SearchResult，不往外拋。**

    這是來源隔離的唯一實作點（掃描、canary、dashboard 的關鍵字搜尋、賣家頁列舉
    共用同一份）：
    一條管道壞掉（被擋、斷線、改版、甚至 source 程式自己有 bug），只污染自己的
    SearchResult，其他管道照常產出——這是本專案的核心約束。
    FetchError/BlockedError 轉對應健康碼；其他例外一律 PARSER_BROKEN
    （對呼叫端而言「來源程式炸了」與「解析壞了」同一種需要告警的病）。
    """
    site_value = src.site.value
    try:
        if seller is not None:
            # 賣家頁列舉。**支援與否由呼叫端先問過**（seller_watch.SELLER_PAGE_SOURCE
            # ＋ hasattr），走到這裡就一定有這個方法；沒有的話讓 AttributeError
            # 落進下面的隔離邊界，而不是安靜地退化成關鍵字搜尋。
            return src.search_seller(seller, pages=pages)
        extra = _optional_kwargs(source_name, src, category=category, sort=sort)
        if hasattr(src, "search_detailed"):
            return src.search_detailed(
                keyword, max_price=max_price, pages=pages, **extra
            )

        # 舊介面：search() 可能拋例外，包成 SearchResult
        listings = src.search(keyword, max_price=max_price, pages=pages, **extra)
        return SearchResult(
            source=source_name,
            site=site_value,
            query=keyword,
            listings=listings,
            # 舊介面沒有健康判定，也沒有商業篩選：解析數就是回傳數
            parsed_count=len(listings),
        )
    except BlockedError as exc:
        return SearchResult(
            source=source_name, site=site_value, query=keyword,
            health=ParseHealth.BLOCKED, detail=str(exc),
        )
    except FetchError as exc:
        return SearchResult(
            source=source_name, site=site_value, query=keyword,
            health=ParseHealth.FETCH_FAILED, detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 - 隔離邊界，見 docstring
        return SearchResult(
            source=source_name, site=site_value, query=keyword,
            health=ParseHealth.PARSER_BROKEN,
            detail=f"{type(exc).__name__}: {exc}",
        )


class Pipeline:
    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or load_config()
        self.fx = FxRates(self.cfg)
        self.store = Store(self.cfg.db_path)
        self.fetcher = CachedFetcher(self.cfg)
        self.sources = build_sources(self.cfg, self.fetcher)
        self.comps = CompsEngine(self.cfg, self.fx, self.store)
        self.notifier = TelegramNotifier(self.cfg)
        self.alerts = AlertEngine(self.cfg, self.store)
        #: 估價模型：**只有掃到競標標的才會建**（要載 758KB 卡片主檔 ＋ 擬合模型）。
        #: 一輪掃描裡建一次就好，但必須在 refresh_comps 之後才建，否則新收進來的
        #: 成交樣本不會進到這一輪的公允價裡。懶建剛好滿足這兩件事。
        self._valuator = None
        #: 狙擊比對器（一輪掃描建一次；lazy——沒有 active watch 時是空 list）。
        self._snipe_cache = None
        #: dry_run 時不寫狙擊帳（`_scan` 設定；預設 True 讓單獨呼叫也能寫）。
        self._snipe_write = True
        #: 這一輪狙擊比對的觀測帳（`_scan` 開頭歸零）。**「比對過幾筆」與「命中
        #: 幾筆」必須分開報**：掛鉤壞掉（matchers 回空、observe_listings 沒被叫到）
        #: 時命中恆為 0，而那與「今天市場上真的沒有那張卡」外顯一模一樣
        #: （CLAUDE.md 第五節）。compared 很大 ＋ hits 0 ＝ 比對跑了但沒貨；
        #: compared 0 ＝ 比對根本沒跑。
        self._snipe_stats = {"compared": 0, "hits": 0}
        #: 排程空窗告警（`scan()` 開頭填；`None` = 沒偵測到問題，也可能是
        #: 這輪還沒跑過 scan()——初始化在這裡而不是只在 scan() 裡設，
        #: 讓呼叫端用 `getattr` 都不必也能安全讀到 None，見 schedule_watch.py）。
        self._schedule_alert: str | None = None

    # ------------------------------------------------------------------
    def valuator(self):
        """出價上限要用的估價模型。第一次被問到才建，之後重用。"""
        if self._valuator is None:
            self._valuator = build_valuator(self.cfg, self.store)
        return self._valuator

    # ------------------------------------------------------------------
    def _price_ceiling_jpy(self, site: Site) -> float | None:
        """把「到手成本 = 鑑定費」反推出的卡價上限交給平台去過濾。

        用最寬鬆的那條路徑（通常是集運攤提後），避免過度過濾。
        再乘 2.5 給 discount trigger 一點空間 —— 有些卡即使超過鑑定費，
        只要相對行情夠便宜還是值得看。
        """
        routes = self.cfg.routes_for_site(site.value)
        if not routes:
            return None
        ceilings = [
            max_item_price_jpy(r, self.cfg.grading_fee_twd, self.fx) for r in routes
        ]
        top = max(ceilings)
        return round(top * 2.5) if top > 0 else None

    # ------------------------------------------------------------------
    def refresh_comps(self, *, force: bool = False) -> int:
        """跑「已售出」搜尋，累積日本市場真實成交價。

        **每輪只跑一小片，`comps_queries.every_n_runs` 輪走完一整份**
        （見 `CompsEngine.sold_shard`）：行情是以週為單位在變的，但排程
        每小時跑一輪，已售出查詢展開後有數十個關鍵字 × 數頁，舊制「每
        every_n_runs 輪一次全跑」會把整份查詢擠成一次幾百請求的尖峰——
        這正是全 log 唯一與硬 blocked 同輪出現過的批次形態。分片把同樣的
        總請求量攤平成每輪一小口，對方看到的是穩定小流量。

        每條 (source, query) 各自隔離：任何一個來源壞掉（現階段 Buyee 系
        的 stub fetcher 就是必拋 BlockedError），只跳過它自己，
        不能拖垮整輪 comps 更新、更不能拖垮後面的掃描。游標只在**這一片
        至少一條查詢成功**時推進（`commit_sold_shard`）——整片全失敗時
        再分兩種：全是 `BlockedError`（semantic，對方剛拒絕過我們，重試
        沒有意義）立刻推進；其餘（transient，例如逾時）原地重試，
        連續三輪才強制推進（工程原則 2）。

        回傳「真的入庫幾筆」（int，CLI 直接印）；擋掉的筆數與原因分布
        印在 log —— 過濾器自己也會壞，擋掉 100% 跟擋掉 0% 一樣需要被看見。
        """
        shard = self.comps.sold_shard(self.sources, force=force)
        if not shard.queries:
            print("[comps] 已售出查詢：展開後為空，跳過")
            self.comps.load_from_store()
            return 0

        pages = self.comps.sold_pages
        suffix = f"（{shard.label}）" if shard.label else ""
        print(f"[comps] 跑 {len(shard.queries)} 個已售出查詢 × 最多 {pages} 頁{suffix}")

        total = 0
        rejected = 0
        reasons: dict[str, int] = {}
        any_success = False
        blocked_failures = 0
        other_failures = 0
        for source_name, keyword in shard.queries:
            src = self.sources[source_name]
            try:
                sold = src.search(keyword, sold=True, pages=pages)
            except Exception as exc:  # noqa: BLE001 - 隔離是刻意的，見 docstring
                print(
                    f"[warn] comps {source_name} 「{keyword}」失敗，跳過："
                    f"{type(exc).__name__}: {exc}"
                )
                if isinstance(exc, BlockedError):
                    blocked_failures += 1
                else:
                    other_failures += 1
                continue
            any_success = True
            report = self.comps.ingest_sold(sold)
            total += report.kept
            rejected += report.rejected
            for reason, n in report.reasons.items():
                reasons[reason] = reasons.get(reason, 0) + n
        if rejected:
            top = ", ".join(
                f"{k}×{v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]
            )
            print(f"[comps] 收 {total} 筆、擋 {rejected} 筆（{top}）")
        self.comps.commit_sold_shard(
            shard,
            any_success=any_success,
            # 「有被擋、且沒有其他種類的失敗」——這個表達式本身在有成功查詢時
            # 也會是 True（any_success=True 時 blocked_failures 可能仍 >0、
            # other_failures 仍 ==0），單看這行不足以保證「純被擋」。
            # 它今天是安全的，是因為 commit_sold_shard 先檢查 any_success
            # 才看 blocked（那個分支順序不在這裡，改動它時要記得這裡依賴它）。
            blocked=blocked_failures > 0 and other_failures == 0,
        )
        self.comps.load_from_store()
        return total

    # ------------------------------------------------------------------
    def _refill_comps(
        self,
        auction_titles: list[str],
        watch_fixed_titles: list[str] | None = None,
    ) -> dict | None:
        """跑一輪需求驅動回補，回傳報告 dict（沒東西可做時回 None）。

        需求端兩組：`auction_titles`（競標中標的，任何發現管道）與
        `watch_fixed_titles`（監控賣家掃出來的定價上架，refill.py 頂註的
        第二個來源）。兩組在 run_refill 裡合併後走同一套節流。

        任何失敗都不准拖垮掃描（隔離邊界，與 refresh_comps 同一立場）：
        回補是加分項，掃描是主業。收進新樣本時把 comps 視圖與懶建的估價模型
        一起刷新——不刷新的話，這一輪的公允價還是看不到剛收進來的同卡成交，
        回補就白跑了（下一輪才生效 = 慢一小時）。
        """
        from .cards import CardIndex
        from .refill import RefillParams, run_refill

        watch_fixed_titles = watch_fixed_titles or []
        params = RefillParams.from_config(self.cfg)
        if not params.enabled or not (auction_titles or watch_fixed_titles):
            return None
        try:
            report = run_refill(
                store=self.store,
                sources=self.sources,
                comps=self.comps,
                index=CardIndex.load(),
                titles=auction_titles,
                watch_titles=watch_fixed_titles,
                params=params,
            )
        except Exception as exc:  # noqa: BLE001 - 隔離邊界，見 docstring
            print(f"[warn] 需求驅動回補失敗，跳過：{type(exc).__name__}: {exc}")
            return None
        if report.selected or report.skipped_cooldown:
            print(f"[refill] {report.summary()}")
        for err in report.errors:
            print(f"[warn] refill {err}")
        if report.kept:
            self.comps.load_from_store()
            self._valuator = None  # 下次要用時以含新樣本的行情重建
        return report.to_dict()

    # ------------------------------------------------------------------
    def _scan_source(
        self,
        source_name: str,
        src,
        keyword: str,
        *,
        pages: int | None = None,
        price_ceiling: bool = True,
        sort: str | None = None,
        category: str | None = None,
    ) -> SearchResult:
        """掃描用的 `run_source_search` 包裝：補上價格上限與分類參數。

        `pages=None` 表示「照這個來源的設定翻頁」（`Config.max_pages_for`，
        允許 `sources.<name>.max_pages` 覆寫全域值）。canary 明確傳 1——
        它只是在問「這條管道還活著嗎」，翻頁對這個問題沒有任何貢獻。

        `category` 是**已經解析好的分類值**（`queries.resolve_category` 的輸出，
        逐來源不同）。這一層不再自己去查表：舊版在這裡對 Buyee 硬套
        `categories.buyee_mercari.yugioh_ocg`，於是「哪條 query 帶哪個分類」
        是寫在程式裡而不是 watchlist 裡，看設定檔完全看不出來。

        `price_ceiling=False` 是給 canary 用的（見 `_run_canaries`）：
        **canary 不該帶 max_price**。價格上限是「這筆划不划算」的商業過濾，
        canary 問的是「這條管道還活著嗎」——把商業過濾套上去，回傳筆數就會
        跟著平台當下的價格分布跳動，答案只會更不穩定（這正是 parsed_count
        那次假警報的同一種錯：拿商業結果當健康指標）。
        """
        pages = pages if pages is not None else self.cfg.max_pages_for(source_name)
        try:
            max_price = self._price_ceiling_jpy(src.site) if price_ceiling else None
        except Exception as exc:  # noqa: BLE001 - 隔離邊界：算上限失敗不該炸掉整輪掃描
            return SearchResult(
                source=source_name, site=src.site.value, query=keyword,
                health=ParseHealth.PARSER_BROKEN,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return run_source_search(
            source_name, src, keyword,
            pages=pages, max_price=max_price, category=category, sort=sort,
        )

    def _scan_source_passes(
        self, source_name: str, src, keyword: str, *, category: str | None = None
    ) -> list[SearchResult]:
        """一個 (source, query) 的**全部抓取趟數**。

        來源自己說要跑幾趟（`scan_passes()`，目前只有 Yahoo 有：新着＋即將結標）；
        沒有這個方法的來源照舊跑一趟。每一趟各自是一個 SearchResult——健康判定
        必須逐趟保留，一趟被擋、另一趟正常，是「這條管道部分可觀測」而不是
        「一切正常」（`_merge_summary` 取最嚴重的那個）。
        """
        def single() -> list[SearchResult]:
            return [self._scan_source(source_name, src, keyword, category=category)]

        passes = getattr(src, "scan_passes", None)
        if not callable(passes):
            return single()
        try:
            plan = list(passes())
        except Exception as exc:  # noqa: BLE001 - 讀設定失敗不該炸掉整輪掃描
            print(f"[warn] {source_name} 讀不到抓取通道設定，改用單趟：{exc}")
            return single()
        if not plan:
            return single()

        # 第一趟一定跑。後續趟數**只在第一趟可能被截斷時才跑**。
        #
        # 2026-08-02 實測：帶價格上限之後，各 query 在上限之下的 pool 是 34/38/12/2 件
        # ——一頁 50 筆裝得下整個 pool，所以第二趟（即將結標）拿到的是同一批，
        # 新增 0 筆。每輪固定跑兩趟等於白花一倍請求。
        #
        # 但也不能直接關掉：pool 會不會超過一頁取決於關鍵字、價格上限（隨鑑定費與
        # 匯率浮動）與市場量，而**被截斷是無聲的**——單看新着那一趟，被截掉的正好
        # 是「快結標」那一群，也就是唯一可行動的那群。
        #
        # 所以判準用「第一趟有沒有裝滿」：沒裝滿代表已經看到整個 pool，第二趟必然
        # 是同一批；裝滿了才代表可能有東西在視野外，這時第二趟才有資訊價值。
        # 成本隨市場自動調整，不必有人記得去改設定。
        first = self._scan_source(
            source_name, src, keyword,
            pages=plan[0].pages, sort=plan[0].mode, category=category,
        )
        results = [first]
        if len(plan) == 1:
            return results

        page_size = int(getattr(src, "page_size", 0) or 0)
        capacity = page_size * max(1, plan[0].pages)
        truncated = capacity > 0 and first.parsed_count >= capacity
        if not truncated:
            return results

        print(
            f"[scan] {source_name}「{keyword}」第一趟已滿 {first.parsed_count} 筆，"
            f"pool 可能被截斷 → 補跑 {len(plan) - 1} 趟"
        )
        results.extend(
            self._scan_source(
                source_name, src, keyword, pages=p.pages, sort=p.mode, category=category
            )
            for p in plan[1:]
        )
        return results

    # ------------------------------------------------------------------
    def _snipe_matchers(self):
        """狙擊比對器（一輪建一次）。沒有 active watch 時是空 list，掛鉤等於沒開。"""
        if self._snipe_cache is None:
            from .card_snipe import load_matchers

            self._snipe_cache = load_matchers(self.store)
        return self._snipe_cache

    def _collect_candidates(
        self, listings: list, source_name: str, candidates: list
    ) -> list[dict]:
        """一批 listing → (候選累加進 `candidates`, 觀測列清單)。

        **關鍵字掃描與賣家頁列舉共用這一支**：年代判準（`parse_card` ＋
        `is_candidate`）與觀測列的欄位（`venue_study.listing_row`）只有一份。
        監控賣家的貨若走另一條轉換路徑，同一個標的會因為「從哪裡發現的」
        而得到不同的判定（工程原則 1）。
        """
        # 狙擊比對走在商業過濾**之前**：狙擊目標不能被排除字／年代／min_grade
        # 吃掉（實測 `【ARS10】魔法の筒 P4-06 ポケモンカード` → 排除字 ポケモン，
        # 一個賣家亂塞的關鍵字就會讓等了半年的標的整筆消失）。那些閘門是為
        # 「大海撈針」設計的，狙擊是「等一根已知的針」——誤殺是靜默的
        # （CLAUDE.md 第一節）。掛在這裡是因為關鍵字掃描與賣家頁列舉都走這一支，
        # 兩條路一次蓋到。
        matchers = self._snipe_matchers()
        if matchers:
            from .card_snipe import observe_listings

            # dry_run 只擋**寫帳**，不擋比對：比對是純字串運算、零副作用，
            # 跳過它只會讓 `scan --dry-run` 印出「比對 0 筆」——而那與「掛鉤壞了」
            # 外顯一模一樣，正好是這組計數要消滅的歧義（CLAUDE.md 第五節）。
            # 兩個計數同源同處：compared 是送進去的那一批，hits 是它的回傳值——
            # 分兩個地方各算一次遲早會分岔。
            self._snipe_stats["compared"] += len(listings)
            self._snipe_stats["hits"] += observe_listings(
                self.store, matchers, listings, source_name=source_name,
                write=self._snipe_write,
            )

        wl = self.cfg.watchlist
        rows: list[dict] = []
        for lst in listings:
            info = parse_card(lst.title, wl)
            ok, _why = is_candidate(info, wl)
            if not ok:
                continue
            candidates.append((lst, info))
            rows.append(
                listing_row(
                    lst, info, source=source_name, fx=self.fx,
                    price_kind=str((lst.raw or {}).get("price_kind") or "fixed"),
                )
            )
        return rows

    def _sync_auto_watch(self, params) -> dict | None:
        """過門檻的賣家自動入選（**兩條軌**）。**失敗只印警告**（隔離邊界）。

        軌 1 Alpha（「比同儕便宜多少」）、軌 2 Supply Fit（「值不值得盯」）。
        兩軌的分數是兩把不同的尺，**永不互比**——印出來時也要分開講門檻，
        不要混成一句「X 分 ≥ Y」讓人以為那是同一個量表。

        只加不刪：分數會隨樣本上下跳，掉到門檻以下就自動移除的話，賣家會在
        名單上進出，而重新加入會清空 `last_scanned_at`——輪替節奏會被自己的
        分數雜訊打亂。要移除是人工決定（`watch-seller remove`）。
        """
        from .seller_alpha import analyze
        from .seller_supply import SupplyParams, supply_fit_all
        from .seller_watch import summarize_rejections, sync_auto_watch

        try:
            report = analyze(self.store, cfg=self.cfg)
            supply = supply_fit_all(list(report.metrics.values()), params=SupplyParams())
            out = sync_auto_watch(self.store, report, params, supply=supply)
        except Exception as exc:  # noqa: BLE001 - 隔離邊界，見 docstring
            print(f"[warn] 賣家自動入選失敗，本輪跳過：{type(exc).__name__}: {exc}")
            return None
        for a in out["added"]:
            supply_track = a.get("track") == "supply"
            label = "供給軌" if supply_track else "Alpha 軌"
            floor = params.supply_min_score if supply_track else params.auto_min_score
            print(
                f"[watch] 自動入選 {a['seller_key']}（{label} {a['score']:.1f} 分 ≥ "
                f"{floor:g}，批次 {a['batch']}）"
                + (f"，淘汰 {a['evicted']}" if a.get("evicted") else "")
            )
        # 拒絕**摘要**，不逐個印：候選人數本來就多於名額，排程一天跑 15 次，
        # 每輪 50 行 `[warn]` 會把真正的告警洗掉（洗版與靜默是同一個病的兩面）。
        # 但非預期的拒絕（例如賣家鍵格式錯誤）仍然逐個全文印出來——那是真的
        # 有東西壞了，就算只有 1 個也要看得見。分類邏輯在 `summarize_rejections`。
        digest = summarize_rejections(out["rejected"])
        for line in digest.summary_lines:
            print(f"[watch] {line}")
        for line in digest.alert_lines:
            print(f"[warn] {line}")
        return out

    def _scan_watched_sellers(
        self, candidates: list, *, force: bool = False
    ) -> tuple[list[dict], dict]:
        """輪替監控：這一輪該掃的那一批賣家，用**賣家頁列舉**抓全部在架。

        回傳 (要落帳的觀測批次, 報告)。批次一律 `exit_scope=False`——賣家頁
        只看得到一個賣家的貨，拿它當地平線會把整個站的其他標的判成消失
        （見 `store.record_listing_scan` 的 exit_scope 註記）。

        **任何失敗都不准拖垮整輪掃描**（與 refresh_comps／refill 同一立場）：
        每個賣家各自走 `run_source_search` 的隔離邊界，claim／落帳失敗也只印警告。
        """
        from .seller_watch import (
            SELLER_PAGE_SOURCE,
            WatchParams,
            claim_batch,
            due_sellers,
        )

        report: dict = {"enabled": True, "batch": None, "reason": "", "sellers": [],
                        "skipped": [], "requests": 0, "found": 0, "candidates": 0}
        params = WatchParams.from_config(self.cfg)
        report["enabled"] = params.enabled
        try:
            batch, why = claim_batch(self.store, params, force=force)
        except Exception as exc:  # noqa: BLE001 - 隔離邊界，見 docstring
            print(f"[warn] 賣家輪替節流帳讀寫失敗，本輪跳過：{type(exc).__name__}: {exc}")
            report["reason"] = f"節流帳失敗：{exc}"
            return [], report
        report["batch"], report["reason"] = batch, why
        if batch is None:
            print(f"[watch] 跳過賣家監控（{why}）")
            return [], report

        # 自動入選**跟著輪替走**（不是每輪 scan 都跑）：dashboard 手動按的掃描
        # 不該一次次重跑全量評分，而輪替本身就是每小時一次。放在 claim 之後、
        # due_sellers 之前，剛入選又剛好落在這一批的賣家這一輪就會被掃到。
        report["auto_sync"] = self._sync_auto_watch(params)

        due, skipped = due_sellers(self.store, params, batch)
        report["skipped"] = [
            {"seller_key": r["seller_key"], "reason": reason} for r, reason in skipped
        ]
        for row, reason in skipped:
            # 「沒掃」的原因要落到那一列上：dashboard 與 CLI 才看得出
            # 「這個賣家四小時沒動靜」是市場沒貨還是我們根本沒去看。
            self.store.mark_seller_watch_scanned(
                row["seller_key"], result=f"跳過：{reason}"
            )
        if not due:
            print(f"[watch] {why}：這一批沒有可掃的賣家（{len(skipped)} 個跳過）")
            return [], report

        print(f"[watch] {why}：掃 {len(due)} 個賣家（每人 {params.pages} 頁）")
        batches: list[dict] = []
        for row in due:
            key = row["seller_key"]
            site, _, sid = key.partition(":")
            source_name = SELLER_PAGE_SOURCE.get(site)
            src = self.sources.get(source_name or "")
            if src is None or not callable(getattr(src, "search_seller", None)):
                note = f"來源 {source_name or site} 不在 registry 或沒有賣家頁列舉"
                self.store.mark_seller_watch_scanned(key, result=f"跳過：{note}")
                report["skipped"].append({"seller_key": key, "reason": note})
                continue
            res = run_source_search(
                source_name, src, "", pages=params.pages, seller=sid
            )
            report["requests"] += max(1, res.pages_fetched)
            rows = self._collect_candidates(res.listings, source_name, candidates)
            report["found"] += len(res.listings)
            report["candidates"] += len(rows)
            healthy = res.health in _OBSERVABLE
            batches.append({
                "source": source_name,
                "site": src.site.value,
                "healthy": healthy,
                "exit_scope": False,
                "rows": rows,
            })
            result_note = (
                f"{res.health.value}：在架 {len(res.listings)} 筆、候選 {len(rows)} 筆"
                + (f"（{res.detail}）" if res.detail else "")
            )
            self.store.mark_seller_watch_scanned(key, result=result_note)
            report["sellers"].append({
                "seller_key": key, "source": row.get("source"),
                "health": res.health.value, "found": len(res.listings),
                "candidates": len(rows), "detail": res.detail,
            })
            if not healthy:
                print(f"[warn] 賣家頁 {key} 抓取異常（{res.health.value}）：{res.detail}")
        return batches, report

    @staticmethod
    def _merge_summary(
        summary: dict[str, dict], res: SearchResult, *, count: int | None = None
    ) -> None:
        """把一次搜尋併進「每個發現管道一格」的摘要：筆數累加、健康取最嚴重。

        `count` 是給多趟抓取用的：兩趟的 listings 有重疊，逐趟累加會把同一個
        標的數兩次。呼叫端算好**去重後**的數量傳進來（其餘趟傳 0），
        摘要的筆數才會跟真的進到評分的筆數同源。
        """
        cur = summary.setdefault(
            res.source, {"health": ParseHealth.OK.value, "count": 0, "detail": ""}
        )
        cur["count"] += len(res.listings) if count is None else count
        if _HEALTH_SEVERITY[res.health] > _HEALTH_SEVERITY[ParseHealth(cur["health"])]:
            cur["health"] = res.health.value
            cur["detail"] = res.detail
        elif res.health.value == cur["health"] and res.detail and not cur["detail"]:
            cur["detail"] = res.detail

    # ------------------------------------------------------------------
    def _run_canaries(self, source_summary: dict[str, dict]) -> list[SearchResult]:
        """每個啟用來源用 canary 關鍵字自檢一次，回傳需要追加的壞消息。

        成本：每個來源每輪一個請求。改成每小時跑之後快取 TTL 降到 15 分鐘，
        所以 canary 每輪都會真的打外網（一天約 24×3 = 72 個請求）。
        這是刻意付的代價——canary 是唯一不會跟著對方改版一起壞掉的判準，
        每小時驗一次才能讓「解析壞了」在一小時內被發現，而不是隔天。
        這輪已經確診 BLOCKED / FETCH_FAILED 的來源直接跳過——
        它的病已經有人告警了，canary 再打一次只會重複吵（而且必定也失敗）。
        """
        extra: list[SearchResult] = []
        skip = {ParseHealth.BLOCKED.value, ParseHealth.FETCH_FAILED.value}
        for name, spec in (self.cfg.sources or {}).items():
            src = self.sources.get(name)
            keyword = (spec or {}).get("canary_keyword")
            if src is None or not keyword:
                continue
            cur = source_summary.get(name)
            if cur is not None and cur["health"] in skip:
                continue
            # 固定 1 頁：canary 問的是「還活著嗎」，多翻頁不會讓答案更確定，
            # 只會讓每輪的請求數跟著 max_pages 一起漲。
            # 不帶價格上限：canary 的答案不可以跟著「今天在架的價格分布」跳動。
            res = self._scan_source(name, src, keyword, pages=1, price_ceiling=False)
            verdict = canary_verdict(res, int((spec or {}).get("canary_min_results", 10)))
            if verdict is not None:
                extra.append(verdict)
        return extra

    # ------------------------------------------------------------------
    def scan(
        self,
        *,
        skip_comps: bool = False,
        dry_run: bool = False,
        trigger: str = "cli",
        watch_only: bool = False,
        watch_force: bool = False,
    ) -> dict:
        """一輪掃描。**掃描狀態的開始／結束一律在這裡落**，CLI 與 dashboard 共用。

        狀態放在這一層而不是各呼叫端：`ygo-sniper daily`、`ygo-sniper scan`、
        dashboard 的 /api/scan 是三個入口，狀態要在每個入口各記一次的話，
        漏掉一個就會出現「明明在掃、dashboard 說沒有」。

        例外一律先落 finished(error=…) 再往外拋——**掃爆了不可以讓狀態卡在
        running**（工程原則 3）。真正的崩潰（kill -9、斷電）走 `scan_status`
        的逾時兜底，那是另一道防線。

        排程空窗偵測也記在這裡（開頭讀舊基準＋印告警、寫新基準；成功收尾時
        再補一個完成戳記，見 `_update_schedule_state`／`_finish_schedule_state`）。
        `--dry-run` 的語意是「只掃不寫庫」、`watch_only` 是 `watch-scan` 的獨立
        節奏（見那兩個方法的 docstring），兩者都不吃邊緣觸發、不動基準。
        """
        started = self.store.begin_scan(trigger=trigger, dry_run=dry_run)
        self._update_schedule_state(dry_run=dry_run, watch_only=watch_only)
        try:
            result = self._scan(
                started, skip_comps=skip_comps, dry_run=dry_run,
                watch_only=watch_only, watch_force=watch_force,
            )
        except BaseException as exc:  # noqa: BLE001 - 落狀態後原樣往外拋，見 docstring
            self.store.finish_scan(started, error=f"{type(exc).__name__}: {exc}")
            # 這裡刻意不呼叫 `_finish_schedule_state`：崩潰時 RUN_FINISHED_KEY
            # 必須維持舊值，下一輪的 schedule_health 才會看到「有開始沒收尾」。
            # 這一輪已經在 `_update_schedule_state` 裡把偵測到的告警寫進
            # PENDING_ALERT_KEY 了，即使 `daily` 的 try/finally 不會走到
            # `_run_notifications`（本輪崩潰），那則告警也不會跟著丟失——
            # 下一輪成功收尾時會把它撿回來一起送（Fix 4）。
            raise
        self.store.finish_scan(
            started,
            result={
                k: result[k]
                for k in ("scanned", "candidates", "signals", "new", "comps_added", "expired")
                if k in result
            },
        )
        # 只有走到這裡（沒有例外往外拋）才算「正常收尾」——crash 時上面的
        # except 分支已經 raise 出去，這行不會執行，基準保持舊值。
        self._finish_schedule_state(dry_run=dry_run, watch_only=watch_only)
        return result

    def _update_schedule_state(self, *, dry_run: bool, watch_only: bool) -> None:
        """排程空窗偵測的開頭那一半：讀舊基準、算這一輪要不要出聲、寫新基準。

        `dry_run`：`--dry-run` 是「只掃不寫庫」，這裡也不能寫、也不能吃掉
        邊緣觸發。

        `watch_only`：`ygo-sniper watch-scan` 是賣家輪替監控的**人工逃生門**，
        跑在自己的節奏上（由 `due_sellers` 的節流決定，不是 15 個 plist
        時間點），而且它跳過關鍵字查詢／canary／comps 回補——排程監督真正
        要盯的正是那些東西有沒有照表跑。讓 watch-scan 寫這裡的基準，會讓一次
        跟排程網格無關的手動執行，蓋掉「真正該跑的那一輪其實漏了」的證據
        （工程原則 1 的變體：基準必須跟被拿去比較的排程表同源，watch-scan
        的節奏不是那個源）。所以 watch_only 一律跳過整段，基準只由完整掃描
        （`ygo-sniper scan`／`daily`／dashboard 的 `/api/scan`）維護。

        偵測器本身包一層 `try/except`：它只是資訊性功能，壞掉不能拖垮這一輪
        真正的掃描（見 schedule_watch.py 與 CLAUDE.md 五、靜默失敗）。
        """
        if dry_run or watch_only:
            return
        try:
            now = datetime.now()
            detected = schedule_health(
                self.store.get_meta(RUN_STARTED_KEY),
                self.store.get_meta(RUN_FINISHED_KEY),
                now,
            )
            # Fix A：watchdog 帳本（run_daily.sh 寫的 data/last_run_exit）折進
            # 同一條偵測。兩者都算「這一輪偵測到的問題」，合併成一句話一起
            # 進 resolve_alert——共用同一套 pending／送達才消耗的保障，
            # 不是另開一條沒有重送機制的路（見 schedule_watch.py 模組開頭
            # Fix A 的事故背景：8.5 小時卡死那次唯一的線索就是這個帳本，
            # 而原本的 curl 通知本身可能也送不出去）。
            watchdog_msg = self._fold_watchdog_ledger()
            if watchdog_msg:
                detected = f"{detected}；{watchdog_msg}" if detected else watchdog_msg
            self._schedule_alert, new_pending = resolve_alert(
                self.store.get_meta(PENDING_ALERT_KEY), detected
            )
            if self._schedule_alert:
                print(self._schedule_alert)
            # 寫入順序無關緊要（三把鍵各自獨立），但 PENDING 先寫、STARTED
            # 後寫，讓「這輪偵測到的東西已經進帳」與「這輪已經開始」在語意上
            # 對齊：pending 記的是「偵測時看到的問題」，started 記的是「這輪
            # 本身何時起跑」，兩者互不覆寫對方。
            self.store.set_meta(PENDING_ALERT_KEY, new_pending)
            self.store.set_meta(RUN_STARTED_KEY, now.isoformat())
        except Exception as exc:  # noqa: BLE001 - 偵測器壞掉不能拖垮本輪掃描
            print(
                f"[warn] 排程空窗偵測失敗（不影響本輪掃描）："
                f"{type(exc).__name__}: {exc}"
            )

    def _fold_watchdog_ledger(self) -> str | None:
        """讀 `run_daily.sh` 寫的上一輪結束帳本（`data/last_run_exit`），
        折成一句話；如果折出了東西就順手刪掉檔案（「讀了就算數」，
        下一輪不會再重複折進來）。

        純／不純分工：檔案讀寫（不純）留在這裡，訊息怎麼寫（純，包含
        「已經送達的失敗通知不重複講」的判斷）在 `schedule_watch.watchdog_message`。

        對檔案不存在／壞掉一律寬容：找不到帳本是**最常見**的正常狀態
        （上一輪成功、run_daily.sh 覆寫成 exit=0，`watchdog_message` 對
        exit=0 回 None，等於沒東西可折——但如果連讀取本身都失敗，這裡
        也不例外拋出，理由與 `schedule_health` 相同：偵測器壞掉不能拖垮
        真正的掃描（外層 `_update_schedule_state` 的 try/except 是最後一道，
        這裡先擋一層是因為「檔案讀不到」本來就不是例外狀況，用 if 處理
        比讓它落進 except 分支更清楚）。

        路徑刻意算成 `cfg.db_path.parent / "last_run_exit"` 而不是
        `cfg.root / "data" / "last_run_exit"`：production 兩者算出來是
        同一個目錄（`storage.db_path` 預設是 `"data/sniper.db"`），但
        `db_path` 是**全 repo 測試已經在用的隔離點**——每一個建立臨時
        `Pipeline` 的測試 fixture 都會覆寫 `storage.db_path` 成
        `tmp_path` 底下的絕對路徑（CLAUDE.md 第六節：測試絕不碰真實
        世界）。如果這裡改用 `cfg.root`，任何沒有額外覆寫 `root` 的既有
        測試（例如 `test_card_snipe.py` 呼叫 `pipeline.scan(...)`）就會
        在跑測試時真的去讀、甚至**刪掉**這台機器上正式環境的
        `data/last_run_exit`——比讀錯資料更糟，是直接吃掉正式帳本。
        沿用 `db_path` 讓這裡自動繼承全 repo 既有的隔離保證，不必逐一
        去改其他測試檔案。
        """
        path = self.cfg.db_path.parent / "last_run_exit"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            ledger = json.loads(raw)
        except (TypeError, ValueError):
            ledger = None
        msg = watchdog_message(ledger)
        if msg:
            try:
                path.unlink()
            except OSError:
                pass  # 刪不掉不影響這次要不要出聲；下一輪頂多重複折一次
        return msg

    def _finish_schedule_state(self, *, dry_run: bool, watch_only: bool) -> None:
        """排程空窗偵測的收尾那一半：只有真正走到這裡才代表「正常收尾」。

        guard 理由與 `_update_schedule_state` 相同（dry-run 不寫庫、
        watch-scan 不是排程網格的一部分）。呼叫點在 `scan()` 裡故意放在
        例外處理**之外**——`_scan()` 拋例外時這個方法完全不會被呼叫，
        `RUN_FINISHED_KEY` 因此維持舊值，下一輪 `schedule_health` 才報得出
        「有開始沒收尾」。

        這裡也包一層 `try/except`，跟 `_update_schedule_state` 對稱：這行只是
        排程監督自己的記帳，不能讓它的失敗把已經跑完、已經 `finish_scan`
        成功的一輪掃描結果拖著一起往外拋例外（工程原則 3 的反面：安全關鍵的
        是「這一輪掃描的結果」本身，記帳失敗只需要出聲，不需要連坐）。
        """
        if dry_run or watch_only:
            return
        try:
            self.store.set_meta(RUN_FINISHED_KEY, datetime.now().isoformat())
        except Exception as exc:  # noqa: BLE001 - 記帳壞掉不能拖垮已經跑完的掃描
            print(
                f"[warn] 排程收尾記帳失敗（不影響本輪掃描結果）："
                f"{type(exc).__name__}: {exc}"
            )

    def _scan(
        self,
        started: str,
        *,
        skip_comps: bool = False,
        dry_run: bool = False,
        watch_only: bool = False,
        watch_force: bool = False,
    ) -> dict:
        """`watch_only=True`：只跑賣家輪替監控那一段（`ygo-sniper watch-scan`）。

        刻意做成同一支 `_scan` 的一面旗，而不是另寫一條迷你管線：候選判定、
        估價、落庫、觀測帳全部要走同一份程式碼，否則「從賣家頁發現的標的」
        與「從關鍵字發現的標的」遲早會有兩套判準（工程原則 1）。
        跳過的只有：關鍵字查詢、canary（那是在問「關鍵字管道還活著嗎」）、
        行情回補——回補的需求端雖然也吃監控賣家的定價上架（見 _refill_comps），
        但它會打真實網路請求並寫節流帳與 comps，那是 `daily`/`scan` 完整輪的事；
        `watch-scan` 是手動輔助指令，維持零回補請求的預算不因本次來源擴充而變。
        """
        skip_comps = skip_comps or watch_only
        comps_added = 0 if skip_comps else self.refresh_comps()
        if skip_comps:
            self.comps.load_from_store()

        wl = self.cfg.watchlist
        # `scan --dry-run` 的語意是「只掃不寫庫」——狙擊帳也是庫。
        self._snipe_write = not dry_run
        self._snipe_stats = {"compared": 0, "hits": 0}
        scanned = 0
        candidates = []
        search_results: list[SearchResult] = []
        source_summary: dict[str, dict] = {}
        #: 在架觀測帳的批次：一個 (發現管道, 關鍵字) 一格。**每格必須帶自己的
        #: healthy 旗標**——離場判定要用它決定「這一批的缺席算不算證據」。
        obs_batches: list[dict] = []

        # 狙擊卡自己的關鍵字查詢：等一根已知的針，就得主動去找它，不能只靠
        # watchlist 那些廣撒查詢碰巧掃到。來源沿用既有查詢的聯集（不猜來源名）；
        # base 是空的（watch_only）就不跑——那一輪根本沒有關鍵字管道。
        base_queries = load_queries(wl) if not watch_only else []
        if base_queries:
            from .card_snipe import scan_queries

            base_queries = base_queries + scan_queries(
                self._snipe_matchers(), base_queries
            )
        for query in base_queries:
            for source_name in query.sources:
                src = self.sources.get(source_name)
                if src is None:
                    print(
                        f"[warn] watchlist 來源 {source_name!r} 不在 registry，跳過"
                        f"（可用：{', '.join(self.sources)}）"
                    )
                    continue

                # 分類別名逐來源解值（各站編號系統完全無關，見 queries.py）
                category = resolve_category(wl, source_name, query.category)
                # 多趟抓取（Yahoo：新着＋即將結標）。兩趟合併去重之後才進評分——
                # 同一個標的兩趟都出現時，它是一筆，不是兩筆。
                results = self._scan_source_passes(
                    source_name, src, query.keyword, category=category
                )
                listings = dedupe_listings(results)
                for i, res in enumerate(results):
                    search_results.append(res)
                    # 筆數只在第一趟記一次（去重後的總數），健康每趟都要併。
                    self._merge_summary(
                        source_summary, res, count=len(listings) if i == 0 else 0
                    )
                scanned += len(listings)
                # 離場判定的「這一批可觀測嗎」：**每一趟都要健康才算數**（all，不是 any）。
                # 一趟被擋、一趟正常時，合併後的清單是不完整的——此時某個標的
                # 不在清單上，可能只是因為它剛好只出現在壞掉的那一趟。
                # 拿殘缺的批次去推論「它下架了」，就是把一次 WAF 挑戰記成賣光。
                healthy = bool(results) and all(r.health in _OBSERVABLE for r in results)

                obs_batches.append({
                    "source": source_name,
                    "site": src.site.value,
                    "healthy": healthy,
                    "rows": self._collect_candidates(listings, source_name, candidates),
                })

        # 賣家輪替監控：這一批賣家的**全部在架**（賣家頁列舉，不是關鍵字搜尋）。
        # 產出的候選與觀測列直接併進同一條管線，下面的估價／評分／落庫一視同仁。
        # 先收在自己的清單再併進主清單：refill 要分得出「監控賣家的定價上架」
        # 這個需求來源（見下方 _refill_comps 的呼叫），靠的就是這條分界。
        watch_candidates: list = []
        watch_batches, watch_report = self._scan_watched_sellers(
            watch_candidates, force=watch_force
        )
        candidates.extend(watch_candidates)
        obs_batches.extend(watch_batches)
        scanned += watch_report.get("found", 0)

        # 掃描結束才跑 canary：正常 query 已經產出的來源健康是免費的證據，
        # canary 只補「全部 query 都 0 筆，到底是沒貨還是瞎了」這個缺口。
        # watch_only 不跑：它問的是「關鍵字管道還活著嗎」，而這一輪根本沒跑關鍵字。
        for res in ([] if watch_only else self._run_canaries(source_summary)):
            search_results.append(res)
            self._merge_summary(source_summary, res)

        # 需求驅動回補：**在估價之前**跑，讓本輪掃到的 L3 競標標的有機會
        # 當場升到 L1/L2（回補的整個目的就是出價上限）。它有自己的節流
        # （每輪卡數上限＋每卡 7 天冷卻），與廣撒式 refresh_comps 互相獨立。
        # dry_run 不跑（要寫節流帳與 comps）；skip_comps 不跑（那面旗的語意
        # 就是「這輪不要碰行情」）。
        refill_report = None
        if not dry_run and not skip_comps and not watch_only:
            refill_report = self._refill_comps(
                [lst.title for lst, _info in candidates if is_live_auction(lst)],
                # 第二個需求來源（2026-08-07 缺口）：監控賣家的**定價**上架。
                # 監控賣家 62/80 是定價，只吃競標標題的佇列結構上永遠看不到它們。
                # 競標中的那部分已在上一個清單（watch_candidates ⊆ candidates），
                # 這裡只挑非競標，兩個清單不重疊、需求不會數兩次。
                # 一般關鍵字掃描的定價上架**刻意不進**（會把佇列衝大，另議）。
                [lst.title for lst, _info in watch_candidates
                 if not is_live_auction(lst)],
            )

        # 同賣家多筆 → 可以問合併運費
        sellers = seller_histogram([lst for lst, _ in candidates])

        # 留不留沒觸發旗標的候選由 config 決定（scoring.keep_all_candidates）。
        # 預設 true：dashboard 是「你自己去看」的清單，洗版成本不存在，
        # 而「符合年代但沒觸發」正是使用者說想自己過目的那一批。
        keep_all = bool(self.cfg.scoring.get("keep_all_candidates", False))
        signals = []
        auctions = 0
        for lst, info in candidates:
            stats = self.comps.stats_for(lst, info)
            # 競標標的才需要估價（出價上限的唯一依據）。定價標的完全不碰模型——
            # 它們的判準是到手成本，跟這一輪的公允價無關，多算只是白花時間。
            estimate = None
            if is_live_auction(lst):
                auctions += 1
                try:
                    estimate = estimate_listing(self.valuator(), lst, info)
                except Exception as exc:  # noqa: BLE001 - 估價炸掉不該讓整輪掃描沒有產出
                    print(f"[warn] 估價失敗，這筆不給出價上限：{type(exc).__name__}: {exc}")
            sig = evaluate(
                lst, info, stats, self.cfg, self.fx,
                seller_counts=sellers, keep_all=keep_all, estimate=estimate,
            )
            if sig:
                signals.append(sig)

        signals.sort(key=lambda s: -s.score)
        triggered = sum(1 for s in signals if is_triggered(s.flags))
        biddable = sum(1 for s in signals if s.bid is not None and s.bid.ok)

        # 到手成本補進在架觀測列。**成本只算一次**（scoring 那一遍），
        # 這裡查表回填而不是重算——兩處各算一次遲早會分岔（工程原則 1）。
        landed = {s.listing.key: s.best_route.landed_twd for s in signals}
        for batch in obs_batches:
            for row in batch["rows"]:
                row["landed_twd"] = landed.get(row["key"])

        new_count = 0
        expired = 0
        obs_report: dict = {}
        obs_pruned = 0
        restored: dict = {"restored": 0, "keys": []}
        if not dry_run:
            for sig in signals:
                if self.store.upsert_signal(sig):
                    new_count += 1
            self.store.snapshot(
                [(s.listing.key, s.best_route.landed_twd) for s in signals]
            )
            # 在架觀測帳：signals 每輪 upsert 覆寫，回答不了「在架多久、何時消失」。
            # 這張表是那個問題的唯一資料來源，所以每輪都要落，不管有沒有訊號。
            obs_report = self.store.record_listing_scan(obs_batches)
            # 「清除已離場」的防線：我們清掉、但這一輪又被看到的標的放回原狀態。
            # **必須排在 record_listing_scan 之後**——那裡才是清掉
            # `disappeared_at` 的地方，放前面的話還原永遠慢一輪。
            # 放在 prune_listing_obs 之前也是刻意的：還原要看得到觀測列。
            restored = self.store.restore_revived_signals()
            if restored["restored"]:
                # 還原＝這個功能自己誤殺了幾筆。安靜地放回去，使用者就永遠不知道
                # 判定有多不準（CLAUDE.md 第五節：靜默失敗是頭號敵人）。
                shown = ", ".join(restored["keys"][:5])
                more = "…" if restored["restored"] > 5 else ""
                print(
                    f"[expiry] 誤殺自癒：{restored['restored']} 筆重新上架，"
                    f"已放回原分頁（{shown}{more}）"
                )
            obs_pruned = self.store.prune_listing_obs(
                int(self.cfg.scan.get("listing_obs_retain_days", 0) or 0)
            )
            from .card_snipe import NEAR_HIT_RETAIN_DAYS, TIER_NEAR

            # 只回收 near（現代重印與未鑑定貨會把它洗出大量列）；exact／partial
            # 永久保留，那是這張卡的出現史。tier 是必填關鍵字參數：保留政策屬於
            # card_snipe，store 只做 CRUD。
            self.store.prune_card_watch_hits(NEAR_HIT_RETAIN_DAYS, tier=TIER_NEAR)
            # keep_all 打開之後每輪落庫的量級從個位數變成上百筆，所以要有出口：
            # 很久沒再被掃到、而且你從沒動過的 new 列標成 expired（不刪除、不碰
            # 人工標過的狀態）。0 或負數 = 關閉。
            expired = self.store.expire_stale_signals(
                int(self.cfg.scan.get("expire_new_after_days", 0) or 0)
            )

        # 只算不發：evaluate() 落觀測帳並回傳「現在該發」的訊息，
        # 真的送出與冷卻落帳由 CLI 的 daily 流程負責（alerts.py 模組註解）。
        alerts: list[Alert] = self.alerts.evaluate(search_results)

        result = {
            "started_at": started,
            "scanned": scanned,
            "candidates": len(candidates),
            "signals": len(signals),
            # 有 trigger 旗標的筆數。keep_all 打開後 signals 會含大量「只是符合
            # 條件」的候選，兩個數字必須分開報，否則「訊號 118 筆」會被讀成
            # 「118 個撿漏機會」——那是最容易自欺的一種指標退化。
            "triggered": triggered,
            # 競標管道的兩個數字必須分開報：`auctions` 是掃到幾筆競標中的候選，
            # `biddable` 是其中**真的算得出出價上限**的（樣本足以給區間下緣）。
            # 只報前者會讓人以為有 40 個機會，實際上可能一個都不能出手。
            "auctions": auctions,
            "biddable": biddable,
            "keep_all": keep_all,
            "new": new_count,
            "expired": expired,
            "comps_added": comps_added,
            # 需求驅動回補的帳（refill.RefillReport.to_dict()；沒跑就是 None）。
            "refill": refill_report,
            # 在架觀測帳的落帳報告（新增／更新／判定離場幾筆）。
            # `revived` 是「判定為消失後又出現」——離場推論規則自己的錯誤率。
            "listing_obs": obs_report,
            # 賣家輪替監控這一輪做了什麼（哪一批、掃了誰、跳過誰為什麼）。
            "seller_watch": watch_report,
            # 「清除已離場」的誤殺自癒帳：這一輪放回去幾筆、哪幾筆。
            # 與 `expired` 是**相反方向**的兩件事，不可合併成一個數字。
            "restored": restored,
            # 狙擊比對的觀測帳。**追蹤幾張卡／比對幾筆／命中幾筆三個數字分開**：
            # 只報命中數的話，0 分不出「今天沒那張卡」與「比對根本沒跑」
            # （規則 4 的命中表只證明得了「規則有在跑」）。
            "snipe": {
                "watches": len(self._snipe_matchers()),
                "compared": self._snipe_stats["compared"],
                "hits": self._snipe_stats["hits"],
            },
            "listing_obs_pruned": obs_pruned,
            "fx_source": self.fx.source,
            # 每個發現管道的健康摘要（JSON-friendly；Phase 4 告警與 summary 用）
            "sources": source_summary,
            # 完整 SearchResult 物件（Phase 4 的 AlertEngine 要吃原始判定材料）
            "search_results": search_results,
            # 到期的告警（Alert 是 str 子類，可直接印／直接送）。scan 只算不發。
            "alerts": alerts,
            "top": [
                {
                    "title": s.listing.title[:70],
                    "landed_twd": s.best_route.landed_twd,
                    "route": s.best_route.route,
                    "score": s.score,
                    "flags": [f.value for f in s.flags],
                    "url": s.listing.url,
                }
                for s in signals[:10]
            ],
        }

        if not dry_run:
            self.store.log_run(
                started_at=started,
                scanned=scanned,
                candidates=len(candidates),
                signals=len(signals),
                notified=0,
                # notes 存 JSON：health 指令要拿「最近一次 scan 的各來源健康」，
                # 而那份摘要沒有別的落點（加欄位會需要遷移既有 db）。
                notes=json.dumps(
                    {
                        "fx": self.fx.source,
                        "comps_added": comps_added,
                        "sources": source_summary,
                    },
                    ensure_ascii=False,
                ),
            )
        return result

    # ------------------------------------------------------------------
    def notification_outcome(self, now=None):
        """這一輪兩條規則各命中什麼。**只判定，不送、不落帳**（preview 也用它）。

        估價模型建不起來時**不整批罷工**：規則 1（競標急件）用的是掃描當下就
        存進 payload 的上限，本來就不需要模型；只有規則 2 會被跳過，而且
        `Outcome.valuation_ok=False` 會讓 CLI 明講「這一輪算不出 P 值」——
        降級要看得見，不能長得像「今天沒好貨」（工程原則 3）。
        """
        from .notify_rules import NotifyRules, evaluate

        try:
            valuator = self.valuator()
        except Exception as exc:  # noqa: BLE001 - 見 docstring
            print(f"[warn] 估價模型建立失敗，本輪規則 2（P 值）跳過：{exc}")
            valuator = None
        return evaluate(
            self.store.notification_candidates(),
            rules=NotifyRules.from_config(self.cfg),
            valuator=valuator,
            now=now,
            notified=self.store.notify_log_map(),
            seller_ctx=self._seller_notify_context(),
            snipe_ctx=self._snipe_notify_context(),
        )

    def _seller_notify_context(self):
        """規則 3 的資料脈絡（監控名單 ＋ 同儕折價）。**建不起來就整條規則跳過**。

        與規則 2 的降級同一個立場：一條規則算不出來不該讓另外兩條也發不出去，
        但降級要看得見（回 None，`Outcome.seller_ctx_ok=False` 會被 CLI 印出來）。
        """
        from .seller_watch import WatchParams, build_notify_context

        if not WatchParams.from_config(self.cfg).enabled:
            return None
        try:
            return build_notify_context(self.store, self.cfg)
        except Exception as exc:  # noqa: BLE001 - 見 docstring
            print(f"[warn] 賣家同儕脈絡建立失敗，本輪規則 3 跳過：{type(exc).__name__}: {exc}")
            return None

    def _snipe_notify_context(self):
        """規則 4 的資料脈絡。建不起來就跳過該規則，不拖垮整輪推播
        （同 `_seller_notify_context` 的立場：降級要看得見，不是靜默）。"""
        from .card_snipe import build_notify_context

        try:
            return build_notify_context(self.store)
        except Exception as exc:  # noqa: BLE001 - 同 _seller_notify_context 的立場
            print(f"[warn] 狙擊脈絡建立失敗，本輪規則 4 跳過："
                  f"{type(exc).__name__}: {exc}")
            return None

    def notify(self):
        """判定 → 送出 → **只對送成功的落帳**。回傳 `notify_rules.Outcome`。"""
        outcome = self.notification_outcome()
        sent = self.notifier.send_rule_matches(outcome)
        self.store.mark_rule_notified(sent)
        return outcome

    def close(self) -> None:
        self.fetcher.close()
