"""賣家監控名單 ＋ 輪替掃描（Seller Alpha 第三棒）。

前兩棒把「誰比同儕便宜」算出來了（`seller_alpha`），但那是**被動**的：
只看得到掃描關鍵字剛好撈到的那幾筆。這一棒讓它會動——挑出值得盯的賣家，
用**賣家頁列舉**定期把他們的全部在架抓回來走同一條管線。

---------------------------------------------------------------------------
## 四個刻意的設計決定

### 1. 名單存 db，不存 config
名單會被兩邊寫：排程（分數過門檻自動入選）與使用者（手動加入）。兩邊都改同一個
yaml 遲早互相覆蓋；而且「上次掃描時間」本來就是狀態不是設定（見 `store.seller_watch`）。

### 2. 手動加入不受分數門檻限制，而且**不假裝它有分數**
2026-08-04 全量實測：95 個有觀測的賣家裡只有 5 個過得了 `seller_alpha` 的證據門檻，
其中 4 個是「比同儕**貴**」——自動名單當時實際上只有 `ebay:psa` 一個。這個現況下若
只允許自動入選，整個監控功能等於只監控一個賣家。
2026-08-05 複測：帳本厚度上來後，360 個有觀測的賣家裡已有 34 個過 Alpha 門檻、
自動名單也已有 16 個成員——但門檻沒變，變的是證據量；仍有 326 個賣家證據不足，
分數門檻永遠會篩掉一批「看起來對但樣本不夠」的賣家。所以手動加入仍是第一等公民，
但它的 `score` 永遠是 NULL：畫面上必須看得出「這是使用者的直覺」而不是「這是
59.2 分的賣家」。給手動加入的賣家補一個 0 分或假分數，就是把兩種完全不同的
證據狀態壓成同一個數字。

### 3. 分批在**加入當下**算一次，之後不動
`batch = sha1(seller_key) % N`。用 sha1 不用內建 `hash()`：後者每個行程都有不同的
隨機種子（PYTHONHASHSEED），每次排程跑起來分批表都會重洗，「每 240 分鐘掃一次」
的保證直接消失，而且完全看不出來。用名單索引（第 i 個賣家 → i % 4）也不行：
名單一增刪，後面每個人的批次都會位移。

### 4. 兩條入選軌，淘汰時跨軌不比分數（2026-08-05）
自動入選現在有兩條路：`SOURCE_AUTO`（Alpha 軌——「他比市場便宜」，score 是
`seller_alpha` 的同儕相對折價分）與 `SOURCE_SUPPLY`（供給軌——「他值得盯」，
score 是 `seller_supply` 的供給契合度分）。兩者存在同一個 `score` 欄位裡只是
為了 schema 不動，**數值不可直接比大小**：Alpha 的 25 分與 Supply 的 90 分是
兩把不同的尺（工程原則 1：同源同基準）。名單滿了要淘汰誰的優先序因此是
「同軌比分數、跨軌比軌道」：manual 誰都不淘汰，auto 可以擠掉分數更低的
auto、擠不到才擠 supply（不比分數，任選 supply 軌內最低分的那個），supply
只能擠分數更低的 supply。理由是 Alpha 是**實證**（同儕相對已經量出真的比較
便宜）、Supply Fit 只是**假設**（看起來值得盯，還沒驗證出折價）——位子不夠時
假設讓給實證。細節與可測試的規則表見 `_evictable` 的 docstring。

---------------------------------------------------------------------------
## 輪替與請求預算

使用者已定：名單上限 **30 個賣家**、每賣家每 **240 分鐘**掃一次，實作形式是
**拆 4 批、每小時輪一批**（30÷4≈8 個賣家／小時 × 每人 1 頁 = 每小時約 8 個請求，
與既有掃描的約 11 個同一量級）。

節流帳落 `meta`（跨行程冪等，做法與 `comps.claim_sold_run` 同一套）：計時器是
**時間**不是輪數——`scan` 可能被 dashboard 手動多按幾次，用輪數計時會讓一個
手動掃描把整輪輪替往前推掉一小時。

## 誠實邊界（2026-08-04）

- 賣家頁列舉目前有 **eBay**、**PayPay（Yahoo!フリマ）**、**Yahoo 拍賣**、
  **Buyee Mercari 鏡像** 四站。
  Yahoo 拍賣是 2026-08-04 補上的（`yahoo.YahooAuctionSource.search_seller`）
  ——在那之前名單上 16 個賣家有 6 個是 `buyee_yahoo`（含分數最高的三個
  79.8／67.5／66.3）全部掃不到，是當時最大的覆蓋缺口。
  Mercari 是 2026-08-09 補上的（`buyee.BuyeeSource.search_seller`，走
  `buyee.jp/mercari/search?seller={id}`＋WafSession，與關鍵字掃描同一條路）
  ——釘選軌收 tw.mercari／jp.mercari 賣家進 `buyee_mercari` 鍵，靠這個才掃得到。
  仍然沒有列舉實作的站台會被**明確記成「來源尚未支援」**，不是安靜地跳過
  （安靜跳過與「這個賣家最近沒上架」外顯一模一樣）。
- **Yahoo 拍賣的賣家頁沒有已售出清單**（實測），所以那一站的成交歷史仍然
  只能走 `yahoo_closed`；`search_seller(sold=True)` 會告警並回空清單。
- 監控掃描抓回來的標的**走既有的完整管線**（`parse_card` → `is_candidate` →
  估價 → `listing_obs` → scoring），這裡不另開一條評估路徑：判準只有一份。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: 輪替節流帳的 meta 鍵（跨行程）。
WATCH_ROTATION_META_KEY = "seller_watch_rotation"

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"
#: 第二條自動入選軌（2026-08-05）：Supply Fit（「這賣家值不值得盯」）。
#: **不與 `SOURCE_AUTO` 共用一把尺**——auto 的 score 是 Seller Alpha（同儕相對
#: 便宜多少），supply 的 score 是供給契合度（跟便宜與否無關）。存在同一個
#: `score` 欄位裡是為了 schema 不動，但**跨軌不准比大小**，見 `_evictable`。
SOURCE_SUPPLY = "supply"
#: 釘選軌（2026-08-09）：使用者貼賣家頁 URL 明確要求長期追蹤的賣家。
#: 三個結構性差異，每一個都是「使用者明講 > 演算法入選」的化身：
#: 1. **不佔 30 名額**——上限檢查與名額顯示都把 pinned 排除，否則釘幾個
#:    就偷吃幾個演算法名額，兩邊都變差。
#: 2. **永不被淘汰**——`_evictable` 的候選池裡根本沒有它。
#: 3. **進每一批輪替**（見 `due_sellers`）——效果是每個輪替時段掃一次
#:    （60 分 vs 其他軌 240 分），這就是「優先權更高」的實作。
#: score 一律 None：釘選是使用者的意志，不是任何一把尺量出來的分數。
SOURCE_PINNED = "pinned"

#: 各軌的中文標籤（訊息用；不要在別處手打字串）。
SOURCE_LABEL: dict[str, str] = {
    SOURCE_AUTO: "Alpha 軌",
    SOURCE_SUPPLY: "供給軌",
    SOURCE_MANUAL: "手動",
    SOURCE_PINNED: "釘選（不佔名額）",
}

#: site → 具備「賣家頁列舉」能力的 source 名稱。實測依據見
#: reports/seller-page-feasibility.md（eBay `filter=sellers:`、PayPay `/user/{id}`）。
SELLER_PAGE_SOURCE: dict[str, str] = {
    "ebay": "ebay",
    "buyee_paypay": "paypay_direct",
    # 2026-08-04 補上（名單上分數最高的三個賣家都是這一站）。賣家頁的
    # `__NEXT_DATA__` 與 closedsearch 同一條路徑，見 yahoo.py `_SELLER_LISTING_PATH`。
    "buyee_yahoo": "yahoo_direct",
    # 2026-08-09 補上（釘選軌 Phase 2）：Buyee Mercari 鏡像的賣家頁
    # `buyee.jp/mercari/search?seller={id}`，見 buyee.BuyeeSource.search_seller。
    "buyee_mercari": "buyee_mercari",
}

#: 還沒有列舉實作的站台 → 一句話說明。**要說得出「為什麼沒掃」**。
UNSUPPORTED_SITE_NOTE: dict[str, str] = {
    "mercari_tw": "Mercari 台灣賣家頁尚未實測（新釘選的 tw.mercari 賣家已收進 buyee_mercari，掃得到）",
    "ruten": "露天賣家頁尚未實測（這條管道目前只跑 canary）",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _age_minutes(value: Any, now: datetime) -> float | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return (now - ts).total_seconds() / 60.0


# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class WatchParams:
    """監控名單與輪替的參數。全部來自 `settings.yaml` 的 `seller_alpha:`。

    非法值一律印警告並退回預設（不靜默）：一個打錯的上限會讓監控在使用者
    不知情的狀況下停擺或暴衝，兩種都比報錯難發現。
    """

    #: 整個功能的開關。false ＝ 不列舉、不推播規則 3（名單與 CLI 仍可用）。
    enabled: bool = True
    #: 名單上限（使用者已定 30）。
    max_sellers: int = 30
    #: 每個賣家的掃描間隔（使用者已定 240 分鐘）。
    per_seller_interval_minutes: float = 240.0
    #: 拆幾批輪替（4 批 × 每小時一批 ＝ 每賣家 4 小時一次）。
    batches: int = 4
    #: 自動入選的分數門檻。依據見 settings.yaml 的註解。
    auto_min_score: float = 25.0
    #: **供給軌**的入選門檻（Supply Fit 0-100）。與 `auto_min_score` 是兩把
    #: 不同的尺，數字大小不可互相比較、也不可互相代入。依據見 settings.yaml。
    supply_min_score: float = 60.0
    #: 每個賣家每次抓幾頁。1 頁：eBay 一頁 200 筆、PayPay 一頁 100 筆，
    #: 遠大於任何一個賣家的遊戲王在架量（實測 38 筆／人）。
    pages: int = 1

    @property
    def batch_interval_minutes(self) -> float:
        """兩批之間該隔多久 ＝ 每賣家間隔 ÷ 批數（240/4 = 60 分）。"""
        return self.per_seller_interval_minutes / max(1, self.batches)

    @classmethod
    def from_config(cls, cfg: Any) -> WatchParams:
        block = dict(getattr(cfg, "seller_alpha", None) or {})
        out = cls()
        kw: dict[str, Any] = {}
        if "watch_enabled" in block:
            kw["enabled"] = bool(block["watch_enabled"])
        for field_name, key, caster, lo in (
            ("max_sellers", "watch_max_sellers", int, 1),
            ("per_seller_interval_minutes", "per_seller_interval_minutes", float, 1.0),
            ("batches", "watch_batches", int, 1),
            ("auto_min_score", "watch_auto_min_score", float, 0.0),
            ("supply_min_score", "watch_supply_min_score", float, 0.0),
            ("pages", "watch_pages", int, 1),
        ):
            if key not in block:
                continue
            try:
                value = caster(block[key])
            except (TypeError, ValueError):
                print(f"[warn] seller_alpha.{key}={block[key]!r} 不是數字，"
                      f"改用 {getattr(out, field_name)}")
                continue
            if value < lo:
                print(f"[warn] seller_alpha.{key}={value} 小於 {lo}，"
                      f"改用 {getattr(out, field_name)}")
                continue
            kw[field_name] = value
        return cls(**kw) if kw else out


# ---------------------------------------------------------------------------
def batch_of(seller_key: str, batches: int) -> int:
    """賣家鍵 → 輪替批次。**穩定**：同一個鍵在任何行程、任何時候都是同一批。

    用 sha1 而不是內建 `hash()`：後者每個行程的隨機種子不同（PYTHONHASHSEED），
    每次排程跑起來分批表都會重洗，而且外顯只是「這個賣家好像沒有每 4 小時掃到」
    ——沒有錯誤訊息的那種壞法。
    """
    n = max(1, int(batches))
    digest = hashlib.sha1(seller_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % n


# ---------------------------------------------------------------------------
#: 拒絕原因的機器可讀代碼。**分類靠這個代碼，不靠比對 `reason` 字串**：
#: 訊息文字改一個字就會讓字串比對安靜地失效（而失效的方向是「非預期被當成
#: 預期而吞掉」，正是 CLAUDE.md 第五節要防的靜默失敗）。
REJECT_LIST_FULL = "list_full"
REJECT_MALFORMED_KEY = "malformed_key"

#: **預期內**的拒絕：候選人數本來就會多於名額，門檻校準得再好也一定會有一批
#: 落在名單外。這一類可以摘要（一到兩行）。
#: 不在這張表上的代碼（含空字串、未來新增而忘了分類的）一律視為**非預期**並
#: 逐個大聲印出來——預設要偏向吵，不是偏向安靜。
EXPECTED_REJECT_CODES = frozenset({REJECT_LIST_FULL})

REJECT_LABEL: dict[str, str] = {
    REJECT_LIST_FULL: "名單已滿（候選多於名額）",
    REJECT_MALFORMED_KEY: "賣家鍵格式錯誤",
}

#: 摘要行裡那個「代表例」的長度上限。只截代表例，**非預期的告警永不截斷**。
_EXAMPLE_MAX_CHARS = 200


@dataclass(slots=True)
class WatchAddResult:
    """一次加入的結果。**拒絕時必須說得出為什麼、以及使用者能做什麼。**"""

    ok: bool
    seller_key: str
    reason: str
    batch: int | None = None
    #: 為了讓位而被淘汰的 auto 賣家（manual 永不被自動淘汰）。
    evicted: str | None = None
    #: 已經在名單上（不是錯誤，但也不是新增）。
    already: bool = False
    #: 拒絕代碼（`ok=True` 時為空字串）。呼叫端據此決定摘要或逐個告警。
    code: str = ""


def add_watch(
    store: Any,
    seller_key: str,
    *,
    source: str,
    reason: str,
    params: WatchParams,
    score: float | None = None,
    now: str | None = None,
) -> WatchAddResult:
    """加一個賣家進監控名單，**名單上限與淘汰規則都在這裡**（只有這一份）。

    規則：
    - 已經在名單上 → 不動它，回 `already=True`（重複加入不是錯誤）。
      例外：候選是 **pinned** 時把該列**升級成 pinned** 並更新 reason——
      使用者明講要追蹤，永遠優先於演算法入選；批次用 `batch_of` 重算，
      同一個鍵永遠算出同一批，所以升級不會讓它換批。
      反向（已是 pinned、auto/supply/manual 再加）維持不動它：釘選不被降級。
    - **pinned 不受上限管**：不檢查名額、不淘汰任何人、score 一律 None
      （見 `SOURCE_PINNED` 的註解）。其他軌檢查上限時把 pinned 列**排除**
      在計數外——否則釘選會偷吃 30 名額，釘選軌「不佔名額」的承諾就破了。
    - 名單未滿 → 直接加。
    - 名單已滿：
        * **manual 永不被自動淘汰**（那是使用者明講要追蹤的人）。
        * auto 候選人可以擠掉「分數比它低的 auto」——一個位子給更有證據的那個。
        * auto 擠不掉 auto 時，可以擠掉**任何一個 supply**，且**不比分數**：
          Alpha 是實證（他確實比同儕便宜），Supply Fit 只是假設（他看起來
          值得盯），位子不夠時假設讓給實證。兩者的分數是兩把不同的尺。
        * supply 候選人**只能**擠掉分數更低的 supply（同軌內才比得起來）。
        * 擠不動（沒有可淘汰對象，或候選人是 manual）→ **拒絕並說明要移除誰**。
          自動幫使用者砍掉一個他手動加的賣家，比拒絕更糟。
    `score` 只有 auto／supply 帶（manual 一律 None，見模組頂註第 2 點），
    **而且兩軌的 score 不同源**：跨軌時只比軌道優先序，不比數字。
    """
    seller_key = (seller_key or "").strip()
    if ":" not in seller_key:
        return WatchAddResult(
            False, seller_key,
            f"賣家鍵格式應為 `{{site}}:{{seller_id}}`（例：ebay:psa），收到 {seller_key!r}",
            code=REJECT_MALFORMED_KEY,
        )
    if source in (SOURCE_MANUAL, SOURCE_PINNED):
        score = None

    existing = store.get_seller_watch(seller_key)
    if existing and existing.get("active"):
        if source == SOURCE_PINNED:
            # 使用者明講要追蹤 > 演算法入選：把既有列升級成 pinned（已是
            # pinned 就等於更新 reason——「修改備註」走的也是這條）。
            # batch 用 `batch_of` 重算＝原值（同鍵永遠同批），輪替表不動。
            old_source = str(existing.get("source") or "")
            batch = batch_of(seller_key, params.batches)
            store.upsert_seller_watch(
                seller_key, source=SOURCE_PINNED, reason=reason,
                batch=batch, score=None, now=now,
            )
            detail = (
                "已更新釘選備註" if old_source == SOURCE_PINNED
                else f"已從 {SOURCE_LABEL.get(old_source, old_source)} 升級為釘選"
                     "（使用者明講要追蹤 > 演算法入選；不佔名額、永不淘汰）"
            )
            return WatchAddResult(True, seller_key, detail, batch=batch, already=True)
        return WatchAddResult(
            True, seller_key,
            f"已經在監控名單上（{existing.get('source')}，批次 {existing.get('batch')}）",
            batch=existing.get("batch"), already=True,
        )

    active = store.list_seller_watch(active_only=True)
    if source == SOURCE_PINNED:
        # 釘選不受上限管、不淘汰任何人：直接落庫。
        batch = batch_of(seller_key, params.batches)
        store.upsert_seller_watch(
            seller_key, source=SOURCE_PINNED, reason=reason,
            batch=batch, score=None, now=now,
        )
        return WatchAddResult(
            True, seller_key,
            f"已釘選（不佔名額，目前名單 {len(_non_pinned(active))}/{params.max_sellers}；"
            f"批次 {batch}/{params.batches}，但釘選列每一批都會掃）",
            batch=batch,
        )

    evicted: str | None = None
    # 上限只數非 pinned 的列：釘選「不佔名額」是對外承諾，數進去就破了。
    if len(_non_pinned(active)) >= params.max_sellers:
        victim = _evictable(active, source=source, score=score)
        if victim is None:
            return WatchAddResult(
                False, seller_key,
                f"監控名單已滿（{len(_non_pinned(active))}/{params.max_sellers}）"
                "且沒有可淘汰的對象："
                + _full_hint(active, source=source, score=score),
                code=REJECT_LIST_FULL,
            )
        store.deactivate_seller_watch(
            victim["seller_key"],
            reason=_evict_reason(victim, params=params, seller_key=seller_key,
                                 source=source, score=score),
            now=now,
        )
        evicted = victim["seller_key"]

    batch = batch_of(seller_key, params.batches)
    store.upsert_seller_watch(
        seller_key, source=source, reason=reason, batch=batch, score=score, now=now
    )
    detail = f"已加入監控（{source}，批次 {batch}/{params.batches}）"
    if source == SOURCE_MANUAL:
        detail += "；**手動加入不受分數門檻限制，也不假裝它有分數**"
    if evicted:
        detail += f"；淘汰 {evicted}"
    return WatchAddResult(True, seller_key, detail, batch=batch, evicted=evicted)


def _track_rows(active: list[dict[str, Any]], track: str) -> list[dict[str, Any]]:
    return [r for r in active if r.get("source") == track]


def _non_pinned(active: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """名額計數用的視圖：釘選列不佔 30 名額，數名額時一律先剔掉它。"""
    return [r for r in active if r.get("source") != SOURCE_PINNED]


def _lowest_scored(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同一軌之內分數最低的那一列（沒有分數的排最前——證據最少的先讓位）。

    只在**單一軌內**呼叫。跨軌排序沒有意義：兩軌的 score 不同源。
    """
    if not rows:
        return None
    return min(
        rows,
        key=lambda r: (
            float(r["score"]) if r.get("score") is not None else float("-inf"),
            str(r.get("seller_key") or ""),   # 同分時仍然可預測
        ),
    )


