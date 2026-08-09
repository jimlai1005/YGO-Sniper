"""Yahoo 拍賣商品頁（含已結束）快照：資料在 `<script id="__NEXT_DATA__">` JSON。

已結束頁約 120 天後刪除（慣例值；實證下界 74 天，2026-08-09 驗）——所以
使用者提供的歷史 URL 必須**入庫當下就抓快照**，不能只存連結。
JSON 路徑（2026-08-09 實測）：props.pageProps.initialState.item.detail.item。

抓不到 `__NEXT_DATA__`（被擋／已刪除／版型改了）一律大聲拋 AuctionPageError，
絕不回一個空 snapshot 假裝成功——讀不到 ≠ 不存在。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


class AuctionPageError(RuntimeError):
    """頁面抓到了但不是預期形狀（被擋、已刪除、或版型改了）。"""


@dataclass(slots=True)
class AuctionSnapshot:
    url: str
    title: str
    price: float | None
    currency: str        # Yahoo 拍賣一律 JPY
    end_time: str        # ISO8601 頁面原樣（含 +09:00）——不轉時區，存原文
    bids: int | None
    status: str          # 'open' / 'closed' 頁面原樣
    seller_id: str       # seller.aucUserId（= /seller/ URL 的 token，同一命名空間）
    seller_name: str


def parse_auction_page(html: str, *, url: str = "") -> AuctionSnapshot:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise AuctionPageError("頁面沒有 __NEXT_DATA__——可能被擋、已刪除或版型改了")
    try:
        item = json.loads(m.group(1))["props"]["pageProps"]["initialState"][
            "item"]["detail"]["item"]
    except (ValueError, KeyError, TypeError) as exc:
        raise AuctionPageError(f"__NEXT_DATA__ JSON 路徑不符：{exc}") from exc
    seller = item.get("seller") or {}
    price = item.get("price")
    return AuctionSnapshot(
        url=url,
        title=str(item.get("title") or ""),
        price=float(price) if price is not None else None,
        currency="JPY",
        end_time=str(item.get("endTime") or ""),
        bids=item.get("bids"),
        status=str(item.get("status") or ""),
        seller_id=str(seller.get("aucUserId") or ""),
        seller_name=str(seller.get("displayName") or ""),
    )


def fetch_auction_snapshot(url: str, *, fetcher) -> AuctionSnapshot:
    """fetcher 只需有 CachedFetcher.get 同形的 get(url)。FetchError 由呼叫端分類。"""
    return parse_auction_page(fetcher.get(url), url=url)
