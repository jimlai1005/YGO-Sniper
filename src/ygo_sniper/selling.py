"""賣方成本模型與跨平台淨價差 —— 「知道有沒有套利空間」的那一半。

`costs.py` 回答「買進到手要多少錢」；這裡回答「賣掉實拿多少、扣完之後還剩幾塊」。

## 為什麼這個模組的地基是「貨在哪裡」而不是「價差多少」

實測 venue 係數：Yahoo ×1.00 / Mercari ×2.14 / PayPay ×2.60 —— **三個都是日本
境內市場**。而 `routes:` 那三條買進路徑全部把貨運到台灣（集運攤提後每張約
NT$290 雜費）。所以「在 Yahoo 競標買進、在 Mercari 賣出」這條看起來 2 倍的
套利，**要求貨留在日本**；貨運到台灣之後那個 2 倍就不再屬於你——要回去拿，
國際運費得付第二次，而且收件端需要一個日本地址。

把這兩個價格直接相減，就是工程原則 1 說的混源比較，而且方向是「看起來有錢賺」，
正是會讓人下單的方向。所以本模組把它拆成三件**分別計價、分別可證偽**的事：

    買進路徑 → `RouteConfig.destination`（貨最後在哪個 `HoldingLocation`）
    賣場     → `SellVenueConfig.location`（要在這裡賣，貨必須在哪裡）
    兩者不同 → `resale.transfers` 明確計價；沒有這一條就是**不可行**

「不可行」是一個結果，不是一個很大的數字。給 `cost=999999` 會讓不可行的組合
默默參加排序，總有一天會因為別條更爛而贏。

## 收入側的匯率方向（另一個工程原則 1 的坑）

`fx.to_twd(..., apply_markup=True)` 會把金額往上抬 3.5%（刷卡海外手續費 ＋
安全緩衝）。那對**成本**是保守的（估貴一點），對**收入**卻正好相反——會把
賣出實拿估高。所以本模組的收入一律 `apply_markup=False`（中價）再**扣掉**
`fx_spread_pct` 的匯差。同一個 fx 物件、相反的方向，各自往保守的那一邊。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .config import Config, RouteConfig, SellVenueConfig
from .costs import quote_route
from .domain import Currency, HoldingLocation, Listing, RouteQuote, Site

#: 淨利／報酬率的分母。用「總投入」而不是「到手成本」：把貨運到賣場的錢
#: 同樣是你掏出去的，不算進分母會讓報酬率虛高。
#: （這裡不是可調參數，是定義；寫成常數只為了讓它有名字。）
_MIN_DENOMINATOR_TWD = 0.01


# ---------------------------------------------------------------------------
# 賣出實拿
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SellQuote:
    """在某個賣場以某個價格賣出，實際拿到多少。**這個結構要能自己解釋自己**。

    `ok=False` 時 `net_twd` **必定是 None**，不是 0 或負數——0 是一個會被
    誤讀成「剛好打平」的數字，None 才是「這筆算不出來」。
    """

    ok: bool
    reason: str
    venue: str
    venue_label: str
    currency: str
    #: 賣出價（賣場自己的幣別）。
    price_native: float
    #: 賣出價換成台幣（中價，不套刷卡加成——那是成本側的東西）。
    gross_twd: float | None = None
    #: 每一項扣除，賣場幣別。加起來 = price_native − net_native。
    commission_native: float = 0.0
    payment_fee_native: float = 0.0
    listing_fee_native: float = 0.0
    shipping_native: float = 0.0
    payout_fee_native: float = 0.0
    remit_fee_native: float = 0.0
    #: 扣完平台費用後、還沒換匯的餘額（賣場幣別）。
    net_native: float | None = None
    #: 匯回台灣的匯差（台幣）。台幣賣場恆為 0。
    fx_haircut_twd: float = 0.0
    #: 最後真正進到你台灣戶頭的金額。
    net_twd: float | None = None
    #: 逐項明細（label / amount_native / amount_twd / source），給 UI 直接畫。
    lines: list[dict[str, Any]] = field(default_factory=list)
    #: 費率查證程度（原樣來自 settings.yaml 的 `verified`）。**必須跟數字一起出門**。
    verified: str = ""
    source_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def take_rate(self) -> float | None:
        """實拿佔成交價的比例。0.78 = 「每賣 100 元實際入袋 78 元」。"""
        if not self.ok or self.net_twd is None or not self.gross_twd:
            return None
        return self.net_twd / self.gross_twd

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["take_rate"] = self.take_rate
        return d


def _fail_sell(venue: SellVenueConfig, reason: str, price: float) -> SellQuote:
    return SellQuote(
        ok=False, reason=reason, venue=venue.name, venue_label=venue.label,
        currency=venue.currency, price_native=price,
        verified=venue.verified, source_url=venue.source_url,
        notes=list(venue.notes),
    )


def net_proceeds(
    price_native: float,
    venue: SellVenueConfig,
    cfg: Config,
    fx: Any,
    *,
    remit_batch_size: int | None = None,
) -> SellQuote:
    """以 `price_native`（賣場幣別）成交，實拿多少台幣。

    扣除順序不影響結果（全是加法），但明細的順序是刻意的：
    先平台抽成、再自己出的運費、最後才是把錢搬回台灣的成本——
    最後那一段是日本賣場最容易被忘記的一項（也是唯一會被「湊幾張匯一次」影響的）。
    """
    if not venue.enabled:
        return _fail_sell(venue, f"{venue.label} 在設定裡被關閉（sell_venues.enabled=false）", price_native)
    if price_native <= 0:
        return _fail_sell(venue, "賣出價必須大於 0", price_native)

    ccy = Currency(venue.currency)
    batch = max(1, remit_batch_size if remit_batch_size is not None else venue.remit_batch_size)

    commission = price_native * venue.commission_pct
    payment = price_native * venue.payment_fee_pct
    listing = venue.listing_fee_native
    shipping = venue.seller_shipping_native
    payout = venue.payout_fee_native
    remit = venue.remit_fee_native / batch

    net_native = price_native - (commission + payment + listing + shipping + payout + remit)

    # 收入側：中價換算（apply_markup 是成本側的加成，套在收入上方向相反）。
    def _twd(x: float) -> float:
        return fx.to_twd(x, ccy, apply_markup=False)

    gross_twd = _twd(price_native)

    if net_native <= 0:
        q = _fail_sell(
            venue,
            f"以 {venue.currency} {price_native:,.0f} 賣出，扣完手續費與運費之後是負的"
            f"（淨額 {venue.currency} {net_native:,.0f}）——這個價位不值得賣",
            price_native,
        )
        q.gross_twd = round(gross_twd, 2)
        q.net_native = round(net_native, 2)
        return q

    net_twd_pre = _twd(net_native)
    haircut = net_twd_pre * venue.fx_spread_pct
    net_twd = net_twd_pre - haircut

    lines: list[dict[str, Any]] = [
        {"label": "成交價", "amount_native": round(price_native, 2),
         "amount_twd": round(gross_twd, 2), "sign": 1, "source": "估價區間下緣（保守）"},
        {"label": f"成交手續費 {venue.commission_pct:.2%}", "amount_native": round(commission, 2),
         "amount_twd": round(_twd(commission), 2), "sign": -1, "source": venue.source_url},
    ]
    if venue.payment_fee_pct:
        lines.append({"label": f"金流／系統處理費 {venue.payment_fee_pct:.2%}",
                      "amount_native": round(payment, 2), "amount_twd": round(_twd(payment), 2),
                      "sign": -1, "source": venue.source_url})
    if listing:
        lines.append({"label": "上架／每筆訂單費", "amount_native": round(listing, 2),
                      "amount_twd": round(_twd(listing), 2), "sign": -1, "source": venue.source_url})
    if shipping:
        lines.append({"label": "賣家負擔境內運費", "amount_native": round(shipping, 2),
                      "amount_twd": round(_twd(shipping), 2), "sign": -1, "source": "保守估計"})
    if payout:
        lines.append({"label": "提領到當地銀行", "amount_native": round(payout, 2),
                      "amount_twd": round(_twd(payout), 2), "sign": -1, "source": venue.source_url})
    if remit:
        lines.append({"label": f"匯回台灣（一次 {venue.currency} {venue.remit_fee_native:,.0f}／湊 {batch} 張攤提）",
                      "amount_native": round(remit, 2), "amount_twd": round(_twd(remit), 2),
                      "sign": -1, "source": "保守估計（未查證）"})
    if haircut:
        lines.append({"label": f"匯差 {venue.fx_spread_pct:.2%}", "amount_native": None,
                      "amount_twd": round(haircut, 2), "sign": -1, "source": "保守估計（未查證）"})
    lines.append({"label": "實拿", "amount_native": round(net_native, 2),
                  "amount_twd": round(net_twd, 2), "sign": 1, "source": ""})

    return SellQuote(
        ok=True,
        reason="可賣出",
        venue=venue.name,
        venue_label=venue.label,
        currency=venue.currency,
        price_native=round(price_native, 2),
        gross_twd=round(gross_twd, 2),
        commission_native=round(commission, 2),
        payment_fee_native=round(payment, 2),
        listing_fee_native=round(listing, 2),
        shipping_native=round(shipping, 2),
        payout_fee_native=round(payout, 2),
        remit_fee_native=round(remit, 2),
        net_native=round(net_native, 2),
        fx_haircut_twd=round(haircut, 2),
        net_twd=round(net_twd, 2),
        lines=lines,
        verified=venue.verified,
        source_url=venue.source_url,
        notes=list(venue.notes),
    )


def breakeven_sell_price_native(
    total_cost_twd: float,
    venue: SellVenueConfig,
    cfg: Config,
    fx: Any,
    *,
    remit_batch_size: int | None = None,
) -> float | None:
    """**反解 `net_proceeds`**：在這個賣場要賣到多少錢才剛好打平？

    net_twd = rate × (1 − spread) × [ P × (1 − 抽成 − 金流) − 固定費用 ]
    ⇒ P = [ total_cost ÷ (rate × (1 − spread)) + 固定費用 ] ÷ (1 − 抽成 − 金流)

    與 `bidding.max_bid_jpy` 同一個紀律：反解出來的數字**一定要拿回去正算驗證**
    （見 `tests/test_selling.py::test_breakeven_price_round_trips_to_zero_profit`）。
    反解與正算若不同源，兩邊會安靜地分岔，而分岔的方向沒有人保證是安全的那邊。

    抽成 ＋ 金流 ≥ 100% 時回 None（賣多少都打不平），不回一個很大的數字。
    """
    ccy = Currency(venue.currency)
    batch = max(1, remit_batch_size if remit_batch_size is not None else venue.remit_batch_size)
    variable = 1.0 - venue.commission_pct - venue.payment_fee_pct
    if variable <= 0:
        return None
    fixed = (
        venue.listing_fee_native + venue.seller_shipping_native
        + venue.payout_fee_native + venue.remit_fee_native / batch
    )
    rate = fx.to_twd(1.0, ccy, apply_markup=False) * (1.0 - venue.fx_spread_pct)
    if rate <= 0:
        return None
    return (total_cost_twd / rate + fixed) / variable


# ---------------------------------------------------------------------------
# 送到賣場：貨在 A、賣場要 B
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class TransferQuote:
    """把貨從 A 地送到 B 地。`ok=False` 時 `cost_twd` 是 **None，不是 0**。"""

    ok: bool
    frm: str
    to: str
    cost_twd: float | None
    reason: str = ""
    note: str = ""
    bundle_size: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def transfer_quote(
    frm: str, to: str, cfg: Config, fx: Any, *, bundle_size: int = 1
) -> TransferQuote:
    """從 `frm` 送到 `to` 要多少錢（台幣）。

    同一個地點 = 0（貨已經在那裡了，這是唯一一個 0 是對的情況）。
    設定裡沒有這一條 = **不可行**，不是 0——「沒設定」與「免費」是兩件事，
    把它們混在一起正是本模組要防的那個錯（工程原則 1 的「不可以是 0」）。
    """
    if frm == to:
        return TransferQuote(ok=True, frm=frm, to=to, cost_twd=0.0,
                             reason="貨已經在賣場所在地", bundle_size=bundle_size)

    spec = cfg.resale.transfer(frm, to)
    if spec is None:
        return TransferQuote(
            ok=False, frm=frm, to=to, cost_twd=None,
            reason=f"設定裡沒有 {frm} → {to} 的運送方案，判定為不可行"
                   "（沒有方案不等於免費）",
        )
    if not spec.feasible:
        return TransferQuote(ok=False, frm=frm, to=to, cost_twd=None,
                             reason=spec.reason or f"{frm} → {to} 被標記為不可行")

    n = max(1, bundle_size) if spec.amortizable else 1
    cost_jpy = spec.cost_jpy / n
    return TransferQuote(
        ok=True, frm=frm, to=to,
        # 運送是**支出**，所以這一段套 markup（與 costs.py 同一個方向）。
        cost_twd=round(fx.to_twd(cost_jpy, Currency.JPY), 2),
        reason="可行", note=spec.note, bundle_size=n,
    )


# ---------------------------------------------------------------------------
# 一趟完整的買→賣
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class RoundTrip:
    """買進到手 → 持有地點 → 送到賣場 → 賣出實拿 → 淨利。

    照 `bidding.BidCeiling` 的做法：**這個結構要能自己解釋自己**。使用者會
    照著 `net_profit_twd` 決定要不要下單，所以每一項金額與它的來源都跟著出門。

    `ok=False` 時 `net_profit_twd` **必定是 None**——不可行的組合不給數字，
    給一個負數會讓它看起來像是「算過了、只是不划算」，而它其實是「做不到」。
    """

    ok: bool
    reason: str
    #: --- 買進側 ---
    site: str
    buy_route: str
    buy_route_label: str
    landed_twd: float
    bundle_size: int
    holding: str
    #: --- 送到賣場 ---
    sell_venue: str
    sell_venue_label: str
    sell_location: str
    transfer: TransferQuote | None = None
    transfer_twd: float | None = None
    #: --- 賣出側 ---
    #: 賣出價的來源說明（例如「估價 80% 區間下緣，L1／n=3」）。
    price_source: str = ""
    sell: SellQuote | None = None
    #: --- 結果 ---
    #: 總投入 = 到手成本 ＋ 送到賣場的成本。
    total_cost_twd: float | None = None
    net_proceeds_twd: float | None = None
    net_profit_twd: float | None = None
    roi: float | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["sell"] = self.sell.to_dict() if self.sell else None
        d["transfer"] = self.transfer.to_dict() if self.transfer else None
        return d


def _fail_trip(reason: str, **kw: Any) -> RoundTrip:
    return RoundTrip(ok=False, reason=reason, **kw)


def round_trip(
    *,
    buy_quote: RouteQuote,
    holding: str,
    site: str,
    sell_venue: SellVenueConfig,
    sell_price_native: float | None,
    cfg: Config,
    fx: Any,
    price_source: str = "",
    transfer_bundle_size: int = 1,
    remit_batch_size: int | None = None,
    price_unavailable_reason: str = "",
    buy_price_is_final: bool = True,
) -> RoundTrip:
    """一趟完整的買→賣。閘門順序是刻意的，先結構、後數字。

    0. 買進價是**付得出去的價格**嗎（競標中的「目前出價」不是）
    1. 賣場開著嗎
    2. 需要日本收款身分嗎、你有嗎
    3. 貨在的地方 ≠ 賣場要求的地方時，送得過去嗎（送不過去就是**不可行**）
    4. 有沒有這個賣場的行情可以估賣出價（沒有就拒絕給數字，不猜）
    5. 才開始算錢

    順序反過來的話，會先算出一個很漂亮的淨利，再說「喔但是做不到」——
    那個數字一旦出現在畫面上就再也收不回來了。
    """
    base: dict[str, Any] = dict(
        site=site,
        buy_route=buy_quote.route,
        buy_route_label=buy_quote.label,
        landed_twd=buy_quote.landed_twd,
        bundle_size=buy_quote.bundle_size,
        holding=holding,
        sell_venue=sell_venue.name,
        sell_venue_label=sell_venue.label,
        sell_location=sell_venue.location,
        price_source=price_source,
    )

    # 閘門 0：競標中的標的沒有「到手成本」。
    #
    # 這一條是實測踩出來的：第一版的盤點表最賺的十筆全部是 1 円起標的競標，
    # 到手成本 NT$63、報酬率 9,171%——那個 NT$63 是用**會漲的目前出價**算的，
    # 不是你付得出去的價格。拿它去減賣出實拿，就是工程原則 1 的混源比較，
    # 而且方向是「看起來爆賺」，正是會讓人下單的方向。
    # 競標標的的正確出口是 `bidding.max_bid_jpy`（出價上限），不是淨價差。
    if not buy_price_is_final:
        return _fail_trip(
            "競標中：到手成本是用**會漲的目前出價**算的，減出來的淨價差沒有意義。"
            "這種標的的判準是「出價上限 vs 目前出價」（bidding.py），不是轉賣淨利",
            **base,
        )

    if not sell_venue.enabled:
        return _fail_trip(f"{sell_venue.label} 在設定裡被關閉", **base)

    if sell_venue.requires_jp_presence and not cfg.resale.jp_presence:
        return _fail_trip(
            f"{sell_venue.label} 需要日本收款身分（日本地址＋日本銀行帳戶＋日本手機門號），"
            f"目前沒有：{cfg.resale.jp_presence_reason}",
            **base,
        )

    tq = transfer_quote(holding, sell_venue.location, cfg, fx, bundle_size=transfer_bundle_size)
    base["transfer"] = tq
    if not tq.ok:
        return _fail_trip(
            f"貨在「{_LOC_LABEL.get(holding, holding)}」，{sell_venue.label} 要求貨在"
            f"「{_LOC_LABEL.get(sell_venue.location, sell_venue.location)}」：{tq.reason}",
            **base,
        )
    base["transfer_twd"] = tq.cost_twd

    if sell_price_native is None or sell_price_native <= 0:
        return _fail_trip(
            price_unavailable_reason
            or (
                f"沒有 {sell_venue.label} 的行情樣本，估不出賣出價——**拒絕給一個沒依據的淨利**"
                if sell_venue.valuation_venue is None
                else f"這張卡在 {sell_venue.label} 的估價區間下緣取不到，拒絕給淨利"
            ),
            **base,
        )

    sq = net_proceeds(sell_price_native, sell_venue, cfg, fx, remit_batch_size=remit_batch_size)
    base["sell"] = sq
    if not sq.ok or sq.net_twd is None:
        return _fail_trip(sq.reason, **base)

    total_cost = buy_quote.landed_twd + (tq.cost_twd or 0.0)
    profit = sq.net_twd - total_cost
    roi = profit / total_cost if total_cost > _MIN_DENOMINATOR_TWD else None

    notes = [
        f"買：{buy_quote.label} → 到手 NT${buy_quote.landed_twd:,.0f}"
        f"（湊 {buy_quote.bundle_size} 張攤提），貨落在「{_LOC_LABEL.get(holding, holding)}」",
        f"送：{tq.reason if tq.cost_twd == 0 else tq.note or '運送'} → NT${(tq.cost_twd or 0):,.0f}",
        f"賣：{sell_venue.label} 成交 {sell_venue.currency} {sell_price_native:,.0f}"
        f"（{price_source or '估價區間下緣'}）→ 實拿 NT${sq.net_twd:,.0f}"
        f"（實拿率 {(sq.take_rate or 0):.0%}）",
        f"淨利 NT${profit:,.0f} ＝ 實拿 NT${sq.net_twd:,.0f} − 總投入 NT${total_cost:,.0f}",
    ]
    if cfg.resale.tax_note:
        notes.append(f"⚠️ {cfg.resale.tax_note}")

    return RoundTrip(
        ok=True,
        reason="可行" if profit > 0 else "可行但不划算（淨利為負）",
        total_cost_twd=round(total_cost, 2),
        net_proceeds_twd=sq.net_twd,
        net_profit_twd=round(profit, 2),
        roi=roi,
        notes=notes,
        **base,
    )


_LOC_LABEL = {
    HoldingLocation.JP_WAREHOUSE.value: "日本（倉庫／你的日本地址）",
    HoldingLocation.TW_HOME.value: "台灣（你家）",
}


def location_label(value: str) -> str:
    return _LOC_LABEL.get(value, value)


# ---------------------------------------------------------------------------
# 枚舉：一筆標的的所有（買進路徑 × 賣出賣場）
# ---------------------------------------------------------------------------
def buy_options(
    listing: Listing, cfg: Config, fx: Any, *, bundle_size: int | None = None
) -> list[tuple[RouteQuote, RouteConfig]]:
    """這筆標的所有可用的買進路徑（含「貨留日本」那條反事實路徑）。

    「留在日本」那條來自 `resale.jp_hold_route`，**不在 `cfg.routes` 裡**——
    所以 `costs.best_route()` 與 `breakeven` 完全看不到它，買方模型不受影響。
    它只在這裡出現，而且需要日本身分才會被列入。
    """
    out: list[tuple[RouteQuote, RouteConfig]] = []
    for route in cfg.routes_for_site(listing.site.value):
        out.append((quote_route(listing, route, fx, bundle_size=bundle_size), route))

    hold = cfg.resale.jp_hold_route
    if hold is not None and listing.site.value in hold.sites:
        if hold.requires_jp_presence and not cfg.resale.jp_presence:
            pass  # 沒有日本身分就沒有這條路徑——不列出比列出一條走不通的好
        else:
            out.append((quote_route(listing, hold, fx, bundle_size=bundle_size), hold))
    return out


def sell_price_for(
    venue: SellVenueConfig, estimate: Any, fx: Any
) -> tuple[float | None, str]:
    """把估價區間下緣換成賣場幣別的賣出價。回 `(價格, 來源說明)`。

    **一律用區間下緣（`Estimate.lo_twd`），不是點估計**——與出價上限同一個
    哲學：收入寧可低估。點估計的中位誤差是 ×1.9，拿它當預期收入等於一半機率
    高估自己賺得到多少。

    換算用 `fx.twd_to`（中價），與 comps 落庫時的 `to_twd(apply_markup=False)`
    互為反函數——同源（工程原則 1）。
    """
    if estimate is None:
        return None, "沒有估價"
    lo = getattr(estimate, "lo_twd", None)
    if lo is None or lo <= 0:
        return None, "估價取不到 80% 區間下緣"
    native = fx.twd_to(lo, Currency(venue.currency))
    label = getattr(estimate, "level_label", "") or ""
    n_eff = getattr(estimate, "n_effective", 0) or 0
    return native, f"估價 80% 區間下緣 NT${lo:,.0f}（{label}／n={n_eff}）"


def round_trips_for(
    listing: Listing,
    cfg: Config,
    fx: Any,
    *,
    estimate_for: Any,
    bundle_size: int | None = None,
    transfer_bundle_size: int = 1,
) -> list[RoundTrip]:
    """一筆標的的**全部**（買進路徑 × 賣出賣場）組合。

    `estimate_for(valuation_venue) -> Estimate | None`：由呼叫端提供，這個
    模組不自己開估價模型（估價只該有一份，見 `valuation.build_valuator`）。

    回傳**含不可行的組合**，而且不可行的排在後面。不可行的組合必須留在清單裡：
    「Mercari JP 賣得比較貴」這件事的正確下文是「但你到不了那裡，因為 X」，
    把它整條濾掉的話使用者只會反覆自己重新發現那個想法。
    """
    from .bidding import is_live_auction

    venues = cfg.resale.venues
    # 競標標的的「目前出價」不是付得出去的價格——判斷只做一次，往下傳，
    # 不讓每個組合各自去問（多一份定義就會有一天分岔）。
    price_is_final = not is_live_auction(listing)
    trips: list[RoundTrip] = []
    for quote, route in buy_options(listing, cfg, fx, bundle_size=bundle_size):
        for venue in venues.values():
            price, source = (None, "")
            reason = ""
            if venue.valuation_venue is None:
                reason = (
                    f"comps 庫沒有任何 {venue.label} 的成交樣本，估不出賣出價"
                    "——**拒絕給一個沒依據的淨利**"
                )
            else:
                est = estimate_for(venue.valuation_venue)
                price, source = sell_price_for(venue, est, fx)
                if price is None:
                    reason = f"{venue.label}：{source}，拒絕給淨利"
            trips.append(
                round_trip(
                    buy_quote=quote,
                    holding=route.destination,
                    site=listing.site.value,
                    sell_venue=venue,
                    sell_price_native=price,
                    cfg=cfg,
                    fx=fx,
                    price_source=source,
                    transfer_bundle_size=transfer_bundle_size,
                    price_unavailable_reason=reason,
                    buy_price_is_final=price_is_final,
                )
            )
    trips.sort(key=lambda t: (not t.ok, -(t.net_profit_twd or 0.0)))
    return trips


def best_round_trip(trips: list[RoundTrip]) -> RoundTrip | None:
    """最好的**可行**組合。全部不可行時回 None（不退而求其次給一個不可行的）。"""
    ok = [t for t in trips if t.ok and t.net_profit_twd is not None]
    return max(ok, key=lambda t: t.net_profit_twd or 0.0) if ok else None


# ---------------------------------------------------------------------------
# 盤點：哪些組合物理上可行
# ---------------------------------------------------------------------------
def feasibility_matrix(cfg: Config, fx: Any) -> list[dict[str, Any]]:
    """（買進路徑 × 賣出賣場）的**結構**可行性，與任何一筆標的無關。

    這張表回答的是「這條路走不走得通」，不是「划不划算」——所以它不需要
    行情、不需要標的，只需要 destination / location / transfers 三件事。
    有沒有行情樣本另外用 `has_market` 一欄標出來（沒有行情 = 算得出可行、
    但算不出數字，這是兩種不同的「不行」，混在一起會看不出該去補什麼）。
    """
    routes: list[RouteConfig] = list(cfg.routes.values())
    if cfg.resale.jp_hold_route is not None:
        routes.append(cfg.resale.jp_hold_route)

    rows: list[dict[str, Any]] = []
    for route in routes:
        for venue in cfg.resale.venues.values():
            ok, why = True, "可行"
            if not venue.enabled:
                ok, why = False, "賣場被關閉"
            elif route.requires_jp_presence and not cfg.resale.jp_presence:
                ok, why = False, f"買進路徑需要日本身分：{cfg.resale.jp_presence_reason}"
            elif venue.requires_jp_presence and not cfg.resale.jp_presence:
                ok, why = False, f"賣場需要日本身分：{cfg.resale.jp_presence_reason}"
            else:
                tq = transfer_quote(route.destination, venue.location, cfg, fx)
                if not tq.ok:
                    ok, why = False, tq.reason
                elif tq.cost_twd:
                    why = f"可行（需先運送，NT${tq.cost_twd:,.0f}／件）"
            rows.append({
                "route": route.name,
                "route_label": route.label,
                "holding": route.destination,
                "holding_label": location_label(route.destination),
                "venue": venue.name,
                "venue_label": venue.label,
                "venue_location": venue.location,
                "feasible": ok,
                "why": why,
                "has_market": venue.valuation_venue is not None,
                "valuation_venue": venue.valuation_venue,
                "verified": venue.verified,
            })
    return rows


def applicable_sites(route: RouteConfig) -> list[str]:
    """這條路徑服務哪些 `Site`（給報告排版用）。"""
    return [s for s in route.sites if s in {m.value for m in Site}]


def venue_estimator_for_row(valuator: Any, row: dict[str, Any]) -> Any:
    """回一個 `estimate_for(valuation_venue) -> Estimate` 的 callable（帶快取）。

    卡片屬性走 `valuation.card_attrs_from_row`，與買進側的
    `estimate_signal_row` **同一支**——差別只在目標平台由呼叫端指定
    （賣出賣場），而不是標的自己的 site（買進賣場）。屬性各抽一份的話，
    「買進估價」與「轉賣估價」會安靜地分岔（工程原則 1）。
    """
    from .valuation import card_attrs_from_row

    card_name, rarity, grade = card_attrs_from_row(valuator, row)
    cache: dict[str, Any] = {}

    def estimate_for(venue: str) -> Any:
        if venue not in cache:
            cache[venue] = valuator.estimate(
                card_name=card_name, rarity=rarity, grade=grade, venue=venue
            )
        return cache[venue]

    return estimate_for


def listing_from_signal_row(row: dict[str, Any]) -> Listing | None:
    """從 signals 表的一列還原一個 `Listing`（只還原成本模型會用到的欄位）。

    還原失敗回 None，**不回一個補了預設值的假 Listing**：價格或幣別缺一個，
    後面算出來的每一個數字都是編的。
    """
    import json as _json

    try:
        payload = _json.loads(row.get("payload") or "{}") or {}
        lst = payload.get("listing") or {}
        return Listing(
            site=Site(lst["site"]),
            external_id=str(lst.get("external_id") or row.get("key") or ""),
            title=str(lst.get("title") or row.get("title") or ""),
            url=str(lst.get("url") or ""),
            price=float(lst["price"]),
            currency=Currency(lst["currency"]),
            shipping_cost=lst.get("shipping_cost"),
            # `raw` 必須跟著回來：`bidding.is_live_auction` 唯一的判準是
            # raw["price_kind"]，掉了它整批競標標的會被當成「可以直接買」，
            # 而它們的到手成本是用會漲的目前出價算的。
            raw=lst.get("raw") or {},
        )
    except (TypeError, ValueError, KeyError):
        return None