def _evictable(
    active: list[dict[str, Any]], *, source: str, score: float | None
) -> dict[str, Any] | None:
    """滿了要淘汰誰。找不到就回 None（＝拒絕新增，不硬擠）。

    軌道優先序（**跨軌不比分數**，見 CLAUDE.md 第三節）：

    | 候選人 | 可以淘汰 | 不可以淘汰 |
    |---|---|---|
    | manual | 誰都不淘汰 | — |
    | pinned | 誰都不淘汰（也不需要——它不佔名額，見 `add_watch`） | — |
    | auto   | 分數更低的 auto；沒有就任一 supply（不比分數） | manual、pinned |
    | supply | 只有分數更低的 supply | manual、auto、pinned |

    **pinned 永遠不會被選為 victim**：候選池只從同軌（auto／supply）與
    supply 軌撈，pinned 天然不在池裡——這是結構保證，不是條件判斷，
    由 `test_pinned_is_never_evicted` 釘死。

    auto 擠 supply 時挑「supply 軌內分數最低的那一個」——那個「最低」是
    supply **軌內**的排序，從頭到尾沒有拿去跟 auto 的分數比過；這樣做只是
    為了行為可預測、可測試。
    """
    if score is None or source not in (SOURCE_AUTO, SOURCE_SUPPLY):
        return None  # manual 候選人不淘汰任何人；沒分數的也不淘汰任何人
    same_track_lower = [
        r for r in _track_rows(active, source)
        if r.get("score") is not None and float(r["score"]) < float(score)
    ]
    victim = _lowest_scored(same_track_lower)
    if victim is not None:
        return victim
    if source == SOURCE_AUTO:
        # 實證（Alpha）擠得掉假設（Supply Fit），**不比分數**：Alpha 的 25 分
        # 與 Supply 的 90 分是兩把不同的尺，比大小沒有意義。
        return _lowest_scored(_track_rows(active, SOURCE_SUPPLY))
    return None


