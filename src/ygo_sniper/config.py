"""設定載入。

單一入口 `load_config()`，所有模組都從這裡拿參數，
避免有人偷偷在別的地方硬編一個費率。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    """src/ygo_sniper/config.py -> 專案根目錄"""
    return Path(__file__).resolve().parents[2]


#: route 沒寫 `destination:` 時的預設落點。既有三條路徑全部把貨送到台灣，
#: 所以預設是 tw_home——舊 settings.yaml 不寫也不會改變任何行為。
DEFAULT_DESTINATION = "tw_home"


@dataclass(slots=True)
class RouteConfig:
    name: str
    label: str
    sites: list[str]
    purchase_fee_jpy: float
    plan_fee_jpy: float
    domestic_ship_jpy: float
    consolidation_fee_jpy: float
    intl_ship_jpy: float
    bundle_size: int
    #: 走完這條路徑之後**貨在哪裡**（`domain.HoldingLocation` 的值）。
    #: 新欄位必須留在尾端且帶預設值（slots dataclass 的位置參數順序）。
    destination: str = DEFAULT_DESTINATION
    #: 這條路徑需不需要「日本收款身分」（日本地址／銀行／門號）才走得通。
    requires_jp_presence: bool = False

    @property
    def per_order_fee_jpy(self) -> float:
        """不能靠湊單攤提的部分 —— 這是 Buyee 最容易被低估的成本。"""
        return self.purchase_fee_jpy + self.plan_fee_jpy + self.domestic_ship_jpy

    @property
    def amortizable_jpy(self) -> float:
        return self.consolidation_fee_jpy + self.intl_ship_jpy


@dataclass(slots=True)
class SellVenueConfig:
    """一個**賣場**的費率表（settings.yaml 的 `sell_venues:`）。

    與 `RouteConfig` 對稱：那邊是「錢怎麼出去」，這邊是「錢怎麼回來」。
    每一項費率在 settings.yaml 都附了來源 URL 與查證日期；`verified` 是
    「這一組數字查證到什麼程度」的自述，會原樣出現在報告裡——**未查證的
    費率不准長得跟查證過的一樣**。
    """

    name: str
    label: str
    #: 賣場的計價幣別（`Currency` 的值）。
    currency: str
    #: 要在這裡賣，貨必須在哪個 `HoldingLocation`。
    location: str
    #: 需不需要「日本收款身分」（日本地址＋日本銀行帳戶＋日本手機門號）。
    requires_jp_presence: bool = False
    #: 成交手續費（佔成交價比例）。
    commission_pct: float = 0.0
    #: 金流／系統處理費（佔成交價比例）。與 commission 分開列是因為兩者
    #: 在各平台的名目與調整節奏不同，合成一個數字之後就查不回來了。
    payment_fee_pct: float = 0.0
    #: 上架費（賣場幣別，每件）。
    listing_fee_native: float = 0.0
    #: 賣家負擔的境內運費（賣場幣別，每件）。
    seller_shipping_native: float = 0.0
    #: 平台餘額 → 當地銀行帳戶的提領手續費（賣場幣別，每次提領＝每件算一次）。
    payout_fee_native: float = 0.0
    #: 當地銀行 → 台灣銀行的一次國際匯款成本（賣場幣別）。可被 batch 攤提。
    remit_fee_native: float = 0.0
    #: 幾件卡的貨款湊一次匯回。1 = 每件都單獨匯（最貴）。
    remit_batch_size: int = 1
    #: 匯回的匯差（佔匯回金額比例）。台幣賣場為 0——不必換匯。
    fx_spread_pct: float = 0.0
    #: 對應到 comps 的 `site`（估價模型的 venue 詞彙）。
    #: **None ＝ 這個賣場沒有任何成交樣本**，估不出行情，一律拒絕給數字。
    valuation_venue: str | None = None
    enabled: bool = True
    #: 費率查證程度的自述（例如 "2026-08-02 官方頁查證" / "未查證（保守估計）"）。
    verified: str = "未查證"
    #: 來源 URL（多個以空白分隔）。
    source_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def remit_fee_per_item_native(self) -> float:
        return self.remit_fee_native / max(1, self.remit_batch_size)


@dataclass(slots=True)
class TransferConfig:
    """把貨從 A 地送到 B 地的成本（settings.yaml 的 `resale.transfers:`）。

    **`feasible: false` 時沒有 cost**——不可行不是「成本很高」，是「沒有這個
    選項」。給一個很大的數字會讓它在排序裡默默參加比較，總有一天會贏。
    """

    frm: str
    to: str
    feasible: bool
    cost_jpy: float = 0.0
    #: True ＝ 這筆成本可以被 bundle_size 攤提（例如一箱寄回台灣裝很多張）。
    amortizable: bool = False
    reason: str = ""
    note: str = ""


@dataclass(slots=True)
class ResaleConfig:
    """轉賣模型的設定總成（settings.yaml 的 `resale:` ＋ `sell_venues:`）。"""

    #: 你有沒有「日本收款身分」。False ＝ 所有日本境內賣場一律不可行。
    jp_presence: bool = False
    jp_presence_reason: str = ""
    #: 「買了先留在日本、不出貨」的取貨路徑。**刻意不放進 `routes:`**：
    #: 它一旦進了 routes，`best_route`／`breakeven` 就會把它當成一條買進路徑，
    #: 而它的到手成本不含國際運費——買方模型會整批低估（而且方向是「更便宜」）。
    jp_hold_route: RouteConfig | None = None
    transfers: list[TransferConfig] = field(default_factory=list)
    venues: dict[str, SellVenueConfig] = field(default_factory=dict)
    #: 稅務註記。模型**不算稅**，但要在每一份報告裡講出來。
    tax_note: str = ""

    def transfer(self, frm: str, to: str) -> TransferConfig | None:
        for t in self.transfers:
            if t.frm == frm and t.to == to:
                return t
        return None


@dataclass(slots=True)
class Config:
    grading_fee_twd: float
    fx: dict[str, float]
    routes: dict[str, RouteConfig]
    scoring: dict[str, Any]
    fetch: dict[str, Any]
    storage: dict[str, Any]
    notify: dict[str, Any]
    # 必須帶預設值：test_fetcher.py 用 dataclasses.replace(cfg, ...) 重建 Config，
    # 沒預設的新欄位會讓 replace 與所有既有建構點一起爆。
    sources: dict[str, Any] = field(default_factory=dict)
    watchlist: dict[str, Any] = field(default_factory=dict)
    # 估價模型參數（settings.yaml 的 valuation:）。缺區塊時 valuation.py 全部走預設值，
    # 所以舊 settings.yaml 不會炸——但也不會有區間，這是刻意的降級方向。
    valuation: dict[str, Any] = field(default_factory=dict)
    # 掃描狀態機（settings.yaml 的 scan:）。缺區塊時全部走下面的預設值。
    scan: dict[str, Any] = field(default_factory=dict)
    # 出價上限引擎（settings.yaml 的 bidding:）。缺區塊時走 bidding.py 的預設值。
    bidding: dict[str, Any] = field(default_factory=dict)
    # Seller Alpha 的門檻與配分（settings.yaml 的 seller_alpha:）。缺區塊時走
    # seller_alpha.AlphaParams 的預設值。**這個欄位存在本身就是為了不靜默**：
    # 沒有它，settings.yaml 裡打的 seller_alpha 區塊會被 load_config 直接丟掉，
    # 使用者以為調了門檻其實沒有。
    seller_alpha: dict[str, Any] = field(default_factory=dict)
    # 買方身分事實（settings.yaml 的 buying:，例如美國收件地址）。缺區塊＝沒有
    # 美國地址，所有依賴它的功能（eBay 替代路徑、US 旗標）安靜地不出現。
    buying: dict[str, Any] = field(default_factory=dict)
    # 轉賣模型（settings.yaml 的 resale: ＋ sell_venues:）。缺區塊時是一個
    # 「沒有任何賣場」的空設定——結果是所有組合都回「不可行：沒設定賣場」，
    # 而不是靜靜給出一個沒有費率支撐的淨利數字。
    resale: ResaleConfig = field(default_factory=ResaleConfig)
    root: Path = field(default_factory=project_root)

    # --- 便利存取 ---
    @property
    def db_path(self) -> Path:
        p = self.root / self.storage["db_path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def cache_dir(self) -> Path:
        p = self.root / self.storage["cache_dir"]
        p.mkdir(parents=True, exist_ok=True)
        return p

    def routes_for_site(self, site: str) -> list[RouteConfig]:
        return [r for r in self.routes.values() if site in r.sites]

    def max_pages_for(self, source_name: str) -> int:
        """這個**發現管道**在架掃描要翻幾頁。

        全域值是 `fetch.max_pages_per_query`；`sources.<name>.max_pages` 可以覆寫它。
        為什麼要能逐來源覆寫：翻頁的邊際效益完全取決於單頁容量與上架速度——
        Yahoo 一頁 50 筆、Mercari 一頁 100 筆，一小時內的新增量遠遠塞不滿第一頁，
        翻頁只會重複拿到上一輪看過的東西；PayPay 一頁只有 40 筆而在架量大，
        第一頁裝不下一小時的新增。用一個全域數字同時服務這兩種形狀，
        只能二選一：要嘛 PayPay 看不到貨，要嘛其他來源白打三倍請求。

        **這裡調的是「抓取覆蓋」，不是「評分權重」。** 任何來源都不會因為多掃幾頁
        而在 scoring／valuation 得到加分——多掃到的標的一樣得自己過同一道門檻。

        非法值（0、負數、非數字）一律退回全域值並印警告，不靜默當成 1。
        """
        default = max(1, int(self.fetch.get("max_pages_per_query", 1)))
        spec = (self.sources or {}).get(source_name) or {}
        raw = spec.get("max_pages")
        if raw is None:
            return default
        try:
            pages = int(raw)
        except (TypeError, ValueError):
            print(f"[warn] sources.{source_name}.max_pages={raw!r} 不是整數，改用全域 {default}")
            return default
        if pages < 1:
            print(f"[warn] sources.{source_name}.max_pages={pages} < 1，改用全域 {default}")
            return default
        return pages

    @property
    def scan_timeout_seconds(self) -> float:
        """掃描狀態的逾時（秒）。超過就當「那次掃描已經死了」，見 store.scan_status。"""
        return float(self.scan.get("timeout_seconds", 1800))

    @property
    def telegram_token(self) -> str | None:
        return os.getenv("TELEGRAM_BOT_TOKEN")

    @property
    def telegram_chat_id(self) -> str | None:
        return os.getenv("TELEGRAM_CHAT_ID")

    @property
    def ebay_client_id(self) -> str | None:
        return os.getenv("EBAY_CLIENT_ID")

    @property
    def ebay_client_secret(self) -> str | None:
        return os.getenv("EBAY_CLIENT_SECRET")

    @property
    def ebay_enabled(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)


def _load_dotenv(root: Path) -> None:
    """極簡 .env 載入，不想為了三個變數拉 python-dotenv 進來。"""
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _route_from_spec(name: str, spec: dict[str, Any]) -> RouteConfig:
    return RouteConfig(
        name=name,
        label=spec.get("label", name),
        sites=spec.get("sites", []),
        purchase_fee_jpy=float(spec.get("purchase_fee_jpy", 0)),
        plan_fee_jpy=float(spec.get("plan_fee_jpy", 0)),
        domestic_ship_jpy=float(spec.get("domestic_ship_jpy", 0)),
        consolidation_fee_jpy=float(spec.get("consolidation_fee_jpy", 0)),
        intl_ship_jpy=float(spec.get("intl_ship_jpy", 0)),
        bundle_size=max(1, int(spec.get("bundle_size", 1))),
        destination=str(spec.get("destination", DEFAULT_DESTINATION)),
        requires_jp_presence=bool(spec.get("requires_jp_presence", False)),
    )


def _resale_from_settings(settings: dict[str, Any]) -> ResaleConfig:
    """組出 `ResaleConfig`。缺區塊時回一個空設定（沒有任何賣場）。

    「沒設定 = 沒有賣場 = 每個組合都不可行」是刻意的降級方向：轉賣的每一分錢
    都得有一條查得到來源的費率撐著，猜一個預設費率等於憑空生出淨利。
    """
    block = settings.get("resale") or {}
    jp = block.get("jp_presence") or {}
    hold_spec = block.get("jp_hold_route")
    hold = (
        _route_from_spec(hold_spec.get("name", "jp_hold"), hold_spec)
        if hold_spec else None
    )
    transfers = [
        TransferConfig(
            frm=str(t["from"]),
            to=str(t["to"]),
            feasible=bool(t.get("feasible", False)),
            cost_jpy=float(t.get("cost_jpy", 0)),
            amortizable=bool(t.get("amortizable", False)),
            reason=str(t.get("reason", "")),
            note=str(t.get("note", "")),
        )
        for t in (block.get("transfers") or [])
    ]
    venues = {
        name: SellVenueConfig(
            name=name,
            label=spec.get("label", name),
            currency=str(spec.get("currency", "JPY")).upper(),
            location=str(spec["location"]),
            requires_jp_presence=bool(spec.get("requires_jp_presence", False)),
            commission_pct=float(spec.get("commission_pct", 0)),
            payment_fee_pct=float(spec.get("payment_fee_pct", 0)),
            listing_fee_native=float(spec.get("listing_fee_native", 0)),
            seller_shipping_native=float(spec.get("seller_shipping_native", 0)),
            payout_fee_native=float(spec.get("payout_fee_native", 0)),
            remit_fee_native=float(spec.get("remit_fee_native", 0)),
            remit_batch_size=max(1, int(spec.get("remit_batch_size", 1))),
            fx_spread_pct=float(spec.get("fx_spread_pct", 0)),
            valuation_venue=spec.get("valuation_venue") or None,
            enabled=bool(spec.get("enabled", True)),
            verified=str(spec.get("verified", "未查證")),
            source_url=str(spec.get("source_url", "")),
            notes=list(spec.get("notes") or []),
        )
        for name, spec in (settings.get("sell_venues") or {}).items()
    }
    return ResaleConfig(
        jp_presence=bool(jp.get("enabled", False)),
        jp_presence_reason=str(jp.get("reason", "")),
        jp_hold_route=hold,
        transfers=transfers,
        venues=venues,
        tax_note=str(block.get("tax_note", "")),
    )


@lru_cache(maxsize=1)
def load_config(config_dir: str | None = None) -> Config:
    root = project_root()
    _load_dotenv(root)
    cdir = Path(config_dir) if config_dir else root / "config"

    settings = yaml.safe_load((cdir / "settings.yaml").read_text(encoding="utf-8"))
    watchlist = yaml.safe_load((cdir / "watchlist.yaml").read_text(encoding="utf-8"))

    routes = {
        name: _route_from_spec(name, spec) for name, spec in settings["routes"].items()
    }

    return Config(
        grading_fee_twd=float(settings["grading_fee_twd"]),
        fx=settings["fx"],
        routes=routes,
        scoring=settings["scoring"],
        fetch=settings["fetch"],
        storage=settings["storage"],
        notify=settings["notify"],
        sources=settings.get("sources", {}),
        watchlist=watchlist,
        valuation=settings.get("valuation", {}),
        scan=settings.get("scan", {}),
        bidding=settings.get("bidding", {}),
        seller_alpha=settings.get("seller_alpha", {}) or {},
        buying=settings.get("buying", {}) or {},
        resale=_resale_from_settings(settings),
        root=root,
    )
