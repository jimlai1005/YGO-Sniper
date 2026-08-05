"""Seller Alpha：賣家折價指標與可解釋分數（Phase 3-4）。

這個模組回答一個問題：**哪些賣家「持續」比同儕便宜**，值得盯著。
它決定使用者把注意力與資金放在誰身上，所以整份設計的偏好是
「寧可少給幾個賣家分數，也不要給一個建立在偏誤上的高分」。

---------------------------------------------------------------------------
## 為什麼主指標不是「賣家售價 ÷ 我們模型的公允價」

那個做法會把**模型的分段偏誤**誤判成賣家 alpha。實測依據（本專案先前量過，
見 `bidding.EvidenceGate.check` 的 broken_bucket 那段）：`L3 × Mercari` 那個
切片的點估計高估 5.9 倍、下尾違反率 100%。也就是說任何**專賣便宜普卡**的賣家，
在模型眼裡都長得像「持續低於公允價」——但那是模型錯，不是賣家便宜。

本棒實測（2026-08-04，正式庫）把這件事量了出來：模型絕對法給 eBay 賣家的
中位比值是 0.24-0.60（＝「40-80% 折價」）**全站皆然**——comps 表裡一筆 eBay
成交都沒有（1009 Mercari／787 Yahoo／469 PayPay），平台係數對 eBay 根本估不出來。
最刺眼的一個：`ebay:collectiblemore` 模型說 0.57（看起來是 alpha），
同儕相對說 **1.59**（比同儕貴 59%）——**符號相反**。

所以主指標是**同儕相對**：同一筆標的，拿「這個賣家的價」除以「同卡同版次
同稀有度同分數、同一個站、同一種價格基準的其他賣家的價」。兩邊都來自市場，
模型偏誤在相除時抵消。模型絕對法仍然算，但只當**輔助欄位**並標明它承受模型偏誤
（`SellerItem.model_ratio`），永遠不進分數。

## 同儕的四個必須對齊的維度（工程原則 1：同源、同單位、同處計算）

1. **同一種價格基準**（`basis`）。定價（buyout／fixed）、競標中出價（current_bid）、
   成交價（comps）是三種不同的東西。拿「競標第 3 天的出價」比「別人的一口價」，
   每一個賣競標的賣家都會憑空長出巨大折價——實測若不分基準，可比數從 31 暴增到
   91，多出來的六成全是這種假折價。**競標中出價完全不進指標**：那不是賣家的定價
   決定，是市場當下的出價，而且它與剩餘時間強相關。
2. **同一個站**。Yahoo 與 Mercari 的價格水準實測差 2 倍以上（venue_study）。
   跨站比＝把平台差算成賣家 alpha。
3. **同一張卡＋同版次**。同稀有度同分數的池子裡，青眼白龍與雜魚普卡差幾十倍
   ——用「同稀有度×同分數」當同儕，量到的是**卡種組合**不是賣家定價
   （實測那個層級的比值散布在 0.03 到 57 倍之間）。所以 `TIER_STRATUM`
   **不計分**，只在 drill-down 當觀察值顯示。英文卡的 1st Edition 與 Unlimited
   價差可達數倍，所以版次也進鍵（實測成本：可比數 48→43，結論不變）。
4. **不能是自己**。同賣家的其他標的一律排除。賣家未知的列**允許**當同儕——
   混進自己的列會把同儕中位拉向自己的價，方向是**低估 alpha**（保守）。

## 為什麼「持續性」的權重高於「深度」

一次大折價可能只是那張卡有裂痕、或那筆是福袋。八次穩定小折價才是可複製的行為。
結構上用四件事保證這個偏好：

- **中位數**（不是平均）：八筆裡的一筆極端值動不了中位數。
- **Winsorize**：每筆的對數折價先夾在 ±ln2（±50%）內，單筆再誇張也只算 50%。
- **收縮**（`_shrink_toward_zero`，k=4，與 `valuation.Params.shrink_k` 同源）：
  n=1 只保留 20% 的折價，n=8 保留 67%。
- **配分**：深度上限 40 分，一致性（30）＋廣度（30）共 60 分，而後兩者在 n 小時
  必然拿不到分。實測 8×12% 折價得 ~62 分；3 筆裡只有 1 筆 70% 折價得 <10 分。

## 「持續性」不必等帳本累積（2026-08-04 修正的方法論錯誤）

原本的設計是「從今天開始記在架觀測，四週後才有跨度」——那是把一件**已經
發生過的事**當成還沒發生的事在等。每個平台都存著歷史成交：Yahoo 落札相場
有 180 天 archive、Yahoo!フリマ 的賣家頁單頁就橫跨半年，而且**每一筆都帶
賣家與真實成交時間**。挖出來就有（見 `history` 模組）。

所以跨度有兩種，**必須分開報**（工程原則 1：同源）：

- **在架帳跨度**（`listing_obs_span_days`）＝我們盯了這個市場多久。
- **成交帳跨度**（`comps_span_days`）＝平台的歷史成交紀錄橫跨多久。

一個賣家的成交跨度 147 天，不代表我們盯了他 147 天的在架定價——兩者的
證據強度不同，加總或取大值當一個數字報就是混源。`SellerMetrics
.observation_span_days` 是**這個賣家自己的**跨度（兩種真實時間戳的聯集），
持續性判定看它、門檻是 `PERSISTENCE_MIN_DAYS`。

⚠️ **成交價（sold）與在架價（ask）仍然不可混池**。歷史資料大量進來會改變
分數，但它改變的是「有多少證據」，不是「拿什麼跟什麼比」：同儕比對永遠在
同一個 basis 內配對（`SCORING_BASES` 各自成池），這條線不因為資料變多而放寬。

## 誠實邊界（2026-08-04 實測）

- eBay **沒有成交價可用**（Marketplace Insights API 403、賣家頁 sold 篩選
  403，見 `history` 模組頂註）。eBay 賣家的持續性只能靠在架帳慢慢累積，
  而在架帳的跨度目前只有 2 天上下——**不可以拿在架價冒充成交價來補**。
- Yahoo 賣家頁評價擷取已完成（2026-08-04 前後接上：`yahoo.seller_feedback`
  → `venue_study.listing_row` → `sellers.feedback_pct`），但覆蓋率跟著監控
  輪替走，不是全量賣家都有——2026-08-05 實測：8 個監控中的 Yahoo 賣家裡
  4 個已抓到、其餘還沒輪到。沒被監控的 Yahoo 賣家（以及還沒輪到的那些）
  風險維度仍是**未知**，不是「沒問題」——`risk_known=False`。
"""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from .cards import CardIndex, extract_title_codes
from .domain import SaleKind

# ---------------------------------------------------------------------------
# 價格基準（basis）
# ---------------------------------------------------------------------------
#: 定價：一口價／固定價。使用者現在就能照這個價買到手。
BASIS_ASK = "ask"
#: 成交價：comps。已經發生的交易。
BASIS_SOLD = "sold"
#: 競標中的目前出價。**不是賣家的定價決定**，不進任何指標（見模組頂註）。
BASIS_BID = "bid"

BASIS_LABEL: dict[str, str] = {
    BASIS_ASK: "定價",
    BASIS_SOLD: "成交價",
    BASIS_BID: "競標中出價",
}

#: `listing_obs.price_kind` → basis。未知的一律當競標（保守：不進指標）。
_PRICE_KIND_BASIS: dict[str, str] = {
    "buyout": BASIS_ASK,
    "fixed": BASIS_ASK,
    "current_bid": BASIS_BID,
}

#: 進得了折價指標的基準。
SCORING_BASES: tuple[str, ...] = (BASIS_ASK, BASIS_SOLD)


def basis_of(price_kind: str | None) -> str:
    """價格種類 → 比較基準。**未知一律當競標**（不進指標）。

    預設往保守的方向倒：認錯一個定價的代價只是少一筆樣本，
    認錯一個競標出價的代價是憑空長出一個假折價。
    """
    return _PRICE_KIND_BASIS.get((price_kind or "").strip(), BASIS_BID)