def _evict_reason(
    victim: dict[str, Any], *, params: WatchParams, seller_key: str,
    source: str, score: float | None,
) -> str:
    """被淘汰的那一列上留什麼字。**同軌才寫分數比較，跨軌只寫軌道優先序。**

    跨軌時寫「被分數更高的 X 擠下」會憑空製造一個混源比較的說法——
    留在 db 裡的解釋錯了，比沒有解釋更難發現。
    """
    head = f"名單已滿（上限 {params.max_sellers}）"
    v_score = victim.get("score")
    v_source = str(victim.get("source") or "")
    if v_source == source:
        return (
            f"{head}，自身 {float(v_score):.1f} 分，被同軌"
            f"（{SOURCE_LABEL.get(source, source)}）分數更高的 {seller_key}"
            f"（{float(score):.1f} 分）擠下"
        )
    return (
        f"{head}，被 {SOURCE_LABEL.get(source, source)} 的 {seller_key} 擠下："
        f"Alpha 是實證（同儕相對確實較便宜）、供給契合只是假設（看起來值得盯），"
        f"位子不夠時假設讓給實證。**兩軌分數不同源，這裡沒有比大小**"
    )


def _track_census(active: list[dict[str, Any]]) -> str:
    """名單組成（各軌各幾個）。拒絕訊息與 CLI 共用同一份說法。"""
    n_manual = len(_track_rows(active, SOURCE_MANUAL))
    n_auto = len(_track_rows(active, SOURCE_AUTO))
    n_supply = len(_track_rows(active, SOURCE_SUPPLY))
    n_pinned = len(_track_rows(active, SOURCE_PINNED))
    n_other = len(active) - n_manual - n_auto - n_supply - n_pinned
    parts = [f"manual {n_manual} 個", f"auto／Alpha 軌 {n_auto} 個",
             f"supply／供給軌 {n_supply} 個"]
    if n_pinned:
        parts.append(f"另有釘選 {n_pinned} 個（不佔名額，不在上面的計數裡）")
    if n_other:
        parts.append(f"其他來源 {n_other} 個")
    return "、".join(parts)


