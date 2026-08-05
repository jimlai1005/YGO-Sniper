"""發現管道 registry。

registry 是 **name-keyed**（PLAN Q3）：鍵是發現管道名（"yahoo_direct"、
"buyee_mercari"…），與 watchlist 的 `queries[].sources` 和 `Listing.source`
同一命名空間。`Site` 是另一個維度（購買路徑），Yahoo 直抓「Yahoo 發現、
Buyee 購買」打破了兩者的 1:1 對應，所以 registry 不能再用 Site 當鍵。

Buyee 鏡像現在只剩 Mercari 一條（`buyee_mercari`），仍走 `WafSession`：
一顆 aws-waf-token 解鎖全部 Buyee 路徑（RECON §4 實測）。session 走
`_LazyWafFetcher` 延遲建立——`build_sources()` 每次掃描都會跑，但沒排到
Buyee 查詢的跑法不該付「建 session」的成本，更不該載入 playwright。
（`waf.py` 本身也不在模組頂層 import playwright，兩道保證疊在一起。）

**已被直抓取代的發現管道**（`Site` 不動，那是購買路徑）：
- `buyee_yahoo` → `yahoo_direct`（sources/yahoo.py）
- `buyee_paypay` → `paypay_direct`（sources/paypay.py，2026-08-02）：實測直抓
  一頁 100 筆／0.86s／純 httpx，走 Buyee 是一頁 40 筆／2.66s／要 Playwright
  解 WAF，價格逐筆相同。`BuyeeSource` 對 PayPay 的支援（`_SITE_SPEC`）**保留**
  ——它是直抓壞掉時的備援，也還是 `probe` 指令的除錯對象，只是不進 registry。
"""

from __future__ import annotations

from ..config import Config
from ..domain import Site
from .base import CachedFetcher, Source
from .buyee import BuyeeSource, probe
from .ebay import EbaySource
from .paypay import PayPayDirectSource
from .ruten import RutenSource
from .waf import WafSession
from .yahoo import YahooAuctionSource
from .yahoo_closed import YahooClosedSource


class _LazyWafFetcher:
    """WafSession 的延遲代理，介面與 CachedFetcher 對齊（get／close）。

    第一次真的要抓 Buyee 才 `WafSession(cfg)`；在那之前完全沒有瀏覽器、
    沒有 httpx client、也沒有 playwright import。多個 source 共用同一個實例
    ＝共用同一顆 token 與同一份重取預算。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._session: WafSession | None = None

    @property
    def session(self) -> WafSession:
        if self._session is None:
            self._session = WafSession(self.cfg)
        return self._session

    def get(
        self,
        url: str,
        *,
        use_cache: bool = True,
        allow_statuses: tuple[int, ...] = (),
        min_bytes: int | None = None,
    ) -> str:
        return self.session.get(
            url, use_cache=use_cache, allow_statuses=allow_statuses, min_bytes=min_bytes
        )

    def close(self) -> None:
        # 沒建過就沒東西可關；關過之後歸零，下次再用會重建（token 本來就不留）
        if self._session is not None:
            self._session.close()
            self._session = None


def build_sources(cfg: Config, fetcher: CachedFetcher | None = None) -> dict[str, Source]:
    """建出 name-keyed registry：發現管道名 → source 實例。

    每個 source 都帶 `name`（發現管道）、`site`（購買路徑）、`supports_sold`
    （能不能跑「已售出」搜尋餵 comps）三個屬性——comps 與 pipeline 只看
    屬性，不 hardcode 名稱前綴。
    """
    fetcher = fetcher or CachedFetcher(cfg)

    yahoo = YahooAuctionSource(cfg, fetcher)  # name/site/supports_sold 是 class 屬性

    # 落札相場（成交價）。supports_sold=True，所以只會被 comps.sold_queries()
    # 挑中；它**不出現在 watchlist 的 queries[].sources**，因此不參與在架掃描，
    # 也不在 settings.yaml 的 sources: 段裡（canary 只跑那一段列出的來源）。
    # 兩個維度各自把關：想讓它進在架掃描必須同時改兩個檔，不會手滑進去。
    yahoo_closed = YahooClosedSource(cfg, fetcher)

    # Yahoo!フリマ（PayPay）直抓。site 仍是 BUYEE_PAYPAY——購買路徑沒變，
    # 換的只是發現端（見模組 docstring 的實測對照）。
    paypay = PayPayDirectSource(cfg, fetcher)

    # Buyee 鏡像只剩 Mercari 一條，但 WafSession 仍走 lazy holder：
    # 沒排到 Mercari 查詢的跑法不該付「開瀏覽器」的成本
    waf = _LazyWafFetcher(cfg)
    mercari = BuyeeSource(cfg, Site.BUYEE_MERCARI, waf)   # name = "buyee_mercari"

    # App ID 未設定時 enabled=False；search_detailed 會回 BLOCKED（不是安靜的 0 筆）
    ebay = EbaySource(cfg)

    # 露天拍賣（台灣境內）。註冊進來是為了讓 canary 每輪自檢解析器還活著，
    # **但它刻意不出現在 watchlist 的 queries 或 comps_queries** ——
    # 台幣成交價混進日本 comps 索引就是混源比較（見 sources/ruten.py 頂註最後一節）。
    ruten = RutenSource(cfg, fetcher)

    return {s.name: s for s in (yahoo, yahoo_closed, paypay, mercari, ebay, ruten)}


__all__ = [
    "Source",
    "CachedFetcher",
    "BuyeeSource",
    "EbaySource",
    "PayPayDirectSource",
    "RutenSource",
    "WafSession",
    "YahooAuctionSource",
    "YahooClosedSource",
    "build_sources",
    "probe",
]
