"""到手成本模型 —— 整個專案的心臟。

核心觀念：**掛牌價毫無意義**。同一張 ¥1,500 的卡走不同路徑，到手成本可以差兩倍。
所以我們對每張卡把所有可行路徑都算一次，取最便宜的那條當作判斷基準。

三條日本路徑的關鍵差異（2026-08 實測費率）：

  Buyee 集運    每筆訂單 ¥500 代購費 + ¥300 國內運費（不可攤提）
                + (集中包裝 ¥700 + 國際運費 ¥2,200) / N（可攤提）
  Buyee 單張    同上但國際運費不攤提
  Mercari TW    服務費 ¥500 + Global Shipping ¥900，
                但 **一件一件直送，無法合併**，所以 N 永遠是 1

推論：買很多張便宜卡 → Buyee 集運勝；買單張 → Mercari TW 勝。
所以不該二選一，兩條都算，讓數字決定。
"""

from __future__ import annotations

from .config import Config, RouteConfig
from .domain import Currency, Listing, RouteQuote, Site


def quote_route(
    listing: Listing,
    route: RouteConfig,
    fx,
    *,
    bundle_size: int | None = None,
) -> RouteQuote:
    """算出單一路徑的到手成本（台幣）。

    bundle_size 可覆寫 config 的預設值，讓 dashboard 的「湊單籃」
    可以即時重算：籃子裡放 3 張 vs 8 張，攤提後的每張成本不一樣。
    """
    n = bundle_size if bundle_size is not None else route.bundle_size
    n = max(1, n)

    if listing.site is Site.EBAY:
        return _quote_ebay(listing, route, fx)

    # 商品價一律用**它自己標的幣別**換算，不假設是日圓。
    # 為什麼這一行是紅線：Mercari 台灣（tw.mercari.com）標的是新台幣，
    # 硬當成日圓會把 NT$5,751 算成 ¥5,751 ≈ NT$1,229——成本低估 4.7 倍，
    # 而且方向是「看起來很便宜」，正是會讓人按下去買的方向。
    # 費用（代購費、運費）是 settings.yaml 裡以日圓寫死的服務費率，與商品幣別無關。
    fee_jpy = route.per_order_fee_jpy
    ship_jpy = route.amortizable_jpy / n

    # 台幣標價＝在台灣用台幣付款：沒有匯率風險、也沒有海外刷卡手續費，
    # 所以不套 markup（套了就是拿一個不存在的成本嚇自己）。
    item_twd = fx.to_twd(
        listing.price, listing.currency, apply_markup=listing.currency is not Currency.TWD
    )
    fee_twd = fx.to_twd(fee_jpy, Currency.JPY)
    ship_twd = fx.to_twd(ship_jpy, Currency.JPY)

    return RouteQuote(
        route=route.name,
        label=route.label,
        landed_twd=round(item_twd + fee_twd + ship_twd, 2),
        item_twd=round(item_twd, 2),
        fee_twd=round(fee_twd, 2),
        shipping_twd=round(ship_twd, 2),
        bundle_size=n,
    )


def _quote_ebay(listing: Listing, route: RouteConfig, fx) -> RouteQuote:
    """eBay 用 listing 上的實際運費，不用估的。

    運費為 None 代表賣家沒有列出寄台灣的選項 —— 這不是 0，是「未知」，
    先用一個保守估計佔位，並讓 scoring 標上 NEEDS_SHIPPING_ASK。

    **為什麼這裡對台幣標價套 markup，日本路徑卻不套（看起來不一致，但是對的）**

    兩邊的「台幣」是兩件不同的事：
    - Mercari 台灣：對台使用者以新台幣收款，那個數字就是你會被扣的金額，
      而且它**自己已經含了 Mercari 的換匯加成**。再套一次我們的 3.5% 是雙重計算。
    - eBay：賣家標的是 USD/CAD，我們送 `contextualLocation=country=TW` 之後
      eBay 幫忙**顯示**成台幣，但那只是估算——實際請款走的是賣家幣別，
      你的卡片會用它自己的匯率換算並收 1.5% 海外手續費。
      所以 markup 該套，它模擬的是「eBay 的估算 → 帳單上的真實金額」那一段。

    也就是說：判準不是「幣別是不是 TWD」，而是「這筆錢會不會以外幣請款」。
    ⚠️ 未經實證的假設：Mercari 台灣確實以台幣在地請款。第一次真的買之後
    請對一次信用卡帳單——若帳單顯示為海外交易，這裡就要改成同樣套 markup。
    """
    item_twd = fx.to_twd(listing.price, listing.currency)
    ship_src = listing.shipping_cost if listing.shipping_cost is not None else 25.0
    ship_twd = fx.to_twd(ship_src, listing.currency)
    return RouteQuote(
        route=route.name,
        label=route.label,
        landed_twd=round(item_twd + ship_twd, 2),
        item_twd=round(item_twd, 2),
        fee_twd=0.0,
        shipping_twd=round(ship_twd, 2),
        bundle_size=1,
    )


def quote_all_routes(
    listing: Listing, cfg: Config, fx, *, bundle_size: int | None = None
) -> list[RouteQuote]:
    routes = cfg.routes_for_site(listing.site.value)
    quotes = [quote_route(listing, r, fx, bundle_size=bundle_size) for r in routes]
    return sorted(quotes, key=lambda q: q.landed_twd)


def best_route(
    listing: Listing, cfg: Config, fx, *, bundle_size: int | None = None
) -> RouteQuote:
    quotes = quote_all_routes(listing, cfg, fx, bundle_size=bundle_size)
    if not quotes:
        raise ValueError(f"{listing.site.value} 沒有設定任何可用 route")
    return quotes[0]


# ---------------------------------------------------------------------------
# 反推：到手成本要壓在 X 元，卡最多能出多少錢？
# ---------------------------------------------------------------------------

def max_item_price_jpy(
    route: RouteConfig, target_twd: float, fx, *, bundle_size: int | None = None
) -> float:
    """反解 quote_route。

    這個函式的實際用途很爽：把結果直接塞進 Buyee 搜尋網址的 price_max 參數，
    讓搜尋結果一開始就只剩「有可能中」的標的，省掉大量無效抓取與解析。
    """
    n = max(1, bundle_size if bundle_size is not None else route.bundle_size)
    overhead_jpy = route.per_order_fee_jpy + route.amortizable_jpy / n
    overhead_twd = fx.to_twd(overhead_jpy, Currency.JPY)
    remaining_twd = target_twd - overhead_twd
    if remaining_twd <= 0:
        return 0.0
    # 反向換算時要把 markup 除回去
    per_jpy_twd = fx.to_twd(1.0, Currency.JPY)
    return remaining_twd / per_jpy_twd


def breakeven_table(cfg: Config, fx, target_twd: float | None = None) -> list[dict]:
    """給 CLI 用的破口表：每條路徑最多能出多少錢買卡。"""
    target = target_twd if target_twd is not None else cfg.grading_fee_twd
    rows = []
    for route in cfg.routes.values():
        if route.name == "ebay_direct":
            continue
        rows.append(
            {
                "route": route.name,
                "label": route.label,
                "bundle_size": route.bundle_size,
                "overhead_twd": round(
                    fx.to_twd(
                        route.per_order_fee_jpy + route.amortizable_jpy / route.bundle_size,
                        Currency.JPY,
                    ),
                    0,
                ),
                "max_item_jpy": round(max_item_price_jpy(route, target, fx), 0),
            }
        )
    return sorted(rows, key=lambda r: -r["max_item_jpy"])