def _full_hint(active: list[dict[str, Any]], *, source: str, score: float | None) -> str:
    same_track = [
        r for r in _track_rows(active, source) if r.get("score") is not None
    ]
    lowest = _lowest_scored(same_track)
    if source == SOURCE_MANUAL:
        head = "手動加入不淘汰任何人（manual 永不自動淘汰、auto 也不由手動加入來砍）"
    elif score is None:
        head = "候選人沒有分數，無從比較"
    elif source == SOURCE_SUPPLY:
        head = (
            f"名單上沒有任何分數低於 {score:.1f} 的 supply 賣家，"
            "而供給軌**不得擠掉 Alpha 軌**（假設不擠實證，兩軌分數也不同源）"
        )
    else:
        head = (
            f"名單上沒有任何分數低於 {score:.1f} 的 auto 賣家，也沒有任何 supply 可讓位"
        )
    tail = (
        f"；名單組成：{_track_census(active)}"
        + (f"，同軌最低分的是 {lowest['seller_key']}（{float(lowest['score']):.1f} 分）"
           if lowest else "")
        + "。要空出位子請先 `ygo-sniper watch-seller remove <key>`"
    )
    return head + tail


def remove_watch(
    store: Any, seller_key: str, *, reason: str = "手動移出名單", now: str | None = None
) -> bool:
    return bool(store.deactivate_seller_watch(seller_key, reason=reason, now=now))