# ---------------------------------------------------------------------------
# 成交型態（sale_kind）：`basis` 之下的第二個「同一把尺」維度
# ---------------------------------------------------------------------------
#: 同一個 `sold` 基準裡還混著兩種語意（2026-08-06 修正）：
#:
#: - **競標結標**（ヤフオク落札）＝ 買家搶到多高。賣家只設了開始価格，
#:   最終那個數字是**市場喊出來的**，不是他的定價決定。
#: - **定價成交**（Yahoo!フリマ／Mercari／ヤフオク一口價即決）＝ 賣家開多少。
#:
#: 這個榜問的是「這個賣家開的價比同儕低多少」，所以兩者不可互為同儕。
#: 實測動機：468 個計分點裡 251 筆是競標結標（53.6%）；13 筆 Yahoo 一口價
#: 標的的 24 個同儕裡有 22 筆是競標結標——賣家自己開的一口價被拿去跟市場
#: 搶出來的價比，靠 winsorize 才沒把分數炸掉（ratio 3.61／15.29／82.32）。
SALE_AUCTION = SaleKind.AUCTION.value
SALE_FIXED = SaleKind.FIXED.value
#: 拿不到證據。**不進任何比較**（既不當標的也不當同儕）——與「湊不到同儕
#: 就拒答」同一個哲學。把它當 `fixed` 才是靜默失敗。
SALE_UNKNOWN = SaleKind.UNKNOWN.value

SALE_KIND_LABEL: dict[str, str] = {
    SALE_AUCTION: "競標結標",
    SALE_FIXED: "定價",
    SALE_UNKNOWN: "型態未知",
}

#: 進得了折價指標的成交型態。
SCORING_SALE_KINDS: tuple[str, ...] = (SALE_AUCTION, SALE_FIXED)


def sale_kind_of_price_kind(price_kind: str | None) -> str:
    """在架列的 `price_kind` → 成交型態。

    在架帳沒有「成交」，但**價格怎麼形成的**這個問題一樣成立：一口價／
    定價是賣家開的（`fixed`），競標中出價是買家喊的（`auction`）。
    對不上任何一種就是 `unknown`——與 `basis_of` 的保守預設同向
    （那種列的 basis 也已經被判成競標中出價，本來就不進指標）。
    """
    kind = (price_kind or "").strip()
    if kind in ("buyout", "fixed"):
        return SALE_FIXED
    if kind == "current_bid":
        return SALE_AUCTION
    return SALE_UNKNOWN


def basis_kind_label(basis: str, sale_kind: str) -> str:
    """畫面用的一句話標籤：**看得出來這筆在跟哪一池比**。

    只有 `sold` 需要展開（`ask` 本來就全是賣家開的價）：
    「成交價（競標結標）」與「成交價（定價成交）」在畫面上長得不一樣，
    使用者才看得出同儕列表裡有沒有混池。
    """
    base = BASIS_LABEL.get(basis, basis)
    if basis != BASIS_SOLD:
        return base
    if sale_kind == SALE_AUCTION:
        return f"{base}（競標結標）"
    if sale_kind == SALE_FIXED:
        return f"{base}（定價成交）"
    return f"{base}（型態未知・不計分）"


# ---------------------------------------------------------------------------
# 版次（英文卡的 1st Edition 溢價可達數倍，必須進同儕鍵）
# ---------------------------------------------------------------------------
_FIRST_ED_RE = re.compile(r"1st\s*ed|first\s*ed|1e\b|初版|１ｓｔ", re.IGNORECASE)

EDITION_FIRST = "1st"
EDITION_OTHER = "unl"
EDITION_LABEL = {EDITION_FIRST: "1st Ed", EDITION_OTHER: "非 1st／未標"}


def edition_of(title: str | None) -> str:
    """標題 → 版次。抓不到一律當「非 1st」——**不是猜「可能是」**。

    寧可把一張沒寫版次的 1st 版歸到 unl 桶（後果：它跟同樣沒寫的比，公平），
    也不要把 unl 誤升成 1st（後果：拿 1st 的高價當同儕，憑空長出折價）。
    """
    return EDITION_FIRST if _FIRST_ED_RE.search(title or "") else EDITION_OTHER


# ---------------------------------------------------------------------------
# 可比層級（tier）
# ---------------------------------------------------------------------------
#: 同基準 × 同站 × 同卡 × 同版次 × 同稀有度 × 同機構 × 同分數。
TIER_STRICT = "T1"
#: 同上但**放寬稀有度**（稀有度抽取覆蓋率只有 26%，硬要求會把樣本殺光）。
TIER_CARD = "T2"
#: 同基準 × 同站 × 同稀有度 × 同機構 × 同分數（**沒有卡名**）。
#: **不計分**：這一層量到的是卡種組合，不是賣家定價（見模組頂註）。
TIER_STRATUM = "T3"

TIER_LABEL: dict[str, str] = {
    TIER_STRICT: "同卡×同版次×同稀有度×同分數",
    TIER_CARD: "同卡×同版次×同分數（稀有度放寬）",
    TIER_STRATUM: "同稀有度×同分數（無卡名・卡種混合，不計分）",
}

#: 只有這兩層進折價指標與分數。
SCORING_TIERS: tuple[str, ...] = (TIER_STRICT, TIER_CARD)

#: 嘗試順序：最嚴的先，命中就停。
TIER_ORDER: tuple[str, ...] = (TIER_STRICT, TIER_CARD, TIER_STRATUM)


# ---------------------------------------------------------------------------
# 參數
# ---------------------------------------------------------------------------
#: 單筆對數折價的夾限。ln2 ＝ 一倍價差。**單筆再誇張也只算 50% 折價**：
#: 極端的單筆多半是「那張卡有問題」或「那是一整套」，不是賣家的定價行為。
WINSOR_LOG = math.log(2.0)

#: 深度滿分對應的收縮後折價（0.288 ≈ ln(1/0.75)，也就是「穩定便宜 25%」）。
DEPTH_FULL_LOG = math.log(1 / 0.75)

#: 一致性滿分對應的 IQR（對數）。0 ＝ 每一筆折價都一樣；ln2 ＝ 散到一倍。
CONSISTENCY_IQR_FULL = math.log(2.0)

#: 觀測跨度低於這個天數，就說「這是橫斷面的便宜，不是持續的便宜」。
#: 14 天不是隨便挑的：一個賣家的一批貨在架期常是 1-2 週，跨度小於它等於
#: **只看到同一批貨**，量不出「他下一批也這樣定價」。寫成常數是因為
#: `persistence_note`、`caveats`、覆蓋率報告三處都要用同一個門檻——
#: 三個地方各寫一個 14，改的時候必然漏掉一個，然後畫面上的數字互相打架。
PERSISTENCE_MIN_DAYS = 14.0


