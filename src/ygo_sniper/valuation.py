"""估價模型：這張卡的公允價是多少，以及我對這個數字有多沒把握。

**要解的問題**：過濾後只有約 170 筆成交、159 個桶，平均每桶 1.07 筆。
「同卡同稀有度同分數取中位數」這種傳統做法幾乎永遠湊不到樣本——
真正的稀缺資源不是資料，是「敢不敢在只有 1 筆樣本時說話」。

三層退化階梯 ＋ 部分池化（經驗貝氏收縮）：

    L1  卡名 × 稀有度 × 分數      最精確，通常 0-1 筆
    L2  卡名 × 稀有度            用全域分數溢價曲線把其他分數換算過來
    L3  稀有度 × 分數            分層先驗
    L0  全域                     稀有度效應 + 分數效應 + 平台效應 + 截距

    估計值(本層) = w × 本層中位 + (1−w) × 上層估計，   w = n / (n + k)

k 預設 4：本層要有 4 筆才跟上層的先驗平起平坐。n=0 時 w=0，完全用上層；
n 很大時 w→1，幾乎只看本層。這正是「樣本少就往先驗靠」的量化版本。

全部在**對數價格空間**運算。價格是乘性的（レリーフ 是 ノーマル 的 4.7 倍、
PSA10 是 PSA9 的 3.95 倍），在原空間做加法會讓便宜卡的誤差被貴卡淹掉。

── 第四個維度：平台（venue）──────────────────────────────────────
2026-08-01 實測：控制稀有度與分數之後，**同規格商品在不同平台的成交價中位
比值 Mercari / Yahoo = 2.17 倍**（14 個可比分層的中位；極端分層到 7.18 倍）。
原因是市場結構不同，不是資料錯誤：

    buyee_yahoo    競價拍賣 → 成交價是市場**出清價**
    buyee_mercari  定價出售 → 成交價是賣家開的**零售價**
    buyee_paypay   定價出售，同上

把三個平台丟進同一個池子取中位數，等於拿一個「混合平台」的數字去評估一個
特定平台的標的——這是工程原則 1 的混源比較。所以估計時必須指定**目標平台**：
評估 Mercari 標的就換算到 Mercari 的價格水準。

平台係數與 rarity/grade 曲線同一套做法（同分層內比較 ＋ 收縮），
`venue=None` 時**完全不做平台校正**（維持舊行為），並在 `Estimate` 標記
`venue_adjusted=False`，讓上層看得見自己拿到的是混合平台的數字。

── 誠實聲明（conformal 區間的前提，請不要略過）──────────────────────
80% 區間用 split conformal：留出一半樣本當校準集，量「模型預測」與「實際成交」
的殘差分位數。它的覆蓋保證前提是**資料可交換（exchangeable）**，而這份資料
兩個前提都不完全成立：

  1. 市場會漂移。這批 comps 是同一天抓的一批成交，未來的成交價分布會變。
  2. 選擇偏差。只有「賣得掉」的標的才有成交價；開價過高流標的那些
     永遠不會進資料庫，所以樣本天生偏向「賣得掉的價格」。

結論：**實際覆蓋率會低於名目 80%**，這個區間應該當成「量級參考」而不是
「機率保證」。`ygo-sniper value --coverage` 會用留出法實測當下的真實覆蓋率，
數字直接印給你看，不要只信 0.80 這個標籤。
校準集不足（預設 < 50 筆）時**不給區間**——寧可說不知道。

⚠️ `coverage_check()` 是**隨機留出**：它假設可交換性，所以量到的數字是
「這個程序在可交換世界裡準不準」，看不到時間漂移與平台組成變化。
真正的考題是 `coverage_time_split()`：拿較早的成交當訓練＋校準、較晚的
成交當測試，那才是「今天的模型評估明天的標的」的實際處境。兩個數字都要看，
差距本身就是漂移的量測值。

── 第五個維度：Mondrian（群組條件）conformal ────────────────────────
vanilla split conformal 只保證**邊際覆蓋率**（所有預測平均 80%），不保證
**條件覆蓋率**（每個子群各自 80%）。症狀是全庫每一筆估計拿到的區間寬度
比例完全相同——一筆 L1 的估計與一筆 L3 的估計，下緣／點估計都是 0.289。

2026-08-02 實測（`coverage_by_group()`，時間切分、平台內切早晚、3 個
test_fraction 合計 1006 筆測試樣本，走競標上限實際用的 venue-aware 路徑）：

    n 分桶     測試筆數   vanilla 覆蓋   Mondrian 覆蓋   vanilla 下尾   Mondrian 下尾
    n<3          436        86%           90%            2%            2%
    3-9          215        84%           88%            6%            4%
    10-49         56        48%           54%           39%           38%
    n>=50        299        74%           75%            9%            6%
    總計        1006        80%           83%            7%            5%

「下尾」＝真實成交價低於區間下緣的比例（名目 10%）。**這一欄才是會賠錢的
那一欄**：出價上限完全由下緣反推，下尾違反＝上限開得比市場實際成交還高。

⚠️ **實測推翻了一個很自然的直覺，請不要照直覺改回去**：低 n 的估計並沒有
比較不準，反而是**最準**的一群（下尾違反 2%，比名目 10% 還保守）。原因是
`n_effective` 在本模型是「**所用層級的池大小**」而不是證據強度——
n=1 通常代表 L1（找到同一張卡的成交），n=325 代表 L3（根本沒比對到卡名，
退到整個稀有度的池）。所以 n_effective 與誤差**反向**相關（同一次實測，
逐層級）：

    層級   測試筆數   點估計中位誤差   下尾 vanilla → Mondrian
    L1       486        ×1.77            5%  →  3%
    L2       177        ×1.99            2%  →  2%
    L3       343        ×2.25           13%  → 10%

拿 `n_effective` 當「證據夠不夠」的閘門會**砍掉最好的估計、留下最差的**。
真正分得開好壞的維度是**層級**（有沒有這張卡自己的成交），
`bidding.EvidenceGate` 的主閘門就是照這張表訂的。

分群鍵因此是 **層級 × n 分桶**，合併階梯是 `L?/n桶 → L? → 全域`，
每一階要 `min_group_calibration`（預設 30）筆才採用，退化情形一律寫進
`Estimate.calibration_group` / `calibration_degraded`（不靜默）。

**下緣安全夾（刻意不是純 Mondrian）**：群組分位數只在讓下緣**更低**（區間
更寬、上限更保守）時採用；想讓下緣更高的群組一律夾回全域值。理由是成本
不對稱，而且這個不對稱是 bidding.py 自己宣告的設計前提——「買不到的成本
是零，所以唯一要守的事是上限不能算錯」。純 Mondrian 會讓 n<3 那群的下緣
抬高約 1.55 倍（＝上限一次調高五成），而支撐它的只有一份資料集的校準；
往寬的方向錯是輸掉競標，往窄的方向錯是真的付錢。

**已診斷、修不好、改成拒絕輸出**：`10-49` 那一桶的下尾違反率 29-38%，
六種分群鍵全部沒有改善。2026-08-02 用 `coverage_by_group(diagnose=True)` 拆開
它的組成，答案很清楚（資料，不是推測；1104 筆測試樣本裡這一桶佔 69 筆）：

    層級：L3 50、L1 14、L2 5          分數：10 佔 52
    稀有度：rare 23、normal 21        平台：Yahoo 37、Mercari 21、PayPay 11

    (層級 × 平台) 子切片        筆數   下尾違反   覆蓋   點估計中位偏誤
    L3 × Mercari                19      100%      0%     ×0.17
    L3 × Yahoo                  27        0%     89%     ×1.05
    L1 × Yahoo                   8        0%     25%     ×4.29

**整桶的 29% 幾乎全部由 `L3 × Mercari` 那 19 筆貢獻，而它是 100% 全滅**。
原因不在分位數選錯群，在點估計本身：平台係數（該切分 Mercari ×3.45）是在
高價卡上估出來的，套到 normal／rare 這種便宜卡完全不成立——實測那些卡在
Mercari 與 Yahoo 是同一個價位（NT$1.1k-1.8k vs NT$1.5k-2.7k），模型卻把它們
乘上 3.45 倍，於是高估 6-8 倍。校準集裡沒有這個切片（訓練期沒有這批成交），
所以**任何**重新分群都救不了它：資訊根本不在校準殘差裡。

修法因此不是調分群，是 `bidding.EvidenceGate.reject_n_buckets`——落在這一桶
的估計**直接不給出價上限**（與 `require_known_grade` 同一個哲學）。估價本身
仍然照給並在 `Estimate.notes` 標註，因為「這個量級大概多少」還是有用的，
只是不夠格拿去下真錢的單。真正的修法是平台係數要分價位帶，那是另一個決定。
"""

from __future__ import annotations

import hashlib
import math
import random
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any

from .cards import CardIndex

#: 分數溢價曲線的基準分數。所有 grade_delta 都是「相對 9」的對數乘數。
BASELINE_GRADE = 9.0

_LEVEL_LABEL = {
    "L1": "卡名×稀有度×分數",
    "L2": "同卡跨分數換算",
    "L3": "稀有度×分數",
    "L0": "全域先驗",
}