def sync_auto_watch(
    store: Any, report: Any, params: WatchParams, *,
    supply: dict[str, Any] | None = None, now: str | None = None,
) -> dict[str, Any]:
    """把過門檻的賣家自動加進名單。**兩條軌道**，回傳報告（加了誰、擋在哪裡）。

    ### 為什麼要第二條軌（2026-08-05）
    Alpha 幾乎只能從**成交價**算出來（實測可比 sold 439 筆 vs ask 24 筆），
    而在架帳要變厚只能靠密集掃賣家庫存——那正是監控名單在做的事：

        要有 Alpha 才進名單 → 進名單才長得出在架觀測
        → 有在架觀測才湊得出同儕 → 湊得出同儕才算得出 Alpha

    supply 軌（Supply Fit，「值不值得盯」）用另一組證據入選來打破這個循環。

    ### 兩軌的分數**永不互比**
    Alpha 軌先跑（維持既有行為），supply 軌後跑；已在名單上的走 `already`。
    兩軌的分數存在同一個 `score` 欄位，但由 `source` 區分軌道，淘汰只在同軌內
    比分數（見 `_evictable`）。回報的 `added` 每一項帶 `track` 讓呼叫端分得出來。

    只加不刪：分數會隨每一輪的樣本上下跳，掉到門檻以下就自動移除的話，
    一個賣家會在名單上進進出出，而 `last_scanned_at` 每次重加都會清空
    （見 `store.upsert_seller_watch`）——輪替節奏會被自己的分數雜訊打亂。
    要移除請人工決定（`watch-seller remove`）。
    """
    out: dict[str, Any] = {
        "added": [], "already": 0, "rejected": [],
        "threshold": params.auto_min_score,
        "supply_threshold": params.supply_min_score,
    }

    def _record(res: WatchAddResult, *, track: str, total: float) -> None:
        if res.already:
            out["already"] += 1
        elif res.ok:
            out["added"].append({"seller_key": res.seller_key, "score": total,
                                 "batch": res.batch, "evicted": res.evicted,
                                 "track": track})
        else:
            out["rejected"].append({"seller_key": res.seller_key, "reason": res.reason,
                                    "track": track, "code": res.code})

    # --- 1. Alpha 軌（實證：他比同儕便宜多少）------------------------------
    ranked = report.ranked() if hasattr(report, "ranked") else []
    for score_obj, _metrics in ranked:
        total = score_obj.total
        if total is None or total < params.auto_min_score:
            continue
        res = add_watch(
            store, score_obj.seller_key, source=SOURCE_AUTO,
            reason=f"自動入選：Seller Alpha {total:.1f} 分 ≥ 門檻 {params.auto_min_score:g}"
                   f"（{score_obj.reason}）",
            params=params, score=float(total), now=now,
        )
        _record(res, track="alpha", total=total)

    # --- 2. 供給軌（假設：他看起來值得盯）----------------------------------
    # 沒傳 supply 就完全不跑——既有呼叫端的行為一個字都不變。
    for fit in sorted(
        (supply or {}).values(),
        key=lambda f: (-(getattr(f, "total", None) or 0.0), getattr(f, "seller_key", "")),
    ):
        total = getattr(fit, "total", None)
        if not getattr(fit, "ok", False) or total is None:
            continue
        if total < params.supply_min_score:
            continue
        n_used = getattr(fit, "n_dimensions_used", 0)
        n_total = getattr(fit, "n_dimensions_total", 5)
        res = add_watch(
            store, fit.seller_key, source=SOURCE_SUPPLY,
            reason=(
                f"自動入選（供給軌）：供給契合 {total:.1f} 分 ≥ "
                f"門檻 {params.supply_min_score:g}（{n_used}/{n_total} 維度）"
                "——尚未有 Alpha 證據，盯著累積在架觀測"
            ),
            params=params, score=float(total), now=now,
        )
        _record(res, track="supply", total=total)
    return out


# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class RejectionDigest:
    """`sync_auto_watch` 的 rejected 清單 → 該印哪幾行。

    為什麼要這個東西（2026-08-05）：排程白天每 2 小時、晚上每 30 分跑一次，
    而過門檻的候選人本來就會多於名額——每輪逐個印 `[warn]` 會洗掉 50 行，
    **真正的告警會淹死在裡面**（洗版與靜默是同一個病的兩面：兩者都讓人看不見
    壞掉的那一行）。

    但也**不准直接吞掉**（CLAUDE.md 第五節）。所以拆成兩堆：

    - `summary_lines`：**預期內**的拒絕，摘要成一到兩行（總數＋最常見原因＋
      一個帶完整脈絡的代表例）。
    - `alert_lines`：**非預期**的拒絕，一個一行，全文不截斷。就算只有 1 個
      也要印——那是真的有東西壞了（例如賣家鍵格式錯誤代表上游組鍵組錯）。

    分類依據是 `code`（見 `EXPECTED_REJECT_CODES`），不是 `reason` 字串比對；
    沒有 code 或 code 不認得的一律歸非預期（預設偏吵）。
    """

    total: int = 0
    n_expected: int = 0
    n_unexpected: int = 0
    summary_lines: list[str] = field(default_factory=list)
    alert_lines: list[str] = field(default_factory=list)


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= _EXAMPLE_MAX_CHARS else text[: _EXAMPLE_MAX_CHARS - 1] + "…"


