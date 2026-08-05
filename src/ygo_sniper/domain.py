"""核心資料模型。

刻意用 stdlib dataclass 而非 pydantic：這層被 parser / scorer / store / web
四邊共用，保持零依賴才不會讓測試變慢。驗證留在 config.py。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 只為型別；執行期 import 會造成循環
    from .bidding import BidCeiling


class Site(str, Enum):
    """購買路徑／市集身分——**不是**發現管道。

    這個 enum 只回答兩個問題：「這筆標的要從哪個市集買」（settings.yaml 的
    routes.sites 用它決定可用運送路徑）、「external_id 屬於哪個 ID 空間」
    （`Listing.key` 用它去重）。資料是誰抓來的（Yahoo 直抓、Buyee 搜尋頁）
    看 `Listing.source`，絕不看這裡——Yahoo 直抓發現的標的一樣掛
    BUYEE_YAHOO，因為購買動作走 Buyee，且拍賣 ID 與 buyee.jp 同一空間。
    """

    BUYEE_MERCARI = "buyee_mercari"
    BUYEE_YAHOO = "buyee_yahoo"
    BUYEE_PAYPAY = "buyee_paypay"
    #: Mercari 台灣官方站（tw.mercari.com）。與 BUYEE_MERCARI 是**同一批貨、
    #: 不同購買路徑**，但必須分開成兩個 Site，理由有兩個且都會出人命：
    #: 1. ID 空間不同（TW 是 UUID、Buyee 是 `m\d+`／22 字 base62），混在一起
    #:    `Listing.key` 會把同一件商品裂成兩列或撞成一列。
    #: 2. **標價幣別不同**（TW 標新台幣、Buyee 標日圓）。掛成 BUYEE_MERCARI 的話
    #:    這筆會被 Buyee 那兩條 route 用「台幣數字當日圓」報價，成本差 4-5 倍。
    MERCARI_TW = "mercari_tw"
    #: 露天拍賣（ruten.com.tw）。台灣境內的二手市集——**這是唯一一個買賣雙方
    #: 都在台灣的 Site**，所以它的成本結構與上面三條日本路徑完全不同：
    #: 沒有代購費、沒有國際運費、沒有海外刷卡手續費（見 settings.yaml 的
    #: `ruten_local` route，以及 costs.quote_route 對 Currency.TWD 不套 markup）。
    #: 標價幣別以每筆商品自己的 `Currency` 欄位為準（實測 94% TWD、6% USD 的
    #: 海外賣家），**絕不假設是台幣**——那正是 MERCARI_TW 那條註記在講的坑。
    RUTEN = "ruten"
    EBAY = "ebay"


class HoldingLocation(str, Enum):
    """**貨現在的物理位置**。轉賣模型的地基。

    為什麼需要這個 enum：買方成本模型假設貨要運到台灣（集運攤提後每張約
    NT$290 雜費），而 venue 係數實測 Yahoo ×1.00 / Mercari ×2.14 /
    PayPay ×2.60 —— 三個都是**日本境內**市場。「在 Yahoo 買、在 Mercari 賣」
    看起來是 2 倍價差，但那條路徑要求貨**留在日本**；如果貨已經運到台灣，
    要賣回 Mercari JP 就得再付一次國際運費（而且需要日本收件地址）。

    沒有這個維度，兩個不同物理位置的價格會被直接相減 —— 那正是工程原則 1
    說的混源比較，而且方向是「看起來有套利空間」，是會讓人下單的方向。
    """

    #: 貨還在日本（Buyee 倉庫、或你自己的日本轉運地址）。日本境內賣場只認這裡。
    JP_WAREHOUSE = "jp_warehouse"
    #: 貨已經運抵台灣。台灣賣場、以及從台灣寄出的 eBay 只認這裡。
    TW_HOME = "tw_home"


class Currency(str, Enum):
    JPY = "JPY"
    USD = "USD"
    TWD = "TWD"


class SaleKind(str, Enum):
    """**這個價格是怎麼形成的**：買家喊上去的，還是賣家開的。

    Seller Alpha 問的是「這個賣家開的價比同儕低多少」，所以兩者不可混池
    （工程原則 1：同源、同基準）：

    - `AUCTION`：競標結標價（ヤフオク落札）。反映**買家搶到多高**，是市場
      需求的結果——賣家只設了開始価格，最終那個數字是別人喊出來的。
    - `FIXED`：定價成交／定價在架（Yahoo!フリマ、Mercari、ヤフオク一口價即決、
      各站的在架定價）。反映**賣家開多少**。
    - `UNKNOWN`：拿不到證據。**不准當成 `FIXED` 處理**——那會讓一筆沒有依據的
      成交安靜地變成同儕，而混池的錯誤方向永遠是「這個賣家好便宜」
      （本專案第三節的老病）。不知道就不計分，與「湊不到同儕就拒答」同一個哲學。

    2026-08-06 實測動機（正式庫）：468 個 Alpha 計分點裡有 251 筆是 Yahoo 競標
    結標（53.6%），而那 13 筆 Yahoo 一口價標的的同儕 24 筆中有 22 筆是競標結標
    ——賣家自己開的一口價被拿去跟市場搶出來的價比。
    """

    AUCTION = "auction"
    FIXED = "fixed"
    UNKNOWN = "unknown"


class Grader(str, Enum):
    PSA = "PSA"
    ARS = "ARS"
    BGS = "BGS"
    UNKNOWN = "UNKNOWN"


class TriageState(str, Enum):
    """人工決策狀態機。沒有這個，你第三天就會重複詢問同一個賣家。"""

    NEW = "new"
    WATCHING = "watching"
    ASKED_SELLER = "asked_seller"
    OFFER_SENT = "offer_sent"
    IN_BUNDLE = "in_bundle"       # 已丟進湊單籃
    BOUGHT = "bought"
    SKIPPED = "skipped"
    EXPIRED = "expired"           # 標的已消失


class CardBucket(str, Enum):
    """卡片**分類**，與 `TriageState` 正交（一張卡同時有一個 state 與一個 bucket）。

    為什麼不是第九、第十種 state：使用者說高價卡「**也是在觀察中**」，
    稀有卡的「等降價再進場」是長期成立的指令。state 是互斥的流程欄位，
    把分類塞進去的話，對一張高價卡按下「已出價」的瞬間分類就被覆寫掉了
    ——那是不可逆的資料遺失，而且是靜默的（使用者只會發現分頁裡少了一張）。

    所以 bucket 獨立存一欄：改 state 不動 bucket，改 bucket 不動 state
    （唯一的例外是 `Store.update_bucket` 會把 `new` 升成 `watching`，
    因為「分類了」本身就代表使用者開始盯它了）。

    刻意**沒有**自動判定：「什麼算貴」「哪些是一刷」有主觀成分，判錯會誤導。
    """

    HIGH_VALUE = "high_value"     # 貴的 PSA9/10、1999 真紅眼、SM-51……：也在觀察中
    RARE = "rare"                 # 2002-2003 比賽卡、限量 1st edition：等降價再進


class Flag(str, Enum):
    FREE_CARD = "free_card"                    # 到手成本 < 鑑定費
    DISCOUNT = "discount"                      # 顯著低於 comps
    SHIPPING_KILLS_IT = "shipping_kills_it"    # 卡便宜但運費吃掉
    #: 純結構判斷：運費＋雜費佔到手成本超過門檻（`scoring.overhead_ratio_alert`）。
    #: 與 SHIPPING_KILLS_IT 的差別是**它不需要行情樣本**——行情要等 comps 累積，
    #: 但「US$30 的卡收 US$32 運費」在結構上就不可能划算，這件事現在就看得出來。
    #: 兩個旗標可以同時成立，看得出是哪一種觸發（reason 會寫明）。
    HIGH_OVERHEAD = "high_overhead"
    NEEDS_SHIPPING_ASK = "needs_shipping_ask"  # 不確定寄不寄台灣
    NEEDS_BUNDLE_ASK = "needs_bundle_ask"      # 同賣家多筆，可問合併運費
    OFFER_CHANCE = "offer_chance"              # 值得丟 offer
    THIN_COMPS = "thin_comps"                  # 參考資料不足，自己看照片
    SUSPICIOUS_CHEAP = "suspicious_cheap"      # 便宜到不合理，可能有雷
    # --- 競標管道（2026-08-02）。這三個只會出現在 price_kind="current_bid" 的標的上 ---
    LIVE_AUCTION = "live_auction"              # 競標中：目前出價會漲，現在的成本沒有意義
    BID_WORTH = "bid_worth"                    # 目前出價 < 你的出價上限 → 可以出手
    BID_NO_CEILING = "bid_no_ceiling"          # 樣本不足以給上限（紅線：不准猜一個數字）
    #: eBay 賣家不寄台灣，但使用者有美國地址（settings.yaml `buying.us_ship_zip`）
    #: 可收。**只是資訊旗標**：這種標的不丟掉（dashboard 要看得到），但不進出價
    #: 上限、不進推播——美國→台灣的轉運成本未建模，給任何數字都會誤導（誠實邊界）。
    US_SHIP_OPTION = "us_ship_option"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Listing:
    """一筆在架上的標的（尚未計算成本）。"""

    site: Site
    external_id: str
    title: str
    url: str
    price: float
    currency: Currency
    image_url: str | None = None
    seller_id: str | None = None
    shipping_cost: float | None = None      # 平台已知的運費（eBay 才有）
    ships_to_tw: bool | None = None         # None = 不確定，要問
    best_offer_enabled: bool = False
    listed_at: datetime | None = None
    is_sold: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    # 以下兩欄必須留在欄位清單最尾端且帶預設值：slots dataclass 的欄位順序
    # 就是位置參數順序，插在中間會讓既有的位置參數呼叫全部錯位。
    source: str = ""                        # 發現管道（如 "yahoo_direct"），只做觀測歸因，絕不參與成本計算
    origin_url: str | None = None           # 發現端原生商品頁（Yahoo/PayPay）；url 一律放購買端（Buyee）
    #: 競標結標時間（**帶時區的 UTC datetime**，來源是 Yahoo 的 epoch 秒）。
    #: 定價標的永遠是 None。用 datetime 而不是字串：競標視圖要靠它排序與算倒數，
    #: 而「哪個時區」是這種欄位最容易出人命的地方（差 9 小時 = 錯過整批結標）。
    end_time: datetime | None = None
    bids: int | None = None                 # 目前出價次數。None = 這個來源沒給，不是 0

    @property
    def key(self) -> str:
        return f"{self.site.value}:{self.external_id}"


@dataclass(slots=True)
class CardInfo:
    """從標題解析出來的卡片屬性。全部都是 best-effort，會錯，所以要留 raw title。"""

    grader: Grader = Grader.UNKNOWN
    grade: float | None = None
    in_era: bool = False                    # 是否落在 1998-2004
    era_evidence: list[str] = field(default_factory=list)
    set_code: str | None = None
    language: str | None = None             # "JP" / "EN" / "ASIA" / None
    excluded_by: str | None = None          # 命中排除字時記下來，方便調 config
    # 稀有度**不是**年代證據（現代卡也有アルティメットレア），所以獨立成欄，
    # 不進 era_evidence。抽不到就是 None，不猜。新欄位必須留在尾端且帶預設值。
    rarity: str | None = None               # ultimate/secret/parallel/ultra/super/normal_rare/normal/rare
    # 分數是**從哪裡抽到的**："title" / "description" / None（沒抽到）。
    # 標題寫的分數與商品描述裡撈出來的分數可信度不同（描述是賣家自由文字，
    # 還常夾 SEO 關鍵字堆），合成同一個欄位就再也分不出來——而分數直接乘進
    # 公允價，錯的方向正好是「上限開太高」。見 parsers/grade.resolve_grade。
    grade_source: str | None = None
    # 年份否決（`"year:2007"`／None）：標題寫了域外年份（1990-1997／2005-2029）
    # 且沒有任何域內年份時，即使命中了初期／舊卡號也不是目標年代。
    # 與 `in_era=False` 一起設定，但**原因要分得出來**：「標題自己說它是 2007」
    # 與「什麼年代線索都沒有」在調 watchlist 時的處置完全不同。
    # 見 parsers/card.py 的 `_OUT_OF_ERA_YEARS`。新欄位必須留在尾端且帶預設值。
    era_veto: str | None = None


@dataclass(slots=True)
class RouteQuote:
    """單一運送路徑的成本試算結果。"""

    route: str
    label: str
    landed_twd: float
    item_twd: float
    fee_twd: float
    shipping_twd: float
    bundle_size: int

    @property
    def overhead_twd(self) -> float:
        """到手成本裡不是卡本身的那一段（手續費＋運費）。"""
        return self.fee_twd + self.shipping_twd

    @property
    def overhead_ratio(self) -> float:
        """到手成本裡有多少比例不是卡本身。越高越像是在買運費。"""
        if self.landed_twd <= 0:
            return 0.0
        return self.overhead_twd / self.landed_twd

    #: `to_dict` / `from_dict` 認得的欄位。property（overhead_*）**不在**這裡：
    #: 它們是算出來的，讀回來時要重新算，不能從 JSON 灌回去。
    _FIELDS = (
        "route", "label", "landed_twd", "item_twd", "fee_twd", "shipping_twd", "bundle_size",
    )

    def to_dict(self) -> dict[str, Any]:
        """序列化。**overhead_ratio 要一起出門**——前端自己拿
        (fee+shipping)/landed 再算一次的話，這個比例就有兩份定義，
        哪天成本模型多一個欄位（例如關稅），畫面上的佔比會安靜地少算（工程原則 1）。
        """
        d = asdict(self)
        d["overhead_twd"] = round(self.overhead_twd, 2)
        d["overhead_ratio"] = self.overhead_ratio
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RouteQuote:
        """從 payload 的 route dict 還原（忽略 overhead_* 這類算出來的欄位）。

        存在的理由：dashboard 讀的是 db 裡的 payload，而「運費佔比」的判準
        必須跟掃描當下同一份（`overhead_ratio`）——把 dict 變回 RouteQuote
        才能用同一個 property，而不是在 web 層再寫一次除法。
        """
        return cls(**{k: d[k] for k in cls._FIELDS if k in d})


@dataclass(slots=True)
class CompStats:
    """comps（近期成交價）統計。"""

    n: int
    median_twd: float | None
    p25_twd: float | None
    p40_twd: float | None
    p75_twd: float | None
    window_days: int
    confidence: str = "low"      # low / medium / high
    samples: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class Signal:
    """最終產出：一筆值得你看一眼的標的。"""

    listing: Listing
    card: CardInfo
    best_route: RouteQuote
    all_routes: list[RouteQuote]
    comps: CompStats
    flags: list[Flag]
    score: float
    reason: str
    state: TriageState = TriageState.NEW
    first_seen: datetime = field(default_factory=_now)
    last_seen: datetime = field(default_factory=_now)
    note: str = ""
    #: 競標標的的出價上限（`bidding.BidCeiling`）。定價標的一律 None——
    #: 「可以直接買」的東西不需要上限，硬給一個只會讓兩種語意混在一起。
    #: 新欄位必須留在尾端且帶預設值（slots dataclass 的位置參數順序）。
    bid: BidCeiling | None = None

    @property
    def discount_pct(self) -> float | None:
        if self.comps.median_twd:
            return (self.comps.median_twd - self.best_route.landed_twd) / self.comps.median_twd
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["listing"]["site"] = self.listing.site.value
        d["listing"]["currency"] = self.listing.currency.value
        # 結標時間一律轉成 ISO 8601 字串再出門：payload 會被 json.dumps(default=str)
        # 序列化，而 `str(datetime)` 產出的是 "2026-08-02 10:00:00+00:00"（空格分隔），
        # 部分瀏覽器的 `new Date()` 解不出來 → 前端倒數會顯示 NaN 而且沒有任何錯誤。
        end = self.listing.end_time
        d["listing"]["end_time"] = end.isoformat() if end is not None else None
        d["card"]["grader"] = self.card.grader.value
        # 成本拆解要能在 dashboard 上展開，所以每一條 route 的
        # overhead_twd／overhead_ratio 跟著出門（見 RouteQuote.to_dict）。
        d["best_route"] = self.best_route.to_dict()
        d["all_routes"] = [q.to_dict() for q in self.all_routes]
        d["flags"] = [f.value for f in self.flags]
        d["state"] = self.state.value
        d["discount_pct"] = self.discount_pct
        return d
