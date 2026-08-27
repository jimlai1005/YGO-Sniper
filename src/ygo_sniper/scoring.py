"""訊號判定：哪些標的值得你花時間看一眼。

需求對應：
  1. Trigger A/B      → FREE_CARD / DISCOUNT
  2. 需人工處理的情況 → SHIPPING_KILLS_IT / NEEDS_SHIPPING_ASK /
                        NEEDS_BUNDLE_ASK / OFFER_CHANCE
  3. 判斷依據         → CompStats 一併帶回，前端呈現

排序用 score 而不是純折價率，因為「便宜 200 元但資料很確定」
通常比「便宜 60% 但只有一筆 comp」值得先看。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from .bidding import BidCeiling, is_live_auction, max_bid_ebay, max_bid_jpy
from .config import Config
from .costs import quote_all_routes
from .domain import (
    CardInfo,
    CompStats,
    Currency,
    Flag,
    Listing,
    RouteQuote,
    Signal,
    Site,
)

#: 運費佔比告警的預設門檻。config `scoring.overhead_ratio_alert` 可覆寫。
DEFAULT_OVERHEAD_RATIO_ALERT = 0.40


def _flag_shipping(quote: RouteQuote, comps: CompStats, cfg: Config) -> str | None:
    """卡本身便宜，但加上運費雜費之後就沒有優勢了。

    回傳**哪一條規則成立**（不是 bool）：`"comps"` ＝ 拿行情分位數比出來的，
    `"structural"` ＝ 沒有行情、純看成本結構。使用者看到旗標時要能分辨這兩件事
    ——「行情說你買貴了」與「這單有六成在付運費」是兩種不同的處置。
    """
    if not comps.p25_twd or not comps.p40_twd:
        # 沒有 comps 就用結構判斷：超過六成成本不是卡，這單就是在買運費
        return "structural" if quote.overhead_ratio > 0.6 else None
    if quote.item_twd <= comps.p25_twd and quote.landed_twd > comps.p40_twd:
        return "comps"
    return None


def overhead_threshold(cfg: Config) -> float:
    """運費佔比告警門檻。非法值退回預設並印警告（不靜默改寬鬆）。"""
    raw = (getattr(cfg, "scoring", None) or {}).get(
        "overhead_ratio_alert", DEFAULT_OVERHEAD_RATIO_ALERT
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"[warn] scoring.overhead_ratio_alert={raw!r} 不是數字，改用 {DEFAULT_OVERHEAD_RATIO_ALERT}")
        return DEFAULT_OVERHEAD_RATIO_ALERT
    if not 0.0 < value <= 1.0:
        print(f"[warn] scoring.overhead_ratio_alert={value} 不在 (0,1]，改用 {DEFAULT_OVERHEAD_RATIO_ALERT}")
        return DEFAULT_OVERHEAD_RATIO_ALERT
    return value


def overhead_alert(quote: RouteQuote, cfg: Config) -> dict[str, Any] | None:
    """「運費吃掉了 N%」——**不需要任何行情樣本**的結構判斷。

    這是 `SHIPPING_KILLS_IT` 補不上的缺口：那個旗標在有 comps 時走
    p25／p40 比較，一筆 US$30 的卡收 US$32 國際運費（運費佔到手成本 50%）
    只要行情中位數夠高就完全不會被標起來——但它在結構上就不可能划算。

    判準只有這一份：`evaluate()`（掃描當下寫旗標）與 dashboard（讀 db 的
    payload 重新標示舊資料）都呼叫它，兩邊不可能給出不同的百分比（工程原則 1）。
    """
    threshold = overhead_threshold(cfg)
    ratio = quote.overhead_ratio
    if ratio < threshold:
        return None
    return {
        "ratio": ratio,
        "threshold": threshold,
        "overhead_twd": round(quote.overhead_twd, 2),
        "item_twd": quote.item_twd,
        "landed_twd": quote.landed_twd,
        "route": quote.route,
        "label": quote.label,
    }


def shipping_alert_for_row(row: dict[str, Any], cfg: Config) -> dict[str, Any] | None:
    """db 的 signals 列（payload 已解析成 dict）→ 運費佔比告警。

    給 dashboard 用：既有的列是上一輪掃描寫的，不會有新旗標，但使用者現在就
    要看得到「運費吃掉了 N%」。用 `RouteQuote.from_dict` 還原後走**同一個**
    `overhead_alert`，所以重新標出來的數字跟下一次掃描寫進旗標的完全一致。

    競標標的一律不標：目前出價會漲，用它算出來的佔比描述的是一個不會發生的
    世界（與 `evaluate()` 對 `_FIXED_PRICE_ONLY_FLAGS` 的處置同一個理由）。
    """
    flags = row.get("flags") or []
    if Flag.LIVE_AUCTION.value in {f.value if isinstance(f, Flag) else str(f) for f in flags}:
        return None
    best = (row.get("payload") or {}).get("best_route") or {}
    try:
        quote = RouteQuote.from_dict(best)
    except TypeError:
        return None
    return overhead_alert(quote, cfg)


#: 「值得主動打擾你」的旗標。任一個成立才算 trigger。
#: 這一組同時是推播閘門與 dashboard 上「🔥 有觸發」的判準——只有一份定義，
#: 兩邊不可能漂掉（工程原則 1）。前端也用同一組名字（web/static/index.html）。
TRIGGER_FLAGS = frozenset(
    {Flag.FREE_CARD, Flag.DISCOUNT, Flag.OFFER_CHANCE, Flag.BID_WORTH}
)

#: 只有在「這個價格現在就付得出去」時才有意義的旗標。競標標的一律不套這一組——
#: 目前出價會漲，用它算出來的到手成本描述的是一個不會發生的世界。
#: 這是打開 include_live_auctions 之後最容易出的錯：¥1 起標的卡會集體變成
#: 假 FREE_CARD ＋ 假 DISCOUNT，而且每一筆看起來都像撿到寶。
_FIXED_PRICE_ONLY_FLAGS = frozenset(
    {
        Flag.FREE_CARD,
        Flag.DISCOUNT,
        Flag.SHIPPING_KILLS_IT,
        Flag.HIGH_OVERHEAD,
        Flag.SUSPICIOUS_CHEAP,
    }
)


def is_triggered(flags) -> bool:
    """這批旗標裡有沒有 trigger 級的。接受 Flag 或字串，兩種都用得上
    （scoring 端拿 Flag、store/web 端拿 db 裡的字串）。"""
    values = {f.value if isinstance(f, Flag) else str(f) for f in flags}
    return bool({f.value for f in TRIGGER_FLAGS} & values)


def evaluate(
    listing: Listing,
    info: CardInfo,
    comps: CompStats,
    cfg: Config,
    fx,
    *,
    seller_counts: Counter | None = None,
    bundle_size: int | None = None,
    keep_all: bool = False,
    estimate: Any = None,
    now: datetime | None = None,
) -> Signal | None:
    """把一筆候選變成 Signal，或 None（＝不值得留）。

    `keep_all=False`（預設）只留有 trigger 旗標的，這是 Telegram 時代的閘門：
    推播是主動打擾，沒有明確理由就不該出聲。實測 1096 筆掃描 → 125 筆符合年代
    → 只有 7 筆過得了這道門，其餘 118 筆連看一眼的機會都沒有。

    `keep_all=True` 時**照樣算 score 與旗標，只是不丟掉沒觸發的那些**。
    改走 dashboard 之後洗版的成本消失了（清單是你自己去看的，不是它來吵你），
    而「符合年代但沒觸發」對人眼仍然有價值——所以由 config
    （`scoring.keep_all_candidates`）決定，不是寫死。

    ⚠️ 兩種模式下 score 與 flags 的算法**完全相同**：keep_all 只改「留不留」，
    不改「值多少」。分數會因為顯示管道而變的話，排序就沒有意義了。

    ── 競標標的（`price_kind="current_bid"`）走完全不同的一條路 ──────────
    `estimate`（`valuation.Estimate`）給了才算得出出價上限。競標標的的判準是
    **目前出價 < 出價上限**，不是「到手成本 < 鑑定費」——現在価格會漲，
    用它算出來的到手成本描述的是一個不會發生的世界。定價標的完全不受影響：
    `estimate` 對它們沒有任何作用，傳不傳都一樣（見 `_FIXED_PRICE_ONLY_FLAGS`）。
    """
    routes = quote_all_routes(listing, cfg, fx, bundle_size=bundle_size)
    if not routes:
        return None
    best = routes[0]

    auction = is_live_auction(listing)
    flags: list[Flag] = []
    reasons: list[str] = []
    sc = cfg.scoring

    # --- Trigger A：到手成本低於鑑定費，等於卡跟鑑定都免費 ---
    if not auction and best.landed_twd < cfg.grading_fee_twd:
        flags.append(Flag.FREE_CARD)
        reasons.append(
            f"到手 NT${best.landed_twd:,.0f} < 鑑定費 NT${cfg.grading_fee_twd:,.0f}"
        )

    # --- Trigger B：顯著低於行情 ---
    discount = None
    if comps.median_twd and not auction:
        discount = (comps.median_twd - best.landed_twd) / comps.median_twd
        if discount >= float(sc["discount_threshold"]):
            flags.append(Flag.DISCOUNT)
            reasons.append(
                f"低於行情中位數 {discount:.0%}（NT${comps.median_twd:,.0f}，n={comps.n}）"
            )

    # --- 需求 2 的各種人工處理旗標 ---
    kill_kind = _flag_shipping(best, comps, cfg) if not auction else None
    if kill_kind:
        flags.append(Flag.SHIPPING_KILLS_IT)
        basis = "行情 P25／P40 比較" if kill_kind == "comps" else "無行情，純看成本結構"
        reasons.append(
            f"卡價 NT${best.item_twd:,.0f} 便宜，但運費雜費佔 {best.overhead_ratio:.0%}"
            f"（{basis}）"
        )

    # 不需要行情也能判斷的那一條：純看成本結構。與上面那個旗標並存，
    # 但它在**有行情時照樣會觸發**——US$30 的卡收 US$32 運費，
    # 不管行情多少，這一單有一半的錢在買運送。
    if not auction:
        alert = overhead_alert(best, cfg)
        if alert:
            flags.append(Flag.HIGH_OVERHEAD)
            reasons.append(
                f"運費雜費 NT${alert['overhead_twd']:,.0f} 佔到手成本 "
                f"{alert['ratio']:.0%}（門檻 {alert['threshold']:.0%}，不需行情即可判斷）"
            )

    if listing.ships_to_tw is None:
        flags.append(Flag.NEEDS_SHIPPING_ASK)
        reasons.append("賣家未列出寄台灣選項，需私訊確認")

    # eBay 賣家明確不寄台灣，但使用者有美國地址可收（buying.us_ship_zip）。
    # **不丟掉、只標記**：dashboard 要看得到這條後路，但美國→台灣的轉運成本
    # 未建模，所以它不進出價上限（max_bid_ebay 會拒絕）、不進推播規則
    # （notify_rules 會排除）——給一個少算了轉運的數字比不給更危險（誠實邊界）。
    if (
        listing.site is Site.EBAY
        and listing.ships_to_tw is False
        and (getattr(cfg, "buying", None) or {}).get("us_ship_zip")
    ):
        zip_code = str(cfg.buying["us_ship_zip"])
        flags.append(Flag.US_SHIP_OPTION)
        reasons.append(
            f"賣家不寄台灣，但可寄你的美國地址（{zip_code}）——"
            "美國→台灣轉運成本未建模，僅供人工評估，不給上限、不推播"
        )

    if seller_counts and listing.seller_id and seller_counts[listing.seller_id] > 1:
        flags.append(Flag.NEEDS_BUNDLE_ASK)
        reasons.append(f"同賣家共 {seller_counts[listing.seller_id]} 筆命中，可問合併運費")

    if listing.best_offer_enabled:
        stale_days = None
        if listing.listed_at:
            # `now` 可注入：時效判斷比的是「上架多久」，拿真實牆上時鐘去比
            # 固定日期的測試 fixture，測試會在 fixture 滿 30 天的那一天開始紅
            # （2026-08-26 test_bidding_ebay 實際發生過）。生產路徑不傳，行為不變。
            stale_days = ((now or datetime.now(UTC)) - listing.listed_at).days
        if stale_days is None or stale_days >= int(sc["offer_stale_days"]):
            flags.append(Flag.OFFER_CHANCE)
            age = f"上架 {stale_days} 天" if stale_days is not None else "接受議價"
            reasons.append(f"{age}，值得直接丟 offer")

    if comps.n < int(sc["min_comps"]):
        flags.append(Flag.THIN_COMPS)
        reasons.append(f"行情樣本僅 {comps.n} 筆，請自己看照片判斷")

    # 便宜到不合理通常有事：假鑑定盒、裂殼、標題寫錯
    if not auction and comps.p25_twd and best.landed_twd < comps.p25_twd * 0.4:
        flags.append(Flag.SUSPICIOUS_CHEAP)
        reasons.append("價格遠低於 P25，注意假殼／破損／標題不符")

    # --- 競標分支：判準是「目前出價 vs 出價上限」，不是到手成本 ---
    # eBay 走自己的幣別鏈（`max_bid_ebay`：台幣反解、listing 自己的比率換回原幣、
    # listing 上的實際運費）；其餘走日圓反解。兩邊的哲學與證據閘門完全相同。
    ceiling: BidCeiling | None = None
    if auction:
        if listing.site is Site.EBAY:
            ceiling = max_bid_ebay(estimate, cfg, fx, listing=listing)
        else:
            ceiling = max_bid_jpy(estimate, cfg, fx, site=listing.site)
        flags.append(Flag.LIVE_AUCTION)
        reasons.append(_auction_reason(listing, ceiling))
        if not ceiling.ok:
            flags.append(Flag.BID_NO_CEILING)
        elif ceiling.is_actionable(listing.price):
            flags.append(Flag.BID_WORTH)

    # 賣家明確不寄台灣（ships_to_tw=False）：這筆「僅供人工評估」，觸發類
    # 旗標一律壓掉——US 後路不是出手理由（美→台轉運成本未建模，FREE_CARD／
    # DISCOUNT 引用的到手成本本來就少算了一段運費）。在單一出口整組剔除而
    # 不是各分支自己判 ships_to_tw：靠每個分支記得判的話，下一個新增的
    # trigger 旗標一定會漏（工程原則 5）。reason 與資訊旗標（US_SHIP_OPTION、
    # OFFER_CHANCE 的文字說明）保留——dashboard 要看得到這條後路。
    if listing.ships_to_tw is False:
        flags = [f for f in flags if f not in TRIGGER_FLAGS]

    # 沒有任何值得看的理由就不要推播（keep_all 時改成留下來讓你自己看）
    if not keep_all and not is_triggered(flags):
        return None

    return Signal(
        listing=listing,
        card=info,
        best_route=best,
        all_routes=routes,
        comps=comps,
        flags=flags,
        score=_score(best, comps, flags, discount, cfg, ceiling, listing.price),
        reason=" ｜ ".join(reasons),
        bid=ceiling,
    )


#: 幣別 → 顯示符號。與 appraise._CURRENCY_SYMBOL 同值；競標 reason 的每個金額
#: 都要帶得出幣別——「NT$ 還是 US$」寫錯一次就是差 30 倍。
_CCY_SYMBOL = {Currency.JPY: "¥", Currency.USD: "US$", Currency.TWD: "NT$"}


def _auction_reason(listing: Listing, ceiling: BidCeiling) -> str:
    """競標標的的第一句話。**目前出價一定要出現在句子裡**——它是會漲的那個數字，
    使用者看到「上限 ¥3,000」卻不知道現在已經 ¥2,980 的話，上限就沒有意義。

    幣別跟著 listing 走：Yahoo 是日圓；eBay 是台幣（eBay 換算顯示），且上限
    另附**原幣**——那才是使用者要填進 eBay 出價欄的數字。
    """
    sym = _CCY_SYMBOL.get(listing.currency, listing.currency.value + " ")
    now = f"目前出價 {sym}{listing.price:,.0f}"
    if listing.bids is not None:
        now += f"（{listing.bids} 次出價）"
    if not ceiling.ok:
        return f"🔨 競標中，{now}；**不提供出價上限**：{ceiling.reason}"
    comparison = ceiling.comparison_ceiling()
    shown = f"{sym}{comparison:,.0f}"
    if ceiling.max_bid_native is not None and ceiling.native_currency:
        shown += f"（eBay 出價欄填 {ceiling.native_currency} {ceiling.max_bid_native:,.2f}）"
    room = ceiling.headroom_value(listing.price)
    if room is not None and room > 0:
        return (
            f"🔨 競標中，{now}，你的出價上限 {shown}"
            f"（還有 {sym}{room:,.0f} 空間）"
        )
    return (
        f"🔨 競標中，{now} 已達／超過你的出價上限 {shown}"
        f"——**放掉它**，追價就沒有安全邊際了"
    )


def _score(
    best: RouteQuote,
    comps: CompStats,
    flags: list[Flag],
    discount: float | None,
    cfg: Config,
    ceiling: BidCeiling | None = None,
    current_bid: float | None = None,
) -> float:
    """0-100。設計原則：確定性 > 幅度。

    折價 60% 但只有一筆 comp，跟折價 25% 但有二十筆 comp，
    後者值得你先看。信心度直接當乘數處理這件事。

    競標標的走**另一把尺**：分數來自「離上限還有多遠」（headroom 比例），
    因為它的到手成本是一個會漲的數字。兩把尺放在同一個 0-100 區間是刻意的
    ——清單是混排的，分數不同基準就不能一起排序（工程原則 1）。
    """
    if ceiling is not None:
        return _auction_score(ceiling, comps, flags, current_bid)

    score = 0.0

    if Flag.FREE_CARD in flags:
        # 越低於鑑定費越高分，但設上限避免一張 ¥300 的爛卡霸榜
        margin = (cfg.grading_fee_twd - best.landed_twd) / cfg.grading_fee_twd
        score += 40 * min(1.0, max(0.0, margin) * 2)

    if discount:
        score += 45 * min(1.0, discount / 0.6)

    if Flag.OFFER_CHANCE in flags:
        score += 8

    conf_mult = {"high": 1.0, "medium": 0.8, "low": 0.55}[comps.confidence]
    score *= conf_mult

    if Flag.SHIPPING_KILLS_IT in flags:
        score -= 12
    # HIGH_OVERHEAD **刻意不扣分**：佔比高不等於不划算。一張到手 NT$294 的卡
    # 有 98% 是雜費，但它照樣低於鑑定費——那是「白撿」，不是「被運費坑」。
    # 這個旗標的作用是讓使用者一眼看出錢花在哪，扣分會讓它變成第二個折價判準。
    if Flag.SUSPICIOUS_CHEAP in flags:
        score -= 15
    if Flag.NEEDS_SHIPPING_ASK in flags:
        score -= 5

    return round(max(0.0, min(100.0, score)), 1)


def _auction_score(
    ceiling: BidCeiling,
    comps: CompStats,
    flags: list[Flag],
    current_bid: float | None,
) -> float:
    """競標標的的分數：離上限越遠越值得先看。

    沒有上限（樣本不足）一律 0 分——不是「中性」，是「這筆我沒有依據」，
    它不該跟一個有依據的候選排在一起。這與紅線是同一個決定：
    沒有依據的時候，工具的正確行為是往後站，不是給一個中間值。

    `current_bid` 的幣別跟著 listing 走（`headroom_pct` 是同幣別相除的比例，
    幣別無關）——Yahoo 傳日圓、eBay 傳台幣，兩邊都是同單位比較。
    """
    if not ceiling.ok:
        return 0.0
    pct = ceiling.headroom_pct(current_bid)
    if pct is None or pct <= 0:
        # 已達上限：留一個很低但非零的分數，讓它排在「沒依據」那批前面
        # ——「算得出上限、只是現在太貴了」確實比「算不出上限」有資訊量。
        return 1.0
    # headroom 佔上限五成以上就給滿分：再多的空間多半是「還沒有人出價」，
    # 而不是「這筆特別便宜」——尾盤才是決勝點，早期的空無一人不值得加倍加分。
    score = 85 * min(1.0, pct / 0.5)
    score *= {"high": 1.0, "medium": 0.9, "low": 0.75}[comps.confidence]
    if Flag.THIN_COMPS in flags:
        score -= 5
    return round(max(0.0, min(100.0, score)), 1)


def seller_histogram(listings: list[Listing]) -> Counter:
    return Counter(x.seller_id for x in listings if x.seller_id)