def summarize_rejections(rejected: list[dict[str, Any]] | None) -> RejectionDigest:
    """把 rejected 清單分成「摘要得起來的」與「必須逐個吼出來的」。

    純函式（不印任何東西、不碰 db），這樣「洗版有沒有被修掉」與「非預期有沒有
    被吞掉」兩件事都測得起來——log 行為本身很難測，把判斷抽出來就測得動。
    """
    rows = list(rejected or [])
    if not rows:
        return RejectionDigest()

    expected = [r for r in rows if str(r.get("code") or "") in EXPECTED_REJECT_CODES]
    unexpected = [r for r in rows if str(r.get("code") or "") not in EXPECTED_REJECT_CODES]

    counts: dict[str, int] = {}
    for r in rows:
        code = str(r.get("code") or "")
        counts[code] = counts.get(code, 0) + 1
    top_code, top_n = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    top_label = REJECT_LABEL.get(top_code, f"未分類原因（code={top_code!r}）")

    summary = [
        f"擋下 {len(rows)} 個未加入監控名單"
        f"（預期內 {len(expected)}、非預期 {len(unexpected)}）："
        f"最常見原因「{top_label}」{top_n} 個"
    ]
    if expected:
        ex = expected[0]
        summary.append(
            f"  例（{REJECT_LABEL.get(str(ex.get('code') or ''), '未分類')}）："
            f"{ex.get('seller_key')} — {_clip(ex.get('reason') or '')}"
        )

    alerts = [
        f"賣家 {r.get('seller_key')} 未能加入監控名單"
        f"（**非預期**，code={str(r.get('code') or '') or '無'}）：{r.get('reason')}"
        for r in unexpected
    ]
    return RejectionDigest(
        total=len(rows), n_expected=len(expected), n_unexpected=len(unexpected),
        summary_lines=summary, alert_lines=alerts,
    )


# ---------------------------------------------------------------------------
# 輪替
# ---------------------------------------------------------------------------
def _read_rotation(store: Any) -> dict[str, Any]:
    raw = store.get_meta(WATCH_ROTATION_META_KEY)
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def rotation_state(store: Any) -> dict[str, Any]:
    """目前的輪替狀態（dashboard／CLI 顯示用）。"""
    return _read_rotation(store)


def claim_batch(
    store: Any, params: WatchParams, *, now: datetime | None = None, force: bool = False
) -> tuple[int | None, str]:
    """這一輪要掃哪一批？**有副作用：認領成功會把狀態寫進 meta。**

    跨行程冪等的關鍵在「時間」而不是「輪數」：`scan` 除了每小時的排程，還可能
    被 dashboard 手動按。用輪數計時的話，手動按兩次就把輪替往前推兩小時，
    而每個賣家的「4 小時一次」保證會安靜地變成別的數字。

    回傳 `(批次或 None, 說明)`。說明一定要印得出來——「這輪為什麼沒掃賣家」
    必須永遠有一句話交代，安靜地跳過與安靜地壞掉外顯是一樣的。
    """
    if not params.enabled:
        return None, "監控掃描已關閉（seller_alpha.watch_enabled=false）"
    now = now or datetime.now(UTC)
    state = _read_rotation(store)
    last_at = state.get("claimed_at")
    age = _age_minutes(last_at, now)
    interval = params.batch_interval_minutes
    if age is not None and age < interval and not force:
        return None, (
            f"節流：距上次輪替 {age:.0f} 分（需 {interval:.0f} 分）"
            f"，上次掃的是第 {state.get('batch')} 批"
        )
    last_batch = state.get("batch")
    try:
        nxt = (int(last_batch) + 1) % params.batches if last_batch is not None else 0
    except (TypeError, ValueError):
        nxt = 0
    store.set_meta(
        WATCH_ROTATION_META_KEY,
        json.dumps({"batch": nxt, "claimed_at": now.isoformat()}, ensure_ascii=False),
    )
    note = f"第 {nxt} 批（共 {params.batches} 批，每 {interval:.0f} 分一批）"
    if force and age is not None and age < interval:
        note += f"；距上次只有 {age:.0f} 分，被 force 蓋過"
    return nxt, note