#: 平台的人話名稱。**括號裡的市場結構是重點**，不是裝飾：
#: 「這個價格是拍賣出清價還是賣家開的零售價」才是兩倍價差的原因。
VENUE_LABEL = {
    "buyee_yahoo": "Yahoo 拍賣（競價）",
    "buyee_mercari": "Mercari（定價）",
    "buyee_paypay": "PayPay（定價）",
    "mercari_tw": "Mercari 台灣（定價・標新台幣）",
}


def venue_label(venue: str | None) -> str:
    return VENUE_LABEL.get(venue or "", venue or "未知平台")


# ---------------------------------------------------------------------------
# Mondrian 分群（群組條件 conformal）
# ---------------------------------------------------------------------------
#: 有效樣本數的分桶邊界（上界, 標籤）。`None` = 無上界。
#: 桶邊選在 3／10／50：3 是 `min_cell`（一個格要幾筆才採信）、
#: 10 是收縮權重 w=n/(n+4) 越過 0.7 的地方（本層開始主導先驗）、
#: 50 是「已經是整個稀有度的池」的量級。不是等距切分，是照模型自己的門檻切。
N_BUCKETS: tuple[tuple[int | None, str], ...] = (
    (3, "n<3"), (10, "3-9"), (50, "10-49"), (None, "n>=50"),
)

#: 退化到全域分位數時的群組名。會直接顯示給使用者看，所以是人話不是代號。
GLOBAL_GROUP = "全域（未分群）"


def n_bucket(n: int) -> str:
    for upper, label in N_BUCKETS:
        if upper is None or n < upper:
            return label
    return N_BUCKETS[-1][1]


def group_key(level: str | None, n: int) -> str:
    """分群鍵 = 估計層級 × 有效樣本數分桶。"""
    return f"{level or 'L0'}/{n_bucket(n)}"


def group_ladder(level: str | None, n: int) -> tuple[str, ...]:
    """合併階梯：最細的群 → 同層級全部 → （呼叫端再退到全域）。

    只往「同一個層級」合併，不往相鄰 n 桶合併：實測 L1 與 L3 的殘差分布差別
    （中位 |殘差| 0.53 vs 0.81）遠大於同層級內不同 n 桶的差別，
    跨層級合併等於把兩個不同的誤差分布混在一起——那正是要修的問題本身。
    """
    return (group_key(level, n), level or "L0")


@dataclass(slots=True, frozen=True)
class GroupQuantiles:
    """一組（可能經過退化的）殘差分位數，**連同它的來歷**。

    分位數本身沒有意義，「這個分位數是幾筆什麼樣的樣本算出來的」才有。
    所以來歷跟數字綁在同一個結構裡，不准只傳 (lo, hi) 出去。
    """

    lo: float
    hi: float
    #: 實際採用的群組（可能是合併後的層級，或 `GLOBAL_GROUP`）
    group: str
    #: 採用的那一群有幾筆校準殘差
    group_n: int
    #: 原本想用的那一群（最細的那一層）
    requested_group: str
    #: True ＝ 最細的群樣本不足，用了合併／全域的分位數
    degraded: bool
    #: True ＝ 群組下緣比全域窄，被安全夾夾回全域值（見模組頂註）
    floor_applied: bool


# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Params:
    confidence: float = 0.80
    shrink_k: float = 4.0
    min_calibration: int = 50
    calibration_fraction: float = 0.4
    min_cell: int = 3
    #: Mondrian 分群的每群最低校準筆數。低於此就往合併階梯的下一階退。
    min_group_calibration: int = 30
    #: False = 完全關掉分群，退回 vanilla split conformal（全域單一分位數）。
    group_conformal: bool = True
    grade_premium_prior: dict[float, float] = field(default_factory=dict)
    rarity_premium_prior: dict[str, float] = field(default_factory=dict)
    #: 各平台的相對價格水準（任選一個平台當 1.0，實際只用比值）。
    venue_premium_prior: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Any) -> Params:
        v = dict(getattr(cfg, "valuation", None) or {})
        return cls(
            confidence=float(v.get("confidence", 0.80)),
            shrink_k=float(v.get("shrink_k", 4)),
            min_calibration=int(v.get("min_calibration", 50)),
            calibration_fraction=float(v.get("calibration_fraction", 0.4)),
            min_cell=int(v.get("min_cell", 3)),
            min_group_calibration=int(v.get("min_group_calibration", 30)),
            group_conformal=bool(v.get("group_conformal", True)),
            grade_premium_prior={
                float(k): float(x) for k, x in (v.get("grade_premium_prior") or {}).items()
            },
            rarity_premium_prior={
                str(k): float(x) for k, x in (v.get("rarity_premium_prior") or {}).items()
            },
            venue_premium_prior={
                str(k): float(x) for k, x in (v.get("venue_premium_prior") or {}).items()
            },
        )


@dataclass(slots=True, frozen=True)
class Obs:
    """一筆成交樣本。`key` 只用來做穩定的 fit／calibration 切分。

    `venue` 是成交平台（comps.site：buyee_yahoo / buyee_mercari / buyee_paypay）。
    `sold_at` 是成交時間（ISO 字串），只給時間切分的覆蓋率自檢用——
    模型本身不看時間（視窗過濾是 store 那一層的決定）。
    `grader` **不是**模型的維度（沒有 grader 係數），它只在估平台係數時當
    分層變數用：PSA9 與 ARS9 是兩個不同的價格水準，而各平台的機構組成不同，
    不分層就會把「賣的東西不一樣」算成平台溢價。
    """

    price_twd: float
    card_name: str | None
    rarity: str | None
    grade: float | None
    key: str = ""
    venue: str | None = None
    sold_at: str | None = None
    grader: str | None = None

    @property
    def logp(self) -> float:
        return math.log(self.price_twd)


@dataclass(slots=True)
class Estimate:
    """一次估價的完整答案。**點估計沒有附上層級與樣本數就是誤導**——
    L1 的 4,500 與 L0 的 4,500 是完全不同的兩個宣稱。"""

    fair_twd: float | None
    level: str | None
    level_label: str
    n_effective: int
    lo_twd: float | None = None
    hi_twd: float | None = None
    confidence: float = 0.80
    calibration_n: int = 0
    p_worth_buying: float | None = None
    card_name: str | None = None
    #: 這個估計是「哪一個分數」的。**None 不等於「不重要」**——模型對未知分數的
    #: 處理是當成基準分數 9（`ValuationModel.g(None)` 回 0），而分數溢價橫跨
    #: 11 倍（7 分 ×0.35 到 10 分 ×3.95）。所以這一欄必須跟數字一起傳到下游，
    #: 讓出價那一側看得見「這個公允價其實建立在一個沒說出口的假設上」。
    grade: float | None = None
    #: 這個分數是**從哪裡來的**："title" / "description" / None。
    #: 描述補抓來的分數可信度低於標題（賣家自由文字、還常夾 SEO 關鍵字堆），
    #: 而分數直接乘進公允價——所以來源必須跟數字一起走到出價那一側，
    #: 讓 `bidding.evidence_tier` 能把它降級（見 parsers/grade.resolve_grade）。
    grade_source: str | None = None
    #: 這個數字是「哪一個平台的價格水準」。None ＝ 沒指定目標平台。
    venue: str | None = None
    #: False ＝ 完全沒做平台校正，數字是混合平台水準（Yahoo 與 Mercari 差 2 倍以上）。
    venue_adjusted: bool = False
    #: 平台係數是資料估的（True）還是先驗／無資料（False）。未指定平台時為 None。
    venue_is_estimated: bool | None = None
    # -- Mondrian 分群校準的來歷（沒有這幾欄，區間寬度是「哪來的」就看不見）--
    #: 這個區間實際用的校準群（可能是合併後的層級，或 `GLOBAL_GROUP`）。
    #: None ＝ 沒有區間，或分群被關掉。
    #:
    #: ⚠️ **這裡的層級可能跟上面的 `level` 不一樣，那不是 bug**：`level` 與
    #: `n_effective` 描述的是**點估計**（全量樣本擬合），這一欄描述的是
    #: **區間寬度**（只看 fit 半邊的校準模型）。校準模型少了四成樣本，
    #: 同一張卡可能在它眼裡退一層——結果是區間偏寬（保守），不是偏窄。
    calibration_group: str | None = None
    #: 上面那一群有幾筆校準殘差。這是「這個區間寬度背後有幾筆成交」的答案。
    calibration_group_n: int = 0
    #: 原本該用的最細群（層級 × n 桶，由校準模型判定）。
    #: 與 `calibration_group` 不同就代表退化過。
    calibration_group_requested: str | None = None
    #: True ＝ 該群校準樣本不足，退到合併／全域分位數。**絕不靜默**。
    calibration_degraded: bool = False
    #: True ＝ 群組下緣比全域窄，被安全夾夾回全域（區間下緣採較保守的那個）。
    interval_floor_applied: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def has_interval(self) -> bool:
        return self.lo_twd is not None and self.hi_twd is not None

    @property
    def has_card_specific_evidence(self) -> bool:
        """有沒有「這張卡自己」的成交撐著（L1／L2），而不是整個稀有度的池。

        L3／L0 的點估計是「這個稀有度＋分數的典型價」，模型**根本不知道這是哪張卡**。
        實測中位誤差 ×2.24（L1 是 ×1.70），而且混合平台路徑的下尾違反率 13-15%
        （名目 10%）——這是出價上限唯一一道以層級為準的閘門的依據。
        """
        return self.level in ("L1", "L2")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
