"""從「觀察中」出發找賣家（Seller Alpha 的種子策略）。

---------------------------------------------------------------------------
## 為什麼從觀察中的標的出發，而不是全市場亂掃

`state IN ('watching','bought')` 的標的是**使用者人工審核過的正面樣本**：
他看過標題、看過價格、按下了「觀察」。這些商品的賣家因此有很高的先驗——
比「掃描關鍵字剛好撈到誰就評誰」有效率得多。

實測的缺口正好也在這裡：35 筆觀察中的標的有 23 筆是 `buyee_mercari`，
而 Mercari 那條管道**先前完全沒有賣家 ID**（搜尋頁不帶），所以使用者最在意
的那批商品，賣家維度是全盲的。

## 三步，每一步都要能單獨看

1. **補賣家**（`backfill_signal_sellers`）：逐一抓商品頁把 `seller_id` 挖回來。
   這是「少量高價值」的用法，**不是全量掃描**：每筆一個請求，而 Mercari 的
   Buyee 鏡像頁還要一顆 WAF token。所以預設只做觀察中／已買，而且有筆數上限。
2. **挖歷史**（`history.mine_paypay_seller`）：拿到賣家之後，能挖歷史成交的
   站台就挖（目前只有 Yahoo!フリマ 有真正的賣家頁，見 `history` 模組頂註）。
3. **評分**：走既有的 `seller_alpha.analyze`，**不另開一條評估路徑**。
   同儕相對折價的算式只有一份（工程原則 5 的同型）。

## 賣家鍵的一致性（2026-08-04 實測結論）

`buyee_mercari` 與 `mercari_tw` 的 `seller_id` **是同一個 ID 空間**：
同一件商品 `m38347072251`，Buyee 鏡像頁的賣家連結是
`/mercari/search?seller=901019808`，Mercari 台灣同一件商品（UUID
`2a2f632e-1b44-4864-b5cd-e684f2db8db1`，圖片檔名 `m38347072251_1.jpg`）的
flight payload 是 `"sellerId":"901019808"`——逐位相同。

即使如此，`seller_key` 仍然維持 `{site}:{seller_id}`、**兩個站不合併**：
標價幣別不同（Buyee 標日圓、Mercari 台灣標新台幣含站內換匯），價格水準也
不同，而同儕比對的第 2 條對齊規則就是「同一個站」。ID 空間相同讓我們**知道**
這兩個鍵指向同一個人（要合併分析隨時可以），但把它們的價格丟進同一個池子
會把平台差算成賣家 alpha——那正是 `seller_alpha` 頂註在擋的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 種子預設看哪些狀態。使用者按過「觀察」或「已買」＝人工確認過有興趣。
SEED_STATES: tuple[str, ...] = ("watching", "bought")


@dataclass(slots=True)
class SellerFill:
    """一筆標的的補賣家結果。**失敗要說得出原因**，不是安靜地少一筆。"""

    key: str
    site: str
    url: str
    ok: bool = False
    seller_id: str | None = None
    seller_name: str | None = None
    written: bool = False
    note: str = ""


@dataclass(slots=True)
class SeedReport:
    fills: list[SellerFill] = field(default_factory=list)
    requests: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return sum(1 for f in self.fills if f.seller_id)

    @property
    def written(self) -> int:
        return sum(1 for f in self.fills if f.written)

    def sellers(self) -> dict[str, list[str]]:
        """seller_key → 這批標的裡屬於他的 key 清單。"""
        out: dict[str, list[str]] = {}
        for f in self.fills:
            if f.seller_id:
                out.setdefault(f"{f.site}:{f.seller_id}", []).append(f.key)
        return out


def seed_targets(
    store: Any, *, states: tuple[str, ...] = SEED_STATES, limit: int = 100
) -> list[dict[str, Any]]:
    """挑出要補賣家的標的：狀態在 `states`、且觀測列還沒有賣家的。

    判準看 `listing_obs.seller_id` 而不是 signals payload——賣家最終要落在
    觀測帳上（`seller_alpha` 讀的是那張表），payload 有值但觀測列沒有的情況
    由既有的 `backfill-sellers` 負責，兩支不重疊。
    """
    obs = {r["key"]: r for r in store.listing_obs(limit=50000)}
    out: list[dict[str, Any]] = []
    for state in states:
        for row in store.list_signals(state=state, limit=limit * 10):
            o = obs.get(row["key"])
            if o is None or o.get("seller_id"):
                continue
            out.append({
                "key": row["key"],
                "site": row.get("site") or o.get("site") or "",
                "url": row.get("url") or o.get("url") or "",
                "title": row.get("title") or "",
                "state": state,
            })
            if len(out) >= limit:
                return out
    return out


def backfill_signal_sellers(
    *,
    cfg: Any,
    store: Any,
    targets: list[dict[str, Any]],
    fetcher: Any = None,
    waf: Any = None,
    ebay: Any = None,
    dry_run: bool = False,
) -> SeedReport:
    """逐一抓商品頁把賣家挖回來。**節流交給 fetcher／WafSession**（≥2 秒／請求）。

    走的是既有的 `parse_target` → `fetch_item_page` 管線：抓取路徑的選擇只有
    一份，Mercari 台灣哪天改版、Buyee 哪天不能改道，兩邊會一起壞、一起修。

    冪等：寫入走 `store.set_listing_obs_seller`（只補 NULL），第二次跑
    `written` 必為 0。`dry_run=True` 不打外網、不寫入。
    """
    from .appraise import UnsupportedUrlError, fetch_item_page, parse_target
    from .sources.base import FetchError

    report = SeedReport()
    for t in targets:
        fill = SellerFill(key=t["key"], site=t.get("site") or "", url=t.get("url") or "")
        if dry_run:
            fill.note = "dry-run：未打外網"
            report.fills.append(fill)
            report.requests += 1
            continue
        try:
            target = parse_target(fill.url)
        except UnsupportedUrlError as exc:
            fill.note = f"網址不支援：{exc.url}"
            report.fills.append(fill)
            continue
        try:
            item = fetch_item_page(cfg, target, fetcher=fetcher, waf=waf, ebay=ebay)
        except FetchError as exc:
            fill.note = f"抓取失敗：{exc}"
            report.requests += 1
            report.fills.append(fill)
            continue
        except Exception as exc:  # noqa: BLE001 - 單筆失敗不得讓整輪停擺
            fill.note = f"{type(exc).__name__}: {exc}"
            report.requests += 1
            report.fills.append(fill)
            continue
        report.requests += 1
        fill.ok = True
        fill.seller_id = item.seller
        fill.seller_name = item.seller_name
        if not item.seller:
            fill.note = "商品頁沒有賣家欄位（抽不到就是 None，不猜）"
        else:
            fill.written = store.set_listing_obs_seller(fill.key, str(item.seller))
            fill.note = "已寫入" if fill.written else "觀測列已有賣家（冪等，不覆寫）"
        report.fills.append(fill)
    return report


__all__ = [
    "SEED_STATES",
    "SeedReport",
    "SellerFill",
    "backfill_signal_sellers",
    "seed_targets",
]
