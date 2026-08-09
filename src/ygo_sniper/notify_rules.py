"""推播規則：什麼樣的標的值得**主動**打斷你。

dashboard 回答「有什麼」，推播只回答一件事：「現在有沒有必須立刻處理的事」。
所以這裡只有兩條規則，而且兩條都刻意窄：

  規則 1 競標急件（`auction_urgent`）
      有出價上限 ＋ 現價仍低於上限 ＋ 結標倒數在窗口內。
      這**就是** dashboard 的梯隊 1（`bidding.auction_tier` == 1），同一個判定
      同一份門檻——推播不自己寫一份「什麼叫快結標」。理由見 bidding.py 的
      `actionable_window_hours`：兩顆旋鈕會讓畫面上排第一的那筆跟手機上收到的
      那筆變成兩批東西（工程原則 1）。

  規則 2 高信心標的（`high_p`）
      P(值得買) 高於門檻，**排除普卡**，**排除價格還沒被競價發現的競標**。

  規則 3 監控賣家新上架（`seller_new`）
      **監控名單上的賣家** ＋ **第一次進 `listing_obs` 的標的** ＋ 折價達門檻。
      折價有兩種來源，訊息上一眼分得出來（見下）。

  規則 3b 監控賣家·估不了（`seller_unpriced`）
      監控名單賣家的新標的，但**連模型都給不出可信估值**。不宣稱它便宜，
      只說「有一件我們估不了的東西」＋為什麼估不了。低音量（每輪有自己的上限）。

---------------------------------------------------------------------------
## 規則 3 的兩種折價來源（2026-08-04 改）

原本的規則 3 只認**同儕相對**（`seller_alpha` 那一套：同站×同卡×同版次×同分數×
同價格基準的其他賣家中位），同儕算不出來就不推。實跑的結果是它**永遠不推**：
60 筆「監控賣家 × 新上架」裡只有 2 筆湊得出同儕，其餘 58 筆靜默略過。
根因不是門檻訂太嚴，是資料密度——`comps` 涵蓋的 298 張卡裡有 172 張（58%）
只有 1 筆成交，同卡×同稀有度×同分數的同儕在結構上就湊不出來。

所以現在：**同儕算得出來就用同儕（強）；算不出來改用模型估值（弱），但要過
`bidding.EvidenceGate` 的證據閘門（通知檔位），而且折價門檻更嚴**。三件事情必須
同時成立，少一件這個 fallback 就會變回「主動製造假 alpha」的那台機器：

1. **閘門引用既有的那一份，不另訂標準**（`rules.gate` 就是
   `EvidenceGate.from_config(cfg, profile="notify")`）：語意閘門與出價檔一樣硬
   （估價層級 L1／L2、分數已知），只放寬「校準政策」兩道（破口桶不拒收、
   校準殘差門檻 30；2026-08-07 重放 80 筆 unpriced，W3=11＋W5=5 筆被出價檔的
   校準政策擋掉，而它們是全庫證據最強的標的——通知與出價的錯誤代價不對稱）。
   擋下來的**仍然不推**——改走規則 3b（「需要你看一眼」），而不是硬給一個
   折價數字。**放寬的必須看得見**：通知檔放行、但出價檔會拒的，訊息帶
   ⚠️「不是出價依據」caveat（`Match.bidding_reject_note`）。出價那一側
   （`bidding.max_bid_jpy`）永遠走 bidding 檔，不受此影響。
2. **訊息必須標示判定來源**：同儕相對（👤，強）vs 模型估值（🤖，弱）＋
   「為什麼沒有同儕」。兩種在手機上要能一眼分辨。
3. **絕不回饋到 Seller Alpha 分數**。賣家評分維持只用同儕相對，這是第二棒
   實測過的紅線：模型絕對法對 eBay 全站給出 0.24-0.60 的比值（comps 表一筆
   eBay 成交都沒有，平台係數估不出來），`ebay:collectiblemore` 模型說 0.57
   （看起來是 alpha）、同儕說 1.54（其實比同儕貴）——**符號相反**。
   模型 fallback 只影響「這一筆要不要通知你」，不影響「這個賣家值不值得追」。
   結構上的保證：`seller_watch.build_notify_context` 依然**不帶 valuator**，
   模型只在 `_match_seller_new` 這一層出現，碰不到 `seller_alpha.analyze`。
   （`tests/test_seller_watch.py` 有一條測試釘住這件事。）

模型 fallback 的折價門檻預設 **25%**，比同儕的 15% 嚴——因為兩個數字的證據
等級不同：同儕中位是「別的賣家真的開這個價」，模型公允價是點估計，實測中位
誤差 ×1.9。門檻不用區間下緣 `lo_twd`（`bidding` 出價上限用的那個基準）的原因
是實測：21 筆可估標的的 `lo/fair` 中位約 0.31，要便宜到低於下緣等於要折價 69%
以上——0 筆達得到，那條路等於沒開。出價會付錢所以用下緣；這裡只是「叫你看一眼」，
用點估計＋更嚴的門檻，再把區間原文印在訊息上讓你自己判斷。

規則 2 的兩個排除各自防一種假陽性，都不是可有可無的：

  - 普卡（`rarity == "normal"`）：便宜到 P 值幾乎必然很高，但那不是撿漏，
    只是它本來就不值錢。⚠️ `rarity is None` **不算普卡**——那是「讀不出稀有度」，
    是不知道，不是便宜。把不知道當普卡排除，會安靜地漏掉真正的好貨。
  - 沒被競價發現的競標：P 值是拿**現價**算的，而競標的現價會漲。實例（本庫
    2026-08-02）：「遊戯王 1円スタート ブラックマジシャンガール ARS10」P=99%，
    現價 ¥1 級、還有 6 天結標——那個 99% 講的是「用 ¥1 買到很划算」，
    不是一個你能成交的價格。**只有價格已經被市場發現過的標的，P 值才有意義。**

判定「價格被發現過了沒」用兩個條件（**都要滿足**）：
  (a) `bids >= min_bids`：出價次數是 Yahoo 直接給的欄位（本庫 37/37 筆競標都有值），
      0 次出價 ＝ 現價就是起標價，那個數字沒有任何市場資訊。
      `bids is None`（來源沒給）一律排除——缺值時寧可不送（紅線）。
  (b) 結標倒數 ≤ `price_discovered_within_hours`（預設沿用
      `bidding.PRICE_DYNAMICS_HOURS` = 48 小時）：光有幾次出價不夠。上面那筆
      ¥1 起標的標的有 3 次出價、還剩 6 天——價格會繼續漲好幾輪。
      這是為什麼**不只用 bids**：兩條各自漏掉的那一半剛好互補。

送出去的每一則訊息都會讓使用者拿去下真錢的單，所以**任何一個要顯示的欄位
缺值，就整則不送**（記進 `skipped`，preview 看得到原因），不送一則有空欄位
或猜測值的訊息。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .bidding import (
    DEFAULT_NOTIFY_MIN_CALIBRATION_SAMPLES,
    DEFAULT_NOTIFY_REJECT_N_BUCKETS,
    PRICE_DYNAMICS_HOURS,
    PROFILE_NOTIFY,
    EvidenceGate,
    actionable_window_hours,
    auction_room_value,
    auction_tier,
    bidding_gate_note,
    ceiling_value_of,
    hours_until_end,
    is_live_auction,
    listing_from_payload,
)

#: 規則代號。**同時是 `notify_log` 的 rule 欄位**，改字串等於清空該規則的去重帳。
RULE_AUCTION_URGENT = "auction_urgent"
RULE_HIGH_P = "high_p"
RULE_SELLER_NEW = "seller_new"
RULE_SELLER_UNPRICED = "seller_unpriced"
#: 規則 4 指定卡狙擊。**必須與 `store.CARD_SNIPE_RULE` 同值**——`list_card_watch_hits`
#: 用它 join `notify_log` 取 `sent_at`，兩邊漂移的話 join 永遠不匹配、`sent_at` 恆為
#: NULL ＝ 每輪重複推播，而且壞掉的樣子與「真的還沒送過」一模一樣。守門的是
#: `tests/test_card_snipe_notify.py::test_store_and_notify_rules_agree_on_the_rule_name`。
RULE_CARD_SNIPE = "card_snipe"

RULE_LABEL = {
    RULE_AUCTION_URGENT: "規則 1 競標急件",
    RULE_HIGH_P: "規則 2 高信心標的",
    RULE_SELLER_NEW: "規則 3 監控賣家新上架",
    RULE_SELLER_UNPRICED: "規則 3b 監控賣家·估不了",
    RULE_CARD_SNIPE: "規則 4 指定卡狙擊",
}

#: 規則 3 的判定來源。**這一欄要走到訊息上**：同儕相對與模型估值不是同一種宣稱。
SOURCE_PEER = "peer"
SOURCE_MODEL = "model"

#: 規則 2 預設排除的稀有度。只有 `normal`——`rarity is None` 不在此列，見模組頂註。
DEFAULT_EXCLUDE_RARITIES: tuple[str, ...] = ("normal",)
DEFAULT_P_WORTH_MIN = 0.70
DEFAULT_MIN_BIDS = 1
#: 一輪最多送幾則。**0（或 yaml 的 `null`）＝ 不限**。
#:
#: 2026-08-03 由 15 改成 0。原本的上限是為了「推播不要變洗版」，但使用者的
#: 實際使用方式是「人在外面，收到就慢慢往下滑」——被併成一則統計的那幾筆
#: 反而要等到下一輪（每小時一輪）才看得到，而競標是有時效的。
#: 機制**保留**（改回任何正整數就恢復截斷），只是預設不設限。
#: ⚠️ 這與健康告警的冷卻（`alerts`）是兩件事：那個防的是「同一個壞消息重複吵」，
#: 這個管的是「一輪送幾則不同的好貨」，不要一起調。
DEFAULT_MAX_ITEMS_PER_RUN = 0
DEFAULT_DEDUPE_DAYS = 7

#: 規則 3 的同儕相對折價門檻（0.15 ＝ 比同儕便宜 15% 以上才推）。
#: 依據：`seller_alpha` 的深度滿分對應「穩定便宜 25%」，而單筆折價的
#: winsorize 上限是 50%——15% 落在「明顯低於同儕、但不到「那筆一定有問題」」
#: 的區間。⚠️ 這是**單筆**的折價，不是賣家的收縮後中位（那是分數的事）。
DEFAULT_SELLER_MIN_DISCOUNT = 0.15
#: 規則 3 的同儕筆數下限。1 筆同儕就是一次 1v1 比價，訊息會把這件事講出來
#: （`seller_alpha.SellerScore.caveats` 同一個立場），但不強制擋掉——
#: 擋掉的話在目前的樣本密度下規則 3 幾乎永遠不會觸發。
DEFAULT_SELLER_MIN_PEERS = 1

#: 規則 3 的**模型 fallback** 折價門檻（0.25 ＝ 比模型公允價便宜 25% 以上才推）。
#: 刻意比同儕的 15% 嚴，理由見模組頂註：同儕中位是「別的賣家真的開這個價」，
#: 模型公允價是點估計（實測中位誤差 ×1.9）。實測（2026-08-04，本庫 60 筆
#: 監控賣家新上架）：≥15% 有 4 筆、≥20% 3 筆、**≥25% 2 筆**、≥30% 1 筆。
DEFAULT_SELLER_MODEL_MIN_DISCOUNT = 0.25

#: 規則 3b（估不了）一輪最多送幾則。**這裡的 0 ＝ 一則都不送**（與
#: `max_items_per_run` 的 0＝不限**相反**）：3b 是「沒有判斷、只是叫你看一眼」的
#: 通知，它的預設立場是有限額而不是無限額，所以「不設限」不該是能打錯出來的狀態。
#: 要關掉整條規則請用 `unpriced_enabled: false`。
DEFAULT_SELLER_UNPRICED_MAX_PER_RUN = 3

#: 標題裡的「這可能是變體卡」線索。**命中就不給折價數字**，改走規則 3b。
#:
#: 為什麼要有這張表：變體卡（印刷錯誤、missing foil、版ズレ…）的卡名比對會配到
#: **母卡**（正常版），於是模型拿正常版的行情去估，而變體通常貴得多——模型
#: fallback 對這類卡的偏差方向是**低估其價值**，外顯成「太貴，不值得」而讓你
#: 錯過。這正好是使用者要這個功能的原因（夢幻逸品因為稀少而永遠不被通知），
#: 所以這裡寧可放棄那個數字，也不要給一個方向已知是錯的折價。
#:
#: ⚠️ 拉丁字母的詞一律用**拉丁字母邊界**（不是 `\b`）：`\b` 在 CJK 相鄰處不成立，
#: 「印刷error」會漏抓（`刷` 也算 word char）。實測反例都在本庫裡抓到過：
#:   - `error` 純子字串會命中 `T-error-king Archfiend`（signals 1 筆）
#:   - `ズレ` 純子字串會命中 `コレクター·ズレ·ア`（コレクターズレア）與
#:     `アイ·ズレ·リーフ`（レッドアイズレリーフ）（comps 2 筆）
#: 所以 `ズレ` 只認複合詞（印刷ズレ／版ズレ／センターズレ／ズレエラー）。
#: ⚠️ **`レリーフ`／`ホロ` 不在這張表裡**：那是正規稀有度（アルティメットレア／
#: ホログラフィック，comps 裡 361／7 筆），列進來會把一大票正常卡誤判成變體。
VARIANT_TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"エラー", "エラー"),
    (r"ミスプリ", "ミスプリ"),
    (r"誤植", "誤植"),
    (r"印刷ミス", "印刷ミス"),
    # ズレ／ずれ 一律只認複合詞（見上面的 コレクターズレア 反例）。
    # ズレ／ずれ 一律只認複合詞，而且是**整詞**的交替（不是字元類）：
    # `[…センター…]ズレ` 那種寫法會被「コレクタ**ー**ズレア」的 `ー` 命中。
    (r"(?:印刷|版|枠|センター)(?:ズレ|ずれ)|(?:ズレ|ずれ)エラー", "印刷ズレ"),
    (r"裁断ミス|未裁断", "裁断ミス"),
    (r"(?<![A-Za-z])error(?![A-Za-z])", "error"),
    (r"(?<![A-Za-z])misprint(ed|s)?(?![A-Za-z])", "misprint"),
    (r"(?<![A-Za-z])missing(?![A-Za-z])", "missing"),
    (r"missing\s+foil|foil\s+missing|(?<![A-Za-z])no[\s-]?foil(?![A-Za-z])", "missing foil"),
    (r"(?<![A-Za-z])variant(?![A-Za-z])", "variant"),
    (r"(?<![A-Za-z])test\s+print(?![A-Za-z])", "test print"),
    (r"プロトタイプ", "プロトタイプ"),
)

_VARIANT_RE = tuple(
    (re.compile(pattern, re.IGNORECASE), label) for pattern, label in VARIANT_TITLE_PATTERNS
)


def variant_hits(title: str | None) -> tuple[str, ...]:
    """標題裡命中的變體線索（去重、保持詞表順序）。空 tuple ＝ 沒有線索。

    **沒命中不代表不是變體**——只代表賣家沒在標題上寫。這個函式的用途是
    「有寫就別給那個誤導的數字」，不是「沒寫就保證是正常版」。
    """
    text = title or ""
    return tuple(label for rx, label in _VARIANT_RE if rx.search(text))


# ---------------------------------------------------------------------------
@dataclass(slots=True, frozen=True)
class NotifyRules:
    """兩條規則的門檻。全部來自 `config/settings.yaml` 的 `notify:`。

    非法值一律退回預設並**印警告**（不靜默）：一個打錯的門檻會讓推播在
    使用者不知情的狀況下變寬鬆或整個閉嘴，兩種都比報錯難發現。
    """

    p_worth_min: float = DEFAULT_P_WORTH_MIN
    exclude_rarities: tuple[str, ...] = DEFAULT_EXCLUDE_RARITIES
    min_bids: int = DEFAULT_MIN_BIDS
    price_discovered_within_hours: float = PRICE_DYNAMICS_HOURS
    #: 規則 1 的時間窗。**不是 notify 區塊的設定**，來自 `bidding.actionable_window_hours`
    #: ——與 dashboard 的梯隊 1 共用同一顆旋鈕。
    actionable_window_hours: float = 24.0
    dedupe_days: int = DEFAULT_DEDUPE_DAYS
    max_items_per_run: int = DEFAULT_MAX_ITEMS_PER_RUN
    auction_urgent_enabled: bool = True
    high_p_enabled: bool = True
    seller_new_enabled: bool = True
    #: 規則 3：同儕相對折價門檻（比例，0.15 ＝ 便宜 15%）。
    seller_min_discount: float = DEFAULT_SELLER_MIN_DISCOUNT
    #: 規則 3：這筆標的至少要有幾個同儕才推。
    seller_min_peers: int = DEFAULT_SELLER_MIN_PEERS
    #: 規則 3：同儕算不出來時可不可以改用模型估值判定折價。
    #: false ＝ 回到 2026-08-04 之前的行為（沒有同儕就不推）。
    seller_model_fallback_enabled: bool = True
    #: 規則 3 模型 fallback 的折價門檻（比例）。
    seller_model_min_discount: float = DEFAULT_SELLER_MODEL_MIN_DISCOUNT
    #: 規則 3b：連模型都估不了的標的要不要浮出來（低音量）。
    seller_unpriced_enabled: bool = True
    #: 規則 3b 一輪的上限。**0 ＝ 一則都不送**（見常數註）。
    seller_unpriced_max_per_run: int = DEFAULT_SELLER_UNPRICED_MAX_PER_RUN
    #: 模型 fallback 的證據閘門——`bidding.EvidenceGate` 的**通知檔**（notify
    #: profile），不是另訂的標準：語意閘門（分數已知、L1/L2）與出價檔一樣硬，
    #: 只放寬「校準政策」那兩道（破口桶不拒收、校準殘差門檻 30）。依據：通知與
    #: 出價的錯誤代價不對稱（2026-08-07 重放 80 筆 unpriced，W3=11、W5=5——
    #: 被出價檔擋掉的正是全庫證據最強的標的）。frozen dataclass，共用預設實例安全。
    gate: EvidenceGate = EvidenceGate(
        min_calibration_samples=DEFAULT_NOTIFY_MIN_CALIBRATION_SAMPLES,
        reject_n_buckets=DEFAULT_NOTIFY_REJECT_N_BUCKETS,
    )
    #: **出價檔**的閘門，只用來做跨檔對照（「出價檔會拒」的 ⚠️ 標記）。
    #: 必須與 bidding.py 真正在用的那一份同源（`EvidenceGate.from_config(cfg)`），
    #: 不能在 notify 這一側自己拍一份門檻（工程原則 1）。
    bidding_gate: EvidenceGate = EvidenceGate()

    @classmethod
    def from_config(cls, cfg: Any) -> NotifyRules:
        notify = dict(getattr(cfg, "notify", None) or {})
        rules = dict(notify.get("rules") or {})
        urgent = dict(rules.get(RULE_AUCTION_URGENT) or {})
        high_p = dict(rules.get(RULE_HIGH_P) or {})
        seller = dict(rules.get(RULE_SELLER_NEW) or {})
        unpriced = dict(rules.get(RULE_SELLER_UNPRICED) or {})
        return cls(
            seller_new_enabled=bool(seller.get("enabled", True)),
            seller_model_fallback_enabled=bool(seller.get("model_fallback_enabled", True)),
            seller_model_min_discount=_float_setting(
                seller, f"rules.{RULE_SELLER_NEW}.model_min_discount",
                DEFAULT_SELLER_MODEL_MIN_DISCOUNT, lo=0.0, hi=1.0,
            ),
            seller_unpriced_enabled=bool(unpriced.get("enabled", True)),
            seller_unpriced_max_per_run=_int_setting(
                unpriced, f"rules.{RULE_SELLER_UNPRICED}.max_per_run",
                DEFAULT_SELLER_UNPRICED_MAX_PER_RUN,
            ),
            gate=EvidenceGate.from_config(cfg, profile=PROFILE_NOTIFY),
            bidding_gate=EvidenceGate.from_config(cfg),
            seller_min_discount=_float_setting(
                seller, f"rules.{RULE_SELLER_NEW}.min_discount",
                DEFAULT_SELLER_MIN_DISCOUNT, lo=0.0, hi=1.0,
            ),
            seller_min_peers=_int_setting(
                seller, f"rules.{RULE_SELLER_NEW}.min_peers", DEFAULT_SELLER_MIN_PEERS
            ),
            p_worth_min=_float_setting(
                high_p, f"rules.{RULE_HIGH_P}.p_worth_min", DEFAULT_P_WORTH_MIN,
                lo=0.0, hi=1.0,
            ),
            exclude_rarities=_rarity_setting(high_p, f"rules.{RULE_HIGH_P}.exclude_rarities"),
            min_bids=_int_setting(high_p, f"rules.{RULE_HIGH_P}.min_bids", DEFAULT_MIN_BIDS),
            price_discovered_within_hours=_float_setting(
                high_p,
                f"rules.{RULE_HIGH_P}.price_discovered_within_hours",
                PRICE_DYNAMICS_HOURS,
                lo=0.0,
                hi=None,
            ),
            actionable_window_hours=actionable_window_hours(cfg),
            dedupe_days=_int_setting(notify, "dedupe_days", DEFAULT_DEDUPE_DAYS),
            max_items_per_run=_cap_setting(notify, "max_items_per_run"),
            auction_urgent_enabled=bool(urgent.get("enabled", True)),
            high_p_enabled=bool(high_p.get("enabled", True)),
        )


def _float_setting(
    block: dict[str, Any], key: str, default: float, *, lo: float, hi: float | None
) -> float:
    raw = block.get(key.rsplit(".", 1)[-1], default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        print(f"[warn] notify.{key}={raw!r} 不是數字，改用 {default}")
        return default
    if value < lo or (hi is not None and value > hi):
        print(f"[warn] notify.{key}={value} 超出合理範圍，改用 {default}")
        return default
    return value


def _int_setting(block: dict[str, Any], key: str, default: int) -> int:
    raw = block.get(key.rsplit(".", 1)[-1], default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"[warn] notify.{key}={raw!r} 不是整數，改用 {default}")
        return default
    if value < 0:
        print(f"[warn] notify.{key}={value} 是負數，改用 {default}")
        return default
    return value


def _cap_setting(block: dict[str, Any], key: str) -> int:
    """一輪的推播則數上限。**`0` 與 `null` 都代表「不限」**，不是「一則都不送」。

    刻意與 `_int_setting` 分開：那一支對 `None` 會印警告並退回預設，而 yaml 的
    `max_items_per_run:`（留空）在這裡是一個**合法且有意義**的寫法。
    """
    if key not in block:
        return DEFAULT_MAX_ITEMS_PER_RUN
    if block.get(key) is None:
        return 0
    return _int_setting(block, key, DEFAULT_MAX_ITEMS_PER_RUN)


def _rarity_setting(block: dict[str, Any], key: str) -> tuple[str, ...]:
    """要排除哪些稀有度。設成空 list 是合法的「我要連普卡一起收」。

    `null`／`~` 也視為空清單。字串會被當成單一稀有度（少打一組括號不該
    變成「逐字元排除」）。
    """
    short = key.rsplit(".", 1)[-1]
    if short not in block:
        return DEFAULT_EXCLUDE_RARITIES
    raw = block.get(short)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        print(f"[warn] notify.{key}={raw!r} 不是清單，改用 {list(DEFAULT_EXCLUDE_RARITIES)}")
        return DEFAULT_EXCLUDE_RARITIES
    return tuple(str(x) for x in raw)


# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Match:
    """一則要送出去的推播。**訊息需要的數字全部在這裡湊齊了才會被建出來。**"""

    key: str
    rule: str
    row: dict[str, Any]
    #: payload 裡的 bid dict（規則 1 用）。規則 2 可能是 None（定價標的）。
    bid: dict[str, Any] | None = None
    #: `valuation.Estimate`（規則 2 用）。規則 1 不需要——上限與證據標籤在掃描
    #: 當下就算好存進 payload 了，推播時重算只會多一個分岔來源。
    estimate: Any = None
    hours_left: float | None = None
    #: 現價／上限／空間，**與 listing 同幣別**（`currency`；Yahoo=JPY、eBay 多為
    #: TWD）。舊名 room_jpy/current_bid_jpy/max_bid_jpy 在 eBay 進來之後就是謊言，
    #: 所以改成幣別中立的名字＋一個明確的幣別欄。
    room: float | None = None
    current_bid: float | None = None
    max_bid: float | None = None
    currency: str | None = None
    #: eBay 專用：原幣上限（**使用者填進出價欄的數字**）與原幣現價。
    max_bid_native: float | None = None
    current_bid_native: float | None = None
    native_currency: str | None = None
    p_worth: float | None = None
    rarity: str | None = None
    #: --- 規則 3 用（監控賣家新上架）------------------------------------
    seller_key: str | None = None
    #: 賣家的 Seller Alpha 分數。**None ＝ 未達評分門檻**（多半是手動加入的），
    #: 訊息會明講「手動加入、未達評分門檻」而不是印一個 0 分。
    seller_score: float | None = None
    #: `auto`／`manual`（`seller_watch` 的 source）。
    seller_watch_source: str | None = None
    #: 賣家**層級**的判定原文（`SellerScore.reason`）。這一行不可省略：
    #: 0 分的賣家（＝整體比同儕貴）也可能上架一件比同儕便宜的貨，只印
    #: 「0 分」會讓人以為工具壞了，只印「便宜 50%」又會讓人以為這個賣家很便宜。
    seller_score_note: str | None = None
    #: 這一筆的同儕相對折價（正數 ＝ 比同儕便宜幾 %）與它的來歷。
    peer_discount_pct: float | None = None
    peer_median_twd: float | None = None
    peer_n: int | None = None
    peer_sellers: int | None = None
    peer_tier_label: str | None = None
    #: 讀這則訊息時必須知道的前提（同儕只有 1 個賣家、跨度太短…）。
    seller_caveats: tuple[str, ...] = ()
    #: 這一筆的折價是**誰**算的：`SOURCE_PEER`（強）或 `SOURCE_MODEL`（弱）。
    #: 規則 3b 是 None（沒有折價可言）。**這一欄必須走到訊息上**。
    judgement_source: str | None = None
    #: 為什麼沒有同儕（模型 fallback 與規則 3b 都要把這句話印給使用者看）。
    peer_absent_reason: str | None = None
    #: 模型 fallback 的折價（正數 ＝ 比模型公允價便宜幾 %）。
    model_discount_pct: float | None = None
    #: 通知檔放行、但**出價檔會拒**這份估價的短理由（None ＝ 出價檔也會收）。
    #: 放寬的必須看得見：這一欄**必須走到訊息上**（⚠️ caveat，見
    #: `notify.format_seller_new`）——通知檔位刻意比出價鬆，鬆在哪裡不能只有
    #: 程式碼知道。
    bidding_reject_note: str | None = None
    #: --- 規則 3b 用（估不了）--------------------------------------------
    #: 為什麼估不了（閘門原文的第一句、或「疑似變體」、或「沒有區間」）。
    unpriced_reason: str | None = None
    #: 標題命中的變體線索（見 `variant_hits`）。
    variant_hits: tuple[str, ...] = ()
    #: 卡片屬性——規則 3b 沒有價格判斷，這幾欄就是使用者唯一的判斷材料。
    card_name: str | None = None
    grade: float | None = None
    era_evidence: tuple[str, ...] = ()

    @property
    def title(self) -> str:
        return str(self.row.get("title") or "")


@dataclass(slots=True)
class Skip:
    """被排除的一筆。preview 靠它回答「為什麼沒收到通知」。"""

    key: str
    title: str
    rule: str
    reason: str
    p_worth: float | None = None


@dataclass(slots=True)
class Outcome:
    """一輪推播判定的完整結果。**命中與送出是兩個數字**，不要合併：

    命中卻沒送出的原因（去重、總量上限、與規則 1 撞號）本身就是使用者
    需要看見的事——「今天沒好貨」與「有好貨但我沒說」不能長得一樣。
    """

    urgent: list[Match] = field(default_factory=list)
    high_p: list[Match] = field(default_factory=list)
    seller_new: list[Match] = field(default_factory=list)
    #: 規則 3b：監控賣家上了一件我們估不了的東西（沒有折價宣稱）。
    seller_unpriced: list[Match] = field(default_factory=list)
    #: 規則 4：指定卡狙擊命中（exact＋partial；near 在脈絡層就不進來）。
    card_snipe: list[Match] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    #: 通過去重、真的要送的（已依總量上限截斷）
    to_send: list[Match] = field(default_factory=list)
    #: 超出總量上限、併成一則統計的
    overflow: list[Match] = field(default_factory=list)
    #: 被去重擋下來的筆數（含「同一標的已由規則 1 送出」）
    deduped: int = 0
    #: 規則 2 這一輪算不算得出來（估價模型建不起來時為 False）
    valuation_ok: bool = True
    #: 規則 3 這一輪算不算得出來（監控名單／同儕脈絡建不起來時為 False）。
    #: **與「名單是空的」是兩件事**：後者 ok=True、命中 0（沒有賣家要看），
    #: 前者代表判定根本沒跑——降級要看得見（工程原則 3）。
    seller_ctx_ok: bool = True
    #: 實際送出的 (key, rule)，由 notifier 回填
    sent: list[tuple[str, str]] = field(default_factory=list)

    @property
    def matched(self) -> int:
        """**有判斷**的命中數。規則 3b 不算在內——它沒有宣稱任何東西便宜，
        把它加進來會讓「今天有幾個訊號」這個數字失去意義。
        規則 4 算在內：它宣稱「你登錄的那張卡出現了」，那是最強的一種判斷。"""
        return (len(self.urgent) + len(self.high_p) + len(self.seller_new)
                + len(self.card_snipe))

    def skips_for(self, reason_contains: str) -> list[Skip]:
        return [s for s in self.skipped if reason_contains in s.reason]


# ---------------------------------------------------------------------------
def evaluate(
    rows: list[dict[str, Any]],
    *,
    rules: NotifyRules,
    valuator: Any = None,
    now: datetime | None = None,
    notified: dict[tuple[str, str], str] | None = None,
    seller_ctx: Any = None,
    snipe_ctx: Any = None,
) -> Outcome:
    """吃候選列，回傳「這一輪該送什麼」。**只判定，不送、不落帳。**

    `notified` 是 `Store.notify_log_map()`；`valuator` 是 None 時規則 2 整條
    跳過（估價模型建不起來不能連帶讓競標急件也發不出去——規則 1 用的是
    掃描當下就存好的 payload，本來就不需要模型）。
    `seller_ctx` 是 `seller_watch.SellerNotifyContext`；None 時規則 3 整條跳過
    （同一個立場：一條規則的資料建不起來，不該讓另外兩條閉嘴）。
    `snipe_ctx` 是 `card_snipe.SnipeNotifyContext`；None 時規則 4 整條跳過
    （同一個立場）。
    """
    now = now or datetime.now(UTC)
    notified = notified or {}
    out = Outcome(
        valuation_ok=valuator is not None or not rules.high_p_enabled,
        seller_ctx_ok=seller_ctx is not None or not rules.seller_new_enabled,
    )

    for row in rows:
        key = str(row.get("key") or "")
        payload = _payload(row)
        listing_d = payload.get("listing") or {}
        bid_d = payload.get("bid") if isinstance(payload.get("bid"), dict) else None

        if rules.auction_urgent_enabled:
            m, skip = _match_urgent(row, key, listing_d, bid_d, rules, now)
            if m is not None:
                out.urgent.append(m)
            elif skip is not None:
                out.skipped.append(skip)

        if rules.high_p_enabled and valuator is not None:
            m, skip = _match_high_p(row, key, payload, listing_d, rules, valuator, now)
            if m is not None:
                out.high_p.append(m)
            elif skip is not None:
                out.skipped.append(skip)

        if rules.seller_new_enabled and seller_ctx is not None:
            m, skip = _match_seller_new(row, key, listing_d, rules, seller_ctx, valuator)
            if m is not None and m.rule == RULE_SELLER_UNPRICED:
                out.seller_unpriced.append(m)
            elif m is not None:
                out.seller_new.append(m)
            elif skip is not None:
                out.skipped.append(skip)

    # 規則 4：狙擊命中不來自 signals 候選池（它們在商業過濾前就被記帳），
    # 由 snipe_ctx 帶進來。pending 已在脈絡層濾掉 near 與已送過的。
    if snipe_ctx is not None:
        for hit in snipe_ctx.pending:
            # ⚠️ 不要傳 title=：`Match.title` 是唯讀 property，從 row["title"]
            # 推導。傳了會 TypeError。
            out.card_snipe.append(Match(
                key=f"{hit['watch_id']}:{hit['listing_key']}",
                rule=RULE_CARD_SNIPE,
                row=hit,
            ))

    _apply_dedupe_and_cap(out, rules, notified, now)
    return out


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("payload") or "{}") or {}
    except (TypeError, ValueError):
        return {}


# --- 規則 1 ---------------------------------------------------------------
def _match_urgent(
    row: dict[str, Any],
    key: str,
    listing_d: dict[str, Any],
    bid_d: dict[str, Any] | None,
    rules: NotifyRules,
    now: datetime,
) -> tuple[Match | None, Skip | None]:
    """梯隊 1 就是這條規則。**判定整個外包給 `bidding.auction_tier`。**"""
    if not listing_d:
        return None, None
    try:
        listing = listing_from_payload(listing_d)
    except (KeyError, ValueError, TypeError):
        return None, None
    if not is_live_auction(listing):
        return None, None

    current = listing.price
    end_time = listing_d.get("end_time")
    tier = auction_tier(
        bid_d, current, end_time, now, window_hours=rules.actionable_window_hours
    )
    if tier != 1:
        return None, None

    # 到這裡梯隊已經是 1，所以上限、現價、空間、倒數必定都有值。仍然再問一次
    # 而不是 assert：這幾個數字要進訊息，而訊息會變成真錢的出價（紅線）。
    # 上限與空間走幣別中立的那組函式——與 `auction_tier` 用同一個基準
    # （listing 自己的幣別），Yahoo=日圓、eBay=台幣，兩邊都是同單位相減。
    ceiling = ceiling_value_of(bid_d)
    room = auction_room_value(bid_d, current)
    left = hours_until_end(end_time, now)
    if bid_d is None or ceiling is None or room is None or left is None or current is None:
        return None, None
    fields = {
        "標題": row.get("title"),
        "連結": row.get("url"),
        "證據標籤": bid_d.get("evidence_label"),
        "證據強度": bid_d.get("evidence_tier_label"),
        "以上限成交的到手成本": bid_d.get("landed_at_ceiling_twd"),
    }
    # eBay：使用者要填進出價欄的是**原幣**數字，缺了它整則訊息就沒有可執行的
    # 動作——列入必備欄位（缺值整則不送，與其他欄位同一條紅線）。
    is_ebay = str(listing_d.get("site") or "") == "ebay"
    if is_ebay:
        fields["原幣上限"] = bid_d.get("max_bid_native")
        fields["原幣幣別"] = bid_d.get("native_currency")
    missing = _missing_fields(fields)
    if missing:
        return None, Skip(
            key, str(row.get("title") or ""), RULE_AUCTION_URGENT,
            f"欄位缺值不送：{missing}",
        )
    current_native = None
    if is_ebay:
        # 原幣現價：從 listing 自己的金額節點讀（與上限同一個換算來源）。
        # 讀不到不擋整則——它是輔助顯示，台幣現價（必備）已經在了。
        from .sources.ebay import native_price_info

        info = native_price_info(listing_d.get("raw") or {})
        current_native = info.value if info is not None else None
    return Match(
        key=key,
        rule=RULE_AUCTION_URGENT,
        row=row,
        bid=bid_d,
        hours_left=left,
        room=room,
        current_bid=float(current),
        max_bid=ceiling,
        currency=str(listing_d.get("currency") or "") or None,
        max_bid_native=bid_d.get("max_bid_native"),
        current_bid_native=current_native,
        native_currency=bid_d.get("native_currency"),
    ), None


# --- 規則 2 ---------------------------------------------------------------
def _match_high_p(
    row: dict[str, Any],
    key: str,
    payload: dict[str, Any],
    listing_d: dict[str, Any],
    rules: NotifyRules,
    valuator: Any,
    now: datetime,
) -> tuple[Match | None, Skip | None]:
    from .valuation import card_attrs_from_row, estimate_signal_row

    title = str(row.get("title") or "")
    try:
        estimate = estimate_signal_row(valuator, row)
    except Exception as exc:  # noqa: BLE001 - 單筆估價失敗不拖垮整輪
        return None, Skip(key, title, RULE_HIGH_P, f"估價失敗，不送：{exc}")

    p = estimate.p_worth_buying
    if p is None or p <= rules.p_worth_min:
        return None, None  # 沒過門檻不是「被排除」，不必列進 skipped 洗版

    # 稀有度與掃描當下同源（payload 優先、退回標題重抽），與 dashboard 看到的一致。
    _name, rarity, _grade = card_attrs_from_row(valuator, row)
    if rarity is not None and rarity in rules.exclude_rarities:
        return None, Skip(key, title, RULE_HIGH_P, f"排除稀有度：{rarity}", p)

    # 賣家明確不寄台灣（eBay 的 us_ship_option 情形）：P 值是用「寄到台灣的
    # 到手成本」算的，而那筆交易不存在——推播會讓人照著一個假成本下單。
    # 這種標的留在 dashboard（旗標看得到），但不主動打擾（誠實邊界）。
    if listing_d.get("ships_to_tw") is False:
        return None, Skip(
            key, title, RULE_HIGH_P,
            "賣家不寄台灣（可寄美國地址）：美國→台灣轉運成本未建模，不推播", p,
        )

    skip = _price_discovery_skip(key, title, listing_d, rules, now, p)
    if skip is not None:
        return None, skip

    if not estimate.has_interval:
        return None, Skip(key, title, RULE_HIGH_P, "欄位缺值不送：80% 區間", p)
    route_label = ((payload.get("best_route") or {}).get("label")) or row.get("route")
    missing = _missing_fields(
        {
            "標題": title,
            "連結": row.get("url"),
            "到手成本": row.get("landed_twd"),
            "現價": row.get("price_native"),
            "幣別": row.get("currency"),
            "路徑": route_label,
            "公允價": estimate.fair_twd,
        }
    )
    if missing:
        return None, Skip(key, title, RULE_HIGH_P, f"欄位缺值不送：{missing}", p)

    return Match(
        key=key, rule=RULE_HIGH_P, row=row, estimate=estimate, p_worth=p, rarity=rarity
    ), None


def _price_discovery_skip(
    key: str,
    title: str,
    listing_d: dict[str, Any],
    rules: NotifyRules,
    now: datetime,
    p: float,
) -> Skip | None:
    """競標的價格被市場發現過了嗎？沒有就排除（理由見模組頂註）。"""
    try:
        listing = listing_from_payload(listing_d) if listing_d else None
    except (KeyError, ValueError, TypeError):
        listing = None
    if listing is None or not is_live_auction(listing):
        return None  # 定價標的：現價就是成交價，沒有「還沒被發現」的問題

    bids = listing.bids
    if bids is None:
        return Skip(key, title, RULE_HIGH_P, "競標但來源沒給出價次數，無法確認價格已被發現", p)
    if int(bids) < rules.min_bids:
        return Skip(
            key, title, RULE_HIGH_P,
            f"競標尚未被競價（出價 {int(bids)} 次 < {rules.min_bids}），現價＝起標價，P 值高估",
            p,
        )
    left = hours_until_end(listing_d.get("end_time"), now)
    if left is None:
        return Skip(key, title, RULE_HIGH_P, "競標但沒有結標時間，無法判斷價格是否已定", p)
    if left <= 0:
        return Skip(key, title, RULE_HIGH_P, f"競標已結標（{-left:.0f} 小時前）", p)
    if left > rules.price_discovered_within_hours:
        return Skip(
            key, title, RULE_HIGH_P,
            f"競標還有 {left:.0f} 小時才結標（>{rules.price_discovered_within_hours:.0f}），"
            "現價還會漲，P 值高估",
            p,
        )
    return None


# --- 規則 3 ---------------------------------------------------------------
def _match_seller_new(
    row: dict[str, Any],
    key: str,
    listing_d: dict[str, Any],
    rules: NotifyRules,
    ctx: Any,
    valuator: Any = None,
) -> tuple[Match | None, Skip | None]:
    """監控名單賣家的新上架 ＋ 折價達門檻（同儕優先，沒有同儕退模型）。

    條件是**串聯**的，而且順序刻意（由便宜到貴、由「與這個人無關」到「要算數字」）：
      1. 這筆的賣家在監控名單上嗎？不在 → 直接離開，**不記 skipped**
         （全庫幾百筆標的，逐筆記「這不是你在追的賣家」只會把 preview 洗版）。
      2. 這是新標的嗎？（`listing_obs.seen_count <= 1` ＝ 第一次進帳本）
         不是 → 離開。舊標的的「新上架」是假的。
      3. 同儕相對折價 ≥ `seller_min_discount`？→ 規則 3（👤 強）。
      4. 同儕算不出來 → 模型 fallback：過 `rules.gate`（通知檔位）且折價 ≥
         `seller_model_min_discount` → 規則 3（🤖 弱）。
      5. 閘門擋下／沒有區間／疑似變體 → 規則 3b（🔍 估不了，低音量）。

    回傳的 Match 可能是 `RULE_SELLER_NEW` **或** `RULE_SELLER_UNPRICED`，
    呼叫端依 `m.rule` 分流。
    """
    title = str(row.get("title") or "")
    seller_key = ctx.seller_of(listing_d.get("site"), listing_d.get("seller_id"))
    if seller_key is None:
        return None, None

    obs = (ctx.obs or {}).get(key)
    if obs is None:
        return None, Skip(
            key, title, RULE_SELLER_NEW,
            "監控賣家的標的但不在 listing_obs 帳本上，無法判斷是不是新上架，不推",
        )
    try:
        seen_count = int(obs.get("seen_count") or 0)
    except (TypeError, ValueError):
        seen_count = 0
    if seen_count > 1:
        return None, None  # 不是新上架：不是被排除，是根本沒觸發

    item = (ctx.items or {}).get(key)
    peer_absent = _peer_absent_reason(item, rules)
    if peer_absent is not None:
        return _model_fallback(
            row, key, title, rules, ctx, seller_key, valuator, peer_absent
        )

    discount = item.discount_pct  # 正數 ＝ 比同儕便宜幾 %
    if discount is None or discount < rules.seller_min_discount * 100:
        return None, None  # 沒過折價門檻不是「被排除」，不必列進 skipped 洗版

    watch = (ctx.watch or {}).get(seller_key) or {}
    score_obj = (ctx.scores or {}).get(seller_key)
    missing = _missing_fields(
        {
            "標題": title,
            "連結": row.get("url"),
            "到手成本": row.get("landed_twd"),
            "同儕中位": item.peer.peer_median_twd,
        }
    )
    if missing:
        return None, Skip(key, title, RULE_SELLER_NEW, f"欄位缺值不送：{missing}")

    caveats = list(getattr(score_obj, "caveats", ()) or ())
    if item.peer.peer_sellers <= 1:
        caveats.insert(
            0,
            f"⚠️ 這筆的同儕只來自 {item.peer.peer_sellers} 個已知賣家"
            f"（另有 {item.peer.peer_unknown_seller_n} 筆賣家未知）——折價是一次 1v1 比價",
        )
    # 同儕路徑也會踩到變體陷阱：同儕是用**卡名**配出來的，配到的是母卡。
    # 這裡不改判定（同儕仍然是最強的證據），只把前提講出來。
    variants = variant_hits(title)
    if variants:
        caveats.insert(
            0,
            f"⚠️ 標題含變體線索（{'、'.join(variants)}）——同儕是用卡名配的，"
            "比到的可能是**正常版母卡**；變體通常更貴，這個折價可能被低估",
        )
    return Match(
        key=key,
        rule=RULE_SELLER_NEW,
        row=row,
        seller_key=seller_key,
        seller_score=(score_obj.total if (score_obj is not None and score_obj.ok) else None),
        seller_watch_source=str(watch.get("source") or ""),
        seller_score_note=(
            score_obj.reason if (score_obj is not None and score_obj.ok) else None
        ),
        peer_discount_pct=discount,
        peer_median_twd=item.peer.peer_median_twd,
        peer_n=item.peer.peer_n,
        peer_sellers=item.peer.peer_sellers,
        peer_tier_label=item.peer.tier_label,
        seller_caveats=tuple(caveats[:3]),
        judgement_source=SOURCE_PEER,
        variant_hits=variants,
    ), None


def _peer_absent_reason(item: Any, rules: NotifyRules) -> str | None:
    """同儕**不能用**的原因（None ＝ 可以用，走同儕路徑）。

    這句話會原封不動印給使用者看，所以它要回答的是「為什麼這張卡沒有同儕」，
    不是「判定失敗」。四種原因各自不同，不能壓成一句：前兩種是**資料密度**
    （這張卡在市場列裡就是孤例），後兩種是**證據層級／門檻**。

    ⚠️ 這裡刻意**不引用 signals 的 `comps_n`**：那個計數是 scoring 那一側用
    另一套比對算的，與同儕池（`seller_alpha.PeerIndex`，由 `market_rows_from_store`
    建）不同源。實測會踩到：`buyee_paypay:z585911874` 的 `comps_n=0`，但同一則
    訊息裡模型的同層成交是 3 筆——同一則訊息裡放兩個不同來源的樣本數，
    使用者只會讀到「這個工具在自相矛盾」（工程原則 1）。
    """
    if item is None:
        return (
            "這一筆沒有進同儕比對（沒有成為市場列：多半是卡名比對不到或缺價格基準）"
        )
    if item.peer is None:
        return (
            "同儕池裡找不到**別的賣家**的同款——同卡×同版次×同稀有度×同分數沒有，"
            "一路放寬到同稀有度×同分數也沒有"
        )
    if not item.scoring:
        return (
            f"只比得到「{item.peer.tier_label}」這一層"
            "（不計分：那量到的是卡種組合，不是賣家定價）"
        )
    if item.peer.peer_n < rules.seller_min_peers:
        return f"同儕只有 {item.peer.peer_n} 筆（門檻 {rules.seller_min_peers} 筆）"
    return None


def _model_fallback(
    row: dict[str, Any],
    key: str,
    title: str,
    rules: NotifyRules,
    ctx: Any,
    seller_key: str,
    valuator: Any,
    peer_absent: str,
) -> tuple[Match | None, Skip | None]:
    """沒有同儕時的第二條路：模型估值（弱）或「估不了」（規則 3b）。

    順序刻意：**變體線索排在閘門之前**。變體卡的問題不是證據薄，是模型
    比對到了**錯的那張卡**——閘門檢查的是「這個估計夠不夠可信」，而這一筆
    的估計就算每一道閘門都過，它估的也是母卡。過得了閘門反而更危險
    （會印出一個看起來有憑有據的折價）。
    """
    from .valuation import card_attrs_from_row, estimate_signal_row

    if not rules.seller_model_fallback_enabled:
        return None, Skip(
            key, title, RULE_SELLER_NEW,
            f"同儕算不出來（{peer_absent}）；模型 fallback 已關閉（"
            f"rules.{RULE_SELLER_NEW}.model_fallback_enabled=false），不推",
        )
    if valuator is None:
        return None, Skip(
            key, title, RULE_SELLER_NEW,
            f"同儕算不出來（{peer_absent}）；估價模型這一輪建不起來，"
            "模型 fallback 也無從判定，不推",
        )
    try:
        estimate = estimate_signal_row(valuator, row)
    except Exception as exc:  # noqa: BLE001 - 單筆估價失敗不拖垮整輪
        return None, Skip(key, title, RULE_SELLER_NEW, f"估價失敗，不送：{exc}")

    card_name, rarity, grade = card_attrs_from_row(valuator, row)
    common: dict[str, Any] = {
        "key": key, "row": row, "seller_key": seller_key, "estimate": estimate,
        "peer_absent_reason": peer_absent, "card_name": card_name, "rarity": rarity,
        "grade": grade, "era_evidence": _era_evidence(row),
        "variant_hits": variant_hits(title),
    }
    common.update(_seller_fields(ctx, seller_key))

    if common["variant_hits"]:
        return _unpriced(
            rules, common, title,
            f"標題含變體線索（{'、'.join(common['variant_hits'])}）"
            "：**模型估值不適用**。卡名比對配到的是正常版母卡，而變體通常貴得多"
            "——模型會把它算成「太貴、不值得」，那個方向的錯誤正好會讓你錯過夢幻逸品",
        )
    gate_reason = rules.gate.check(estimate)
    if gate_reason is not None:
        return _unpriced(rules, common, title, _first_sentence(gate_reason))
    if estimate.fair_twd is None or not estimate.has_interval:
        return _unpriced(
            rules, common, title,
            "模型過了證據閘門但給不出公允價或 80% 區間（校準集不足）"
            "——沒有數字就不給數字",
        )

    landed = row.get("landed_twd")
    if landed is None or float(landed) <= 0:
        return None, Skip(key, title, RULE_SELLER_NEW, "欄位缺值不送：到手成本")
    discount = (1.0 - float(landed) / float(estimate.fair_twd)) * 100.0
    if discount < rules.seller_model_min_discount * 100:
        return None, None  # 有判斷、判斷是「不夠便宜」——不必列進 skipped 洗版

    missing = _missing_fields({"標題": title, "連結": row.get("url")})
    if missing:
        return None, Skip(key, title, RULE_SELLER_NEW, f"欄位缺值不送：{missing}")
    return Match(
        rule=RULE_SELLER_NEW,
        judgement_source=SOURCE_MODEL,
        model_discount_pct=discount,
        # 跨檔對照：通知檔放行、但出價檔會拒的，要在訊息上講出來（⚠️ caveat）。
        # 拿**真的**出價檔（rules.bidding_gate）來比，不是通知檔自問自答。
        bidding_reject_note=bidding_gate_note(rules.bidding_gate, estimate),
        **common,
    ), None


def _unpriced(
    rules: NotifyRules, common: dict[str, Any], title: str, reason: str
) -> tuple[Match | None, Skip | None]:
    """規則 3b 的 Match（或整條關閉時的一筆 skipped）。

    **不宣稱任何東西便宜**——`judgement_source` 留 None、折價欄全部空著。
    這則訊息唯一的宣稱是「監控賣家上了一件我們估不了的東西」。
    """
    key = common["key"]
    if not rules.seller_unpriced_enabled:
        return None, Skip(
            key, title, RULE_SELLER_UNPRICED,
            f"估不了（{reason[:40]}…）且規則 3b 已關閉，不推",
        )
    missing = _missing_fields(
        {"標題": title, "連結": common["row"].get("url"),
         "到手成本": common["row"].get("landed_twd")}
    )
    if missing:
        return None, Skip(key, title, RULE_SELLER_UNPRICED, f"欄位缺值不送：{missing}")
    return Match(rule=RULE_SELLER_UNPRICED, unpriced_reason=reason, **common), None


def _seller_fields(ctx: Any, seller_key: str) -> dict[str, Any]:
    """賣家層級的三個欄位（名單來源、分數、分數的判定原文）。

    ⚠️ 分數一律取自 `ctx.scores`，而 `ctx` 是 `build_notify_context` 用
    **不帶 valuator** 的 `seller_alpha.analyze` 建出來的——模型 fallback 走到
    這裡也只是「讀」分數，不會回寫、也不參與分數計算（模組頂註第 3 點）。
    """
    watch = (ctx.watch or {}).get(seller_key) or {}
    score_obj = (ctx.scores or {}).get(seller_key)
    ok = score_obj is not None and score_obj.ok
    return {
        "seller_watch_source": str(watch.get("source") or ""),
        "seller_score": (score_obj.total if ok else None),
        "seller_score_note": (score_obj.reason if ok else None),
        "seller_caveats": tuple((getattr(score_obj, "caveats", ()) or ())[:3]),
    }


def _era_evidence(row: dict[str, Any]) -> tuple[str, ...]:
    """payload 裡的年代證據（`card.era_evidence`）。估不了的時候這是判斷材料之一。"""
    try:
        card = (json.loads(row.get("payload") or "{}") or {}).get("card") or {}
    except (TypeError, ValueError):
        return ()
    ev = card.get("era_evidence")
    return tuple(str(x) for x in ev) if isinstance(ev, (list, tuple)) else ()


def _first_sentence(text: str) -> str:
    """閘門理由的第一句。全文是給 dashboard 的，推播上放不下（也沒人在手機上讀）。"""
    head = text.split("。", 1)[0].strip()
    return head + "。" if head else text


def _missing_fields(fields: dict[str, Any]) -> str:
    """回傳缺值欄位的中文清單（空字串＝都齊）。空字串也算缺值。"""
    missing = [name for name, v in fields.items() if v is None or v == ""]
    return "、".join(missing)


# --- 去重與總量上限 --------------------------------------------------------
def _apply_dedupe_and_cap(
    out: Outcome,
    rules: NotifyRules,
    notified: dict[tuple[str, str], str],
    now: datetime,
) -> None:
    """去重規則（兩條規則刻意不同）：

    - 規則 1 競標急件：同一標的**這輩子只送一次**。它的觸發條件本身只會發生
      一次（進入結標窗到結標為止），而且不受規則 2 是否送過影響——那是另一件
      事實。用 `dedupe_days` 反而會在 7 天後對同一個標的再吵一次。
    - 規則 2 高信心：`dedupe_days` 天內同一標的只送一次。它的觸發條件會一直
      成立（P 值不會自己掉下來），沒有時間窗就是每輪洗版。

    - 規則 3 監控賣家新上架：同一標的**這輩子只送一次**（與規則 1 同一個形狀）。
      它的觸發條件本身只會發生一次——「第一次進 listing_obs」是不可逆的事實，
      下一輪 `seen_count` 就 ≥2 了。用 `dedupe_days` 反而會在 7 天後對同一個
      標的再吵一次（而且那時它早就不是「新上架」了）。

    取捨：一個標的可能先收到規則 2、幾天後再收到規則 1 的急件——**這是刻意的**，
    第二則講的是「快結標了」，不是重複。反過來同一輪同時命中兩條規則時只送
    規則 1（它嚴格更急、內容也涵蓋證據），規則 2 那則記為 deduped。
    規則 3 **不與其他兩條互斥**：它講的是「你在追的這個賣家剛上了一件比同儕
    便宜的貨」，那是另一件事實（賣家層級），不是同一則訊息的重複。

    - 規則 3b 估不了：同樣是**這輩子只送一次**，而且**自己一本去重帳**
      （`notify_log` 的 rule 欄位是 `seller_unpriced`，與 `seller_new` 不同鍵）。
      分開記帳是必要的：一筆標的可能今天估不了、下週有了成交樣本就估得出來，
      那時它該能以規則 3 的身分再送一次——共用一本帳會把第二則吞掉。
      3b 另有**自己的每輪上限**（`seller_unpriced_max_per_run`），不跟三條有判斷
      的規則搶額度：它是「沒有判斷、只是叫你看一眼」的通知，音量必須是被限制的
      那一種。超量的那幾筆記進 `skipped`（preview 看得到），**不進 overflow**：
      overflow 講的是「全域上限 `max_items_per_run` 砍掉的」，兩個上限是不同的
      機制，混在同一則統計裡會讓「為什麼沒收到」變成一個答不出來的問題。
      超量時保留**到手成本最高**的幾筆：估不了的東西無從比較，唯一能排序的是
      「漏掉的代價」。
      ⚠️ 被上限砍掉的那幾筆**不保證下一輪還在**：它們沒有落已通知帳，所以只要
      `seen_count` 還是 1 就會重新排隊，但 `seen_count` 會在這個賣家下一次被
      輪替掃到時（預設 4 小時）變成 2，那之後它就永遠不再是「新上架」。
      實測（2026-08-04，本庫）：37 筆命中、上限 3 → 約 4 小時的窗口裡浮得出
      12 筆左右，其餘留在 dashboard。要多看一點就把 `max_per_run` 調大。

    - 規則 4 指定卡狙擊：同一 `{watch_id}:{listing_key}` **這輩子只送一次**
      （同規則 1／3 的形狀）。🎯 exact **完全不受 `max_items_per_run` 裁切**——
      使用者登錄的可能是半年才出現一次的東西，被當輪其他規則的音量擠掉一次
      ＝白等半年，而「被 cap 砍掉」與「今天沒出現」在使用者眼裡一模一樣。
      👀 partial 另有自己的每輪小上限（`card_snipe.PARTIAL_MAX_PER_RUN`），
      超量的進 `skipped`、**不落已通知帳**（下輪重新排隊），也**不進 overflow**
      ——overflow 講的是全域上限砍掉的，兩個機制不能混在同一則統計裡。
    """
    cutoff = now - timedelta(days=rules.dedupe_days)
    urgent_keys = {m.key for m in out.urgent}
    sendable: list[Match] = []

    for m in out.urgent:
        if (m.key, RULE_AUCTION_URGENT) in notified:
            out.deduped += 1
            continue
        sendable.append(m)

    for m in out.high_p:
        if m.key in urgent_keys:
            out.deduped += 1
            out.skipped.append(
                Skip(m.key, m.title, RULE_HIGH_P, "同一標的由規則 1 涵蓋（急件優先）", m.p_worth)
            )
            continue
        last = notified.get((m.key, RULE_HIGH_P))
        if last is not None and _parse_ts(last) is not None and _parse_ts(last) > cutoff:
            out.deduped += 1
            continue
        sendable.append(m)

    for m in out.seller_new:
        if (m.key, RULE_SELLER_NEW) in notified:
            out.deduped += 1
            continue
        sendable.append(m)

    sendable.extend(_unpriced_sendable(out, rules, notified))

    # --- 規則 4：終身一次；🎯 exact 不受總量上限，👀 partial 有自己的小上限 ---
    from .card_snipe import PARTIAL_MAX_PER_RUN, TIER_EXACT

    snipe_exact: list[Match] = []
    snipe_partial: list[Match] = []
    for m in out.card_snipe:
        if (m.key, RULE_CARD_SNIPE) in notified:
            out.deduped += 1
            continue
        if str(m.row.get("tier") or "") == TIER_EXACT:
            snipe_exact.append(m)
        else:
            snipe_partial.append(m)
    if len(snipe_partial) > PARTIAL_MAX_PER_RUN:
        for m in snipe_partial[PARTIAL_MAX_PER_RUN:]:
            out.skipped.append(Skip(
                m.key, m.title, RULE_CARD_SNIPE,
                f"本輪 👀 疑似命中已達上限 {PARTIAL_MAX_PER_RUN} 則——"
                "未送、沒落帳，下輪重新排隊；dashboard 狙擊分頁看得到全部",
            ))
        snipe_partial = snipe_partial[:PARTIAL_MAX_PER_RUN]
    snipe_sendable = snipe_exact + snipe_partial

    # `max_items_per_run <= 0` ＝ 不限（見 DEFAULT_MAX_ITEMS_PER_RUN 的註記）。
    # 不限時 overflow 必須是空 list，否則 notifier 會多送一則「另有 0 筆」的統計。
    # 狙擊排最前、且**不吃 cap**：使用者等的可能是半年才出現一次的東西，被別的
    # 規則的音量擠掉一次＝白等半年。它的音量由上面那兩個機制自己管。
    cap = rules.max_items_per_run
    if cap <= 0:
        out.to_send = snipe_sendable + sendable
        out.overflow = []
        return
    out.to_send = snipe_sendable + sendable[:cap]
    out.overflow = sendable[cap:]


def _unpriced_sendable(
    out: Outcome, rules: NotifyRules, notified: dict[tuple[str, str], str]
) -> list[Match]:
    """規則 3b 的去重 ＋ 自己的音量上限。理由見 `_apply_dedupe_and_cap` 的 docstring。"""
    fresh = [m for m in out.seller_unpriced if (m.key, RULE_SELLER_UNPRICED) not in notified]
    out.deduped += len(out.seller_unpriced) - len(fresh)
    cap = rules.seller_unpriced_max_per_run
    if len(fresh) <= cap:
        return fresh
    fresh.sort(key=lambda m: float(m.row.get("landed_twd") or 0.0), reverse=True)
    for m in fresh[cap:]:
        out.skipped.append(
            Skip(
                m.key, m.title, RULE_SELLER_UNPRICED,
                f"本輪「估不了」通知已達上限 {cap} 則（保留到手成本最高的幾筆），"
                "這筆未送、也沒落已通知帳；只要它還算新標的就會在下一輪重新排隊，"
                "dashboard 的賣家分頁隨時看得到",
            )
        )
    return fresh[:cap]


def _parse_ts(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


__all__ = [
    "DEFAULT_EXCLUDE_RARITIES",
    "DEFAULT_P_WORTH_MIN",
    "DEFAULT_SELLER_MIN_DISCOUNT",
    "DEFAULT_SELLER_MODEL_MIN_DISCOUNT",
    "DEFAULT_SELLER_UNPRICED_MAX_PER_RUN",
    "RULE_AUCTION_URGENT",
    "RULE_CARD_SNIPE",
    "RULE_HIGH_P",
    "RULE_SELLER_NEW",
    "RULE_SELLER_UNPRICED",
    "RULE_LABEL",
    "SOURCE_MODEL",
    "SOURCE_PEER",
    "VARIANT_TITLE_PATTERNS",
    "Match",
    "NotifyRules",
    "Outcome",
    "Skip",
    "evaluate",
    "variant_hits",
]