def _median(xs: list[float]) -> float:
    return statistics.median(xs)


def _shrink(layer_value: float, parent: float, n: int, k: float) -> float:
    """部分池化。n=0 → 完全用上層；k=0 → 完全不收縮（只要本層有樣本就全信）。"""
    if n <= 0:
        return parent
    w = n / (n + k)
    return w * layer_value + (1 - w) * parent


class ValuationModel:
    """從成交樣本學出來的加法對數模型 + 退化階梯。

    只用中位數，不用平均：二手卡價右尾極長（同一張卡 PSA10 可以是 PSA9 的
    四倍），一筆離群值就能把平均拉到沒有意義的地方。
    """

    def __init__(self, rows: list[Obs], params: Params) -> None:
        self.params = params
        self.rows = [r for r in rows if r.price_twd > 0]
        self.grade_delta: dict[float | None, float] = {}
        self.rarity_delta: dict[str | None, float] = {}
        self.venue_delta: dict[str, float] = {}
        #: 每個係數有多少是「資料說的」（1.0 = 全部，0.0 = 全靠先驗）。
        #: 係數本身也吃收縮：一個 3 筆樣本的格算出來的中位數不該直接推翻先驗。
        self.grade_weight: dict[float, float] = {}
        self.rarity_weight: dict[str | None, float] = {}
        self.venue_weight: dict[str, float] = {}
        #: 平台係數的參照點（樣本最多的平台，係數恆為 0）。None ＝ 樣本沒有平台資訊。
        self.baseline_venue: str | None = None
        #: 兩個截距，**刻意分開**：`base` 是混合平台基準（venue=None 的路徑用），
        #: `base_venue` 是基準平台基準（有指定平台時用）。同一次估價只會用其中一個，
        #: 而且該路徑的每一項都在同一個基準上算——不混用就不會有混源比較。
        self.base = 0.0
        self.base_venue = 0.0
        self._fit()

    def grade_is_estimated(self, grade: float | None) -> bool:
        """係數以資料為主（w ≥ 0.5）才算「估的」。基準分數本身不需要係數。"""
        if grade is None or grade == BASELINE_GRADE:
            return True
        return self.grade_weight.get(grade, 0.0) >= 0.5

    def rarity_is_estimated(self, rarity: str | None) -> bool:
        return self.rarity_weight.get(rarity, 0.0) >= 0.5

    def venue_is_estimated(self, venue: str | None) -> bool:
        """基準平台不需要係數；其餘看收縮權重。未知平台一律 False（係數 0，等於沒校正）。"""
        if venue is None:
            return False
        if venue == self.baseline_venue:
            return True
        return self.venue_weight.get(venue, 0.0) >= 0.5

    # -- 擬合 ----------------------------------------------------------
    def _fit(self) -> None:
        """順序是刻意的，而且分數曲線要跑兩輪（backfit）。

        第一輪的分數曲線是 venue-blind 的；但同一個 (稀有度, 分數) 格裡的
        平台組成會不同（例如 PSA10 的樣本多半來自 Mercari、PSA9 多半來自
        Yahoo），那會把平台溢價算進分數溢價裡。所以先用「分層內比較」估出
        平台係數（這一步只需要分層，不需要任何係數），再把平台效應扣掉重估
        分數曲線與稀有度曲線。

        沒有任何樣本帶平台資訊時，第二輪與第一輪完全等價（venue 係數全空），
        數字一模一樣——這保證了「沒有平台資料」的舊行為不變。
        """
        self._fit_grade_curve()
        self._fit_venue_curve()
        if self.venue_delta:
            self._fit_grade_curve()      # backfit：扣掉平台效應後重估分數溢價
        self._fit_rarity_curve()
        if self.rows:
            self.base = _median([self._residual_base(r) for r in self.rows])
            venued = [r for r in self.rows if r.venue]
            self.base_venue = (
                _median([self._residual_base(r) - self.v(r.venue) for r in venued])
                if venued else self.base
            )

    def _residual_base(self, r: Obs) -> float:
        return r.logp - self.g(r.grade) - self.r(r.rarity)

    def _fit_grade_curve(self) -> None:
        """全域分數溢價曲線：在**同一個稀有度分層之內**比較不同分數。

        跨稀有度直接比分數是混源比較（工程原則 1）——PSA10 樣本裡稀有度
        本來就偏高，不分層會把稀有度溢價算進分數溢價裡。平台同理，所以
        比較的是**扣掉平台效應後**的對數價（第一輪時平台係數全是 0）。

        可重入：每次呼叫都先清空自己的輸出，backfit 第二輪不會疊到第一輪上。
        """
        p = self.params
        self.grade_delta = {}
        self.grade_weight = {}
        cells: dict[tuple[str | None, float], list[float]] = {}
        for r in self.rows:
            if r.grade is None:
                continue
            cells.setdefault((r.rarity, r.grade), []).append(r.logp - self.v(r.venue))

        deltas: dict[float, list[float]] = {}
        support: dict[float, int] = {}
        for (rarity, grade), vals in cells.items():
            baseline = cells.get((rarity, BASELINE_GRADE))
            if grade == BASELINE_GRADE or len(vals) < p.min_cell:
                continue
            if not baseline or len(baseline) < p.min_cell:
                continue
            deltas.setdefault(grade, []).append(_median(vals) - _median(baseline))
            support[grade] = support.get(grade, 0) + min(len(vals), len(baseline))

        prior = p.grade_premium_prior
        grades = set(deltas) | {float(g) for g in prior} | {
            r.grade for r in self.rows if r.grade is not None
        }
        for g in grades:
            if g == BASELINE_GRADE:
                self.grade_delta[g] = 0.0
                self.grade_weight[g] = 1.0
                continue
            prior_delta = (
                math.log(prior[g]) if g in prior and prior[g] > 0
                # 沒有先驗：從最近的先驗點做 log-linear 外插
                else self._extrapolate_grade(g)
            )
            n = support.get(g, 0)
            if deltas.get(g):
                self.grade_delta[g] = _shrink(_median(deltas[g]), prior_delta, n, p.shrink_k)
            else:
                self.grade_delta[g] = prior_delta
            self.grade_weight[g] = n / (n + p.shrink_k) if n else 0.0

    def _extrapolate_grade(self, grade: float) -> float:
        prior = self.params.grade_premium_prior
        if not prior:
            return 0.0
        nearest = min(prior, key=lambda g: abs(g - grade))
        step = math.log(prior[nearest] / prior.get(BASELINE_GRADE, 1.0) or 1.0)
        span = nearest - BASELINE_GRADE
        per = step / span if span else 0.0
        return per * (grade - BASELINE_GRADE)

    def _fit_rarity_curve(self) -> None:
        """稀有度溢價：先扣掉分數效應再比，基準是**全體中位數**。

        先驗（相對 normal）要換算到同一個基準才能跟估計值混用，否則
        「用估計的那幾個」跟「用先驗的那幾個」會落在兩條不同的水平線上。

        可重入：與 `_fit_grade_curve` 同理，先清空自己的輸出。
        """
        p = self.params
        self.rarity_delta = {}
        self.rarity_weight = {}
        if not self.rows:
            return
        adj: dict[str | None, list[float]] = {}
        for r in self.rows:
            adj.setdefault(r.rarity, []).append(r.logp - self.g(r.grade) - self.v(r.venue))
        global_adj = _median([v for vals in adj.values() for v in vals])

        prior = p.rarity_premium_prior
        # 先驗錨點：只看「本庫實際出現過的稀有度」，讓先驗與 global_adj 同基準
        observed_prior = [
            math.log(prior[r]) for r in adj if r in prior and prior[r] > 0
        ]
        anchor = _median(observed_prior) if observed_prior else 0.0

        keys = set(adj) | set(prior)
        for r in keys:
            vals = adj.get(r, [])
            prior_delta = math.log(prior[r]) - anchor if r in prior and prior[r] > 0 else 0.0
            n = len(vals) if len(vals) >= p.min_cell else 0
            if n:
                self.rarity_delta[r] = _shrink(
                    _median(vals) - global_adj, prior_delta, n, p.shrink_k
                )
            else:
                self.rarity_delta[r] = prior_delta
            self.rarity_weight[r] = n / (n + p.shrink_k) if n else 0.0

    def _fit_venue_curve(self) -> None:
        """平台價格水準：在**同一個 (稀有度, 機構, 分數) 分層之內**比較不同平台。

        分層是必要的，不是講究：Yahoo 的樣本裡低稀有度／低分數本來就偏多，
        直接比兩個平台的整體中位數會把「賣的東西不一樣」算成「平台溢價」。
        機構也要分層（雖然模型其他地方不用 grader）：PSA9 與 ARS9 是兩個價格
        水準，而 Yahoo 的 PSA 佔比明顯高於 Mercari。實測差別不小——
        只分 (稀有度, 分數) 時 Mercari/Yahoo 量到 2.54x，加上機構分層是 2.11x，
        後者才對得上主對話獨立量到的 2.17x（14 個可比分層的中位）。

        參照平台 = 樣本最多的那個（係數恆為 0），字典序 tie-break 讓它決定性。
        係數同樣吃收縮：一個只在兩三個分層出現過的平台不該直接推翻先驗。
        先驗是各平台的相對價格水準（`venue_premium_prior`），沒設就等於
        「先驗上假設沒有平台差異」——那是保守方向（不校正）。
        """
        p = self.params
        self.venue_delta = {}
        self.venue_weight = {}
        counts: dict[str, int] = {}
        for r in self.rows:
            if r.venue:
                counts[r.venue] = counts.get(r.venue, 0) + 1
        if not counts:
            return
        self.baseline_venue = max(sorted(counts), key=lambda name: counts[name])

        cells: dict[tuple[str | None, str | None, float | None, str], list[float]] = {}
        for r in self.rows:
            if r.venue:
                cells.setdefault((r.rarity, r.grader, r.grade, r.venue), []).append(r.logp)

        deltas: dict[str, list[float]] = {}
        support: dict[str, int] = {}
        for (rarity, grader, grade, venue), vals in cells.items():
            if venue == self.baseline_venue or len(vals) < p.min_cell:
                continue
            ref = cells.get((rarity, grader, grade, self.baseline_venue))
            if not ref or len(ref) < p.min_cell:
                continue
            deltas.setdefault(venue, []).append(_median(vals) - _median(ref))
            support[venue] = support.get(venue, 0) + min(len(vals), len(ref))

        prior = p.venue_premium_prior
        ref_prior = prior.get(self.baseline_venue or "", 0.0)
        for venue in set(counts) | set(prior):
            if venue == self.baseline_venue:
                self.venue_delta[venue] = 0.0
                self.venue_weight[venue] = 1.0
                continue
            prior_delta = (
                math.log(prior[venue] / ref_prior)
                if prior.get(venue, 0.0) > 0 and ref_prior > 0
                else 0.0
            )
            n = support.get(venue, 0)
            if deltas.get(venue):
                self.venue_delta[venue] = _shrink(
                    _median(deltas[venue]), prior_delta, n, p.shrink_k
                )
            else:
                self.venue_delta[venue] = prior_delta
            self.venue_weight[venue] = n / (n + p.shrink_k) if n else 0.0

    # -- 係數存取 ------------------------------------------------------
    def g(self, grade: float | None) -> float:
        if grade is None:
            return 0.0          # 分數不明 → 當成基準分數，不猜
        if grade not in self.grade_delta:
            self.grade_delta[grade] = self._extrapolate_grade(grade)
        return self.grade_delta[grade]

    def r(self, rarity: str | None) -> float:
        return self.rarity_delta.get(rarity, 0.0)

    def v(self, venue: str | None) -> float:
        """平台係數。平台不明（None）或沒學過 → 0，等於「當成基準平台、不校正」。

        不猜是刻意的：猜一個平台等於憑空給一筆樣本 2 倍的價格水準，
        錯的方向還看不出來。
        """
        if venue is None:
            return 0.0
        return self.venue_delta.get(venue, 0.0)

    def venue_multiplier(self, venue: str | None) -> float:
        """平台係數的人話版：相對基準平台的價格倍率。"""
        return math.exp(self.v(venue))

    # -- 退化階梯 ------------------------------------------------------
    def predict_log(
        self,
        card_name: str | None,
        rarity: str | None,
        grade: float | None,
        venue: str | None = None,
    ) -> tuple[float, str, int]:
        """回傳 (對數點估計, 使用層級, 該層有效樣本數)。

        每一層都對上層做收縮，所以回傳的估計值其實含了整條鏈；
        `level` 報的是「最深、真的有樣本的那一層」。

        `venue` 是**目標平台**。給了就把每一筆樣本從它自己的平台換算到目標
        平台的價格水準（截距也換成基準平台的 `base_venue`）；沒給就完全不碰
        平台維度，回傳的是混合平台水準的數字——兩條路各自基準一致，
        不會出現「一半 Yahoo 一半 Mercari」的混源比較。
        """
        k = self.params.shrink_k
        target_g = self.g(grade)
        adjust = venue is not None
        target_v = self.v(venue) if adjust else 0.0

        def conv(r: Obs) -> float:
            """把一筆樣本換算到 (目標分數, 目標平台) 的價格水準。"""
            out = r.logp - self.g(r.grade) + target_g
            return out - self.v(r.venue) + target_v if adjust else out

        est = (self.base_venue if adjust else self.base) + target_g + self.r(rarity) + target_v
        level, n_eff = "L0", 0

        # L3：同稀有度（任何卡、任何分數），把分數換算到目標分數
        pool3 = [r for r in self.rows if r.rarity == rarity]
        if pool3:
            est = _shrink(_median([conv(r) for r in pool3]), est, len(pool3), k)
            level, n_eff = "L3", len(pool3)

        if not card_name:
            return est, level, n_eff

        # L2：同卡同稀有度，跨分數換算
        pool2 = [r for r in self.rows if r.card_name == card_name and r.rarity == rarity]
        if pool2:
            est = _shrink(_median([conv(r) for r in pool2]), est, len(pool2), k)
            level, n_eff = "L2", len(pool2)

        # L1：同卡同稀有度同分數，只剩平台需要換算
        pool1 = [r for r in pool2 if r.grade == grade]
        if pool1:
            vals = [
                (r.logp - self.v(r.venue) + target_v) if adjust else r.logp for r in pool1
            ]
            est = _shrink(_median(vals), est, len(pool1), k)
            level, n_eff = "L1", len(pool1)

        return est, level, n_eff


