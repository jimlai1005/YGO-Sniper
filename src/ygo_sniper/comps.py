"""行情資料（comps）。

沒有 comps，「discount」這個字就沒有意義 —— 你只是在買便宜的東西，
不是在買被低估的東西。這兩件事差很多。

三個來源，信心度不同：

  high    Buyee 的 Mercari「已售出」搜尋 —— 日本市場真實成交價。
          對日版 / ARS 卡，這比 eBay 準很多。
  medium  自建 snapshot：每天記錄在架標的，某天消失了就當作「可能成交」。
          有明顯偏誤（下架 ≠ 賣掉），只能當補充。
  low     樣本數 < min_comps，只給你看，不要拿來當判斷依據。

comps 一律用「不含刷卡手續費」的匯率換算，跟成本用不同口徑。
把 markup 加進行情會讓兩邊一起往上漂，折價率就假了。
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .config import Config
from .domain import CardInfo, CompStats, Listing
from .parsers import is_candidate, normalize, parse_card

_TOKEN_RE = re.compile(r"[ぁ-んァ-ヶ一-龯A-Za-z0-9]+")
_STOP = {
    "遊戯王", "YUGIOH", "YU", "GI", "OH", "CARD", "カード", "美品", "即決",
    "送料無料", "匿名配送", "PSA", "ARS", "BGS", "鑑定", "鑑定品", "鑑定書",
}


def card_signature(title: str, info: CardInfo) -> str:
    """把標題壓成一個可比對的鍵。

    優先用卡號（最可靠）。沒有卡號就用「機構+分數+前三個有意義的詞」，
    這會有誤判，所以 CompStats 一定要把 samples 帶回前端讓你自己看一眼。
    """
    grade_part = f"{info.grader.value}{info.grade or ''}"
    if info.set_code:
        return f"{info.set_code}|{grade_part}"

    tokens = [
        t for t in _TOKEN_RE.findall(normalize(title).upper())
        if t not in _STOP and len(t) >= 2 and not t.isdigit()
    ]
    return f"{'-'.join(tokens[:3])}|{grade_part}"


def _reason_bucket(why: str) -> str:
    """把 is_candidate 的原因收斂成可統計的桶。

    分數原因帶著實際數值（「分數 5.0 低於 7」），逐字統計會炸成一堆
    只有 1 筆的桶，所以只有這一類收斂；排除字與機構保留原文，
    因為「被哪個字擋掉」正是調 watchlist 時要看的東西。
    """
    # 年份否決同理：原因帶著實際年份（「標題年份 2007 不在 1998-2004」），
    # 逐字統計會炸成一堆只有 1-3 筆的桶。
    if why.startswith("標題年份"):
        return "標題年份不在 1998-2004"
    return "分數低於門檻" if why.startswith("分數") else why


# ---------------------------------------------------------------------------
# 已售出查詢的組合展開與請求節流
# ---------------------------------------------------------------------------
#: 每小時排程跑一輪；這個計數器決定「第幾輪才真的跑已售出查詢」。
#: 存在 store 的 meta 表（跨行程、跨重啟都要記得住，用記憶體變數等於沒節流）。
SOLD_RUN_COUNTER_KEY = "comps_sold_run_counter"

#: 展開後的查詢數硬上限。組合展開是「乘法」，稀有度 6 × 年代 6 × 機構 2 = 72，
#: 手一滑多加兩個詞就變 120——而每個查詢最多還要翻 N 頁。這個上限不是禮貌，
#: 是**結構性的請求預算**：超出就截斷並印出來，讓人看得見自己開了多少水龍頭
#: （工程原則 5 的同型：把判準放在必經之處，而不是放在註解裡靠人記得）。
DEFAULT_MAX_COMPS_QUERIES = 120

_WS_RE = re.compile(r"\s+")


def expand_comps_queries(spec: Mapping | None) -> list[str]:
    """把 `watchlist.comps_queries` 展開成關鍵字清單（純函式，可單測）。

    `template` × `eras` × `rarities` × `graders` 的笛卡兒積，再接上 `extra`
    的固定查詢。任一維度留空就等於「這一維不參與展開」（用空字串佔位），
    所以只填 eras 也是合法設定。

    **三個維度全空時完全不展開**（不會退化成只送 template 的常數部分）：
    「設定被改空了」與「刻意只跑一個 `遊戯王` 這種超廣查詢」是兩件事，
    前者遠比後者常見，預設要往「什麼都不做」倒。

    展開後去重且保序：保序讓「哪些查詢先跑」是可預測的（截斷發生在尾端，
    不會每次砍掉不同的一批）。
    """
    spec = spec or {}
    template = str(spec.get("template") or "遊戯王 {era} {rarity} {grader}")
    out: list[str] = []

    if any(spec.get(dim) for dim in ("eras", "rarities", "graders")):
        eras = [str(x) for x in (spec.get("eras") or [""])]
        rarities = [str(x) for x in (spec.get("rarities") or [""])]
        graders = [str(x) for x in (spec.get("graders") or [""])]
        for grader in graders:
            for era in eras:
                for rarity in rarities:
                    kw = template.format(era=era, rarity=rarity, grader=grader)
                    # 空維度會留下連續空白（"遊戯王  PSA"），Yahoo 會照收但兩個
                    # 只差空白數的查詢就成了兩個 cache key、兩次請求
                    kw = _WS_RE.sub(" ", kw).strip()
                    if kw:
                        out.append(kw)

    out.extend(str(x) for x in (spec.get("extra") or []) if str(x).strip())

    seen: set[str] = set()
    deduped: list[str] = []
    for kw in out:
        if kw not in seen:
            seen.add(kw)
            deduped.append(kw)

    cap = int(spec.get("max_queries", DEFAULT_MAX_COMPS_QUERIES) or DEFAULT_MAX_COMPS_QUERIES)
    if cap > 0 and len(deduped) > cap:
        print(f"[comps] 展開出 {len(deduped)} 個已售出查詢，超過上限 {cap}，截斷尾端")
        deduped = deduped[:cap]
    return deduped


@dataclass(slots=True)
class IngestReport:
    """一次入庫的結果。**擋掉幾筆、為什麼**跟收了幾筆一樣重要——

    沒有這份帳，行情表被垃圾汙染時你看不出來：實測 405 筆歷史 comps 裡
    有 175 筆（43%）是寶可夢、卡套、現代卡，就是因為當初 ingest 沒過濾也沒記帳。
    """

    kept: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        top = ", ".join(
            f"{k}×{v}" for k, v in sorted(self.reasons.items(), key=lambda kv: -kv[1])[:5]
        )
        return f"收 {self.kept} 筆、擋 {self.rejected} 筆" + (f"（{top}）" if top else "")


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


class CompsEngine:
    def __init__(self, cfg: Config, fx, store=None) -> None:
        self.cfg = cfg
        self.fx = fx
        self.store = store
        self.window_days = int(cfg.scoring["comps_window_days"])
        self.min_comps = int(cfg.scoring["min_comps"])
        self._index: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    def ingest_sold(self, listings: list[Listing]) -> IngestReport:
        """把「已售出」的標的餵進行情索引。

        **每一筆都必須過 parse_card + is_candidate**，與掃描在架標的用完全
        同一組判準（同源、同基準——工程原則 1）。以前這裡只擋「排除字或
        沒有鑑定機構」，比掃描端寬鬆得多，於是寶可夢（メガリザードンXEX）、
        卡套（CARDBOARD GOLD PSA鑑定品用スリーブ）、現代卡全進了行情表，
        再回頭當「1998-2004 卡的合理價」用——那是最貴的一種靜默錯誤。

        入庫時同時落結構化屬性（rarity/grader/grade/set_code/era_evidence），
        原始 title 永久保留：分桶是查詢時的決定，不是寫入時的決定。

        `sold_at` 優先採用來源帶回的真實成交時間（`raw["sold_at"]`，必須是
        UTC ISO 字串）。來源給不出時間才退回 now()——那是「我們什麼時候看到
        它已售出」，對 Buyee 的 sold_out 搜尋是唯一能知道的近似值（實測那些
        頁面**完全沒有成交時間**：搜尋頁的 tile 只有 SOLD／標題／價格，商品頁
        也沒有），但對 Yahoo 落札相場（視窗 180 天、comps 視窗 90 天）就會把
        半年前的成交蓋成今天的行情。時間戳與視窗必須同基準（工程原則 1）。

        **退回 now() 的那一批一律標 `sold_at_is_ingest=1`**（2026-08-02）。
        沒有這個旗標，下游分不出「這是成交時間」與「這是我們抓到的時間」，
        於是 90 天視窗對那批資料形同虛設、時間切分變成偽裝的平台切分。
        標記而不是猜：猜一個成交時間會讓所有數字看起來很正常，然後全部是錯的。
        """
        report = IngestReport()
        wl = self.cfg.watchlist
        fallback_sold_at = datetime.now(UTC).isoformat()
        for lst in listings:
            info = parse_card(lst.title, wl)
            ok, why = is_candidate(info, wl)
            if not ok:
                report.rejected += 1
                bucket = _reason_bucket(why)
                report.reasons[bucket] = report.reasons.get(bucket, 0) + 1
                continue
            sig = card_signature(lst.title, info)
            price_twd = self.fx.to_twd(lst.price, lst.currency, apply_markup=False)
            raw_sold_at = lst.raw.get("sold_at")
            has_real_time = isinstance(raw_sold_at, str) and bool(raw_sold_at)
            sold_at = raw_sold_at if has_real_time else fallback_sold_at
            self._index.setdefault(sig, []).append(
                {
                    "title": lst.title,
                    "price_twd": round(price_twd, 0),
                    "price_native": lst.price,
                    "currency": lst.currency.value,
                    "url": lst.url,
                    "site": lst.site.value,
                    "sold_at": sold_at,
                    # 0 = sold_at 是來源給的真實成交時間；1 = 是我們入庫的時間。
                    # 下游任何用到時間的查詢都必須看這一欄（見 store.load_comps）。
                    "sold_at_is_ingest": 0 if has_real_time else 1,
                    "confidence": "high",
                    "rarity": info.rarity,
                    "grader": info.grader.value,
                    "grade": info.grade,
                    "set_code": info.set_code,
                    "era_evidence": ",".join(info.era_evidence),
                    # 賣掉這筆的賣家（來源給不出就是 None，不猜）。
                    # 成交紀錄帶賣家，「同一賣家持續低於行情成交」才量得出來。
                    "seller_id": lst.seller_id,
                    # 卡名抽取還沒做（下一輪）。留空欄位而不是塞猜的值：
                    # 猜出來的卡名會直接變成分桶依據，錯了看不出來。
                    "card_name": None,
                }
            )
            report.kept += 1
        if self.store and report.kept:
            self.store.save_comps(self._index)
        return report

    def load_from_store(self) -> None:
        if not self.store:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.window_days)
        self._index = self.store.load_comps(since=cutoff)

    # ------------------------------------------------------------------
    def stats_for(self, listing: Listing, info: CardInfo) -> CompStats:
        sig = card_signature(listing.title, info)
        rows = self._index.get(sig, [])

        # 找不到完全相同的簽章時，退一步用卡號比對
        if not rows and info.set_code:
            rows = [
                r for key, lst in self._index.items()
                if key.startswith(f"{info.set_code}|") for r in lst
            ]

        prices = [float(r["price_twd"]) for r in rows if r.get("price_twd")]
        n = len(prices)

        if n >= self.min_comps * 2:
            conf = "high"
        elif n >= self.min_comps:
            conf = "medium"
        else:
            conf = "low"

        return CompStats(
            n=n,
            median_twd=round(statistics.median(prices), 0) if prices else None,
            p25_twd=round(_percentile(prices, 25), 0) if prices else None,
            p40_twd=round(_percentile(prices, 40), 0) if prices else None,
            p75_twd=round(_percentile(prices, 75), 0) if prices else None,
            window_days=self.window_days,
            confidence=conf,
            samples=sorted(rows, key=lambda r: r.get("sold_at", ""), reverse=True)[:8],
        )

    # ------------------------------------------------------------------
    def sold_queries(self, sources: dict[str, object]) -> list[tuple[str, str]]:
        """哪些 (發現管道, 關鍵字) 要另外跑「已售出」搜尋來累積行情。

        兩個來路，合併去重：

        1. **watchlist 的 `queries`**（舊行為）——判準是 source 自己宣告的
           `supports_sold`，不是名稱前綴。Yahoo 直抓（False，搜尋頁沒有成交
           紀錄）不進來；Buyee 系（True）進來。
        2. **`comps_queries` 的組合展開**（新）——稀有度 × 年代 × 機構。
           在架掃描要的是「新上架、可能定價錯誤」，行情累積要的是「涵蓋整個
           屬性空間、每個桶都有樣本」，兩者的查詢集合本來就不該是同一組：
           改這裡之前，watchlist 的 5 條在架查詢撐出來的樣本平均每個
           card_signature 只有 1.07 筆，統計上等於沒有行情——絕大多數訊號
           只能報「無足夠樣本」。

        `comps_queries.sources` 必須明列（不預設「所有 supports_sold 的來源」）：
        展開後有數十個關鍵字，讓它自動套用到每一條管道，請求數會直接乘上管道數。
        不在 registry 或不支援 sold 的名字一律跳過。

        comps 索引鍵是 card_signature、跨站台共用，所以任一站的成交價
        都能評估其他站在架的標的。
        """
        out: list[tuple[str, str]] = []
        for q in self.cfg.watchlist.get("queries", []):
            for source_name in q.get("sources", []):
                src = sources.get(source_name)
                if src is None or not getattr(src, "supports_sold", False):
                    continue
                out.append((source_name, q["keyword"]))

        spec = self.cfg.watchlist.get("comps_queries") or {}
        targets = [
            name for name in (spec.get("sources") or [])
            if getattr(sources.get(name), "supports_sold", False)
        ]
        # 先確定有人要跑才展開：展開會印截斷警告，沒有目標來源時印它只會誤導
        if targets:
            expanded = expand_comps_queries(spec)
            for source_name in targets:
                out.extend((source_name, kw) for kw in expanded)

        seen: set[tuple[str, str]] = set()
        deduped: list[tuple[str, str]] = []
        for pair in out:
            if pair not in seen:
                seen.add(pair)
                deduped.append(pair)
        return deduped

    # ------------------------------------------------------------------
    @property
    def sold_pages(self) -> int:
        """每個已售出查詢翻幾頁。

        與在架掃描的 `fetch.max_pages_per_query`（＝1）**刻意分開**：
        在架掃描每小時跑、翻頁只會重複拿到上一輪看過的東西；行情累積一天
        跑一兩次，而 Yahoo 落札相場的視窗有 180 天，翻頁是在挖存量、
        每一頁都是新資料。兩件事的最佳頁數不同，共用一個參數就會有一邊被犧牲。
        """
        spec = self.cfg.watchlist.get("comps_queries") or {}
        return max(1, int(spec.get("pages", 2)))

    def claim_sold_run(self, *, force: bool = False) -> tuple[bool, str]:
        """這一輪要不要跑已售出查詢？**有副作用：每呼叫一次計數 +1。**

        行情是以「週」為單位在變的，但排程每小時跑一輪。已售出查詢展開後
        有數十個關鍵字 × 數頁，每小時全跑一次等於每天對人家打幾千個請求，
        換來的是幾乎完全重複的資料（落札相場的日流量遠小於我們的抓取量）。
        所以節流：每 `comps_queries.every_n_runs` 輪才真的跑一次
        （每小時排程 → 12 代表一天兩次）。

        計數器落在 store 的 meta 表而不是記憶體：CLI 每輪都是全新的行程，
        記憶體變數的「節流」在這個部署形態下等於沒有節流。
        `every_n_runs <= 1` 或沒有 store（單元測試、dry-run 情境）→ 每輪都跑。

        `force=True` 無視節流（人工「我現在就要更新行情」的逃生門），
        但**計數器照樣前進**——手動跑過一次就等於這一輪的配額用掉了，
        不然人工跑幾次就把排程的節流洗掉了。

        回傳 `(要不要跑, 說明)`；說明會被印出來，讓「這輪為什麼沒有新行情」
        永遠有一句話交代——安靜地跳過與安靜地壞掉，外顯是一樣的。
        """
        spec = self.cfg.watchlist.get("comps_queries") or {}
        every = int(spec.get("every_n_runs", 1) or 1)
        if every <= 1 or self.store is None:
            return True, ""
        try:
            counter = int(self.store.get_meta(SOLD_RUN_COUNTER_KEY) or 0)
        except (TypeError, ValueError):
            counter = 0
        self.store.set_meta(SOLD_RUN_COUNTER_KEY, str((counter + 1) % every))
        if counter % every == 0:
            return True, f"第 {counter} 輪（每 {every} 輪跑一次）"
        if force:
            return True, f"第 {counter} 輪，本應節流（每 {every} 輪一次）但被 force 蓋過"
        return False, f"節流：第 {counter} 輪，每 {every} 輪才跑一次已售出查詢"
