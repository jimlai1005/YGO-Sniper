"""賣家供給契合度（Supply Fit）——回答「這賣家值不值得盯」。

**這個分數與 Seller Alpha 是兩件事，永不相加、永不互相 fallback。**
Alpha 回答「他比市場便宜多少」（同儕相對，湊不到就拒答）；
Supply Fit 回答「盯著他，未來會不會產出可買的機會」（跟便宜與否無關）。
把兩者加總會讓「賣很多 PSA9 又固定賣 Vol 系列」的賣家看起來像有 alpha，
那是 CLAUDE.md 第四節（不要拿自己的模型當尺）的變種。

維度不可得時一律 score=None 並從權重分母移除，**不是給 0 分**——
0 分的語意是「這方面很差」，但事實是「我們不知道」。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ygo_sniper.seller_alpha import SellerMetrics


@dataclass(slots=True)
class SupplyDimension:
    """一個維度的量測結果。不可得時 `raw` 與 `score` 都是 None，不是 0。"""

    name: str
    available: bool
    raw: float | None = None
    score: float | None = None      # 0-100 站內百分位，Task 3 才填
    detail: str = ""
    missing: str = ""

    @classmethod
    def of(cls, name: str, *, raw: float, detail: str) -> SupplyDimension:
        if not detail:
            raise ValueError(f"維度 {name} 沒有帶依據字串——裸數字不准輸出")
        return cls(name=name, available=True, raw=raw, detail=detail)

    @classmethod
    def unavailable(cls, name: str, missing: str) -> SupplyDimension:
        if not missing:
            raise ValueError(f"維度 {name} 不可得，但沒說缺什麼")
        return cls(name=name, available=False, missing=missing)


#: 8-9 分是性價比帶的假設——PSA10 溢價通常很高、7 分以下品相風險上升，
#: 賣家常態性供應 8-9 分代表「不追頂級溢價、也不清倉低品相貨」。
#: **這是未經驗證的假設，不是已驗證的事實**，之後有校準資料要重看這個區間。
GRADE_PROFILE_SWEET_SPOT_MIN = 8.0
GRADE_PROFILE_SWEET_SPOT_MAX = 9.5


def dim_supply_depth(m: SellerMetrics) -> SupplyDimension:
    """這個賣家的供給規模——用累積觀測列數，不是「件/週」這種速率。

    速率在這個資料集上會產生垃圾數字：全域在架帳的觀測跨度中位數只有
    3.2 天，且大量賣家 `observation_span_days` 是 0——除以一個接近 0 的
    跨度，任何微小樣本都會爆出離譜的高速率。累積量沒有這個除零風險，
    且與「盯這個賣家未來會不會持續有貨」的問題更直接對應。恆可得
    （即使一列觀測都沒有，0 本身也是有意義的答案）。

    **`n_rows` 的組成隨站台而異，這個數字只能站內比，不能跨站比**：
    buyee_yahoo / paypay 的列多半來自挖回來的成交帳（觀測跨度可達
    182 天），eBay 的列則幾乎只有在架（跨度僅 3.2 天）。同一個 117
    在兩站背後的供給密度完全不是一回事。跨站可比性由 Task 3 的
    站內百分位負責，這裡不處理。

    （這裡原本拆成成交筆數與觀測列數兩個維度，但實測 corr(n_sold,
    n_rows)=0.989、87% 的賣家兩者相等——多數賣家只出現在成交帳，
    從未被看到在架，`n_rows == n_sold`。當成兩個獨立維度等於把同一個
    訊號算兩次，見 CLAUDE.md 第三節，故合併為這個單一維度。）
    """
    return SupplyDimension.of(
        "supply_depth",
        raw=float(m.n_rows),
        detail=(
            f"累積觀測 {m.n_rows} 列（跨度 {m.observation_span_days:.1f} 天"
            "——這是累積量不是速率）"
        ),
    )


def dim_persistence(m: SellerMetrics) -> SupplyDimension:
    """這個賣家是不是持續在賣，還是我們只是恰好瞄到一個時間點。

    `dim_supply_depth` 量的是「多少」（累積列數），這個維度量的是
    「多久」（觀測跨度天數）——實測兩者相關性只有 0.52（相較於被合併掉的
    n_sold/n_rows 的 0.989），是真正獨立的資訊。「持續經營 vs
    一次性清倉」的語意本來就是時間維度的事，筆數再多也回答不了這個問題
    （清一次倉也可以留下上百列觀測）。

    可以直接信任 `m.observation_span_days`：`SellerMetrics` 在計算跨度時
    已經排除了入庫時間戳偽裝的「觀測」（`n_fake_timestamps`，見
    `seller_alpha.py` 建構 SellerMetrics 處的註解），這裡不需要再過濾一次。

    跨度為 0 代表我們只在單一時間點看過這個賣家，談不上「持續」，
    視為不可得而不是給 0 分（0 分會暗示「他不持續」，但事實是「不知道」）。
    """
    if m.observation_span_days <= 0:
        return SupplyDimension.unavailable(
            "persistence",
            "只觀測到單一時間點（跨度 0 天），談不了持續性——"
            "缺的是這個賣家在不同時間再被觀測到",
        )
    return SupplyDimension.of(
        "persistence",
        raw=float(m.observation_span_days),
        detail=f"觀測跨度 {m.observation_span_days:.1f} 天（{m.n_rows} 列）",
    )


def dim_grade_profile(m: SellerMetrics) -> SupplyDimension:
    """賣家的分數分布是否落在 8-9 分「性價比帶」——不是只認 PSA。

    `grade_mix` 的 key 是「機構 空格 分數」（實測含 `PSA 8`、`ARS 9`、
    `ARS 10` 等），ARS 佔比在部分賣家極高（實測有賣家 121 筆全是
    ARS 10），只認 PSA 會漏掉大半市場，所以這裡用 `rsplit` 取出分數
    字串、不管機構是誰。無法解析的 key（例如機構縮寫不含空格分隔分數）
    計入分母但不計入分子，且在 `detail` 裡講出有幾筆解析不了——
    静默丟掉會讓分母失真而不被發現。
    """
    total = 0
    in_band = 0
    unparseable = 0
    for key, count in m.grade_mix.items():
        total += count
        try:
            grade = float(key.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            unparseable += count
            continue
        if GRADE_PROFILE_SWEET_SPOT_MIN <= grade <= GRADE_PROFILE_SWEET_SPOT_MAX:
            in_band += count

    if total < 3:
        return SupplyDimension.unavailable(
            "grade_profile", f"樣本數 {total} 筆，不足 3 筆最低門檻"
        )

    detail = f"8-9 分佔 {in_band}/{total}"
    if unparseable:
        detail += f"（另有 {unparseable} 筆分數無法解析，計入分母未計入分子）"
    return SupplyDimension.of(
        "grade_profile", raw=in_band / total, detail=detail
    )


def dim_series_focus(m: SellerMetrics) -> SupplyDimension:
    """賣家是否固定專攻某個系列——用 `series_top1_share`，門檻 3 筆已知系列。

    兩筆全同系列不能叫「固定賣某系列」（樣本太小，任何賣家都可能巧合
    連續兩筆同系列），所以門檻是 `series_known_n >= 3`；`series_top1_share`
    為 `None`（系列從未被辨識出）也視為不可得。
    """
    if m.series_top1_share is None or m.series_known_n < 3:
        return SupplyDimension.unavailable(
            "series_focus",
            f"已知系列樣本數 {m.series_known_n} 筆，不足 3 筆最低門檻，"
            "或系列從未被辨識出",
        )
    return SupplyDimension.of(
        "series_focus",
        raw=m.series_top1_share,
        detail=(
            f"最大系列佔比 {m.series_top1_share:.0%}"
            f"（已知系列樣本 {m.series_known_n} 筆）"
        ),
    )


def dim_listing_rhythm(m: SellerMetrics) -> SupplyDimension:
    """賣家上架時段是否集中——衡量的是「上架時間可預測性」，不是賣家好壞。

    上架時段越集中，代表我們越能在他上架前後排掃描去堵他，跟這個賣家
    「好不好」無關（一個上架時段隨機的優質賣家一樣值得盯，只是比較難堵）。
    門檻是總觀測數 `>= 5`，樣本太少時任何時段分布都可能只是巧合集中。
    """
    total = sum(m.listing_hour_hist.values())
    if total < 5:
        return SupplyDimension.unavailable(
            "listing_rhythm", f"時段觀測數 {total} 筆，不足 5 筆最低門檻"
        )
    top1 = max(m.listing_hour_hist.values())
    return SupplyDimension.of(
        "listing_rhythm",
        raw=top1 / total,
        detail=f"最大時段佔比 {top1 / total:.0%}（{top1}/{total} 筆）",
    )