# ---------------------------------------------------------------------------
def _conformal_quantiles(residuals: list[float], confidence: float) -> tuple[float, float] | None:
    """split conformal 的雙尾分位數，含 (n+1) 有限樣本修正。

    用**帶號**殘差的雙尾分位數而不是 |殘差| 的單一分位數：對數價格殘差是偏的
    （上檔厚尾），對稱區間會在下緣浪費寬度、上緣不夠。兩尾各自 α/2，
    合起來仍然是 ≥ 1−α 的保證（union bound）。
    分位數索引落在樣本範圍外（樣本太少）時回 None——不給區間比給一個
    「其實沒被校準過」的區間誠實。
    """
    n = len(residuals)
    if n < 2:
        return None
    s = sorted(residuals)
    alpha = 1.0 - confidence
    lo_i = math.floor((n + 1) * alpha / 2) - 1
    hi_i = math.ceil((n + 1) * (1 - alpha / 2)) - 1
    if lo_i < 0 or hi_i > n - 1:
        return None
    return s[lo_i], s[hi_i]


def _split(rows: list[Obs], fraction: float) -> tuple[list[Obs], list[Obs]]:
    """決定性切分（用 key 的雜湊），同一份資料每次跑都切在同一刀上。

    隨機切分會讓同一個標的今天有區間、明天沒有——那種不穩定會讓人
    不再相信這個數字，比區間稍微不理想嚴重得多。
    """
    fit: list[Obs] = []
    cal: list[Obs] = []
    cutoff = int(fraction * 1000)
    for i, r in enumerate(rows):
        h = hashlib.sha1((r.key or f"#{i}").encode("utf-8")).hexdigest()
        (cal if int(h[:6], 16) % 1000 < cutoff else fit).append(r)
    return fit, cal