def due_sellers(
    store: Any, params: WatchParams, batch: int, *, now: datetime | None = None
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
    """這一批裡真的該掃的賣家 ＋ 被跳過的（含原因）。

    **pinned 列進每一批**（不只自己 `batch_of` 到的那批），而且排在回傳
    清單最前面。這就是「釘選優先權更高」的實作：其他軌 4 批輪一圈＝每
    240 分掃一次，pinned 每個輪替時段（60 分）都在場——快 4 倍，而且
    不用另開一條排程路徑。防暴衝靠的是下面同一條 per-seller 護欄：
    同一時段內已掃過就跳過，所以「每批都在場」不會變成「每批都重抓」。

    per-seller 的護欄只擋「同一個輪替時段內重複掃到同一個人」（例如 force 連按），
    門檻用 `batch_interval_minutes` 而不是 `per_seller_interval_minutes`：
    後者剛好等於輪替週期，兩個門檻貼在一起時，排程早跑幾秒就會整批被自己擋掉。

    **pinned 列的門檻要再乘 0.9 留餘裕**（2026-08-09 審查 F1）：pinned 的目標
    節奏（每批 60 分）與護欄門檻（60 分）零餘裕，而 mark 恆晚於 claim 幾秒
    （claim 在輪替開頭、掃完才 mark）。下一輪 claim 整整 60 分後到來時
    age ≈ 59.9 分 → 被跳過；pipeline 的 skip 路徑又會用 mark 重寫
    `last_scanned_at`，把資格再推走一整輪——掃描頻率就這樣靜默退化成兩輪
    一次。非 pinned 列維持原門檻：它們的掃描間隔 240 分對 60 分門檻餘裕充足。
    """
    now = now or datetime.now(UTC)
    batch_rows = store.list_seller_watch(active_only=True, batch=batch)
    # 釘選列從全量另撈再合併（去重）：它們不管 `batch_of` 落在哪一批，
    # 每一批都要在場。排最前面＝同一輪請求預算裡使用者指定的人先掃。
    pinned_rows = [
        r for r in store.list_seller_watch(active_only=True)
        if r.get("source") == SOURCE_PINNED
    ]
    pinned_keys = {r["seller_key"] for r in pinned_rows}
    rows = pinned_rows + [r for r in batch_rows if r["seller_key"] not in pinned_keys]
    due: list[dict[str, Any]] = []
    skipped: list[tuple[dict[str, Any], str]] = []
    for r in rows:
        age = _age_minutes(r.get("last_scanned_at"), now)
        # pinned 每批都在場，門檻乘 0.9 留餘裕（理由見 docstring 的 F1 時序）。
        guard = params.batch_interval_minutes * (
            0.9 if r.get("source") == SOURCE_PINNED else 1.0
        )
        if age is not None and age < guard:
            skipped.append((r, f"{age:.0f} 分鐘前才掃過（同一輪替時段內不重複掃）"))
            continue
        site = str(r.get("site") or "")
        if site not in SELLER_PAGE_SOURCE:
            skipped.append((
                r,
                UNSUPPORTED_SITE_NOTE.get(site, f"{site} 沒有賣家頁列舉實作"),
            ))
            continue
        due.append(r)
    return due, skipped


# ---------------------------------------------------------------------------
# 推播規則 3 的資料脈絡
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class SellerNotifyContext:
    """規則 3（監控名單賣家的新上架）判定所需的全部事實。

    **同儕折價直接沿用 `seller_alpha.analyze` 的逐筆結果**，不在推播端另算一份：
    第二棒證明「模型絕對值折價」會製造假 alpha，而防止那件事重演的唯一結構性
    做法就是讓折價只有一個算式、一個入口（工程原則 1）。
    """

    #: seller_key → seller_watch 列（只含 active）
    watch: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: seller_key → SellerScore（沒過門檻的賣家不會有）
    scores: dict[str, Any] = field(default_factory=dict)
    #: listing key → SellerItem（只含監控名單賣家的標的）
    items: dict[str, Any] = field(default_factory=dict)
    #: listing key → listing_obs 列（`seen_count` 決定「是不是新標的」）
    obs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def seller_of(self, site: str | None, seller_id: str | None) -> str | None:
        if not site or not seller_id:
            return None
        key = f"{site}:{seller_id}"
        return key if key in self.watch else None


def build_notify_context(
    store: Any, cfg: Any = None, *, index: Any = None, report: Any = None
) -> SellerNotifyContext:
    """跑一次 `seller_alpha.analyze`（不帶模型）並收成規則 3 要的形狀。

    不帶 `valuator` 是刻意的：模型絕對值在這條路徑上**完全用不到**
    （規則 3 的折價判準是同儕相對），少建一顆模型也少一次失敗點。
    """
    from .seller_alpha import analyze

    ctx = SellerNotifyContext()
    watch_rows = store.list_seller_watch(active_only=True)
    if not watch_rows:
        return ctx
    ctx.watch = {r["seller_key"]: r for r in watch_rows}
    rep = report if report is not None else analyze(store, cfg=cfg, index=index)
    ctx.scores = {k: s for k, s in rep.scores.items() if k in ctx.watch}
    for key in ctx.watch:
        metrics = rep.metrics.get(key)
        if metrics is None:
            continue
        for item in metrics.items:
            ctx.items[item.row.key] = item
    ctx.obs = {r["key"]: r for r in store.listing_obs(limit=50000)}
    return ctx


__all__ = [
    "EXPECTED_REJECT_CODES",
    "REJECT_LABEL",
    "REJECT_LIST_FULL",
    "REJECT_MALFORMED_KEY",
    "SELLER_PAGE_SOURCE",
    "SOURCE_AUTO",
    "SOURCE_LABEL",
    "SOURCE_MANUAL",
    "SOURCE_PINNED",
    "SOURCE_SUPPLY",
    "UNSUPPORTED_SITE_NOTE",
    "WATCH_ROTATION_META_KEY",
    "RejectionDigest",
    "SellerNotifyContext",
    "WatchAddResult",
    "WatchParams",
    "add_watch",
    "batch_of",
    "build_notify_context",
    "claim_batch",
    "due_sellers",
    "remove_watch",
    "rotation_state",
    "summarize_rejections",
    "sync_auto_watch",
]