@dataclass(slots=True, frozen=True)
class AlphaParams:
    """分數引擎的門檻與配分。**每一個值都要說得出依據**。"""

    #: 最低可比標的數。低於此**不給分數**（`SellerScore.ok=False`）。
    min_comparable: int = 3
    #: 最低可比「相異卡」數。同一張卡刊三次不算三個證據——賣家把同一張卡
    #: 重上架（yahoo 的「その1／その2」）在原始計數裡看起來像活躍，其實是一件事。
    min_distinct_cards: int = 3
    #: 每筆標的至少要幾個同儕才採計。1 就夠（中位數在賣家層級再取一次），
    #: 但 `PeerMatch.peer_n` 會一路帶到畫面上，讓「這個折價背後幾筆」看得見。
    min_peers: int = 1
    #: 收縮強度。與 `valuation.Params.shrink_k` 同一個值（4）：那裡的依據是
    #: n/(n+4) 在 n=10 越過 0.7（見 `valuation.N_BUCKETS` 的註）。同一個專案裡
    #: 兩個「樣本少就別太相信」的地方用同一個 k，不各自發明。
    shrink_k: float = 4.0
    #: 各元件上限。深度刻意低於「一致性＋廣度」，見模組頂註。
    max_depth: float = 40.0
    max_consistency: float = 30.0
    max_breadth: float = 30.0
    max_risk_penalty: float = 30.0
    #: 廣度滿分要幾張相異卡。
    breadth_full_cards: int = 8
    #: 廣度的時間那一半滿分要幾天觀測跨度。
    breadth_full_days: float = 30.0

    @classmethod
    def from_config(cls, cfg: Any) -> AlphaParams:
        b = dict(getattr(cfg, "seller_alpha", None) or {})
        out = cls()
        kw: dict[str, Any] = {}
        for f, caster in (
            ("min_comparable", int), ("min_distinct_cards", int), ("min_peers", int),
            ("shrink_k", float), ("max_depth", float), ("max_consistency", float),
            ("max_breadth", float), ("max_risk_penalty", float),
            ("breadth_full_cards", int), ("breadth_full_days", float),
        ):
            if f not in b:
                continue
            try:
                value = caster(b[f])
            except (TypeError, ValueError):
                print(f"[warn] seller_alpha.{f}={b[f]!r} 不是數字，改用 {getattr(out, f)}")
                continue
            if value < 0:
                print(f"[warn] seller_alpha.{f}={value} 是負數，改用 {getattr(out, f)}")
                continue
            kw[f] = value
        return cls(**kw) if kw else out


# ---------------------------------------------------------------------------
# 市場列（listing_obs ＋ comps 的統一形狀）
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class MarketRow:
    """一筆市場觀測。在架列與成交列共用同一個形狀，**但 `basis` 分得清清楚楚**。

    `seller_key` 是 `"{site}:{seller_id}"`（與 `store.sellers` 同一套鍵）；
    None ＝ 這一列不知道賣家（多數 comps 是這種），它**仍然可以當同儕**。
    """

    key: str
    site: str
    basis: str
    price_twd: float
    title: str
    #: **這個價格是買家喊上去的還是賣家開的**（`SALE_AUCTION`／`SALE_FIXED`／
    #: `SALE_UNKNOWN`）。`basis` 之下的第二個同源維度，一樣進同儕鍵。
    #:
    #: 預設 `SALE_FIXED` ＝「賣家開的價」：在架列（listing_obs）全部屬於這種。
    #: **成交列一律在 `market_rows_from_store` 明確指定**，db 欄位讀不到值就是
    #: `SALE_UNKNOWN`——沒有任何一條路徑會讓一筆成交安靜地預設成定價。
    sale_kind: str = SALE_FIXED
    seller_key: str | None = None
    card_name: str | None = None
    edition: str = EDITION_OTHER
    rarity: str | None = None
    grader: str | None = None
    grade: float | None = None
    landed_twd: float | None = None
    url: str | None = None
    observed_at: str | None = None
    #: `observed_at` 是不是真的時間。**False ＝ 那是我們入庫的時間**
    #: （Buyee 系的已售出頁根本沒有成交時間，見 `store._COMPS_ATTR_COLUMNS`
    #: 的 `sold_at_is_ingest` 註記）。拿入庫時間算「觀測跨度」等於量到我們自己
    #: 什麼時候跑的抓取，不是賣家什麼時候在賣——那會把持續性憑空變出來。
    observed_at_is_real: bool = True
    set_series: str | None = None
    #: 來源表："listing_obs" / "comps"。報表要說得出這個數字是哪張帳本來的。
    source_table: str = "listing_obs"

    @property
    def stratum(self) -> tuple[Any, ...]:
        return (self.rarity, self.grader, self.grade)


#: 沒有編號的套組寫法（"LOD-LEGACY OF DARKNESS"）。要求前綴恰好 3 個大寫字母，
#: 「YU-GI-OH!」那種 2 字母的分隔天生不會命中。
_SET_PREFIX_RE = re.compile(r"\b([A-Z]{3})-[A-Z]{3,}")


def _set_series_of(title: str | None) -> str | None:
    """標題 → 套組系列（卡號前綴，例如 LOB-054 → LOB）。抓不到回 None。

    先走 `cards.extract_title_codes`（有編號的正規卡號），抓不到再用前綴樣式
    ——eBay 的英文標題常寫成 `LOD-LEGACY OF DARKNESS 1ST ED #075`，
    沒有 `XXX-000` 的形狀，只走卡號會讓整個集中度指標對 eBay 全盲。
    """
    for code in extract_title_codes(title or ""):
        head = code.split("-")[0].strip().upper()
        if head:
            return head
    m = _SET_PREFIX_RE.search(title or "")
    return m.group(1) if m else None


def _card_name_of(index: CardIndex | None, title: str | None, fallback: str | None) -> str | None:
    """標題 → 目標年代卡名。**卡名在這裡才決定**（與 `valuation.obs_from_comps` 同哲學）。

    `comps.card_name` 只是物化快取（2026-08-05 實測 2554 筆裡只有 892 筆有值），
    而 `listing_obs.card_name` 目前 **0/542**——只靠欄位會讓在架側整個算不出來。
    2026-08-05 實測用 `CardIndex` 現場比對：listing_obs 506/542、
    comps 2346/2554 有卡名。
    """
    if index is not None and index.available:
        m = index.match(title or "")
        return m.name_ja if (m and m.in_era) else None
    return fallback or None