class Valuator:
    """對外的估價入口：點估計（全量資料）＋ conformal 區間（留出校準）。

    點估計用**全部**樣本擬合，區間寬度用「只看 fit 半邊」的模型在校準集上的
    殘差。嚴格的 split conformal 會要求點估計也來自 fit 半邊，但那等於丟掉
    40% 的樣本；這裡選擇保留全量點估計、接受區間**略偏保守**，並用
    `coverage_check()` 實測我們真的出貨的這個組合的覆蓋率（而不是理論值）。

    **分群鍵一律由 `cal_model`（fit 半邊）算，不由 `self.model`（全量）算。**
    這是工程原則 1：校準殘差的群組標籤是 fit 模型給的，查詢時的群組標籤若改用
    全量模型，同一句「L1/n<3」在兩邊就是兩個不同的意思（全量模型看得到的樣本
    多四成，同一個查詢的 n_eff 系統性偏大），等於拿 A 的尺去讀 B 的刻度。
    """

    def __init__(
        self, rows: list[Obs], params: Params | None = None, index: CardIndex | None = None
    ) -> None:
        self.params = params or Params()
        self.index = index
        self.rows = [r for r in rows if r.price_twd and r.price_twd > 0]
        self.model = ValuationModel(self.rows, self.params)

        fit, cal = _split(self.rows, self.params.calibration_fraction)
        self.calibration_n = len(cal)
        #: 校準模型（只看 fit 半邊）。除了算殘差，還負責**查詢時的分群標籤**。
        self.cal_model: ValuationModel | None = None
        #: 兩組殘差，**因為有兩種點估計**。用沒校正平台的殘差去給校正過的點估計
        #: 配區間，等於把 2 倍的平台價差當成模型的不確定性算進區間寬度裡，
        #: 而且中心還是偏的——那正是工程原則 1 說的混源比較。
        self.residuals: list[float] = []
        self.residuals_venue: list[float] = []
        #: 同兩組殘差，但依 `group_key(層級, 有效n)` 分群；每個層級鍵下另存一份
        #: 合併版（階梯的第二階）。鍵重複存兩份是刻意的：查表時不必再做集合運算。
        self.residual_groups: dict[str, list[float]] = {}
        self.residual_groups_venue: dict[str, list[float]] = {}
        if fit and cal:
            self.cal_model = ValuationModel(fit, self.params)
            for r in cal:
                for venue, bag, groups in (
                    (None, self.residuals, self.residual_groups),
                    (r.venue, self.residuals_venue, self.residual_groups_venue),
                ):
                    est, level, n_eff = self.cal_model.predict_log(
                        r.card_name, r.rarity, r.grade, venue
                    )
                    resid = r.logp - est
                    bag.append(resid)
                    for key in group_ladder(level, n_eff):
                        groups.setdefault(key, []).append(resid)

    # ------------------------------------------------------------------
    def _residuals_for(self, venue: str | None) -> list[float]:
        """點估計用哪個基準，區間就得用哪一組殘差。"""
        return self.residuals_venue if venue is not None else self.residuals

    def _groups_for(self, venue: str | None) -> dict[str, list[float]]:
        return self.residual_groups_venue if venue is not None else self.residual_groups

    def quantiles_for(
        self,
        *,
        card_name: str | None,
        rarity: str | None,
        grade: float | None,
        venue: str | None,
    ) -> GroupQuantiles | None:
        """這一筆查詢該用哪一組殘差分位數（含退化與安全夾的完整來歷）。

        步驟：
          1. 用 `cal_model` 算出這筆查詢的 (層級, 有效n) → 最細的群組鍵。
          2. 沿合併階梯（細群 → 同層級 → 全域）找第一個樣本數 ≥
             `min_group_calibration` 且取得出分位數的群。
          3. **下緣安全夾**：群組下緣若比全域高（區間更窄、上限更激進），
             夾回全域下緣。理由見模組頂註——往寬的方向錯是輸掉競標，
             往窄的方向錯是真的付錢。

        回 None ＝ 連全域分位數都取不出來（校準樣本太少），呼叫端不給區間。
        """
        residuals = self._residuals_for(venue)
        q_global = _conformal_quantiles(residuals, self.params.confidence)
        if q_global is None:
            return None

        level, n_eff = "L0", 0
        if self.cal_model is not None:
            _est, level, n_eff = self.cal_model.predict_log(card_name, rarity, grade, venue)
        requested = group_key(level, n_eff)

        if not self.params.group_conformal or self.cal_model is None:
            return GroupQuantiles(
                lo=q_global[0], hi=q_global[1], group=GLOBAL_GROUP,
                group_n=len(residuals), requested_group=requested,
                degraded=True, floor_applied=False,
            )

        groups = self._groups_for(venue)
        for key in group_ladder(level, n_eff):
            vals = groups.get(key, [])
            if len(vals) < self.params.min_group_calibration:
                continue
            q = _conformal_quantiles(vals, self.params.confidence)
            if q is None:
                continue
            floored = q[0] > q_global[0]
            return GroupQuantiles(
                lo=min(q[0], q_global[0]), hi=q[1], group=key, group_n=len(vals),
                requested_group=requested, degraded=key != requested,
                floor_applied=floored,
            )

        return GroupQuantiles(
            lo=q_global[0], hi=q_global[1], group=GLOBAL_GROUP, group_n=len(residuals),
            requested_group=requested, degraded=True, floor_applied=False,
        )

    @property
    def calibrated(self) -> bool:
        return len(self.residuals) >= self.params.min_calibration

    def estimate(
        self,
        *,
        card_name: str | None = None,
        rarity: str | None = None,
        grade: float | None = None,
        grade_source: str | None = None,
        landed_twd: float | None = None,
        venue: str | None = None,
    ) -> Estimate:
        """估一個標的的公允價。

        `venue` 是**目標平台**（comps.site 的值：`buyee_yahoo` / `buyee_mercari`
        / `buyee_paypay`）。給了就回「該平台價格水準」的公允價；

        **不給就完全不做平台校正**，回傳的是混合平台水準的數字，並標
        `venue_adjusted=False` ＋ 一條 note。這是刻意的預設：三個平台的成交價
        水準差 2 倍以上（Yahoo 是競價出清價、Mercari/PayPay 是定價零售價），
        猜一個平台比不校正更危險，所以未指定時就誠實地說「這是混合水準」，
        讓呼叫端看得見自己少給了什麼。
        """
        notes: list[str] = []
        if not self.rows:
            return Estimate(
                fair_twd=None, level=None, level_label="無樣本", n_effective=0,
                confidence=self.params.confidence, card_name=card_name,
                grade=grade, grade_source=grade_source, venue=venue, venue_adjusted=False,
                notes=["行情庫沒有可用樣本"],
            )

        residuals = self._residuals_for(venue)
        log_est, level, n_eff = self.model.predict_log(card_name, rarity, grade, venue)
        est = Estimate(
            fair_twd=round(math.exp(log_est), 0),
            level=level,
            level_label=_LEVEL_LABEL.get(level, level),
            n_effective=n_eff,
            confidence=self.params.confidence,
            calibration_n=len(residuals),
            card_name=card_name,
            grade=grade,
            grade_source=grade_source,
            venue=venue,
            venue_adjusted=venue is not None,
            venue_is_estimated=(
                self.model.venue_is_estimated(venue) if venue is not None else None
            ),
            notes=notes,
        )

        if grade is not None and grade_source == "description":
            notes.append(
                f"分數 {grade:g} 是從**商品描述**撈出來的（標題沒寫）。描述是賣家自由"
                "文字，可信度低於標題——這個公允價的地基比平常軟，下單前請對一次"
                "照片上的鑑定殼"
            )
        if not self.model.grade_is_estimated(grade):
            notes.append(f"分數 {grade:g} 的溢價係數以先驗為主（樣本不足以估計）")
        if not self.model.rarity_is_estimated(rarity):
            notes.append(f"稀有度 {rarity or '未知'} 的溢價係數以先驗為主")
        if venue is None:
            notes.append(
                "未指定目標平台：這是**混合平台**的價格水準。三個平台的成交價差 2 倍"
                "以上（Yahoo 競價出清價 vs Mercari/PayPay 定價零售價），拿它跟單一"
                "平台的標的比會系統性偏掉"
            )
        elif not est.venue_is_estimated:
            notes.append(
                f"平台 {venue} 的價格水準係數以先驗為主（可比分層不足以估計）"
            )

        if not self.calibrated:
            notes.append(
                f"校準集僅 {len(residuals)} 筆（門檻 {self.params.min_calibration}），"
                "樣本不足以校準，不提供區間"
            )
            return est

        q = self.quantiles_for(card_name=card_name, rarity=rarity, grade=grade, venue=venue)
        if q is None:
            notes.append("校準樣本不足以取出分位數，不提供區間")
            return est
        est.lo_twd = round(math.exp(log_est + q.lo), 0)
        est.hi_twd = round(math.exp(log_est + q.hi), 0)
        est.calibration_group = q.group
        est.calibration_group_n = q.group_n
        est.calibration_group_requested = q.requested_group
        est.calibration_degraded = q.degraded
        est.interval_floor_applied = q.floor_applied
        if q.degraded:
            notes.append(
                f"區間**未經群組校準**：{q.requested_group} 這一群的校準樣本不足 "
                f"{self.params.min_group_calibration} 筆，退到「{q.group}」"
                f"（{q.group_n} 筆）的分位數"
            )
        if q.requested_group.endswith("10-49") or n_bucket(n_eff) == "10-49":
            notes.append(
                "⚠️ 有效樣本 10-49 這一桶是已診斷的校準破口：實測下尾違反率 29%"
                "（名目 10%），六種分群鍵都修不好——壞的那一角是 L3 × Mercari ×"
                "低稀有度（該切片 100% 違反、點估計高估 ×5.9）。這一筆的區間下緣"
                "可能比實際市場樂觀，**出價側預設不會給上限**（bidding.reject_n_buckets）"
            )

        if landed_twd and landed_twd > 0:
            # P(公允價 > 到手成本)：直接用殘差經驗分布，不假設常態。
            # 這是「相對於本模型殘差」的機率，不是市場真實機率——見模組頂註。
            #
            # ⚠️ 基準差異，刻意保留並在此標明：區間現在是**群組條件**的
            # （Mondrian），這個機率仍是**邊際**的（全域殘差分布）。理由是
            # `appraise.P_WORTH_AVOID = 0.35` 這道門檻是對著全域分布訂的，
            # 換分母等於在沒有實測的情況下悄悄改動一個否決門檻。要改成群組
            # 條件的話，得先像區間那樣量一次各群的實際準度再動。
            target = math.log(landed_twd) - log_est
            est.p_worth_buying = sum(1 for r in residuals if r > target) / len(residuals)
        return est


