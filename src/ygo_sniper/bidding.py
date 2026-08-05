"""出價上限引擎 —— 競標管道唯一的數字出口。

## 為什麼要有這個模組

實測（venue_study）：**Yahoo 的競標出清價只有它自己即決價的 0.19 倍**。
在此之前 `include_live_auctions: false` 把純競標標的全部丟掉，等於只看全市場
最貴的買法（即決＝賣家開的溢價），瞎在唯一真正便宜的管道上。

競標之所以能被打開，是因為使用者給了一個承諾：**設上限、絕不追價**。
有了這個承諾，競標的風險結構就翻轉了——買不到的成本是零，所以唯一要守的事
是「上限本身不能算錯」。這個模組只做這一件事。

## 核心設計決定：上限用區間下緣，不是點估計

模型點估計的中位誤差是 ×1.9。拿點估計當上限，等於有**一半的機率出價過高**
——而且錯的那一半你會真的付錢。所以上限一律從 conformal 80% 區間的下緣
（`Estimate.lo_twd`）反推：意思是「只有便宜到連最保守的估計都划算時才出價」。

代價很明確：**大多數競標會輸**。這是設計，不是缺陷——輸的成本是零，
贏的每一次都在安全邊際內。UI 必須把這句話講出來，不然使用者會以為工具壞了。

## 紅線：樣本不足時不准輸出上限

`lo_twd` 是 None（校準集不足以給區間）時，這裡回 `ok=False`、`max_bid_jpy=None`，
**絕不退化成用點估計猜一個數字**。使用者會照著這個數字下真錢的單，
一個沒有依據的數字比沒有數字危險得多。

2026-08-02 把同一條紅線延伸成五道**證據閘門**（`EvidenceGate`），全部可在
config 的 `bidding:` 區塊調整。每一道的門檻都有實測依據，不是拍腦袋：

0. `require_known_grade`（預設 true）——**分數不明**就不給上限。
   `ValuationModel.g(None)` 對未知分數的處理是「當成基準分數 9，不猜」，那在
   估價時是合理的降級，但**出價不能建立在一個沒說出口的假設上**：分數溢價
   從 7 分的 ×0.35 到 10 分的 ×3.95 橫跨 11 倍，猜錯的方向正好是「公允價被
   高估」。實測（出貨路徑、venue-aware）：分數未知的估計中位誤差 **×7.50**、
   區間覆蓋率只有 **25%**、下尾違反率 15%；分數已知是 ×1.96／85%／5%。
   ⚠️ 分數未知的測試樣本只有 20 筆，這個數字本身不精確——但方向與量級跟
   機制（11 倍的分數溢價被替換成 1.0）完全吻合，而且 `appraise.decide_verdict`
   早就把 grade=None 列為「無法判斷」的缺口。同一個缺口在鑑價那邊夠格否決，
   在出價這邊沒理由放行。
   2026-08-02 補：分數現在**不只看標題**。標題只寫「鑑定品」的標的，
   `parsers.grade.resolve_grade` 會再去撈商品描述（`appraise` 抓得到描述的
   平台才有），撈到就解鎖這道閘門，但 `Estimate.grade_source="description"`
   會讓 `evidence_tier` 從 strong 降到 moderate——同一個數字，來源不同，
   可信度就不同。撈不到、或描述與標題矛盾，一律維持 grade=None（不猜）。
1. `require_card_specific_level`（預設 true）——估價層級必須是 L1／L2，
   也就是**至少有一筆「這張卡」自己的成交**撐著。L3／L0 代表模型根本沒比對到
   卡名（或這張卡在庫裡沒有同稀有度成交），點估計其實是「這個稀有度＋分數的
   典型價」。2026-08-02 實測（出貨路徑、1006 筆測試樣本）：L3 的點估計中位
   誤差 ×2.25、下尾違反率 13%（分群後 10%，剛好貼著名目值）；L1／L2 是
   ×1.77／×1.99、下尾 5%／2%。也就是說 L3 的「上限開太高」風險是有同卡成交
   時的 3-5 倍。拿一個「這種稀有度大概值多少」的數字去出價，跟賭沒有兩樣。
2. `reject_n_buckets`（預設 `("10-49",)`）——**校準已知壞掉的分桶直接不給上限**。
   這是 `require_known_grade` 的同一個哲學：修不好就不要輸出。
   2026-08-02 診斷（時間切分、平台內切早晚、3 個 test_fraction 合計 1104 筆，
   同一份 comps 快照）：`10-49` 桶的下尾違反率 **29%**（名目 10%），
   其餘三桶是 2%／3%／11%。組成（實測，不是推測；用
   `coverage-groups --diagnose` 可重跑）：69 筆裡 50 筆是 L3、52 筆是分數 10、
   44 筆是 normal／rare 這種低稀有度；最壞的一角是 **L3 × Mercari**
   （19 筆，下尾違反 **100%**、覆蓋率 **0%**、點估計高估 ×5.9）。
   原因是模型的平台係數（該切分 Mercari ×3.45）由高價卡估出來，套到便宜的
   normal／rare 卡完全不成立——那些卡在 Mercari 與 Yahoo 是同一個價位
   （實測 NT$1.1k-1.8k vs NT$1.5k-2.7k）。六種分群鍵實測全部修不好
   （層級×平台 25%、層級×稀有度層 30%、層級×n桶×稀有度層 29%、
   層級×n桶×平台 29%、10-49 併入 n>=50 29%、只用層級 29%），而且最好的那個
   （層級×平台）還讓 3-9 桶從 3% 惡化到 6%。修不好的原因是校準集裡根本沒有
   這個失敗切片——那是點估計的段狀偏誤，不是分位數選錯群。
   所以這一桶改成拒絕輸出，並在 reason 裡把上面這串講給使用者聽。
   實測影響：37 筆競標標的中，有上限的從 27 筆降到 25 筆。
3. `min_calibration_samples`（預設 50）——撐著這個區間的校準殘差筆數下限。
   估價層自己的門檻是 30（低於就退化合併），出價這一側刻意訂得更嚴：
   **區間可以拿來參考，不代表足以下單**。
4. `min_effective_samples`（預設 1）——所用層級至少要有這麼多筆成交。
   ⚠️ 這道閘門刻意**很鬆**，理由寫在 `valuation.py` 頂註：`n_effective` 是
   「所用層級的池大小」，與誤差**反向**相關（n=1 多半是 L1 找到同一張卡，
   n=325 是退到整個稀有度的池）。實測 n<3 那一桶的下尾違反率只有 2%，是全部
   分桶裡最保守的一群——把門檻調高會砍掉最好的估計、留下最差的。
   留這個鍵是給你自己收緊用的，但**調高之前請先跑 `ygo-sniper coverage-groups`
   重新量一次**，不要照直覺調。

## 反解與正算同源（工程原則 1）

上限是用 `costs.max_item_price_jpy()` 反解出來的，而它是 `costs.quote_route()`
的嚴格逆函式：同一組 route 參數、同一個 fx 物件、同一個 bundle_size。
每次算完還會**用正算再驗一次**（`landed_at_ceiling_twd`），對不上就拒絕輸出
上限並在 reason 裡講明原因——寧可不給數字，也不要給一個「照著出價之後
到手成本卻超過公允價」的無聲錯誤。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Config
from .costs import max_item_price_jpy, quote_route
from .domain import Currency, Listing, Site

#: 目標利潤率預設值。config `bidding.target_margin` 可覆寫。
DEFAULT_TARGET_MARGIN = 0.30

#: 證據閘門的預設值。每一條的依據見模組頂註「五道證據閘門」。
DEFAULT_MIN_EFFECTIVE_SAMPLES = 1
DEFAULT_MIN_CALIBRATION_SAMPLES = 50
DEFAULT_REQUIRE_CARD_SPECIFIC_LEVEL = True
DEFAULT_REQUIRE_KNOWN_GRADE = True

#: 校準已知壞掉、直接拒絕給上限的 `n_effective` 分桶（`valuation.n_bucket` 的標籤）。
#: 依據見模組頂註第 2 道閘門：`10-49` 這一桶實測下尾違反率 29%（名目 10%），
#: 六種分群鍵全部修不好，因為問題不在分群而在點估計本身有段狀偏誤。
DEFAULT_REJECT_N_BUCKETS: tuple[str, ...] = ("10-49",)

#: 「有這張卡自己的成交」的層級。與 `valuation.Estimate.has_card_specific_evidence`
#: 同一個定義——兩邊各寫一份就會有一天分岔。
CARD_SPECIFIC_LEVELS = ("L1", "L2")

#: `Listing.raw["price_kind"]` 裡代表「競標中、這個價會漲」的值。
#: 定義只有一份：sources/yahoo.py 寫入、這裡判讀、scoring 與 web 都問這個函式。
LIVE_AUCTION_KIND = "current_bid"

#: 正算回頭驗證的容差（台幣）。來源是 `quote_route` 對每個欄位的 2 位小數捨入，
#: 三個欄位加起來最多 0.015；給 0.05 已經是十倍餘裕。**不是**「差不多就好」的旋鈕：
#: 超過這個值代表反解與正算真的不同源，那是必須拒絕出數字的等級。
FORWARD_CHECK_TOLERANCE_TWD = 0.05

#: UI 必須顯示的誠實標註。放在後端當常數的理由：這些句子是判斷的一部分，
#: 前端自己寫一份就會跟模型漂掉（工程原則 1）。
HONESTY_NOTES: tuple[str, ...] = (
    "上限用的是 80% 區間的**下緣**，不是點估計——模型點估計的中位誤差是 ×1.9，"
    "拿點估計當上限等於有一半機率出價過高。",
    "**你會經常輸掉競標，這是設計不是缺陷**：只有便宜到連最保守估計都划算時才出價。"
    "輸掉的成本是零，贏的每一次都在安全邊際內。",
    "目前出價**會漲**，最終成交價通常在結標前幾分鐘才跳動。"
    "這裡顯示的目前出價是上一次掃描當下的值，不是即時報價。",
    "上限已經扣掉代購手續費、國內運費、集運攤提、國際運費、刷卡海外手續費與匯率緩衝，"
    "所以**它就是你可以直接填進出價欄的數字**，不要再自己加減。",
    "eBay 競標的上限以**原幣**顯示（那才是 eBay 出價欄吃的幣別），已扣掉該筆 listing "
    "的實際國際運費與刷卡加成；台幣等值用的是**這筆 listing 自己的 eBay 匯率**。"
    "eBay 原生支援自動出價（automatic bidding）：設好上限就可以離開。",
    "本工具只計算，**不碰錢**：沒有任何自動出價／自動下單功能，出價一律你自己按。",
)

#: --- 競標視圖的可行動性梯隊 -------------------------------------------------
#: 「現在不看就沒了」的時間窗（小時）。競標價在結標前幾分鐘才會跳，所以
#: 「有空間」這件事只有在**快結標**時才是一個可以行動的事實。
ACTIONABLE_WINDOW_HOURS = 24.0
#: 超過這個時數才結標的標的，現價**沒有參考價值**——此刻的「空間」只是還沒開始
#: 競價。實測依據：新着排序抓到的 50 筆結標倒數中位數 115 小時、現價大量是 ¥1。
#: 使用者看到 `¥1 → 上限 ¥5,840` 會以為撿到寶，那是這個標註要防的誤讀。
PRICE_DYNAMICS_HOURS = 48.0

#: 梯隊定義（1 最急）。**排序規則與文案放後端**：前端自己寫一份，門檻改了
#: 畫面不會跟著改（工程原則 1，與 evidence_tier 同一個立場）。
AUCTION_TIERS: tuple[dict[str, Any], ...] = (
    {
        "tier": 1,
        "label": "⚡ 現在就該看",
        "hint": (
            f"有出價上限、現價仍低於上限、而且 {ACTIONABLE_WINDOW_HOURS:.0f} 小時內結標"
            "——這是唯一「現在不看就沒了」的類別"
        ),
    },
    {
        "tier": 2,
        "label": "🕒 有空間，但還早",
        "hint": "現價低於你的上限，但離結標還久：價格一定會漲，現在的空間不算數",
    },
    {
        "tier": 3,
        "label": "· 其餘",
        "hint": "沒有出價上限（證據不足），或現價已經越過上限",
    },
)

#: >48 小時那條的誠實標註。與 HONESTY_NOTES 同一個角色：它是判斷的一部分。
PRICE_DYNAMICS_NOTE = (
    f"⏳ 還有 {PRICE_DYNAMICS_HOURS:.0f} 小時以上才結標：**現價會漲**，"
    "此刻的空間不代表最終成交價。競標價通常在結標前幾分鐘才跳動，"
    "現在的「低於上限」只表示競價還沒開始。"
)


def actionable_window_hours(cfg: Any = None) -> float:
    """梯隊 1 的時間窗（小時）。config `bidding.actionable_window_hours` 可覆寫。

    **這個旋鈕刻意只有一顆**：dashboard 的「⚡ 現在就該看」與 Telegram 的
    競標急件推播是同一個判定，兩邊各給一顆門檻的話，畫面上排第一的那筆
    跟手機上收到的那筆會是兩批東西（工程原則 1）。所以 notify 區塊**沒有**
    自己的窗口設定，一律問這裡。非法值退回預設並印警告（不靜默）。
    """
    raw = (getattr(cfg, "bidding", None) or {}).get(
        "actionable_window_hours", ACTIONABLE_WINDOW_HOURS
    )
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        print(
            f"[warn] bidding.actionable_window_hours={raw!r} 不是數字，"
            f"改用 {ACTIONABLE_WINDOW_HOURS}"
        )
        return ACTIONABLE_WINDOW_HOURS
    if hours <= 0:
        print(
            f"[warn] bidding.actionable_window_hours={hours} 不是正數，"
            f"改用 {ACTIONABLE_WINDOW_HOURS}"
        )
        return ACTIONABLE_WINDOW_HOURS
    return hours


def auction_view_config(cfg: Any = None) -> dict[str, Any]:
    """競標視圖要用的門檻與文案（前端拿去排序與標註，不自己拍數字）。"""
    return {
        "actionable_window_hours": actionable_window_hours(cfg),
        "price_dynamics_hours": PRICE_DYNAMICS_HOURS,
        "tiers": [dict(t) for t in AUCTION_TIERS],
        "price_dynamics_note": PRICE_DYNAMICS_NOTE,
    }


# --- 梯隊判定（後端的那一份定義）-------------------------------------------
# 前端 `web/static/index.html` 的 `auctionTier()` 是同一套規則的 JS 實作（畫面
# 排序用），兩邊吃同一份門檻（`auction_view_config`），而且 `tests/test_notify_rules.py`
# 有一條交叉測試把同一組樣本同時餵進 node 與這裡，兩邊的梯隊必須一模一樣。
# 推播不自己判「什麼叫急件」——它問的是這個函式。
def auction_end_time(listing: Any) -> datetime | None:
    """標的的結標時間（帶時區的 datetime）。解析不出來一律 None——**不猜**。"""
    end = listing if isinstance(listing, datetime) else getattr(listing, "end_time", None)
    if end is None and isinstance(listing, str):
        end = listing
    if isinstance(end, str):
        try:
            end = datetime.fromisoformat(end)
        except ValueError:
            return None
    if not isinstance(end, datetime):
        return None
    return end if end.tzinfo is not None else end.replace(tzinfo=UTC)


def hours_until_end(listing: Any, now: datetime) -> float | None:
    """還有幾小時結標。負值＝已經結標；沒有結標時間一律 None。"""
    end = auction_end_time(listing)
    if end is None:
        return None
    return (end - now).total_seconds() / 3600.0


def ceiling_jpy_of(bid: Any) -> float | None:
    """從 `BidCeiling` 或 payload 裡的 bid dict 取出「有效的」上限。

    `ok=False` 一律回 None：那時候的 `max_bid_jpy` 本來就是 None，
    但 payload 是外部資料，防的是一個 ok=False 卻留著舊數字的列。
    """
    if bid is None:
        return None
    if isinstance(bid, BidCeiling):
        return bid.max_bid_jpy if bid.ok else None
    if not isinstance(bid, dict):
        return None
    if not bid.get("ok"):
        return None
    value = bid.get("max_bid_jpy")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def ceiling_value_of(bid: Any) -> float | None:
    """「與 `Listing.price` 同幣別基準」的有效上限（Yahoo→`max_bid_jpy`、
    eBay→`max_bid_listing`）。梯隊判定與推播規則 1 一律問這裡。

    與 `ceiling_jpy_of` 的分工：那支**只講日圓**（Yahoo 專用欄位），這支回的是
    「跟這筆標的自己的現價同單位」的數字——eBay 的現價是台幣，上限也得是台幣
    才能相減（工程原則 1）。
    """
    if bid is None:
        return None
    if isinstance(bid, BidCeiling):
        return bid.comparison_ceiling()
    if not isinstance(bid, dict) or not bid.get("ok"):
        return None
    value = bid.get("max_bid_listing")
    if value is None:
        value = bid.get("max_bid_jpy")
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def auction_room_value(bid: Any, current_bid: float | None) -> float | None:
    """現價與上限的距離（**與 Listing.price 同幣別**）。沒有上限、沒有現價一律
    None，**不退化成 0**——0 會被讀成「剛好沒空間」，那是一個假的事實。"""
    ceiling = ceiling_value_of(bid)
    if ceiling is None or current_bid is None:
        return None
    try:
        return ceiling - float(current_bid)
    except (TypeError, ValueError):
        return None


def auction_room_jpy(bid: Any, current_bid_jpy: float | None) -> float | None:
    """現價與上限的距離（**日圓**，Yahoo 專用）。eBay 標的沒有日圓上限，一律 None。"""
    ceiling = ceiling_jpy_of(bid)
    if ceiling is None or current_bid_jpy is None:
        return None
    try:
        return ceiling - float(current_bid_jpy)
    except (TypeError, ValueError):
        return None


def auction_tier(
    bid: Any,
    current_bid_jpy: float | None,
    end_time: Any,
    now: datetime,
    *,
    window_hours: float | None = None,
) -> int:
    """梯隊：1 = 現在就該看（有空間＋快結標）、2 = 有空間但還早、3 = 其餘。

    與前端 `auctionTier()` 逐條對齊：沒空間 → 3；結標時間未知或已結標 → 2
    （一個不知道何時結標的標的排進第一梯隊，等於用猜測擠掉真的在倒數的那筆）。

    room 走 `auction_room_value`（與 listing 同幣別）：Yahoo 行為不變（退回
    `max_bid_jpy`），eBay 的台幣現價對台幣上限——兩邊都是同單位相減。
    """
    window = ACTIONABLE_WINDOW_HOURS if window_hours is None else float(window_hours)
    room = auction_room_value(bid, current_bid_jpy)
    if room is None or room <= 0:
        return 3
    left = hours_until_end(end_time, now)
    if left is not None and 0 < left <= window:
        return 1
    return 2


#: 代理出價（自動入札）的實地查證結果。2026-08-02 由 buyee.jp 官方說明頁實測擷取。
PROXY_BID_FINDING: dict[str, Any] = {
    "supported": True,
    "checked_at": "2026-08-02",
    "sources": (
        "https://buyee.jp/helpcenter/guide/how-to-bid",
        "https://buyee.jp/helpcenter/guide/sniper-bid",
    ),
    "summary": (
        "Buyee **支援**代理出價：官方說明頁原文「you bid the maximum amount of your "
        "budget, and bids will automatically be made from the current price up to your "
        "entered amount」——填入最高出價後系統會自動替你跟價到那個上限為止，"
        "**設好上限就可以離開，不必守在結標時間**。"
    ),
    "details": (
        "計價是**第二價格**（second-price）：官方原文「the highest bidder wins the auction, "
        "but the amount paid is the second-highest bid plus the specified bidding range」"
        "——贏的時候通常付得比你的上限低。",
        "另有免費的「Sniper Bid（狙擊出價）」：結標前 5 分鐘自動送出，同一標的"
        "**不能同時下狙擊與一般出價**，且結標前 15 分鐘不接受取消。",
        "有 bid increment（最小加價幅度）：上限低於最小加價幅度時出價會失敗。"
        "所以上限算出來太接近目前出價時，實務上可能根本按不下去。",
    ),
    "caveat": (
        "說明頁把這條管道稱為「JDirectItems Auction」（Buyee 對 Yahoo! JAPAN 拍賣的品牌名），"
        "本工具產出的購買連結是 buyee.jp/item/yahoo/auction/{id}，兩者指的是同一條管道；"
        "**第一次實際出價前請自己在頁面上確認一次欄位名稱**。"
    ),
}


#: eBay 代理出價（automatic bidding）的實地查證結果。2026-08-03 由 eBay 官方
#: 說明頁（Customer Service → Buying → How bidding works → Automatic bidding）
#: 實抓擷取（curl 直抓 HTML、去標籤後逐句核對）。格式與 `PROXY_BID_FINDING` 同構。
EBAY_PROXY_BID_FINDING: dict[str, Any] = {
    "supported": True,
    "checked_at": "2026-08-03",
    "sources": (
        "https://www.ebay.com/help/buying/bidding/automatic-bidding?id=4014",
    ),
    "summary": (
        "eBay **原生支援**代理出價（automatic bidding）：官方說明頁原文「Simply "
        "enter the highest price you're willing to pay for an item, and we'll do "
        "the rest.」「Once you set up automatic bidding, you can stay ahead of the "
        "competition for an item without needing to be on the eBay site.」——"
        "填入最高出價後 eBay 會自動替你跟價，**設好上限就可以離開**，"
        "不必守著結標時間（結標多在台灣深夜，這一點很重要）。"
    ),
    "details": (
        "跟價機制是**增量代理**（效果等同第二價格）：官方原文「We'll bid in "
        "increments on your behalf to keep you in the lead but only up to your "
        "limit.」「When someone else places a bid, we'll place a slightly higher "
        "bid on your behalf.」——贏的時候付的是第二高出價＋一個 bid increment，"
        "通常低於你的上限。",
        "有 bid increment（最小加價幅度）：「Bid increments are smaller when the "
        "bid price is low and larger in higher price brackets.」上限太貼近現價時"
        "可能連一個增量都加不上去。",
        "官方也提醒**上限之外還要付運費**：「you'll need to pay the cost of "
        "shipping too」——本工具的上限已把 listing 的實際運費反解掉，"
        "所以出價欄照填上限即可，不要再自己扣一次。",
    ),
    "caveat": (
        "說明頁未使用「second-price」一詞；「付第二高出價＋增量」是從官方描述的"
        "增量跟價機制直接推得（引文如上），非官方原句。出價是契約義務"
        "（官方原文「it is a contractual obligation」），上限填下去就要願意付。"
    ),
}


def is_live_auction(listing: Any) -> bool:
    """這筆標的是不是「競標中、價格還會漲」。

    唯一判準是 `raw["price_kind"] == "current_bid"`（由 sources/yahoo.py 寫入）。
    scoring、pipeline、market_search、web 全部問這個函式，不各自比字串——
    多一份定義，就會有一天出現「這邊當競標、那邊當即決」的分岔。
    """
    raw = getattr(listing, "raw", None) or {}
    return str(raw.get("price_kind") or "") == LIVE_AUCTION_KIND


def target_margin_from(cfg: Any) -> float:
    """讀 config `bidding.target_margin`，非法值退回預設並印警告（不靜默）。"""
    raw = (getattr(cfg, "bidding", None) or {}).get("target_margin", DEFAULT_TARGET_MARGIN)
    try:
        margin = float(raw)
    except (TypeError, ValueError):
        print(f"[warn] bidding.target_margin={raw!r} 不是數字，改用 {DEFAULT_TARGET_MARGIN}")
        return DEFAULT_TARGET_MARGIN
    if not 0.0 <= margin < 1.0:
        print(f"[warn] bidding.target_margin={margin} 不在 [0,1)，改用 {DEFAULT_TARGET_MARGIN}")
        return DEFAULT_TARGET_MARGIN
    return margin


@dataclass(slots=True, frozen=True)
class EvidenceGate:
    """出價上限的五道證據閘門。依據見模組頂註。

    每一條非法值都退回預設並**印警告**（不靜默）：一個被打錯的門檻會讓工具
    在使用者不知情的狀況下變寬鬆，那正是這個結構要防的事。
    """

    min_effective_samples: int = DEFAULT_MIN_EFFECTIVE_SAMPLES
    min_calibration_samples: int = DEFAULT_MIN_CALIBRATION_SAMPLES
    require_card_specific_level: bool = DEFAULT_REQUIRE_CARD_SPECIFIC_LEVEL
    require_known_grade: bool = DEFAULT_REQUIRE_KNOWN_GRADE
    reject_n_buckets: tuple[str, ...] = DEFAULT_REJECT_N_BUCKETS

    @classmethod
    def from_config(cls, cfg: Any) -> EvidenceGate:
        b = dict(getattr(cfg, "bidding", None) or {})
        return cls(
            min_effective_samples=_int_setting(
                b, "min_effective_samples", DEFAULT_MIN_EFFECTIVE_SAMPLES
            ),
            min_calibration_samples=_int_setting(
                b, "min_calibration_samples", DEFAULT_MIN_CALIBRATION_SAMPLES
            ),
            require_card_specific_level=bool(
                b.get("require_card_specific_level", DEFAULT_REQUIRE_CARD_SPECIFIC_LEVEL)
            ),
            require_known_grade=bool(
                b.get("require_known_grade", DEFAULT_REQUIRE_KNOWN_GRADE)
            ),
            reject_n_buckets=_bucket_setting(b, "reject_n_buckets"),
        )

    # ------------------------------------------------------------------
    def check(self, estimate: Any) -> str | None:
        """通過回 None，擋下來回一句「還缺什麼」。

        訊息刻意寫成「缺什麼」而不是「不合格」：使用者看到的下一步應該是
        「再等幾筆同卡成交進來」，不是「這個工具壞了」。

        **順序即嚴重度**：分數未知排第一，因為它是唯一一個「模型替你做了一個
        沒說出口的假設」的情形——另外三道只是證據薄，這一道是基準可能整個錯掉。
        """
        if self.require_known_grade and getattr(estimate, "grade", None) is None:
            return (
                "**證據不足，不提供出價上限**：標題抽不到鑑定分數，模型只能"
                "當成基準分數 9 處理。分數溢價從 7 分的 ×0.35 到 10 分的 ×3.95"
                "橫跨 11 倍，猜錯的方向正好是「公允價被高估、上限開太高」。"
                "實測分數未知的估計中位誤差 ×7.50、區間覆蓋率只有 25%"
                "（分數已知是 ×1.96、85%）。"
                "缺的是：確認這張卡的鑑定分數（看商品照片上的殼），"
                "或等一個標題有寫分數的同款標的"
            )
        level = getattr(estimate, "level", None)
        n_eff = int(getattr(estimate, "n_effective", 0) or 0)
        if self.require_card_specific_level and level not in CARD_SPECIFIC_LEVELS:
            return (
                f"**證據不足，不提供出價上限**：估價層級是 {level or 'L0'}"
                f"（{getattr(estimate, 'level_label', '') or '無'}），"
                "庫裡沒有任何一筆「這張卡」自己的成交，這個公允價其實是"
                "「這種稀有度＋分數的典型價」。實測這一層的點估計中位誤差是 ×2.25、"
                "上限開太高的比率 13%（有同卡成交時是 ×1.77、5%）。"
                "缺的是：至少 1 筆同卡同稀有度的成交價"
            )
        broken = self.broken_bucket(estimate)
        if broken:
            return (
                f"**校準已知壞掉，不提供出價上限**：這一筆的有效樣本落在 "
                f"`{broken}` 分桶，而這一桶的區間下緣**實測是錯的**——2026-08-02 "
                "時間切分（1104 筆測試樣本、這一桶 69 筆）量到它的下尾違反率 29%，"
                "名目只有 10%，也就是每三筆就有一筆的真實成交價比區間下緣還低"
                "（＝上限開得比市場還高）。壞的是特定一角：`L3 × Mercari` 那 19 筆"
                "**違反率 100%、覆蓋率 0%、點估計高估 ×5.9**——平台係數"
                "（該切分 Mercari ×3.45）是在高價卡上估出來的，套到這一桶主要的"
                "normal／rare 便宜卡完全不成立。同一次實測試過六種分群鍵"
                "（層級×平台、層級×稀有度層、層級×n桶×稀有度層、層級×n桶×平台、"
                "併入相鄰桶、只用層級）全部修不好，因為校準集裡根本沒有這個失敗"
                "切片——那是點估計的段狀偏誤，不是分位數選錯群。"
                "缺的是：這張卡自己的成交價（能升到 L1／L2 就不會落在這一桶），"
                "或等模型改成分價位帶的平台係數"
            )
        if n_eff < self.min_effective_samples:
            return (
                f"**證據不足，不提供出價上限**：所用層級只有 {n_eff} 筆成交"
                f"（門檻 {self.min_effective_samples} 筆）。"
                "缺的是：更多同卡同稀有度的成交價"
            )
        backing = _calibration_backing(estimate)
        if backing < self.min_calibration_samples:
            return (
                f"**證據不足，不提供出價上限**：撐著這個區間的校準殘差只有 "
                f"{backing} 筆（門檻 {self.min_calibration_samples} 筆）"
                f"，群組「{getattr(estimate, 'calibration_group', None) or '全域'}」。"
                "區間可以當量級參考，但不足以拿來下單。缺的是：更多成交樣本"
            )
        return None

    # ------------------------------------------------------------------
    def broken_bucket(self, estimate: Any) -> str | None:
        """這一筆是不是落在「校準已知壞掉」的分桶？回傳桶名或 None。

        兩個地方都要看，命中任一個就算：

          - `n_effective` 的分桶（**使用者看到的那個 n**，也是覆蓋率實測分桶的依據）
          - `calibration_group_requested` 的桶名（校準模型判定的那個）

        兩者可能不同（校準模型少看四成樣本，見 `Estimate.calibration_group` 的註）。
        取聯集是刻意往保守的方向：這道閘門擋掉的是「上限開太高」的風險，
        少擋一筆的代價是真的付錢，多擋一筆的代價只是少一次出價機會。
        """
        from .valuation import n_bucket

        if not self.reject_n_buckets:
            return None
        n_eff = int(getattr(estimate, "n_effective", 0) or 0)
        bucket = n_bucket(n_eff)
        if bucket in self.reject_n_buckets:
            return bucket
        requested = getattr(estimate, "calibration_group_requested", None) or ""
        for name in self.reject_n_buckets:
            if requested.endswith("/" + name):
                return name
        return None


def _int_setting(cfg_block: dict[str, Any], key: str, default: int) -> int:
    raw = cfg_block.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"[warn] bidding.{key}={raw!r} 不是整數，改用 {default}")
        return default
    if value < 0:
        print(f"[warn] bidding.{key}={value} 是負數，改用 {default}")
        return default
    return value


def _bucket_setting(cfg_block: dict[str, Any], key: str) -> tuple[str, ...]:
    """讀「要拒絕哪些 n 分桶」。**打錯的桶名一律大聲警告**（不靜默）。

    寫錯一個桶名的後果是那道閘門安靜地變成空門——使用者以為破口被擋著，
    其實沒有。所以桶名必須對得上 `valuation.N_BUCKETS` 的標籤，對不上就丟掉
    並印警告；整個鍵沒設時才用預設值（`DEFAULT_REJECT_N_BUCKETS`）。
    設成空 list 是合法的「我知道風險、我要關掉這道閘門」。
    """
    from .valuation import N_BUCKETS

    if key not in cfg_block:
        return DEFAULT_REJECT_N_BUCKETS
    raw = cfg_block.get(key)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        print(f"[warn] bidding.{key}={raw!r} 不是清單，改用 {list(DEFAULT_REJECT_N_BUCKETS)}")
        return DEFAULT_REJECT_N_BUCKETS
    valid = {label for _upper, label in N_BUCKETS}
    out: list[str] = []
    for item in raw:
        name = str(item)
        if name in valid:
            out.append(name)
        else:
            print(
                f"[warn] bidding.{key} 裡的 {name!r} 不是合法的 n 分桶名"
                f"（合法值：{sorted(valid)}），已忽略"
            )
    return tuple(out)


def _calibration_backing(estimate: Any) -> int:
    """撐著這個區間的校準殘差筆數。

    有群組校準就是**那一群**的筆數（不是全庫的），沒有就退回全域校準集大小。
    這裡拿群組的數字是重點：一個 30 筆的群給出來的區間，不能用「全庫 532 筆」
    去背書——那是拿 A 的樣本數替 B 的宣稱撐腰（工程原則 1）。
    """
    group_n = int(getattr(estimate, "calibration_group_n", 0) or 0)
    if group_n:
        return group_n
    return int(getattr(estimate, "calibration_n", 0) or 0)


#: 證據強度的三級與人話標籤。**分級規則放後端**：前端自己判一份，
#: 遲早會跟模型的實測依據漂掉（工程原則 1）。
EVIDENCE_TIERS: dict[str, str] = {
    "strong": "證據強",
    "moderate": "證據中等",
    "weak": "證據弱",
}


def evidence_tier(estimate: Any) -> str:
    """證據強度三級。每一級的界線都對得上一個實測數字，不是感覺：

    - `weak`：沒有同卡成交（L3／L0）。點估計中位誤差 ×2.25、下尾違反 13%。
      預設閘門下這一級根本拿不到上限，留著這一級是為了讓「為什麼沒有上限」
      在 UI 上有顏色可以講。
    - `moderate`：有同卡成交，但**區間的校準或分數的來源有已知瑕疵**：
      (a) 退化到合併／全域分位數（該群樣本不足 `min_group_calibration`）；
      (b) 分數是從**商品描述**撈出來的而不是標題寫的——描述是賣家自由文字、
          尾巴常有 SEO 關鍵字堆（見 `parsers/grade.resolve_grade`），
          同一個數字的可信度就是比標題低，那必須在標籤上看得出來；
      (c) 校準群的層級跟點估計的層級對不上——校準模型只看 fit 半邊，
          少了四成樣本就可能找不到這張卡而退一層。那個區間是**偏寬**的
          （安全方向），但「區間由自己那一群校準出來」這句話不成立，
          所以不能掛 strong。這一級的標籤必須說得出口才算數。
    - `strong`：有同卡成交、分數寫在標題上、區間確實由自己那一層的群校準出來、
      且不在破口桶裡。這一級實測下尾違反 2-3%，比名目 10% 保守。

    ⚠️ 破口桶（`10-49`）在預設 config 下已經**拿不到上限**（`reject_n_buckets`），
    但這裡仍然把它判為 moderate 而不是 weak：這個函式也被「為什麼沒有上限」
    那條訊息用，把它當 weak 會讓 UI 說成「沒有同卡成交」——那不是事實。
    """
    level = getattr(estimate, "level", None)
    if level not in CARD_SPECIFIC_LEVELS:
        return "weak"
    if getattr(estimate, "calibration_degraded", False):
        return "moderate"
    if getattr(estimate, "grade_source", None) == "description":
        return "moderate"
    requested = getattr(estimate, "calibration_group_requested", None) or ""
    if requested.endswith("10-49"):
        return "moderate"
    group = getattr(estimate, "calibration_group", None) or ""
    if group.split("/")[0] != level:
        return "moderate"
    return "strong"


def evidence_label(estimate: Any) -> str:
    """證據強度的一行人話。UI 必須把它貼在數字旁邊。"""
    level = getattr(estimate, "level", None) or "L0"
    label = getattr(estimate, "level_label", "") or ""
    n_eff = int(getattr(estimate, "n_effective", 0) or 0)
    group = getattr(estimate, "calibration_group", None)
    parts = [f"{level} {label}".strip(), f"同層成交 {n_eff} 筆"]
    if getattr(estimate, "grade_source", None) == "description":
        parts.append("分數來自商品描述（非標題）")
    if group:
        tail = f"校準群 {group}／{int(getattr(estimate, 'calibration_group_n', 0) or 0)} 筆"
        if getattr(estimate, "calibration_degraded", False):
            tail += "（退化：未經該群校準）"
        parts.append(tail)
    else:
        parts.append("無群組校準")
    return "｜".join(parts)


@dataclass(slots=True)
class BidCeiling:
    """一次出價上限計算的完整答案。**這個結構要能自己解釋自己**——

    使用者會照著 `max_bid_jpy` 下真錢的單，所以「這個數字怎麼來的」必須跟數字
    一起送到他眼前：保守公允價是多少、扣了哪些成本、用哪條 route 算的、
    以及若真的以這個價成交，到手成本會是多少（`landed_at_ceiling_twd`）。

    `ok=False` 時 `max_bid_jpy` **必定是 None**，不是 0——0 是一個可以被誤讀成
    「出價 0 元」的數字，None 才是「不給上限」。
    """

    ok: bool
    reason: str
    site: str
    #: 保守公允價＝估價區間下緣（`Estimate.lo_twd`）。**不是點估計**。
    conservative_fair_twd: float | None = None
    #: 區間上緣。上限完全用不到它——放在這裡純粹是為了讓 UI 能把整條 80% 區間
    #: 秀出來：只給下緣會讓人以為那就是公允價，而它其實是「最保守的那一端」。
    interval_hi_twd: float | None = None
    fair_twd: float | None = None
    confidence: float | None = None
    level_label: str = ""
    n_effective: int = 0
    venue_adjusted: bool | None = None
    #: 估價層級（L1/L2/L3/L0）。ok=False 時也要填——「為什麼不給上限」跟
    #: 「上限是多少」一樣需要證據。
    level: str | None = None
    #: 區間是哪一群校準出來的、那一群幾筆、有沒有退化。這三欄是 UI 上
    #: 「這個 ¥7,383 背後有幾筆成交撐著」的答案。
    calibration_group: str | None = None
    calibration_group_n: int = 0
    calibration_degraded: bool = False
    #: 一行人話版（`evidence_label`），前端直接顯示，不自己再拼一份。
    evidence_label: str = ""
    #: 證據強度三級（`evidence_tier`）：strong / moderate / weak。
    #: 前端拿它決定顏色與排版——低證據的上限**不可以跟高證據的長得一樣**。
    evidence_tier: str = "weak"
    evidence_tier_label: str = ""
    target_margin: float = DEFAULT_TARGET_MARGIN
    #: 保守公允價再乘 (1 - margin)：贏的時候要有空間，不是剛好打平。
    budget_twd: float | None = None
    max_bid_jpy: float | None = None
    max_bid_twd: float | None = None
    #: 若真的以 max_bid_jpy 成交，正算出來的到手成本。必須 ≤ budget_twd。
    landed_at_ceiling_twd: float | None = None
    route: str | None = None
    route_label: str = ""
    bundle_size: int = 1
    fee_twd: float | None = None
    shipping_twd: float | None = None
    overhead_twd: float | None = None
    notes: list[str] = field(default_factory=list)
    # --- eBay 專用的幣別欄位（2026-08-03；Yahoo 標的一律 None）------------
    #: **使用者填進 eBay 出價欄的數字**（原幣：USD／GBP…）。`max_bid_jpy` 是
    #: 日圓專用欄位，絕不拿來裝別的幣別——欄名寫死幣別而值不是那個幣別，
    #: 是本專案最貴的一種 bug。
    max_bid_native: float | None = None
    native_currency: str | None = None
    #: 與 `Listing.price` **同幣別同基準**的上限（eBay 帶 contextualLocation 掃回來
    #: 是台幣）。headroom／梯隊比較用它——現價與上限必須同源同單位（工程原則 1）。
    max_bid_listing: float | None = None
    listing_currency: str | None = None
    #: 這筆 listing 自己的換匯比率（listing 顯示幣別 / 原幣，`value/convertedFromValue`）。
    #: 留在結構裡是為了讓「原幣 ↔ 台幣」的換算可以被驗算，不是一個黑箱數字。
    native_rate: float | None = None

    # ------------------------------------------------------------------
    def comparison_ceiling(self) -> float | None:
        """與 `Listing.price` 同幣別基準的有效上限（Yahoo→日圓、eBay→listing 幣別）。

        headroom 與可行動性判定**只准**經過這裡拿上限：拿 `max_bid_jpy` 去比
        eBay 的台幣現價就是混單位比較（工程原則 1）。
        """
        if not self.ok:
            return None
        return self.max_bid_listing if self.max_bid_listing is not None else self.max_bid_jpy

    def headroom_value(self, current_bid: float | None) -> float | None:
        """還可以再往上出多少（**與 Listing.price 同幣別**）。缺任一邊一律 None，不猜 0。"""
        ceiling = self.comparison_ceiling()
        if ceiling is None or current_bid is None:
            return None
        return round(ceiling - float(current_bid), 2)

    def headroom_jpy(self, current_bid_jpy: float | None) -> float | None:
        """還可以再往上出多少**日圓**（Yahoo 專用）。eBay 標的沒有日圓上限，一律 None。"""
        if not self.ok or self.max_bid_jpy is None or current_bid_jpy is None:
            return None
        return round(self.max_bid_jpy - float(current_bid_jpy), 0)

    def headroom_pct(self, current_bid: float | None) -> float | None:
        """headroom 佔上限的比例。0.4 = 「還有四成空間」。（幣別無關：同幣別相除）"""
        room = self.headroom_value(current_bid)
        ceiling = self.comparison_ceiling()
        if room is None or not ceiling:
            return None
        return room / ceiling

    def is_actionable(self, current_bid: float | None) -> bool:
        """現在這個價位值不值得出手：**目前出價 < 你的上限**（同幣別比較）。

        注意這裡用嚴格小於：等於上限代表已經沒有空間，而競標最小加價幅度
        （Buyee／eBay 都有 bid increment）保證你連加價都加不上去。
        """
        room = self.headroom_value(current_bid)
        return room is not None and room > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fail(site: str, reason: str, **kw: Any) -> BidCeiling:
    return BidCeiling(ok=False, reason=reason, site=site, **kw)


def max_bid_jpy(
    estimate: Any,
    cfg: Config,
    fx: Any,
    *,
    site: Site | str,
    target_margin: float | None = None,
    bundle_size: int | None = None,
    gate: EvidenceGate | None = None,
) -> BidCeiling:
    """從保守公允價反推「這張卡最多能出多少日圓」。

    路徑：`Estimate.lo_twd` →（乘 1-margin）→ `costs.max_item_price_jpy()` 反解
    掉所有雜費 → 向下取整到 1 円 → 用 `costs.quote_route()` 正算回頭驗證。

    `site` 決定可用 route（`cfg.routes_for_site`）；選 route 的規則是
    **overhead 最低的那條**，也就是 `max_item_price_jpy` 最大的那條——同一筆
    商品價下它同時也是 `costs.best_route()` 會挑的那條（雜費與商品價無關，
    兩個排序必然一致），所以不存在「反解用 A 條、實際買走 B 條」的分岔。
    """
    site_value = site.value if isinstance(site, Site) else str(site)
    margin = target_margin if target_margin is not None else target_margin_from(cfg)
    if not 0.0 <= margin < 1.0:
        return _fail(site_value, f"目標利潤率 {margin} 不在 [0,1)，拒絕輸出上限")

    base = dict(
        target_margin=margin,
        conservative_fair_twd=getattr(estimate, "lo_twd", None),
        interval_hi_twd=getattr(estimate, "hi_twd", None),
        fair_twd=getattr(estimate, "fair_twd", None),
        confidence=getattr(estimate, "confidence", None),
        level_label=getattr(estimate, "level_label", "") or "",
        n_effective=int(getattr(estimate, "n_effective", 0) or 0),
        venue_adjusted=getattr(estimate, "venue_adjusted", None),
        level=getattr(estimate, "level", None),
        calibration_group=getattr(estimate, "calibration_group", None),
        calibration_group_n=int(getattr(estimate, "calibration_group_n", 0) or 0),
        calibration_degraded=bool(getattr(estimate, "calibration_degraded", False)),
        evidence_label=evidence_label(estimate) if estimate is not None else "",
        evidence_tier=evidence_tier(estimate) if estimate is not None else "weak",
        evidence_tier_label=(
            EVIDENCE_TIERS[evidence_tier(estimate)] if estimate is not None
            else EVIDENCE_TIERS["weak"]
        ),
    )

    # --- 紅線：沒有可信區間就不給上限 ---------------------------------
    lo = base["conservative_fair_twd"]
    if estimate is None or lo is None or lo <= 0:
        return _fail(
            site_value,
            "樣本不足以給出 80% 區間下緣，**不提供出價上限**"
            "（點估計的中位誤差是 ×1.9，拿它當上限等於一半機率出價過高）",
            **base,
        )

    # --- 同一條紅線的延伸：證據不足就不給上限 ---------------------------
    # 順序是刻意的（先區間、後證據）：沒有區間時連「證據多強」都談不上。
    missing = (gate if gate is not None else EvidenceGate.from_config(cfg)).check(estimate)
    if missing:
        return _fail(site_value, missing, **base)

    if site_value == Site.EBAY.value:
        return _fail(
            site_value,
            "eBay 的運費出自 listing 本身而不是 route 費率表，這條日圓反解不適用"
            "——eBay 競標請走 `max_bid_ebay(estimate, cfg, fx, listing=…)`"
            "（需要整筆 listing 才有運費與換匯比率）",
            **base,
        )

    routes = cfg.routes_for_site(site_value)
    if not routes:
        return _fail(site_value, f"{site_value} 沒有設定任何可用 route", **base)

    budget = lo * (1.0 - margin)
    base["budget_twd"] = round(budget, 2)

    # --- 反解：挑 overhead 最低（＝上限最高）的那條 route ----------------
    best_route_cfg = None
    best_jpy = 0.0
    for route in routes:
        cand = max_item_price_jpy(route, budget, fx, bundle_size=bundle_size)
        if best_route_cfg is None or cand > best_jpy:
            best_route_cfg, best_jpy = route, cand

    n = max(1, bundle_size if bundle_size is not None else best_route_cfg.bundle_size)
    overhead_jpy = best_route_cfg.per_order_fee_jpy + best_route_cfg.amortizable_jpy / n
    base.update(
        route=best_route_cfg.name,
        route_label=best_route_cfg.label,
        bundle_size=n,
        overhead_twd=round(fx.to_twd(overhead_jpy, Currency.JPY), 2),
    )

    # 出價是整數円，一律**向下**取整：往上取會讓正算的到手成本越過預算。
    ceiling = math.floor(best_jpy)
    if ceiling <= 0:
        return _fail(
            site_value,
            f"扣掉固定成本 NT${base['overhead_twd']:,.0f}（{best_route_cfg.label}）之後"
            f"預算 NT${budget:,.0f} 已經沒有剩餘，這張卡在這條路徑上不值得出價",
            **base,
        )

    # --- 正算回頭驗證：同一個 fx、同一條 route、同一個 bundle_size -------
    probe = Listing(
        site=Site(site_value),
        external_id="__bid_ceiling_probe__",
        title="",
        url="",
        price=float(ceiling),
        currency=Currency.JPY,
    )
    q = quote_route(probe, best_route_cfg, fx, bundle_size=n)
    if q.landed_twd > budget + FORWARD_CHECK_TOLERANCE_TWD:
        # 大聲失敗，不吞（工程原則 3）：這代表反解與正算不同源，是程式 bug，
        # 但 scan 不該因此整輪炸掉——拒絕輸出上限就已經是安全的一邊。
        print(
            f"[error] 出價上限反解與正算不一致：ceiling=¥{ceiling} → 到手 "
            f"NT${q.landed_twd:.2f} > 預算 NT${budget:.2f}（route={best_route_cfg.name}）"
        )
        return _fail(
            site_value,
            f"反解與正算對不上（到手 NT${q.landed_twd:,.2f} > 預算 NT${budget:,.2f}），"
            "拒絕輸出上限",
            **base,
        )

    notes = [
        f"上限 = 保守公允價 NT${lo:,.0f}（80% 區間下緣）× (1 − {margin:.0%} 目標利潤) "
        f"− 雜費 NT${base['overhead_twd']:,.0f}（{best_route_cfg.label}，湊 {n} 件攤提）",
        f"若真的以 ¥{ceiling:,} 成交，到手成本 NT${q.landed_twd:,.0f}"
        f"（≤ 預算 NT${budget:,.0f}，已含刷卡海外手續費與匯率緩衝）",
    ]
    return BidCeiling(
        ok=True,
        reason="可出價",
        site=site_value,
        max_bid_jpy=float(ceiling),
        max_bid_twd=q.item_twd,
        landed_at_ceiling_twd=q.landed_twd,
        fee_twd=q.fee_twd,
        shipping_twd=q.shipping_twd,
        notes=notes,
        **base,
    )


# ---------------------------------------------------------------------------
# eBay 出價上限：幣別跟著 listing 走的反解（2026-08-03）
# ---------------------------------------------------------------------------
#: 原幣上限的向下取整粒度（美分級）。eBay 出價欄吃到小數兩位；一律**向下**：
#: 往上取會讓正算的到手成本越過預算——與日圓側「floor 到 1 円」同一個立場。
_NATIVE_STEP = 0.01


def max_bid_ebay(
    estimate: Any,
    cfg: Config,
    fx: Any,
    *,
    listing: Any,
    target_margin: float | None = None,
    gate: EvidenceGate | None = None,
) -> BidCeiling:
    """eBay 競標的出價上限。**與 `max_bid_jpy` 同一個哲學、完全不同的幣別鏈。**

    哲學相同的部分（一條都不能少）：上限從 80% 區間下緣（`lo_twd`）反推、
    乘 (1 − target_margin)、五道證據閘門原樣適用、算完必以正算回頭驗證，
    對不上就拒絕輸出。**不為 eBay 另開一套標準。**

    幣別鏈（每一步的「同源」都有名字）：

      lo_twd ×(1−margin) ＝ 預算（台幣）
        → 扣掉**這筆 listing 的實際運費**（台幣、含刷卡加成——與
          `costs._quote_ebay` 的正算完全同構；eBay 沒有 route 固定費，
          `ebay_direct` 雜費 0 是對的，運費就是全部的 overhead）
        → 除以每 1 單位 listing 幣別的台幣成本（`fx.to_twd(1, ccy)`，
          含 (1+card_markup)(1+safety_buffer)——同一顆 fx、同一個 markup 開關）
        → 得到 listing 幣別的商品上限（eBay 顯示幣別，通常是台幣）
        → 用**這筆 listing 自己的換匯比率**（`value/convertedFromValue`，
          `sources.ebay.native_price_info`）換回**原幣**，向下取整到 0.01
        →（正算驗證）原幣 × 同一比率 → listing 幣別 → `costs.quote_route`
          （走 `ebay_direct`，即 `_quote_ebay`）→ 到手台幣 ≤ 預算 ＋ 容差。

    為什麼不用我們的 fx 表換回原幣：eBay 顯示的台幣是**它自己的匯率**換出來的，
    上限的台幣↔原幣若走另一張表，同一個數字就有兩個匯率（工程原則 1）——
    使用者填進出價欄的原幣，經 eBay 自己的匯率換回來必須正好是我們反解的台幣。

    三條 eBay 專屬的拒絕（都是「一個沒依據的數字比沒有數字危險」）：
      - **運費未知**（`shipping_cost is None`）：到手成本會被低估，掃描端的
        US$25 佔位值是給「參考成本」用的，不夠格進出價上限。
      - **賣家不寄台灣**（`ships_to_tw is False`）：到手台灣的成本描述的是
        一個不存在的交易（美國地址的後續轉運成本未建模）。
      - **換匯資訊缺失**且顯示幣別是台幣：台幣不是 eBay 的掛牌幣別，
        給不出使用者能填進出價欄的原幣數字。
    """
    site_value = Site.EBAY.value
    margin = target_margin if target_margin is not None else target_margin_from(cfg)
    if not 0.0 <= margin < 1.0:
        return _fail(site_value, f"目標利潤率 {margin} 不在 [0,1)，拒絕輸出上限")

    base = dict(
        target_margin=margin,
        conservative_fair_twd=getattr(estimate, "lo_twd", None),
        interval_hi_twd=getattr(estimate, "hi_twd", None),
        fair_twd=getattr(estimate, "fair_twd", None),
        confidence=getattr(estimate, "confidence", None),
        level_label=getattr(estimate, "level_label", "") or "",
        n_effective=int(getattr(estimate, "n_effective", 0) or 0),
        venue_adjusted=getattr(estimate, "venue_adjusted", None),
        level=getattr(estimate, "level", None),
        calibration_group=getattr(estimate, "calibration_group", None),
        calibration_group_n=int(getattr(estimate, "calibration_group_n", 0) or 0),
        calibration_degraded=bool(getattr(estimate, "calibration_degraded", False)),
        evidence_label=evidence_label(estimate) if estimate is not None else "",
        evidence_tier=evidence_tier(estimate) if estimate is not None else "weak",
        evidence_tier_label=(
            EVIDENCE_TIERS[evidence_tier(estimate)] if estimate is not None
            else EVIDENCE_TIERS["weak"]
        ),
        listing_currency=getattr(getattr(listing, "currency", None), "value", None),
    )

    # --- 紅線與證據閘門：與日圓側逐字同一套，順序也相同 -----------------
    lo = base["conservative_fair_twd"]
    if estimate is None or lo is None or lo <= 0:
        return _fail(
            site_value,
            "樣本不足以給出 80% 區間下緣，**不提供出價上限**"
            "（點估計的中位誤差是 ×1.9，拿它當上限等於一半機率出價過高）",
            **base,
        )
    missing = (gate if gate is not None else EvidenceGate.from_config(cfg)).check(estimate)
    if missing:
        return _fail(site_value, missing, **base)

    # --- eBay 專屬的三條拒絕 -------------------------------------------
    if getattr(listing, "ships_to_tw", None) is False:
        return _fail(
            site_value,
            "賣家不寄台灣（shipToLocations 明確排除）：寄台灣的到手成本是一個"
            "不存在的交易，不給上限。有美國地址可收（dashboard 的"
            "「可寄美國地址」旗標），但美國→台灣的轉運成本未建模，"
            "**不能**拿這裡的任何數字去出價",
            **base,
        )
    shipping = getattr(listing, "shipping_cost", None)
    if shipping is None:
        return _fail(
            site_value,
            "eBay 這筆的運費是**未知**的（賣家沒有列出寄台灣的金額）："
            "運費常佔 eBay 到手成本三到五成，缺了它反解出的上限必然偏高"
            "——一個沒依據的數字比沒有數字危險，所以不給。"
            "缺的是：到商品頁確認運費，或等賣家補上運送選項",
            **base,
        )

    from .sources.ebay import native_price_info  # 延遲 import：避免循環依賴

    native = native_price_info(getattr(listing, "raw", None) or {})
    listing_ccy = base["listing_currency"]
    if native is None and listing_ccy and listing_ccy != Currency.TWD.value:
        # listing 本來就以原幣顯示（無換算節點可讀）：原幣＝listing 幣別、比率 1。
        from .sources.ebay import NativePrice

        native = NativePrice(value=float(listing.price), currency=listing_ccy, rate=1.0)
    if native is None or native.currency == Currency.TWD.value:
        # 台幣不是 eBay 的掛牌幣別：走到這裡代表換匯資訊缺失（convertedFrom* 沒有值）
        # 而顯示幣別是台幣——給不出使用者能填進出價欄的原幣數字。
        return _fail(
            site_value,
            "這筆的換匯資訊缺失（沒有 convertedFromValue/Currency），而顯示"
            "幣別是台幣——台幣不是 eBay 的掛牌幣別，給不出你能填進出價欄的"
            "原幣數字，拒絕輸出上限",
            **base,
        )

    routes = cfg.routes_for_site(site_value)
    if not routes:
        return _fail(site_value, f"{site_value} 沒有設定任何可用 route", **base)
    route = routes[0]

    budget = lo * (1.0 - margin)
    base.update(
        budget_twd=round(budget, 2),
        native_currency=native.currency,
        native_rate=native.rate,
    )

    # --- 反解（`costs._quote_ebay` 的嚴格逆函式）------------------------
    # 正算：landed = to_twd(price, ccy) + to_twd(ship, ccy)（兩段都含刷卡加成）
    # 反解：price_max = (budget − to_twd(ship, ccy)) / to_twd(1, ccy)
    ship_twd = fx.to_twd(float(shipping), listing.currency)
    per_unit_twd = fx.to_twd(1.0, listing.currency)
    remaining = budget - ship_twd
    if remaining <= 0 or per_unit_twd <= 0:
        return _fail(
            site_value,
            f"這筆的國際運費 NT${ship_twd:,.0f}（含刷卡加成）已吃掉整個預算 "
            f"NT${budget:,.0f}，這張卡走 eBay 直寄不值得出價",
            **base,
        )
    max_item_listing = remaining / per_unit_twd

    # listing 幣別 → 原幣：用這筆 listing 自己的比率，向下取整到 0.01。
    ceiling_native = math.floor((max_item_listing / native.rate) / _NATIVE_STEP) * _NATIVE_STEP
    ceiling_native = round(ceiling_native, 2)
    if ceiling_native <= 0:
        return _fail(
            site_value,
            f"扣掉運費 NT${ship_twd:,.0f} 之後預算 NT${budget:,.0f} 已無剩餘，"
            "不值得出價",
            **base,
        )
    # 原幣 → listing 幣別：同一個比率換回去，這就是拿去跟現價比的那個數字。
    ceiling_listing = round(ceiling_native * native.rate, 2)

    # --- 正算回頭驗證：同一顆 fx、同一條 route、同一筆運費 ---------------
    probe = Listing(
        site=Site.EBAY,
        external_id="__ebay_bid_ceiling_probe__",
        title="",
        url="",
        price=ceiling_listing,
        currency=listing.currency,
        shipping_cost=float(shipping),
    )
    q = quote_route(probe, route, fx)
    if q.landed_twd > budget + FORWARD_CHECK_TOLERANCE_TWD:
        print(
            f"[error] eBay 出價上限反解與正算不一致：{native.currency} {ceiling_native} "
            f"→ 到手 NT${q.landed_twd:.2f} > 預算 NT${budget:.2f}"
        )
        return _fail(
            site_value,
            f"反解與正算對不上（到手 NT${q.landed_twd:,.2f} > 預算 NT${budget:,.2f}），"
            "拒絕輸出上限",
            **base,
        )

    notes = [
        f"上限 = 保守公允價 NT${lo:,.0f}（80% 區間下緣）× (1 − {margin:.0%} 目標利潤) "
        f"− 這筆 listing 的國際運費 NT${ship_twd:,.0f}（含刷卡加成；eBay 直寄無代購費）",
        f"原幣換算用**這筆 listing 自己的 eBay 匯率**"
        f"（{native.currency} 1 ≈ {base['listing_currency']} {native.rate:,.4f}，"
        "與現價同一個比率，不是我們的匯率表）",
        f"若真的以 {native.currency} {ceiling_native:,.2f} 成交，"
        f"到手成本 NT${q.landed_twd:,.0f}（≤ 預算 NT${budget:,.0f}，"
        "已含刷卡海外手續費與匯率緩衝）",
        "🔁 eBay 原生自動出價：出價欄填上限後可以離開，eBay 會自動替你跟價"
        "（見 EBAY_PROXY_BID_FINDING，2026-08-03 官方頁查證）",
    ]
    return BidCeiling(
        ok=True,
        reason="可出價",
        site=site_value,
        max_bid_jpy=None,               # 這不是日圓，永遠不准塞進日圓欄位
        max_bid_native=ceiling_native,
        max_bid_listing=ceiling_listing,
        max_bid_twd=q.item_twd,
        landed_at_ceiling_twd=q.landed_twd,
        fee_twd=q.fee_twd,
        shipping_twd=q.shipping_twd,
        overhead_twd=round(q.fee_twd + q.shipping_twd, 2),
        route=route.name,
        route_label=route.label,
        bundle_size=1,
        notes=notes,
        **base,
    )


# ---------------------------------------------------------------------------
# 既有資料的重算
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class CeilingChange:
    """一筆既有競標標的重算前後的對照。"""

    key: str
    title: str
    site: str
    current_bid_jpy: float | None
    before_ok: bool
    after_ok: bool
    before_jpy: float | None
    after_jpy: float | None
    before_lo_twd: float | None
    after_lo_twd: float | None
    level: str | None
    n_effective: int
    calibration_group: str | None
    reason: str

    @property
    def delta_jpy(self) -> float | None:
        if self.before_jpy is None or self.after_jpy is None:
            return None
        return self.after_jpy - self.before_jpy

    @property
    def sort_weight(self) -> float:
        """「變動最大」的排序權重：撤掉上限一律排最前（那是最重要的改變）。"""
        if self.before_ok and not self.after_ok:
            return float("inf")
        d = self.delta_jpy
        return abs(d) if d is not None else -1.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["delta_jpy"] = self.delta_jpy
        return d


def listing_from_payload(d: dict[str, Any]):
    """把 payload 裡的 listing dict 還原成 Listing。**不要手工列舉欄位。**

    事故（2026-08-02）：這裡原本是一長串 `field=d.get("field")`，列了 `bids`
    卻漏了 `end_time`。`recalc-bids --apply` 於是把每一筆競標的結標時間洗成 None
    ——倒數、排序、「還剩幾小時」全部失效，而且**沒有任何錯誤訊息**，
    因為漏掉的欄位有預設值。競標少了結標時間等於廢了。

    手工清單一定會跟 dataclass 定義漂移：新增一個欄位，就得記得同步每一個
    重建點。改成從 dataclass 的 fields 反射，新欄位自動被帶上——
    這是結構性修法，不是「下次記得改」（工程原則的 meta-rule）。
    """
    import dataclasses
    from datetime import datetime

    from .domain import Currency, Listing, Site

    enums = {"site": Site, "currency": Currency}
    datetimes = {"listed_at", "end_time"}
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(Listing):
        if f.name not in d:
            continue
        v = d[f.name]
        if v is not None and f.name in enums:
            v = enums[f.name](v)
        elif v is not None and f.name in datetimes and isinstance(v, str):
            v = datetime.fromisoformat(v)
        kwargs[f.name] = v
    return Listing(**kwargs)


def recompute_ceilings(
    rows: list[dict[str, Any]],
    cfg: Config,
    fx: Any,
    *,
    comps_engine: Any,
    valuator: Any,
    apply_to: Any = None,
) -> list[CeilingChange]:
    """把既有 signals 的出價上限用**現在這一版**的模型重算一次。

    重算走的是**完整的出貨路徑**（`scoring.evaluate`），不是只把新的
    `max_bid_jpy` 塞回 payload：上限一變，`BID_WORTH` 旗標、競標分數與
    reason 那句話全部跟著變，只改其中一個就會出現「旗標說值得出價、
    但上限是 None」這種自相矛盾的列（工程原則 1）。

    `apply_to` 給了 Store 才寫回；沒給就是純 dry-run。寫回走
    `Store.upsert_signal`，所以**人工狀態與筆記不會被洗掉**。
    一律用 `keep_all=True` 重算：重算是修正數字，不該順手把使用者看得到的
    列刪掉——那是另一個決定，不該藏在這個指令裡。

    冪等：同一份 comps 與同一份 config 下重跑，第二次的 before 會等於第一次的
    after，不會再有變動（`tests/test_bidding.py` 有釘）。
    """
    from .domain import CardInfo, Grader
    from .scoring import evaluate
    from .valuation import estimate_listing

    out: list[CeilingChange] = []
    for row in rows:
        try:
            payload = json.loads(row.get("payload") or "{}") or {}
        except (TypeError, ValueError):
            continue
        listing_d = payload.get("listing") or {}
        if not listing_d:
            continue
        try:
            listing = listing_from_payload(listing_d)
        except (KeyError, ValueError, TypeError):
            continue
        if not is_live_auction(listing):
            continue

        card_d = payload.get("card") or {}
        info = CardInfo(
            grader=Grader(card_d.get("grader") or Grader.UNKNOWN.value),
            grade=card_d.get("grade"),
            in_era=bool(card_d.get("in_era")),
            era_evidence=list(card_d.get("era_evidence") or []),
            set_code=card_d.get("set_code"),
            language=card_d.get("language"),
            excluded_by=card_d.get("excluded_by"),
            rarity=card_d.get("rarity"),
            # 舊 payload 沒有這一欄 → None ＝「來源不明」，不假裝是標題來的
            grade_source=card_d.get("grade_source"),
        )
        before = payload.get("bid") or {}

        estimate = estimate_listing(valuator, listing, info)
        sig = evaluate(
            listing, info, comps_engine.stats_for(listing, info), cfg, fx,
            keep_all=True, estimate=estimate,
        )
        if sig is None:
            continue
        after = sig.bid
        out.append(CeilingChange(
            key=row.get("key") or listing.key,
            title=listing.title,
            site=listing.site.value,
            current_bid_jpy=listing.price,
            before_ok=bool(before.get("ok")),
            after_ok=bool(after and after.ok),
            before_jpy=before.get("max_bid_jpy"),
            after_jpy=after.max_bid_jpy if after else None,
            before_lo_twd=before.get("conservative_fair_twd"),
            after_lo_twd=after.conservative_fair_twd if after else None,
            level=estimate.level,
            n_effective=estimate.n_effective,
            calibration_group=estimate.calibration_group,
            reason=after.reason if after else "這筆已不是競標標的",
        ))
        if apply_to is not None:
            apply_to.upsert_signal(sig)
    return out


__all__ = [
    "ACTIONABLE_WINDOW_HOURS",
    "AUCTION_TIERS",
    "CARD_SPECIFIC_LEVELS",
    "PRICE_DYNAMICS_HOURS",
    "PRICE_DYNAMICS_NOTE",
    "actionable_window_hours",
    "auction_end_time",
    "auction_room_jpy",
    "auction_room_value",
    "auction_tier",
    "auction_view_config",
    "ceiling_jpy_of",
    "ceiling_value_of",
    "hours_until_end",
    "DEFAULT_MIN_CALIBRATION_SAMPLES",
    "DEFAULT_MIN_EFFECTIVE_SAMPLES",
    "DEFAULT_REQUIRE_CARD_SPECIFIC_LEVEL",
    "DEFAULT_REJECT_N_BUCKETS",
    "DEFAULT_REQUIRE_KNOWN_GRADE",
    "DEFAULT_TARGET_MARGIN",
    "EBAY_PROXY_BID_FINDING",
    "HONESTY_NOTES",
    "LIVE_AUCTION_KIND",
    "PROXY_BID_FINDING",
    "BidCeiling",
    "CeilingChange",
    "EvidenceGate",
    "EVIDENCE_TIERS",
    "evidence_label",
    "evidence_tier",
    "recompute_ceilings",
    "is_live_auction",
    "max_bid_ebay",
    "max_bid_jpy",
    "target_margin_from",
]