def _f(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def market_rows_from_store(
    store: Any, index: CardIndex | None = None, *, limit: int = 50000
) -> list[MarketRow]:
    """把 `listing_obs`（在架）與 `comps`（成交）攤成同一組 `MarketRow`。

    兩張帳本都進來是刻意的：同儕池越大，可比覆蓋率越高。但**基準永遠分開**
    （`basis`），比較時不會跨基準配對。
    """
    rows: list[MarketRow] = []
    for r in store.listing_obs(limit=limit):
        price = _f(r.get("price_twd"))
        if price is None:
            continue
        site = str(r.get("site") or "")
        sid = r.get("seller_id")
        title = r.get("title") or ""
        rows.append(
            MarketRow(
                key=str(r.get("key") or ""),
                site=site,
                basis=basis_of(r.get("price_kind")),
                sale_kind=sale_kind_of_price_kind(r.get("price_kind")),
                price_twd=price,
                title=title,
                seller_key=f"{site}:{sid}" if (site and sid) else None,
                card_name=_card_name_of(index, title, r.get("card_name")),
                edition=edition_of(title),
                rarity=(r.get("rarity") or None),
                grader=(r.get("grader") or None),
                grade=_f(r.get("grade")),
                landed_twd=_f(r.get("landed_twd")),
                url=r.get("url"),
                observed_at=r.get("first_seen"),
                set_series=_set_series_of(title),
                source_table="listing_obs",
            )
        )
    for r in store.comps_by(limit=limit):
        price = _f(r.get("price_twd"))
        if price is None:
            continue
        site = str(r.get("site") or "")
        sid = r.get("seller_id")
        title = r.get("title") or ""
        rows.append(
            MarketRow(
                key=f"comps:{r.get('id')}",
                site=site,
                basis=BASIS_SOLD,
                #: 欄位是 NULL（回填前的舊列）→ `SALE_UNKNOWN`，**不是** `fixed`。
                #: 這是整條路徑上唯一「沒有值」會出現的地方，也是最容易安靜
                #: 出錯的地方：猜 fixed 會把一批落札價塞進定價池（見 SALE_UNKNOWN）。
                sale_kind=str(r.get("sale_kind") or SALE_UNKNOWN),
                price_twd=price,
                title=title,
                seller_key=f"{site}:{sid}" if (site and sid) else None,
                card_name=_card_name_of(index, title, r.get("card_name")),
                edition=edition_of(title),
                rarity=(r.get("rarity") or None),
                grader=(r.get("grader") or None),
                grade=_f(r.get("grade")),
                landed_twd=None,
                url=r.get("url"),
                observed_at=r.get("sold_at"),
                observed_at_is_real=not bool(r.get("sold_at_is_ingest")),
                set_series=(r.get("set_code") or "").split("-")[0].upper() or _set_series_of(title),
                source_table="comps",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# 同儕比對
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class PeerMatch:
    """一筆標的的同儕比對結果，**連同它的來歷**（同 `valuation.GroupQuantiles` 哲學）。"""

    tier: str
    tier_label: str
    peer_median_twd: float
    peer_n: int
    #: 這些同儕分屬幾個「已知的」賣家。0 ＝ 全部是賣家未知的列。
    peer_sellers: int
    #: 其中賣家未知的有幾筆（可能混進自己的列 → 方向是低估 alpha，保守）。
    peer_unknown_seller_n: int
    #: 同儕的原始列（給 drill-down 逐筆秀「你跟誰比」）。
    sources: tuple[MarketRow, ...] = ()

    @property
    def scoring(self) -> bool:
        """這一層能不能進分數。`TIER_STRATUM` 一律不行。"""
        return self.tier in SCORING_TIERS


def _tier_key(row: MarketRow, tier: str) -> tuple[Any, ...] | None:
    """同儕鍵。回 None ＝ **這一列不進這個層級的池**（既不當標的也不當同儕）。

    `sale_kind` 是 `basis` 之下的第二個同源維度：競標結標只跟競標結標比、
    定價成交只跟定價成交比。型態未知的列直接退出所有層級——沒有證據就不
    參與比較，把它塞進 `fixed` 那一池才是靜默失敗（見 `SALE_UNKNOWN`）。
    """
    if row.sale_kind not in SCORING_SALE_KINDS:
        return None
    if tier == TIER_STRICT:
        if not row.card_name:
            return None
        return (row.basis, row.sale_kind, row.site, row.card_name, row.edition,
                row.rarity, row.grader, row.grade)
    if tier == TIER_CARD:
        if not row.card_name:
            return None
        return (row.basis, row.sale_kind, row.site, row.card_name, row.edition,
                row.grader, row.grade)
    return (row.basis, row.sale_kind, row.site, row.rarity, row.grader, row.grade)


class PeerIndex:
    """同儕池。每個可比層級各一份索引，查詢時**由嚴到寬**取第一個有樣本的。"""

    def __init__(self, rows: list[MarketRow], *, min_peers: int = 1) -> None:
        self.min_peers = max(1, int(min_peers))
        self._pools: dict[str, dict[tuple[Any, ...], list[MarketRow]]] = {
            t: defaultdict(list) for t in TIER_ORDER
        }
        for r in rows:
            for tier in TIER_ORDER:
                key = _tier_key(r, tier)
                if key is not None:
                    self._pools[tier][key].append(r)

    def peers(self, row: MarketRow, tier: str) -> list[MarketRow]:
        """同一層級裡的其他賣家。**同賣家一律排除**；賣家未知的留著（見模組頂註）。"""
        key = _tier_key(row, tier)
        if key is None:
            return []
        own = row.seller_key
        return [
            p for p in self._pools[tier].get(key, ())
            if p.key != row.key and not (own and p.seller_key == own)
        ]

    def match(self, row: MarketRow, *, tiers: tuple[str, ...] = TIER_ORDER) -> PeerMatch | None:
        """由嚴到寬找同儕。全部不足回 None（**不退回模型估值頂替**）。"""
        for tier in tiers:
            peers = self.peers(row, tier)
            if len(peers) < self.min_peers:
                continue
            known = [p for p in peers if p.seller_key]
            return PeerMatch(
                tier=tier,
                tier_label=TIER_LABEL[tier],
                peer_median_twd=statistics.median([p.price_twd for p in peers]),
                peer_n=len(peers),
                peer_sellers=len({p.seller_key for p in known}),
                peer_unknown_seller_n=len(peers) - len(known),
                sources=tuple(sorted(peers, key=lambda p: p.price_twd)),
            )
        return None


# ---------------------------------------------------------------------------
# 逐筆標的
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SellerItem:
    """賣家的一筆標的 ＋ 它的同儕折價 ＋（分開放的）模型輔助值。"""

    row: MarketRow
    peer: PeerMatch | None
    #: 這個賣家的價 ÷ 同儕中位。<1 ＝ 比同儕便宜。None ＝ 同儕不足。
    ratio: float | None = None
    #: 用到手成本（含運費雜費）算的同一個比值。**穩健性檢查用**：
    #: eBay 的 `price_twd` 不含賣家運費，把成本塞進運費的賣家在卡價上會看起來便宜。
    landed_ratio: float | None = None
    #: ⚠️ 輔助欄位：售價 ÷ **模型公允價**。承受模型分段偏誤（實測 L3×Mercari
    #: 高估 5.9 倍、eBay 平台係數根本估不出來），**絕不進分數**。
    model_ratio: float | None = None
    model_level: str | None = None
    model_n_effective: int = 0
    #: 這筆標的上的風險旗標（signals.flags）。
    flags: tuple[str, ...] = ()

    @property
    def scoring(self) -> bool:
        return self.ratio is not None and self.peer is not None and self.peer.scoring

    @property
    def log_ratio(self) -> float | None:
        return math.log(self.ratio) if self.ratio and self.ratio > 0 else None

    @property
    def discount_pct(self) -> float | None:
        """比同儕便宜幾 %（正數＝便宜）。"""
        return None if self.ratio is None else (1.0 - self.ratio) * 100.0


def _winsorized_log(item: SellerItem) -> float | None:
    lr = item.log_ratio
    if lr is None:
        return None
    return max(-WINSOR_LOG, min(WINSOR_LOG, lr))


def build_seller_items(
    rows: list[MarketRow],
    peers: PeerIndex,
    *,
    valuator: Any = None,
    flags_by_key: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, list[SellerItem]]:
    """每個賣家的逐筆標的（含同儕折價）。**只處理有賣家的列**。

    競標中出價（`BASIS_BID`）也會被收進來（要算旗標、上架時段那些維度），
    但 `peer` 一律留空——它不進折價指標，見模組頂註第 1 點。
    """
    flags_by_key = flags_by_key or {}
    out: dict[str, list[SellerItem]] = defaultdict(list)
    for row in rows:
        if not row.seller_key:
            continue
        match = peers.match(row) if row.basis in SCORING_BASES else None
        item = SellerItem(row=row, peer=match, flags=flags_by_key.get(row.key, ()))
        if match is not None and match.peer_median_twd > 0:
            item.ratio = row.price_twd / match.peer_median_twd
            landed_peers = [p.landed_twd for p in match.sources if p.landed_twd]
            if row.landed_twd and landed_peers:
                item.landed_ratio = row.landed_twd / statistics.median(landed_peers)
        if valuator is not None:
            est = _model_estimate(valuator, row)
            if est is not None and est.fair_twd:
                item.model_ratio = row.price_twd / est.fair_twd
                item.model_level = est.level
                item.model_n_effective = int(est.n_effective or 0)
        out[row.seller_key].append(item)
    return dict(out)


def _model_estimate(valuator: Any, row: MarketRow) -> Any:
    """模型估值。**失敗不准讓整條管路停擺**——輔助欄位而已。"""
    try:
        return valuator.estimate(
            card_name=row.card_name, rarity=row.rarity, grade=row.grade, venue=row.site
        )
    except Exception as exc:  # noqa: BLE001 - 輔助欄位，壞了只是少一欄
        print(f"[warn] seller_alpha 模型輔助值算不出來（{row.key}）：{exc}")
        return None


# ---------------------------------------------------------------------------
# 賣家指標
# ---------------------------------------------------------------------------
def _quantile(sorted_xs: list[float], pct: float) -> float:
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    k = (len(sorted_xs) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def _herfindahl(counts: list[int]) -> float | None:
    """Herfindahl 集中度（1 ＝ 全押同一個，→0 ＝ 完全分散）。無樣本回 None。"""
    total = sum(counts)
    if total <= 0:
        return None
    return sum((c / total) ** 2 for c in counts)


def _days_between(stamps: list[str]) -> float:
    from datetime import datetime

    parsed = []
    for s in stamps:
        try:
            parsed.append(datetime.fromisoformat(s))
        except (TypeError, ValueError):
            continue
    if len(parsed) < 2:
        return 0.0
    return (max(parsed) - min(parsed)).total_seconds() / 86400.0


@dataclass(slots=True)
class SellerMetrics:
    """一個賣家的完整指標。**每一個數字都帶著樣本數與可比層級**。"""

    seller_key: str
    site: str
    seller_id: str

    # -- 樣本 --
    n_rows: int = 0                  # 這個賣家被觀測到的所有列
    n_ask: int = 0
    n_sold: int = 0
    n_bid_excluded: int = 0          # 競標中出價，刻意不入指標
    #: 成交型態未知（回填不到證據）的列數。**這些列不進任何比較**，
    #: 所以它們要在畫面上有名字——不然「可比數少了」看起來會像壞掉。
    n_sale_kind_unknown: int = 0
    n_comparable: int = 0            # 有同儕、且層級可計分的筆數
    n_distinct_cards: int = 0        # 上一項涵蓋幾張相異卡（收縮用的有效 n）
    tier_counts: dict[str, int] = field(default_factory=dict)
    basis_counts: dict[str, int] = field(default_factory=dict)
    #: 這個賣家的列在四種型態上的分布（競標結標／定價／型態未知）。
    sale_kind_counts: dict[str, int] = field(default_factory=dict)
    #: 同儕來自幾個相異賣家。**1 ＝ 這個折價其實是一次 1v1 比價**（對方偏貴
    #: 就會讓整個分數是假的），必須在畫面上講出來，見 `SellerScore.caveats`。
    peer_seller_pool: int = 0
    peer_seller_mix: dict[str, int] = field(default_factory=dict)
    peer_seller_top_share: float | None = None

    # -- 1. 折價（主指標，同儕相對）--
    discount_ratio_median: float | None = None
    discount_ratio_p25: float | None = None
    discount_ratio_p75: float | None = None
    discount_log_median: float | None = None     # winsorize 後
    landed_ratio_median: float | None = None     # 穩健性檢查
    # -- 2. 價格一致性 --
    discount_log_iqr: float | None = None
    # -- 輔助（模型絕對法，承受模型偏誤，不進分數）--
    model_ratio_median: float | None = None
    model_n: int = 0

    # -- 3. 分數分布 --
    grade_mix: dict[str, int] = field(default_factory=dict)
    # -- 4. 集中度 --
    series_mix: dict[str, int] = field(default_factory=dict)
    series_herfindahl: float | None = None
    series_top1_share: float | None = None
    series_known_n: int = 0
    # -- 5. 刊登頻率與時段 --
    first_seen: str | None = None
    last_seen: str | None = None
    observation_span_days: float = 0.0
    #: 有幾筆時間戳其實是入庫時間（已從跨度中排除）。
    n_fake_timestamps: int = 0
    listing_hour_hist: dict[int, int] = field(default_factory=dict)
    persistence_note: str = ""
    # -- 6. 售出率 --
    n_disappeared: int = 0
    n_window_exit: int = 0
    n_still_open: int = 0
    sold_through_note: str = ""
    # -- 7. 風險 --
    suspicious_cheap_n: int = 0
    suspicious_cheap_share: float | None = None
    feedback_score: int | None = None
    feedback_pct: float | None = None
    risk_known: bool = False
    risk_notes: list[str] = field(default_factory=list)

    #: 逐筆標的（drill-down 用）
    items: list[SellerItem] = field(default_factory=list)

    @property
    def comparable_items(self) -> list[SellerItem]:
        return [i for i in self.items if i.scoring]


def seller_metrics(
    seller_key: str,
    items: list[SellerItem],
    *,
    ledger: dict[str, Any] | None = None,
    obs_rows: dict[str, dict[str, Any]] | None = None,
) -> SellerMetrics:
    """把一個賣家的逐筆標的聚合成指標。

    `ledger` 是 `store.sellers` 那一列（feedback 在裡面）；
    `obs_rows` 是 `listing_obs` 原始列（離場欄在裡面），以 key 索引。
    """
    site, _, sid = seller_key.partition(":")
    m = SellerMetrics(seller_key=seller_key, site=site, seller_id=sid, items=list(items))
    obs_rows = obs_rows or {}

    m.n_rows = len(items)
    m.basis_counts = dict(Counter(i.row.basis for i in items))
    m.n_ask = m.basis_counts.get(BASIS_ASK, 0)
    m.n_sold = m.basis_counts.get(BASIS_SOLD, 0)
    m.n_bid_excluded = m.basis_counts.get(BASIS_BID, 0)
    m.sale_kind_counts = dict(Counter(i.row.sale_kind for i in items))
    #: 只算**成交列**的未知：在架列的型態由 price_kind 決定，未知的那些
    #: 已經被算進 n_bid_excluded（basis 也是未知 → 競標），重複計會誤導。
    m.n_sale_kind_unknown = sum(
        1 for i in items if i.row.basis == BASIS_SOLD and i.row.sale_kind == SALE_UNKNOWN
    )
    m.tier_counts = dict(Counter(i.peer.tier for i in items if i.peer))

    comparable = [i for i in items if i.scoring]
    m.n_comparable = len(comparable)

    #: **先按「卡」收斂，再跨卡取中位。** 同一張卡刊四次（賣家重上架、
    #: 「その1／その2」）在逐筆中位裡會投四票，一張卡就能主宰整個賣家的折價。
    #: 收斂之後每張卡一票，與收縮用的有效 n（相異卡數）也才是同一個東西
    #: （工程原則 1：被比較的兩個量要在同一個基礎上算）。
    by_card: dict[tuple[Any, ...], list[SellerItem]] = defaultdict(list)
    for i in comparable:
        by_card[(i.row.card_name, i.row.edition, i.row.grade)].append(i)
    m.n_distinct_cards = len(by_card)

    card_ratios = sorted(
        statistics.median([i.ratio for i in group if i.ratio])
        for group in by_card.values() if any(i.ratio for i in group)
    )
    if card_ratios:
        m.discount_ratio_median = statistics.median(card_ratios)
        m.discount_ratio_p25 = _quantile(card_ratios, 0.25)
        m.discount_ratio_p75 = _quantile(card_ratios, 0.75)
    card_logs = sorted(
        statistics.median([x for x in (_winsorized_log(i) for i in group) if x is not None])
        for group in by_card.values()
        if any(_winsorized_log(i) is not None for i in group)
    )
    if card_logs:
        m.discount_log_median = statistics.median(card_logs)
        m.discount_log_iqr = _quantile(card_logs, 0.75) - _quantile(card_logs, 0.25)
    landed = sorted(i.landed_ratio for i in comparable if i.landed_ratio)
    if landed:
        m.landed_ratio_median = statistics.median(landed)

    peer_sellers: Counter[str] = Counter()
    for i in comparable:
        if i.peer:
            for p in i.peer.sources:
                if p.seller_key:
                    peer_sellers[p.seller_key] += 1
    m.peer_seller_pool = len(peer_sellers)
    m.peer_seller_mix = dict(peer_sellers.most_common())
    if peer_sellers:
        m.peer_seller_top_share = peer_sellers.most_common(1)[0][1] / sum(peer_sellers.values())

    model = sorted(i.model_ratio for i in items if i.model_ratio)
    m.model_n = len(model)
    if model:
        m.model_ratio_median = statistics.median(model)

    m.grade_mix = dict(Counter(
        f"{i.row.grader or '未鑑定'}{f' {i.row.grade:g}' if i.row.grade is not None else ''}"
        for i in items
    ))
    series = [i.row.set_series for i in items if i.row.set_series]
    m.series_known_n = len(series)
    if series:
        counts = Counter(series)
        m.series_mix = dict(counts.most_common())
        m.series_herfindahl = _herfindahl(list(counts.values()))
        m.series_top1_share = counts.most_common(1)[0][1] / len(series)

    #: **只用真實時間戳算跨度。** Buyee 系的已售出頁沒有成交時間，那批 comps 的
    #: `sold_at` 是我們入庫的時間——拿它算跨度會量到我們自己的抓取排程，
    #: 然後把一個從來沒被觀察過的「持續性」憑空變出來（工程原則 1：同源）。
    all_stamps = [i.row.observed_at for i in items if i.row.observed_at]
    stamps = [i.row.observed_at for i in items if i.row.observed_at and i.row.observed_at_is_real]
    m.n_fake_timestamps = len(all_stamps) - len(stamps)
    if stamps:
        m.first_seen, m.last_seen = min(stamps), max(stamps)
        m.observation_span_days = _days_between(stamps)
    hours: Counter[int] = Counter()
    for s in stamps:
        try:
            hours[int(str(s)[11:13])] += 1
        except (ValueError, IndexError):
            continue
    m.listing_hour_hist = dict(sorted(hours.items()))
    m.persistence_note = (
        f"觀測跨度 {m.observation_span_days:.1f} 天、{len(stamps)} 筆可用時間戳"
        + (f"（另 {m.n_fake_timestamps} 筆的時間其實是我們入庫的時間，已排除）"
           if m.n_fake_timestamps else "")
        + (
            "——**不足以判定「持續性」**（我們對這個賣家的觀測還太短）"
            if m.observation_span_days < PERSISTENCE_MIN_DAYS
            else "——**足以談持續性**（跨度已超過同一批貨的在架期）"
        )
    )

    for i in items:
        obs = obs_rows.get(i.row.key)
        if obs is None:
            continue
        if obs.get("disappeared_at"):
            m.n_disappeared += 1
        elif obs.get("window_exit_at"):
            m.n_window_exit += 1
        else:
            m.n_still_open += 1
    tracked = m.n_disappeared + m.n_window_exit + m.n_still_open
    m.sold_through_note = (
        f"在架帳追蹤到 {tracked}/{m.n_rows} 筆："
        f"疑似售出／下架 {m.n_disappeared}、被觀測窗擠出（無結論）{m.n_window_exit}、"
        f"仍在架 {m.n_still_open}。⚠️「消失」≠「賣掉」（新著降冪只抓第 1 頁），"
        "PayPay 賣家頁的 SOLD 尚未抓 → 真正的售出率目前**無法判定**"
    )

    m.suspicious_cheap_n = sum(1 for i in items if "suspicious_cheap" in i.flags)
    if m.n_rows:
        m.suspicious_cheap_share = m.suspicious_cheap_n / m.n_rows
    if ledger:
        fb = ledger.get("feedback_score")
        pct = ledger.get("feedback_pct")
        m.feedback_score = int(fb) if fb is not None else None
        m.feedback_pct = float(pct) if pct is not None else None
    m.risk_known = m.feedback_pct is not None or m.feedback_score is not None
    if not m.risk_known:
        m.risk_notes.append(
            f"{site} 沒有賣家評價（Yahoo 賣家頁評價擷取已支援，"
            "但這個賣家還沒被監控輪替掃到，或站台本身沒有評價可讀）"
            "——風險維度是**未知**，不是「沒問題」"
        )
    return m


# ---------------------------------------------------------------------------
# 分數
# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class ScoreComponent:
    """分數的一項。**裸數字不准輸出**——每一項都要說得出它是怎麼來的。"""

    name: str
    label: str
    points: float
    max_points: float
    detail: str

    @property
    def is_penalty(self) -> bool:
        return self.points < 0


@dataclass(slots=True)
class SellerScore:
    """一次評分的完整答案。`ok=False` 時 `total` **必定是 None**（不是 0）。

    0 分會被讀成「這個賣家很差」，None 才是「我不知道，證據不足」。
    """

    seller_key: str
    ok: bool
    reason: str
    total: float | None = None
    components: list[ScoreComponent] = field(default_factory=list)
    #: 還缺什麼才給得出分數（`ok=False` 時必填，照 `BidCeiling.reason` 的風格）。
    missing: list[str] = field(default_factory=list)
    #: 分數成立、但**讀的時候必須知道的前提**。與 missing 分開：missing 是
    #: 「還不能給」，caveats 是「給了，但這個數字脆在哪裡」。
    caveats: list[str] = field(default_factory=list)
    n_comparable: int = 0
    n_distinct_cards: int = 0
    tier_label: str = ""

    @property
    def penalties(self) -> list[ScoreComponent]:
        return [c for c in self.components if c.is_penalty]


def _shrink_toward_zero(value: float, n: int, k: float) -> float:
    """往「零 alpha」的先驗收縮。n=0 → 0；k=0 → 完全不收縮。

    形狀與 `valuation._shrink(layer, parent, n, k)` 在 parent=0 時完全相同
    （w = n/(n+k)）。這裡把 parent 寫死成 0 是刻意的：賣家 alpha 的先驗就是
    「沒有 alpha」，樣本不夠時的正確答案是「看起來跟大家一樣」。
    """
    if n <= 0:
        return 0.0
    return value * (n / (n + k))


def score_seller(m: SellerMetrics, params: AlphaParams | None = None) -> SellerScore:
    """賣家指標 → 可解釋分數。門檻不過**不給分數**，並說得出缺什麼。"""
    p = params or AlphaParams()
    tier_label = "｜".join(
        f"{TIER_LABEL[t]}×{n}" for t, n in sorted(m.tier_counts.items()) if t in SCORING_TIERS
    )

    missing: list[str] = []
    if m.n_comparable < p.min_comparable:
        missing.append(
            f"可比標的只有 {m.n_comparable} 筆（門檻 {p.min_comparable} 筆）"
            "——缺的是：這個賣家再上架幾張「別的賣家也在同一個站、用同一種價格基準、"
            "賣同一張卡同版次同分數」的卡，或同一張卡的其他賣家標的進到帳本"
        )
    if m.n_distinct_cards < p.min_distinct_cards:
        missing.append(
            f"可比標的只涵蓋 {m.n_distinct_cards} 張相異卡（門檻 {p.min_distinct_cards} 張）"
            "——同一張卡刊三次不算三個證據"
        )
    if missing:
        parts: list[str] = []
        if m.n_bid_excluded:
            parts.append(
                f"另有 {m.n_bid_excluded} 筆是競標中出價，"
                "刻意不入指標：那是市場當下的出價，不是賣家的定價決定"
            )
        if m.n_sale_kind_unknown:
            parts.append(
                f"另有 {m.n_sale_kind_unknown} 筆成交查不出型態（競標結標／定價成交），"
                "不進比較——證據補得回來就會自動計入（`ygo-sniper backfill-sale-kind`）"
            )
        detail = f"（{'；'.join(parts)}）" if parts else ""
        return SellerScore(
            seller_key=m.seller_key,
            ok=False,
            reason="**證據不足，不給 Seller Alpha 分數**：" + "；".join(missing) + detail,
            missing=missing,
            n_comparable=m.n_comparable,
            n_distinct_cards=m.n_distinct_cards,
            tier_label=tier_label,
        )

    n_eff = m.n_distinct_cards
    raw = -(m.discount_log_median or 0.0)          # 正 ＝ 比同儕便宜
    shrunk = _shrink_toward_zero(raw, n_eff, p.shrink_k)
    weight = n_eff / (n_eff + p.shrink_k) if p.shrink_k else 1.0

    components: list[ScoreComponent] = []
    #: **三個加分項全部以「有折價」為前提。** 廣度與一致性是「這個 alpha 有多可信」，
    #: 不是獨立的優點——不上這道閘，一個比同儕貴 165% 但上架很多卡的賣家會靠廣度
    #: 爬到排行榜第二名（2026-08-04 實測到的真實排序錯誤），那正是這個榜要避免的事。
    has_alpha = shrunk > 0

    depth_pts = p.max_depth * max(0.0, min(1.0, shrunk / DEPTH_FULL_LOG))
    components.append(ScoreComponent(
        name="depth",
        label="折價深度（同儕相對）",
        points=round(depth_pts, 1),
        max_points=p.max_depth,
        detail=(
            f"同儕相對中位 {m.discount_ratio_median:.3f}×"
            f"（＝比同儕{'便宜' if (m.discount_ratio_median or 1) < 1 else '貴'} "
            f"{abs(1 - (m.discount_ratio_median or 1)) * 100:.1f}%），"
            f"P25/P75 {m.discount_ratio_p25:.2f}／{m.discount_ratio_p75:.2f}；"
            f"逐筆先夾在 ±50% 內、再以 n={n_eff} 相異卡收縮 ×{weight:.2f}"
            f"（k={p.shrink_k:g}）→ 收縮後折價 {(1 - math.exp(-shrunk)) * 100:.1f}%；"
            f"滿分對應「穩定便宜 25%」"
        ),
    ))

    if has_alpha and m.discount_log_iqr is not None and m.n_comparable >= p.min_comparable:
        tightness = max(0.0, min(1.0, 1.0 - m.discount_log_iqr / CONSISTENCY_IQR_FULL))
        cons_pts = p.max_consistency * tightness * weight
        cons_detail = (
            f"折價對數 IQR {m.discount_log_iqr:.3f}"
            f"（比值 P25→P75 {m.discount_ratio_p25:.2f}→{m.discount_ratio_p75:.2f}，"
            f"散度 {math.exp(m.discount_log_iqr):.2f} 倍）→ 緊密度 {tightness:.2f}，"
            f"再乘樣本收縮 {weight:.2f}"
        )
    else:
        cons_pts = 0.0
        cons_detail = (
            "不給分：" + ("這個賣家比同儕貴（沒有折價可談一致性）"
                         if not has_alpha else "可比樣本不足以量離散度")
        )
    components.append(ScoreComponent(
        name="consistency", label="價格一致性（規律＝可預測）",
        points=round(cons_pts, 1), max_points=p.max_consistency, detail=cons_detail,
    ))

    card_pts = (p.max_breadth / 2) * min(1.0, n_eff / max(1, p.breadth_full_cards))
    day_pts = (p.max_breadth / 2) * min(
        1.0, m.observation_span_days / max(1e-9, p.breadth_full_days)
    )
    if has_alpha:
        breadth_pts = card_pts + day_pts
        breadth_detail = (
            f"相異卡 {n_eff}/{p.breadth_full_cards} → {card_pts:.1f} 分；"
            f"觀測跨度 {m.observation_span_days:.1f}/{p.breadth_full_days:g} 天 → {day_pts:.1f} 分。"
            + (
                f"⚠️ 這個賣家的觀測跨度不到 {PERSISTENCE_MIN_DAYS:.0f} 天"
                "——時間那一半拿不到什麼分，那是**證據短**不是他定價不穩"
                if m.observation_span_days < PERSISTENCE_MIN_DAYS else ""
            )
        )
    else:
        breadth_pts = 0.0
        breadth_detail = (
            "不給分：沒有折價就沒有 alpha 可談持續性"
            "（廣度是「這個 alpha 有多可信」，不是獨立的優點）"
        )
    components.append(ScoreComponent(
        name="breadth", label="持續性（相異卡數 × 觀測跨度）",
        points=round(breadth_pts, 1), max_points=p.max_breadth, detail=breadth_detail,
    ))

    penalty, risk_detail = _risk_penalty(m, p)
    components.append(ScoreComponent(
        name="risk", label="風險扣分", points=-round(penalty, 1),
        max_points=p.max_risk_penalty, detail=risk_detail,
    ))

    caveats: list[str] = []
    if has_alpha and m.peer_seller_pool <= 1:
        only = next(iter(m.peer_seller_mix), "（未知）")
        caveats.append(
            f"⚠️ 全部同儕只來自 **1 個賣家**（{only}）——這個折價其實是一次 1v1 比價，"
            "對方自己偏貴就會讓整個分數是假的。缺的是：第三個賣家的同款標的進到帳本"
        )
    elif has_alpha and (m.peer_seller_top_share or 0) > 0.8:
        caveats.append(
            f"⚠️ {(m.peer_seller_top_share or 0) * 100:.0f}% 的同儕來自單一賣家"
            f"（{next(iter(m.peer_seller_mix), '—')}）——折價對那一家的定價高度敏感"
        )
    if has_alpha and m.observation_span_days < PERSISTENCE_MIN_DAYS:
        caveats.append(
            f"⚠️ 觀測跨度只有 {m.observation_span_days:.1f} 天——"
            "這是**橫斷面**的便宜，不是「持續」便宜；持續性要等帳本累積"
        )
    if not m.risk_known:
        caveats.append(f"⚠️ {m.site} 讀不到賣家評價，風險維度是未知而不是安全")

    total = max(0.0, sum(c.points for c in components))
    verdict = (
        f"比同儕便宜 {(1 - (m.discount_ratio_median or 1)) * 100:.1f}%"
        if has_alpha else
        f"**沒有 alpha**：比同儕貴 {((m.discount_ratio_median or 1) - 1) * 100:.1f}%"
    )
    return SellerScore(
        seller_key=m.seller_key, ok=True,
        reason=(
            f"可比 {m.n_comparable} 筆／{n_eff} 張相異卡（{tier_label or '—'}），"
            f"同儕相對中位 {m.discount_ratio_median:.3f}× → {verdict}"
        ),
        total=round(total, 1), components=components, caveats=caveats,
        n_comparable=m.n_comparable, n_distinct_cards=n_eff, tier_label=tier_label,
    )


def _risk_penalty(m: SellerMetrics, p: AlphaParams) -> tuple[float, str]:
    """風險維度。**只扣分不加分**，而且每一項都要單獨看得見。"""
    parts: list[str] = []
    penalty = 0.0

    share = m.suspicious_cheap_share or 0.0
    if m.suspicious_cheap_n:
        sub = min(15.0, 15.0 * share / 0.2)   # 兩成標的被標可疑 → 扣滿 15
        penalty += sub
        parts.append(
            f"suspicious_cheap 旗標 {m.suspicious_cheap_n}/{m.n_rows}"
            f"（{share * 100:.0f}%）→ -{sub:.1f}"
        )
    else:
        parts.append(f"suspicious_cheap 旗標 0/{m.n_rows} → -0")

    if m.feedback_pct is not None:
        if m.feedback_pct < 95:
            penalty += 15.0
            parts.append(f"好評率 {m.feedback_pct:.1f}%（<95）→ -15")
        elif m.feedback_pct < 98:
            penalty += 10.0
            parts.append(f"好評率 {m.feedback_pct:.1f}%（<98）→ -10")
        else:
            parts.append(f"好評率 {m.feedback_pct:.1f}% → -0")
        if (m.feedback_score or 0) < 50:
            penalty += 5.0
            parts.append(f"評價筆數 {m.feedback_score}（<50，新帳號）→ -5")
    else:
        parts.append(
            f"⚠️ {m.site} 沒有賣家評價可讀（Yahoo 賣家頁評價擷取已支援，"
            "覆蓋率仍限監控輪替掃過的賣家）→ 風險**未知**，不扣分但不代表安全"
        )
    return min(penalty, p.max_risk_penalty), "；".join(parts)


# ---------------------------------------------------------------------------
# 全量分析
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AlphaReport:
    """一次全量分析的答案 ＋ **覆蓋率現況**（沒有覆蓋率的排行榜是誤導）。"""

    metrics: dict[str, SellerMetrics] = field(default_factory=dict)
    scores: dict[str, SellerScore] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    params: AlphaParams = field(default_factory=AlphaParams)

    def ranked(self, *, site: str | None = None) -> list[tuple[SellerScore, SellerMetrics]]:
        out = [
            (s, self.metrics[k]) for k, s in self.scores.items()
            if s.ok and (site is None or self.metrics[k].site == site)
        ]
        # 同分時把「比同儕便宜的」排前面：一堆 0 分的賣家（比同儕貴）之間
        # 用賣家鍵排序沒有意義，用折價排至少讀得出誰離門檻近。
        out.sort(key=lambda pair: (
            -(pair[0].total or 0),
            pair[1].discount_ratio_median if pair[1].discount_ratio_median else 1e9,
            pair[0].seller_key,
        ))
        return out

    def rejected(self, *, site: str | None = None) -> list[tuple[SellerScore, SellerMetrics]]:
        out = [
            (s, self.metrics[k]) for k, s in self.scores.items()
            if not s.ok and (site is None or self.metrics[k].site == site)
        ]
        out.sort(key=lambda pair: (-pair[1].n_comparable, -pair[1].n_rows, pair[0].seller_key))
        return out


def _flags_by_key(store: Any, limit: int = 20000) -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for r in store.list_signals(state="all", limit=limit):
        try:
            flags = json.loads(r.get("flags") or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(flags, list):
            out[str(r.get("key"))] = tuple(str(f) for f in flags)
    return out


def analyze(
    store: Any,
    *,
    cfg: Any = None,
    index: CardIndex | None = None,
    valuator: Any = None,
    params: AlphaParams | None = None,
) -> AlphaReport:
    """全量跑一次。`valuator` 給了才算模型輔助欄位（可為 None）。"""
    p = params or (AlphaParams.from_config(cfg) if cfg is not None else AlphaParams())
    idx = index if index is not None else CardIndex.load()
    rows = market_rows_from_store(store, idx)
    peers = PeerIndex(rows, min_peers=p.min_peers)
    flags = _flags_by_key(store)
    by_seller = build_seller_items(rows, peers, valuator=valuator, flags_by_key=flags)

    ledger = {r["seller_key"]: r for r in store.list_sellers(limit=100000)}
    obs_rows = {r["key"]: r for r in store.listing_obs(limit=50000)}

    report = AlphaReport(params=p)
    for key, items in by_seller.items():
        m = seller_metrics(key, items, ledger=ledger.get(key), obs_rows=obs_rows)
        report.metrics[key] = m
        report.scores[key] = score_seller(m, p)
    report.coverage = coverage_report(rows, report, p)
    return report


def coverage_report(
    rows: list[MarketRow], report: AlphaReport, params: AlphaParams
) -> dict[str, Any]:
    """同儕可比的覆蓋率。**這份數字決定整個排行榜能不能被相信**。"""
    seller_rows = [r for r in rows if r.seller_key]
    scoring_rows = [r for r in seller_rows if r.basis in SCORING_BASES]
    items = [i for m in report.metrics.values() for i in m.items]
    comparable = [i for i in items if i.scoring]
    stratum_only = [i for i in items if i.peer and i.peer.tier == TIER_STRATUM]
    ok = [s for s in report.scores.values() if s.ok]
    return {
        "sellers_total": len(report.metrics),
        "sellers_scored": len(ok),
        "sellers_insufficient": len(report.scores) - len(ok),
        "rows_total": len(rows),
        "rows_with_seller": len(seller_rows),
        "rows_scoring_basis": len(scoring_rows),
        "rows_bid_excluded": sum(1 for r in seller_rows if r.basis == BASIS_BID),
        "rows_no_card_name": sum(1 for r in scoring_rows if not r.card_name),
        #: **成交型態的分解**（工程原則 1：說得出這些數字是幾把尺量出來的）。
        #: 競標結標與定價成交各自成池，型態未知的完全不進比較——三個數字
        #: 分開報，覆蓋率下降時看得出來是「修好了」還是「壞了」。
        "rows_sale_kind": dict(Counter(r.sale_kind for r in scoring_rows)),
        "rows_sale_kind_unknown": sum(
            1 for r in scoring_rows if r.sale_kind == SALE_UNKNOWN
        ),
        "comparable_items": len(comparable),
        "comparable_rate": (len(comparable) / len(scoring_rows)) if scoring_rows else 0.0,
        "tier_counts": dict(Counter(i.peer.tier for i in comparable if i.peer)),
        "basis_counts": dict(Counter(i.row.basis for i in comparable)),
        #: 有算出同儕折價的那些列，各是哪一種型態撐出來的。
        "comparable_sale_kind": dict(Counter(i.row.sale_kind for i in comparable)),
        #: 逐站 × 逐型態（成交列）。哪一站的證據補不回來，這裡看得見。
        "sold_sale_kind_by_site": {
            f"{site}:{kind}": n
            for (site, kind), n in sorted(
                Counter(
                    (r.site, r.sale_kind) for r in rows if r.basis == BASIS_SOLD
                ).items()
            )
        },
        "stratum_only_items": len(stratum_only),
        "min_comparable": params.min_comparable,
        "min_distinct_cards": params.min_distinct_cards,
        "observation_span_days": max(
            (m.observation_span_days for m in report.metrics.values()), default=0.0
        ),
        #: **成交帳本自己的跨度**（只算 comps 的真實成交時間）。
        #: 與下面的在架跨度分開，因為它們回答的是兩個不同的問題：
        #: 在架帳只知道「我們盯了多久」，成交帳知道「這個賣家過去半年怎麼賣」。
        #: 平台的歷史成交紀錄本來就存在（Yahoo 落札相場 180 天、Yahoo!フリマ
        #: 賣家頁單頁就橫跨半年），所以「持續性」不必等帳本累積——挖出來就有
        #: （見 `history` 模組）。**不可以把兩個跨度加總或取大值當一個數字報**：
        #: 一個賣家的成交跨度 147 天不代表我們盯了他 147 天的在架定價，
        #: 兩件事的證據強度不同（工程原則 1：同源）。
        "comps_span_days": _days_between(
            [
                r.observed_at for r in rows
                if r.source_table == "comps" and r.observed_at and r.observed_at_is_real
            ]
        ),
        #: 有分數、且個人觀測跨度 ≥ 14 天的賣家數 ＝ **持續性判定得出來的人數**。
        "sellers_persistent": sum(
            1 for k, s in report.scores.items()
            if s.ok and report.metrics[k].observation_span_days >= PERSISTENCE_MIN_DAYS
        ),
        "persistence_min_days": PERSISTENCE_MIN_DAYS,
        #: **在架觀測帳自己的跨度**（只算 listing_obs 的真實時間戳）。
        #: 與上面那個刻意分開：`observation_span_days` 取的是各賣家跨度的最大值，
        #: 而賣家的跨度含 comps 的成交時間——那是「別人什麼時候賣掉的」，可以橫跨
        #: 好幾個月。但「持續性」問的是「**我們盯了這個賣家多久**」，那只有在架帳
        #: 回答得了。混用的後果是畫面顯示 99 天、實際帳本只有 2 天，而「持續性」
        #: 那半個分數對所有人仍然是 0——數字與警告互相矛盾（工程原則 1：同源）。
        "listing_obs_span_days": _days_between(
            [
                r.observed_at for r in rows
                if r.source_table == "listing_obs" and r.observed_at and r.observed_at_is_real
            ]
        ),
    }


__all__ = [
    "BASIS_ASK",
    "BASIS_BID",
    "BASIS_LABEL",
    "BASIS_SOLD",
    "PERSISTENCE_MIN_DAYS",
    "SALE_AUCTION",
    "SALE_FIXED",
    "SALE_KIND_LABEL",
    "SALE_UNKNOWN",
    "SCORING_BASES",
    "SCORING_SALE_KINDS",
    "SCORING_TIERS",
    "TIER_CARD",
    "TIER_LABEL",
    "TIER_STRATUM",
    "TIER_STRICT",
    "AlphaParams",
    "AlphaReport",
    "MarketRow",
    "PeerIndex",
    "PeerMatch",
    "ScoreComponent",
    "SellerItem",
    "SellerMetrics",
    "SellerScore",
    "analyze",
    "basis_kind_label",
    "basis_of",
    "build_seller_items",
    "sale_kind_of_price_kind",
    "coverage_report",
    "edition_of",
    "market_rows_from_store",
    "score_seller",
    "seller_metrics",
]