# ---------------------------------------------------------------------------
def obs_from_comps(
    rows: list[dict[str, Any]], index: CardIndex | None = None
) -> list[Obs]:
    """把 comps 原始列轉成樣本。**卡名在這裡才決定**。

    card_name 欄位只是物化快取，真正的依據永遠是永久保留的 title：
    比對規則還在演進，寫死在欄位裡等於「改比對邏輯就要重抓資料」
    （store.py 的分桶哲學：分桶是查詢時的決定，不是寫入時的決定）。
    """
    out: list[Obs] = []
    for r in rows:
        price = r.get("price_twd")
        if not price:
            continue
        name = r.get("card_name") or None
        if index is not None and index.available:
            m = index.match(r.get("title") or "")
            name = m.name_ja if (m and m.in_era) else None
        out.append(
            Obs(
                price_twd=float(price),
                card_name=name,
                rarity=r.get("rarity"),
                grade=float(r["grade"]) if r.get("grade") is not None else None,
                key=str(r.get("id") or r.get("url") or r.get("title") or ""),
                venue=r.get("site") or None,
                sold_at=r.get("sold_at") or None,
                grader=r.get("grader") or None,
            )
        )
    return out


def load_comps_rows(store: Any, *, era_verified_only: bool = True, limit: int = 5000
                    ) -> list[dict[str, Any]]:
    """撈估價要用的成交列。預設只要有年代證據的——沒有年代證據的那批
    （寶可夢、卡套、現代卡）進來會直接把中位數帶歪。"""
    rows = store.comps_by(limit=limit)
    if era_verified_only:
        rows = [r for r in rows if (r.get("era_evidence") or "").strip()]
    return rows


def build_valuator(cfg: Any, store: Any, index: CardIndex | None = None) -> Valuator:
    idx = index if index is not None else CardIndex.load()
    rows = load_comps_rows(store)
    return Valuator(obs_from_comps(rows, idx), Params.from_config(cfg), index=idx)


def estimate_listing(
    valuator: Valuator, listing: Any, info: Any, *, landed_twd: float | None = None
) -> Estimate:
    """在架標的（Listing ＋ 已解析的 CardInfo）→ 估價。

    與 `estimate_signal_row`（db 的 signals 列）是同一件事的兩個入口，差別只在
    卡片屬性從哪裡來：這裡直接吃掃描當下解析好的 `CardInfo`，不必再繞一次 JSON。
    **目標平台一律是標的自己的 site**——Yahoo 的成交樣本是競價出清價，
    拿混合平台（含 Mercari 定價零售價）的水準去估競標會系統性高估 2 倍以上，
    而高估的方向正好是「上限開太高」，是會付錢的那一邊。
    """
    card_name = None
    if valuator.index is not None and valuator.index.available:
        m = valuator.index.match(getattr(listing, "title", "") or "")
        card_name = m.name_ja if (m and m.in_era) else None
    return valuator.estimate(
        card_name=card_name,
        rarity=getattr(info, "rarity", None),
        grade=getattr(info, "grade", None),
        grade_source=getattr(info, "grade_source", None),
        landed_twd=landed_twd,
        venue=listing.site.value,
    )


def card_attrs_from_row(
    valuator: Valuator, row: dict[str, Any]
) -> tuple[str | None, str | None, float | None]:
    """從 signals 表的一列抽出 (卡名, 稀有度, 分數)。

    抽出來獨立成函式是為了**同源**（工程原則 1）：買進側（`estimate_signal_row`，
    目標平台＝標的自己的 site）與轉賣側（`selling`，目標平台＝賣出賣場）必須
    對同一張卡看到同一組屬性。各寫一份的話，哪天稀有度的抽法改了，兩邊的
    公允價會安靜地分岔，而畫面上「買進估價」與「轉賣估價」看起來仍然一致。

    稀有度優先讀 payload 裡解析好的那份（與掃描當下同源），payload 壞掉或
    是舊資料才回頭從標題重抽——通知管路不能因為一個欄位解析失敗就整批停擺。
    """
    import json as _json

    from .parsers import extract_rarity

    title = row.get("title") or ""
    rarity = None
    try:
        payload = _json.loads(row.get("payload") or "{}") or {}
        rarity = ((payload.get("card") or {}) or {}).get("rarity")
    except (TypeError, ValueError):
        rarity = None
    if rarity is None:
        rarity = extract_rarity(title)

    card_name = None
    if valuator.index is not None and valuator.index.available:
        m = valuator.index.match(title)
        card_name = m.name_ja if (m and m.in_era) else None

    grade = row.get("grade")
    return card_name, rarity, (float(grade) if grade is not None else None)


def estimate_signal_row(valuator: Valuator, row: dict[str, Any]) -> Estimate:
    """把 signals 表的一列餵進估價。

    **目標平台就是這個 signal 自己的 site**：一個 Mercari 標的要跟 Mercari 的
    價格水準比，拿混合平台（含 Yahoo 競標出清價）的中位數去比會系統性高估折價。
    要估「賣到別的平台值多少」請走 `selling.venue_estimator_for_row`——
    那是另一個問題，不該偷用這個函式的答案。
    """
    card_name, rarity, grade = card_attrs_from_row(valuator, row)
    return valuator.estimate(
        card_name=card_name,
        rarity=rarity,
        grade=grade,
        grade_source=grade_source_from_row(row),
        landed_twd=row.get("landed_twd"),
        venue=row.get("site") or None,
    )


def grade_source_from_row(row: dict[str, Any]) -> str | None:
    """signals 列的分數來源（payload 裡 `card.grade_source`）。

    獨立成函式而不是塞進 `card_attrs_from_row`：那一支被買進側與轉賣側共用，
    改它的回傳形狀等於同時改兩條線的簽章。payload 壞掉或是舊資料（沒有這一欄）
    一律回 None ＝「來源不明」，而不是假裝是標題來的。
    """
    import json as _json

    try:
        payload = _json.loads(row.get("payload") or "{}") or {}
    except (TypeError, ValueError):
        return None
    src = ((payload.get("card") or {}) or {}).get("grade_source")
    return str(src) if src else None


# ---------------------------------------------------------------------------
def coverage_check(
    rows: list[Obs], params: Params, *, trials: int = 200, test_fraction: float = 0.25,
    seed: int = 0, venue_aware: bool = False,
) -> dict[str, Any]:
    """留出法實測名目區間的真實覆蓋率。**隨機留出＝假設可交換**。

    每一輪：隨機留出 `test_fraction` 當測試集，剩下的照**出貨時完全相同的
    流程**建 Valuator（全量點估計 + 內部再切一次校準），然後數測試樣本
    有幾成落在區間內。測試樣本從未參與擬合或校準，所以沒有洩漏。

    ⚠️ 但這裡的訓練集與測試集是**同一天抓的同一批資料互相驗證**，可交換性
    當然成立——所以這個數字量不到時間漂移，也量不到平台組成的變化。
    它只回答「程序本身有沒有寫錯」。真正的考題請看 `coverage_time_split()`。

    這裡刻意繞過 `min_calibration` 門檻：留出之後校準集必然變小，
    但我們要量的是「這個程序準不準」，不是「這份資料夠不夠」。
    回傳的 `mean_calibration_n` 就是這個折衷的證據，請一起看。
    """
    rng = random.Random(seed)
    covered = total = 0
    widths: list[float] = []
    cal_sizes: list[int] = []
    skipped = 0

    for _ in range(trials):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        cut = max(1, int(len(shuffled) * test_fraction))
        test, train = shuffled[:cut], shuffled[cut:]
        if len(train) < 10:
            continue
        v = Valuator(train, params)
        residuals = v.residuals_venue if venue_aware else v.residuals
        if not residuals:
            skipped += 1
            continue
        cal_sizes.append(len(residuals))
        q = _conformal_quantiles(residuals, params.confidence)
        if q is None:
            skipped += 1
            continue
        for t in test:
            log_est, _lvl, _n = v.model.predict_log(
                t.card_name, t.rarity, t.grade, t.venue if venue_aware else None
            )
            lo, hi = log_est + q[0], log_est + q[1]
            total += 1
            covered += int(lo <= t.logp <= hi)
            widths.append(math.exp(hi) - math.exp(lo))

    return {
        "nominal": params.confidence,
        "empirical": covered / total if total else None,
        "n_tested": total,
        "trials_skipped": skipped,
        "venue_aware": venue_aware,
        "mean_calibration_n": (sum(cal_sizes) / len(cal_sizes)) if cal_sizes else 0,
        "median_width_twd": _median(widths) if widths else None,
    }


def _time_split(
    rows: list[Obs], test_fraction: float, stratify_by_venue: bool
) -> tuple[list[Obs], list[Obs]]:
    """依成交時間切「早」「晚」兩半。缺時間的樣本直接排除。

    `stratify_by_venue=True` 是**每個平台各自切自己的早晚**再合併。這不是講究，
    是必要的：Buyee 系（Mercari/PayPay）的 `sold_at` 是入庫時間而不是真實成交
    時間（來源沒給，comps.py 退回 now()），只有 Yahoo 落札相場有真的成交日。
    所以全域排序之後「最晚的那 25%」幾乎全是 Buyee——實測 331 筆測試樣本裡
    304 筆 Mercari、2 筆 Yahoo，而訓練集是 712 筆 Yahoo。
    那不是時間切分，是**偽裝成時間切分的平台切分**，拿它去比「有沒有做平台
    校正」等於用被汙染的尺量自己要驗的東西。分層切分讓兩邊的平台組成一致，
    時間才是唯一被切開的維度。
    """
    dated = [r for r in rows if r.sold_at]
    if not stratify_by_venue:
        dated.sort(key=lambda r: r.sold_at or "")
        cut = len(dated) - max(1, int(len(dated) * test_fraction))
        return dated[:cut], dated[cut:]

    groups: dict[str | None, list[Obs]] = {}
    for r in dated:
        groups.setdefault(r.venue, []).append(r)
    train: list[Obs] = []
    test: list[Obs] = []
    for group in groups.values():
        group.sort(key=lambda r: r.sold_at or "")
        cut = len(group) - max(1, int(len(group) * test_fraction))
        train.extend(group[:cut])
        test.extend(group[cut:])
    return train, test


def coverage_by_group(
    rows: list[Obs],
    params: Params,
    *,
    test_fractions: tuple[float, ...] = (0.20, 0.25, 0.30),
    venue_aware: bool = True,
    stratify_by_venue: bool = True,
    diagnose: bool = False,
) -> dict[str, Any]:
    """**條件**覆蓋率：vanilla（全域分位數）vs Mondrian（分群），逐 n 分桶對照。

    這是「分群到底有沒有用」的唯一誠實問法，而且它量的是**出貨路徑本身**：
    Mondrian 那一側直接呼叫 `Valuator.quantiles_for()`，不是另外寫一份等價
    實作——另外寫一份的話，量到的就不是使用者拿到的那個區間（工程原則 1）。

    「下尾違反率」是本函式最重要的輸出：真實成交價低於區間下緣的比例，名目
    10%。出價上限完全由下緣反推，**下尾違反＝上限開得比市場實際成交還高**，
    也就是會賠錢的那一邊。兩側覆蓋率好看但下尾違反率高，是最危險的組合。

    多個 `test_fractions` 合併計數是為了讓小桶不至於只有十幾筆——代價是這幾份
    測試集互相重疊，所以**它們不是獨立樣本**，合計筆數不能當成信賴區間的 n。
    這是刻意的取捨，也是為什麼下面那個 `10-49` 桶的結論只寫「已知破口」
    而不敢寫成一個精確的數字。

    `diagnose=True` 另外回一份**每桶的組成**（層級／平台／稀有度／分數的次數
    分布，加上「層級×平台」子切片的逐格下尾違反率）。加這一段的理由很實際：
    「這一桶為什麼壞」以前只能靠一次性腳本回答，答案沒有落在工具裡，
    下次資料變了就得重寫一份——而診斷結論正是決定「要不要拒絕給上限」的依據。
    """
    order = [label for _upper, label in N_BUCKETS]
    stats: dict[str, dict[str, int]] = {
        b: {"n": 0, "cov_vanilla": 0, "cov_group": 0, "lo_vanilla": 0, "lo_group": 0}
        for b in order
    }
    #: 每桶的原始逐筆紀錄（只有 diagnose=True 才收集，平時不付這個記憶體）
    detail: dict[str, list[dict[str, Any]]] = {b: [] for b in order}
    out: dict[str, Any] = {
        "nominal": params.confidence,
        "nominal_lower_tail": round((1.0 - params.confidence) / 2, 4),
        "venue_aware": venue_aware,
        "test_fractions": list(test_fractions),
        "min_group_calibration": params.min_group_calibration,
        "buckets": [],
        "n_tested": 0,
        "note": "",
    }
    if len([r for r in rows if r.sold_at]) < 20:
        out["note"] = "有成交時間的樣本不足以做時間切分"
        return out

    for fraction in test_fractions:
        train, test = _time_split(rows, fraction, stratify_by_venue)
        v = Valuator(train, params)
        residuals = v.residuals_venue if venue_aware else v.residuals
        q_vanilla = _conformal_quantiles(residuals, params.confidence)
        if q_vanilla is None:
            continue
        for t in test:
            venue = t.venue if venue_aware else None
            log_est, _lvl, _n = v.model.predict_log(t.card_name, t.rarity, t.grade, venue)
            q = v.quantiles_for(
                card_name=t.card_name, rarity=t.rarity, grade=t.grade, venue=venue
            )
            if q is None:
                continue
            err = t.logp - log_est
            # 分桶用**回報給使用者的那個 n**（全量模型），因為使用者看到的是它
            b = stats[n_bucket(_n)]
            b["n"] += 1
            b["cov_vanilla"] += int(q_vanilla[0] <= err <= q_vanilla[1])
            b["cov_group"] += int(q.lo <= err <= q.hi)
            b["lo_vanilla"] += int(err < q_vanilla[0])
            b["lo_group"] += int(err < q.lo)
            if diagnose:
                detail[n_bucket(_n)].append({
                    "level": _lvl, "venue": t.venue, "rarity": t.rarity,
                    "grade": t.grade, "grader": t.grader, "card_name": t.card_name,
                    "n_effective": _n, "err": err, "lo": q.lo, "hi": q.hi,
                    "requested_group": q.requested_group, "group": q.group,
                })

    total = sum(stats[b]["n"] for b in order)
    out["n_tested"] = total
    for b in order:
        s = stats[b]
        if not s["n"]:
            continue
        entry = {
            "bucket": b,
            "n_tested": s["n"],
            "coverage_vanilla": s["cov_vanilla"] / s["n"],
            "coverage_group": s["cov_group"] / s["n"],
            "lower_tail_vanilla": s["lo_vanilla"] / s["n"],
            "lower_tail_group": s["lo_group"] / s["n"],
        }
        if diagnose:
            entry["composition"] = _bucket_composition(detail[b])
        out["buckets"].append(entry)
    if total:
        out["overall"] = {
            "coverage_vanilla": sum(stats[b]["cov_vanilla"] for b in order) / total,
            "coverage_group": sum(stats[b]["cov_group"] for b in order) / total,
            "lower_tail_vanilla": sum(stats[b]["lo_vanilla"] for b in order) / total,
            "lower_tail_group": sum(stats[b]["lo_group"] for b in order) / total,
        }
    return out


def _bucket_composition(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """一個分桶「是什麼組成的」＋「哪一角最壞」。

    `slices` 是照 (層級, 平台) 切開後**逐格**的下尾違反率，由壞到好排序。
    這一欄才是診斷的重點：整桶 35% 的違反率可能是均勻的（模型整體太窄），
    也可能是某一格 100% 拖垮的（模型對某個切片有段狀偏誤）——
    兩者的修法完全不同，而只看整桶的平均值分不出來。
    """
    def counts(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in rows:
            v = r.get(key)
            name = "未知" if v is None else (f"{v:g}" if isinstance(v, float) else str(v))
            out[name] = out.get(name, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    slices: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((str(r["level"]), str(r["venue"])), []).append(r)
    for (level, venue), group in groups.items():
        lo = sum(1 for r in group if r["err"] < r["lo"]) / len(group)
        cov = sum(1 for r in group if r["lo"] <= r["err"] <= r["hi"]) / len(group)
        med = _median([r["err"] for r in group])
        slices.append({
            "level": level, "venue": venue, "n_tested": len(group),
            "lower_tail_group": lo, "coverage_group": cov,
            # 正的中位誤差 ＝ 模型低估（安全方向）；負的 ＝ 高估（會買貴）
            "median_log_error": med, "median_error_ratio": math.exp(med),
        })
    slices.sort(key=lambda s: (-s["lower_tail_group"], -s["n_tested"]))
    return {
        "level": counts("level"), "venue": counts("venue"), "rarity": counts("rarity"),
        "grade": counts("grade"), "grader": counts("grader"),
        "requested_group": counts("requested_group"),
        "slices": slices,
    }


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rarity_relax_study(rows: list[Obs], params: Params) -> dict[str, Any]:
    """量測「把 L2 放寬到跨稀有度換算」的代價。**只量測，不改退化階梯。**

    問題背景：L2 要求「同卡同稀有度」，很多卡只有其他稀有度的成交——
    如果允許用稀有度係數把 A 稀有度的價換算到 B 稀有度（下稱 L2X），
    這些卡就能從 L3 升級。但升級只有在 L2X 比 L3 準的時候才是升級。

    測法：拿庫裡「同一張卡有多個稀有度成交」的樣本。對每一筆目標 t
    （卡 c、稀有度 B），**排除 c 的所有 B 稀有度樣本**（模擬「該卡該稀有度
    0 筆」的真實處境），比較三個估計：

      L2X_raw     同卡其他稀有度樣本，經 稀有度＋分數＋平台 係數換算後取中位
      L2X_ladder  L2X_raw 再照出貨階梯對 L3 收縮（w = n/(n+k)）——
                  **這才是放寬後使用者實際會拿到的數字**
      L3          現行行為：同稀有度（他卡）池收縮到全域先驗

    誤差 = |log(估計) − log(實際成交)|，報成倍率（exp）。三個估計對同一批
    目標算，可直接配對比較。

    誠實聲明（不要略過）：
    - 係數由**全量樣本**擬合（含目標自己），輕微洩漏、方向偏樂觀；
      但三個估計吃同一組係數，相對比較仍然成立。
    - 逐筆統計會被熱門卡放大（一張卡 20 筆成交就佔 20 個目標），
      所以同時報「每卡中位」——兩個數字不一致時，以每卡中位為準。
    - 有多稀有度成交的卡本身偏熱門，冷門卡的換算誤差**只會更大不會更小**
      （樣本更少、稀有度組合更偏）。這份量測是代價的下界，不是上界。
    """
    model = ValuationModel(rows, params)
    k = params.shrink_k

    by_card: dict[str, list[Obs]] = {}
    for r in rows:
        if r.card_name and r.rarity and r.price_twd > 0:
            by_card.setdefault(r.card_name, []).append(r)
    multi = {
        name: obs for name, obs in by_card.items()
        if len({o.rarity for o in obs}) >= 2
    }

    def conv(o: Obs, t: Obs) -> float:
        """把樣本 o 換算到目標 t 的 (稀有度, 分數, 平台) 水準。"""
        return (
            o.logp
            - model.g(o.grade) + model.g(t.grade)
            - model.r(o.rarity) + model.r(t.rarity)
            - model.v(o.venue) + model.v(t.venue)
        )

    err_raw: list[float] = []
    err_ladder: list[float] = []
    err_l3: list[float] = []
    per_card_ladder: dict[str, list[float]] = {}
    per_card_l3: dict[str, list[float]] = {}
    pair_errors: dict[tuple[str, str], list[float]] = {}
    donor_buckets: dict[str, dict[str, list[float]]] = {}
    n_targets = 0
    skipped_no_l3 = 0
    wins = 0

    for name, obs in multi.items():
        for t in obs:
            donors = [o for o in obs if o.rarity != t.rarity]
            if not donors:
                continue
            pool3 = [o for o in rows if o.rarity == t.rarity and o.card_name != name]
            if not pool3:
                skipped_no_l3 += 1
                continue
            n_targets += 1

            est0 = (
                model.base_venue + model.g(t.grade) + model.r(t.rarity) + model.v(t.venue)
            )
            est_l3 = _shrink(
                _median([conv(o, t) for o in pool3]), est0, len(pool3), k
            )
            est_raw = _median([conv(o, t) for o in donors])
            est_ladder = _shrink(est_raw, est_l3, len(donors), k)

            e_raw = abs(t.logp - est_raw)
            e_ladder = abs(t.logp - est_ladder)
            e_l3 = abs(t.logp - est_l3)
            err_raw.append(e_raw)
            err_ladder.append(e_ladder)
            err_l3.append(e_l3)
            per_card_ladder.setdefault(name, []).append(e_ladder)
            per_card_l3.setdefault(name, []).append(e_l3)
            wins += int(e_ladder < e_l3)

            n_d = len(donors)
            bucket = "1" if n_d == 1 else ("2-4" if n_d <= 4 else "5+")
            b = donor_buckets.setdefault(bucket, {"ladder": [], "l3": []})
            b["ladder"].append(e_ladder)
            b["l3"].append(e_l3)

            for donor_rarity in {o.rarity for o in donors}:
                sub = [o for o in donors if o.rarity == donor_rarity]
                e_pair = abs(t.logp - _median([conv(o, t) for o in sub]))
                pair_errors.setdefault(
                    (str(donor_rarity), str(t.rarity)), []
                ).append(e_pair)

    def stats(errors: list[float], by_card: dict[str, list[float]] | None = None
              ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "n": len(errors),
            "median_ratio": math.exp(_median(errors)) if errors else None,
            "p90_ratio": math.exp(_quantile(errors, 0.90)) if errors else None,
        }
        if by_card is not None and by_card:
            card_medians = [_median(v) for v in by_card.values()]
            out["median_ratio_by_card"] = math.exp(_median(card_medians))
        return out

    return {
        "n_rows": len(rows),
        "n_cards_multi_rarity": len(multi),
        "n_targets": n_targets,
        "n_skipped_no_l3": skipped_no_l3,
        "estimators": {
            "L2X_raw": stats(err_raw),
            "L2X_ladder": stats(err_ladder, per_card_ladder),
            "L3": stats(err_l3, per_card_l3),
        },
        "win_rate_ladder_vs_l3": (wins / n_targets) if n_targets else None,
        "donor_buckets": [
            {
                "bucket": b,
                "n": len(v["ladder"]),
                "l2x_ladder_median_ratio": math.exp(_median(v["ladder"])),
                "l3_median_ratio": math.exp(_median(v["l3"])),
            }
            for b, v in sorted(donor_buckets.items())
        ],
        "pairs": sorted(
            (
                {
                    "from": a, "to": b, "n": len(v),
                    "median_ratio": math.exp(_median(v)),
                    "p90_ratio": math.exp(_quantile(v, 0.90)),
                }
                for (a, b), v in pair_errors.items()
            ),
            key=lambda d: -d["n"],
        ),
        "caveats": [
            "係數由全量樣本擬合（含目標自己），輕微洩漏、方向偏樂觀；"
            "三個估計吃同一組係數，相對比較仍成立",
            "逐筆統計被熱門卡放大——與「每卡中位」不一致時以每卡中位為準",
            "有多稀有度成交的卡偏熱門，這份量測是放寬代價的下界不是上界"
            "（真正會被放寬救到的冷門卡，換算誤差只會更大）",
        ],
    }


def _venue_mix(rows: list[Obs]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for r in rows:
        mix[r.venue or "unknown"] = mix.get(r.venue or "unknown", 0) + 1
    return mix


def coverage_time_split(
    rows: list[Obs], params: Params, *, test_fraction: float = 0.25,
    venue_aware: bool = False, stratify_by_venue: bool = True,
) -> dict[str, Any]:
    """**時間切分**的覆蓋率自檢：早期成交訓練＋校準，晚期成交當測試。

    這是唯一誠實的問法。`coverage_check()` 的隨機留出讓訓練與測試來自同一天
    抓的同一批資料，可交換性是被構造出來的；實務上模型永遠是「用過去的成交
    評估現在的標的」，中間隔著市場漂移與樣本平台組成的變化。

    切分見 `_time_split`（預設**平台內**切早晚，理由在那支函式的 docstring，
    很重要，請讀）。回傳同時給點估計誤差（對數空間的中位絕對誤差，換算成
    倍率），因為平台校正應該同時改善「區間覆蓋率」與「點估計準度」，
    只看其中一個會漏掉「區間變寬所以覆蓋率變好」這種假改善。
    """
    dated = [r for r in rows if r.sold_at]
    out: dict[str, Any] = {
        "nominal": params.confidence,
        "venue_aware": venue_aware,
        "stratify_by_venue": stratify_by_venue,
        "empirical": None,
        "n_tested": 0,
        "n_train": 0,
        "calibration_n": 0,
        "median_width_twd": None,
        "median_abs_log_error": None,
        "median_error_ratio": None,
        "train_until": None,
        "test_from": None,
        "train_venue_mix": {},
        "test_venue_mix": {},
        "n_undated": len(rows) - len(dated),
        "note": "",
    }
    if len(dated) < 20:
        out["note"] = f"有成交時間的樣本只有 {len(dated)} 筆，不足以做時間切分"
        return out

    train, test = _time_split(rows, test_fraction, stratify_by_venue)
    out["n_train"] = len(train)
    out["train_until"] = max((r.sold_at or "") for r in train)
    out["test_from"] = min((r.sold_at or "") for r in test)
    out["train_venue_mix"] = _venue_mix(train)
    out["test_venue_mix"] = _venue_mix(test)

    v = Valuator(train, params)
    residuals = v.residuals_venue if venue_aware else v.residuals
    out["calibration_n"] = len(residuals)
    q = _conformal_quantiles(residuals, params.confidence)
    if q is None:
        out["note"] = "訓練集切出來的校準集不足以取分位數"
        return out

    covered = 0
    widths: list[float] = []
    abs_errors: list[float] = []
    for t in test:
        log_est, _lvl, _n = v.model.predict_log(
            t.card_name, t.rarity, t.grade, t.venue if venue_aware else None
        )
        lo, hi = log_est + q[0], log_est + q[1]
        covered += int(lo <= t.logp <= hi)
        widths.append(math.exp(hi) - math.exp(lo))
        abs_errors.append(abs(t.logp - log_est))

    out["n_tested"] = len(test)
    out["empirical"] = covered / len(test) if test else None
    out["median_width_twd"] = _median(widths) if widths else None
    out["median_abs_log_error"] = _median(abs_errors) if abs_errors else None
    if abs_errors:
        out["median_error_ratio"] = math.exp(_median(abs_errors))
    return out
