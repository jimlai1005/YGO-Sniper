"""卡片主檔：把一行賣家標題釘到一張具體的卡。

沒有卡名，comps 就只能按「稀有度×分數」分桶——那等於把青眼の白龍和
一張同樣是 PSA9 ウルトラ 的雜魚卡丟進同一個中位數裡。卡名是分桶的第一維度。

**資料來源（兩個，各補對方的缺口）**

  YGOPRODeck  `cardinfo.php?misc=yes` —— 免費無金鑰，但**沒有日文卡名**
              （實測 `language=ja` 直接回 error，`misc_info` 只有 views／
              tcg_date／ocg_date／konami_id 這些）。真正有用的是
              `misc_info[0].ocg_date`：OCG（日版）首發日，這就是最乾淨的
              年代判準——比拿 set code 前綴猜準得多。
  yaml-yugi   DawnbrandBots/yaml-yugi 的 aggregate 分支 `cards.json` —— 有
              `name.ja`，以 `password`（＝YGOPRODeck 的 `id`）與 `konami_id`
              對得起來。實測 1998-2004 子集 2016 張，日文名命中 2016/2016。

日文名帶 ruby 標記（`<ruby>青眼の白龍<rt>ブルーアイズ・ホワイト・ドラゴン</rt></ruby>`），
所以要拆兩個鍵：漢字本體（賣家最常打的）＋整名單一 ruby 區塊時的片假名讀音
（「ブルーアイズ・ホワイト・ドラゴン」也是賣家會打的寫法）。逐字注音那種
（３<rt>まん</rt>年<rt>ねん</rt>…）串起來是「まんねん…」，沒有人會這樣打，
所以只收「整個名字是一個 ruby 區塊且讀音不含平假名」的那 91 張。

**為什麼是明確的 refresh 指令而不是 TTL**：1998-2004 發行過哪些卡是**不會變的
歷史事實**，沒有任何理由讓每小時跑一次的 scan 去打外網重抓（而且 yaml-yugi
那份是 98MB）。所以主檔只在 `ygo-sniper refresh-cards` 時更新，scan 只讀本地檔。
主檔不存在時整個模組安靜降級（`available == False`），估價自動退到不看卡名的那層。

**比對規則**：最長子字串優先。「青眼の白龍」不可以被「青眼」搶走，所以
候選鍵一律按長度排序後取全域最長命中。比對前兩邊都做同一套折疊
（NFKC → 小寫 → 片假名轉平假名 → 去空白與中黑點與連字號），因為賣家
會寫「ブラックマジシャンガール」而卡名是「ブラック・マジシャン・ガール」、
會寫「ハニワ」而卡名是「はにわ」。折疊只做在比對鍵上，回傳的永遠是原始卡名。

── 2026-08-02：為了提高命中率加的三條規則，每條都附它的防誤配設計 ──

實測基線（db 裡 1786 筆真實標題）：整體 85.8%，其中 **eBay 0/103**——
eBay 標題是英文，主檔的 `name_en` 當時完全沒進索引。三條修法：

1. **英文卡名進索引**（`name_en`）。純英數的鍵長度門檻拉到
   `min_ascii_key_len`（預設 6）：「Yami」「Wall」這種短英文名會被
   「YAMI YUGI」之類的字誤觸發，寧可漏掉。
2. **套組名剝除**（主檔的 `set_names`）。這條不是選配，是**防誤配的必要
   條件**：eBay 標題長成 `LOB-LEGEND OF BLUE EYES WHITE DRAGON 1ST ED
   #075 MAN EATER`——套組名「Legend of Blue Eyes White Dragon」**內含**
   卡名「Blue-Eyes White Dragon」。沒有這一步，實測 60 筆幾百塊的雜魚卡
   全部被配成青眼の白龍（全庫最貴的卡），估價會爆高。剝除只拿掉套組名
   那一段，同一個標題裡另外寫的真卡名仍然留著。
3. **卡號反查**（主檔的 `set_codes`）——但**只當卡名比不到時的後備**，
   而且**標題要帶「遊戯王／Yu-Gi-Oh」字樣**才啟用。兩個限制都是實測換來的：
     · 卡名優先：實測 6 筆「卡名與卡號指到不同卡」的標題，4 筆卡號對
       （賣家把卡名打錯：グレイモア→グレイモヤ、ビック→ビッグ）、
       2 筆卡名對（賣家寫了套組裡另一張卡的號）。卡名錯的那 4 筆本來就
       比不到卡名、照樣會走到後備；卡號錯的那 2 筆若讓卡號優先就會配錯。
     · 遊戯王字樣：沒有這道閘門時，`ARS アルスコーポレーション ピストル型鋸
       … PS-18 のこぎり`（一把園藝鋸）被 PS-18 配成「クラブ・タートル」。
       一串裸卡號在非遊戯王商品裡是雜訊，不是證據。
   卡號只收「該前綴的所有印刷都落在 1998-2004」的套組（`build_master` 用
   ocg_date 逐前綴驗），同一個卡號指到多張卡時整條丟掉（實測 6661 個卡號
   有 16 個這樣，多半是歐版與美版編號撞號）。

**量測過但沒有採用**：把「－」分段當別名（「ＴＭ－１ランチャースパイダー」
→「ランチャースパイダー」）。寬鬆版整體 +1.0pp 但**實測製造誤配**
（「Ｄ－シールド」分出的「シールド」把ビッグ・シールド・ガードナー搶走、
「閃刀姫－レイ」分出的「レイ」命中「グレイモア」）；加上「分段不可以是
任何其他卡名的子字串」這道防線之後只剩 +0.2pp（3 筆），不值得那個複雜度。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import project_root

#: 主檔預設落點（相對專案根）。data/ 不進版控，缺檔時整個模組安靜降級。
DEFAULT_MASTER_PATH = "data/cards_1998_2004.json"

#: 目標年代。判準是 OCG（日版）首發日，不是 TCG。
ERA_START = "1998-01-01"
ERA_END = "2004-12-31"

_YGOPRODECK_URL = "https://db.ygoprodeck.com/api/v7/cardinfo.php?misc=yes"
_YAML_YUGI_URL = "https://raw.githubusercontent.com/DawnbrandBots/yaml-yugi/aggregate/cards.json"

#: 比對前要丟掉的分隔符。**故意不含長音符 ー**——它是真的音，
#: 丟掉會讓「エルフ」比對到「ホーリーエルフ」以外的東西。
#: 各種點（•·∙‧）是實測補的：有賣家把「ブラック・マジシャン」打成「ブラック•マジシャン」。
#:
#: 2026-08-02 補上兩批：
#:   ① 減號 U+2212「−」與罫線 ─━、假名連字 ゠ —— NFKC **不會**把它們正規化成
#:      半形連字號，所以「スロットマシーンAM−7」與卡名「スロットマシーンＡＭ－７」
#:      折出來不一樣（實測 miss）。
#:   ② 英文標點 . , ! ? # & : ; " ( ) [ ] ' ’ / —— 英文卡名進索引之後才需要：
#:      「Fiend Reflection #2」「Ray & Temperature」「Yu-Gi-Oh!」的標點在賣家
#:      標題裡寫法不一，不折掉就等於英文索引只對「標點打得跟官方一樣」的標題有效。
_DROP = frozenset("・•·∙‧‐‑‒–—―-~〜") | frozenset("−゠─━'’.,!?#&:;\"()[]/")

_RUBY_BLOCK = re.compile(r"<ruby>([^<]*)<rt>[^<]*</rt></ruby>")
_RUBY_WHOLE = re.compile(r"^<ruby>([^<]+)<rt>([^<]+)</rt></ruby>$")
_HIRAGANA = re.compile(r"[ぁ-ん]")

#: 卡號反查只在標題認得出自己是遊戯王商品時啟用（折疊後比對）。
#: 見模組頂註第 3 條：一把園藝鋸的型號 PS-18 也長得像卡號。
_YUGIOH_MARKERS: tuple[str, ...] = ("遊戯王", "遊戲王", "yugioh")

#: 從標題抽卡號。要求**一定要有連字號**（「LOB-018」「DL4-136」「P5-08」），
#: 因為無連字號版本會被 PSA10／ARS10／MT10 這類字串大量誤觸發。
#: 中間可以夾一段地區碼（EN/JP/A/K…），數字前導零不算數（LOB-018 == LOB-18）。
_TITLE_CODE_RE = re.compile(
    r"(?<![A-Z0-9])(?!PSA|ARS|BGS)([A-Z]{1,4}\d?|\d{3})[-‐–—]\s?([A-Z]{0,3})(\d{1,3})(?![0-9])"
)

#: 允許的地區碼。**單字母的歐版碼（E/F/G/I/P/S）刻意不收**：實測歐版與美版
#: 的編號不同步（LOB-E040 是 Yami、LOB-EN040 是伝説の剣），混進來就是撞號。
_CODE_REGION_TAGS = frozenset({"", "EN", "JP", "JA", "A", "K", "KR"})


def strip_ruby(name_ja: str) -> str:
    """把 `<ruby>漢字<rt>よみ</rt></ruby>` 收斂成漢字本體。"""
    return _RUBY_BLOCK.sub(r"\1", name_ja)


def reading_alias(name_ja: str) -> str | None:
    """整個卡名是單一 ruby 區塊、且讀音不含平假名時，讀音本身也是合法卡名寫法。

    這條規則只放行「青眼の白龍 → ブルーアイズ・ホワイト・ドラゴン」這種
    特殊讀名，擋掉逐字注音串起來的假名（那種讀音一定含平假名）。
    """
    m = _RUBY_WHOLE.match(name_ja)
    if not m:
        return None
    reading = m.group(2)
    return None if _HIRAGANA.search(reading) else reading


def fold(text: str) -> str:
    """比對用的折疊鍵。兩邊（標題與卡名）必須走同一個函式——同源同基準。"""
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text).lower():
        if ch.isspace() or ch in _DROP:
            continue
        o = ord(ch)
        if 0x30A1 <= o <= 0x30F6:          # 片假名 → 平假名（ー U+30FC 不在範圍內）
            ch = chr(o - 0x60)
        out.append(ch)
    return "".join(out)


@dataclass(slots=True, frozen=True)
class CardMatch:
    """一次比對命中。`in_era=False` 代表「認得這張卡，但它不是 1998-2004」——
    這跟「完全不認得」是兩種訊號：前者是明確的排除，後者多半是標題沒寫卡名。
    """

    name_ja: str
    in_era: bool
    card_id: int | None = None
    name_en: str = ""
    ocg_date: str | None = None
    matched_key: str = ""          # 標題裡實際命中的折疊字串（除錯用）
    #: 這一筆是怎麼比中的：`name`（卡名／別名／英文名）或 `code`（卡號反查）。
    #: 誤配稽核要用——兩條路徑的錯法完全不同，混在一起就看不出是哪一條在漏。
    via: str = "name"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name_ja": self.name_ja,
            "in_era": self.in_era,
            "card_id": self.card_id,
            "name_en": self.name_en,
            "ocg_date": self.ocg_date,
        }


def _with_key(match: CardMatch, key: str, via: str = "name") -> CardMatch:
    """把命中的折疊字串黏回結果——除錯時要看得出「到底是哪一段被比中的」。"""
    return CardMatch(
        name_ja=match.name_ja,
        in_era=match.in_era,
        card_id=match.card_id,
        name_en=match.name_en,
        ocg_date=match.ocg_date,
        matched_key=key,
        via=via,
    )


def _is_usable_key(key: str, min_len: int, min_ascii_len: int) -> bool:
    """太短的鍵會把整個索引變成雜訊產生器。

    實測 1998-2004 子集裡有「森」「氷」「山」「海」「闇」「７」這種單字卡名——
    「闇」出現在幾乎每個遊戲王標題裡，收進索引等於每一筆都誤判。
    純英數的短鍵（PSA10 裡的 "7"）同理。

    純英數的門檻另外拉高（`min_ascii_len`，預設 6）：英文卡名進索引之後，
    「Yami」（LOB-051）這種四字母名會被標題裡的「YAMI YUGI」誤觸發，
    而漏掉一張 Yami 的代價是退回 L3，配錯的代價是拿別張卡的行情去出價。
    """
    if len(key) < min_len:
        return False
    return not (key.isascii() and key.isalnum() and len(key) < min_ascii_len)


def extract_title_codes(title: str) -> list[str]:
    """從標題抽出正規化卡號（`LOB-18`、`DL4-136`、`P5-8`）。抽不到回空 list。

    正規化＝**去掉地區碼、去掉數字前導零**，因為賣家寫 `LOB-018`、資料源寫
    `LOB-EN001`，兩邊得折到同一個鍵才比得起來（同源同基準）。
    """
    t = unicodedata.normalize("NFKC", title).upper()
    out: list[str] = []
    for m in _TITLE_CODE_RE.finditer(t):
        if m.group(2) in _CODE_REGION_TAGS:
            out.append(f"{m.group(1)}-{int(m.group(3))}")
    return out


def looks_like_yugioh(title: str) -> bool:
    """標題本身有沒有說自己是遊戲王商品。卡號反查的前置條件，見模組頂註第 3 條。"""
    folded = fold(title)
    return any(m in folded for m in _YUGIOH_MARKERS)


class CardIndex:
    """日文卡名 → 卡片。最長子字串優先。

    索引依折疊鍵的**首字**分桶，每桶內按長度遞減排序：對標題的每一個起點
    只需掃該首字的桶，命中第一個就是該起點的最長鍵，全域再取最長。
    """

    def __init__(
        self,
        cards: list[dict[str, Any]],
        out_of_era: dict[str, str | None] | None = None,
        *,
        min_key_len: int = 2,
        min_ascii_key_len: int = 6,
        set_names: list[str] | None = None,
    ) -> None:
        self.min_key_len = min_key_len
        self.min_ascii_key_len = min_ascii_key_len
        self._era: dict[str, list[tuple[str, CardMatch]]] = {}
        self._other: dict[str, list[tuple[str, CardMatch]]] = {}
        #: 正規化卡號 → 年代內卡片。撞號（一個卡號指到多張卡）的整條丟掉。
        self._codes: dict[str, CardMatch] = {}
        self.n_cards = len(cards)

        code_owners: dict[str, set[str]] = {}
        for c in cards:
            match = CardMatch(
                name_ja=c["name_ja"],
                in_era=True,
                card_id=c.get("id"),
                name_en=c.get("name_en", ""),
                ocg_date=c.get("ocg_date"),
            )
            # 英文卡名與日文卡名進同一個索引：判準（起點最前 → 同起點最長）
            # 對兩種語言是同一套，分開兩份索引才會出現「哪一份先查」的偏袒。
            for alias in [c["name_ja"], *(c.get("aliases") or []), c.get("name_en") or ""]:
                self._add(self._era, alias, match)
            for code in c.get("set_codes") or []:
                code_owners.setdefault(code, set()).add(c["name_ja"])
                self._codes[code] = match

        for code, owners in code_owners.items():
            if len(owners) > 1:
                del self._codes[code]

        for name, ocg_date in (out_of_era or {}).items():
            self._add(
                self._other, name, CardMatch(name_ja=name, in_era=False, ocg_date=ocg_date)
            )

        for bucket in (self._era, self._other):
            for lst in bucket.values():
                lst.sort(key=lambda kv: -len(kv[0]))

        # 套組名剝除清單：折疊後長度夠、而且**本身不是任何一個卡名鍵**
        # （不然「青眼の白龍伝説」那種以卡名命名的套組會把卡名一起剝掉）。
        keys = {k for b in (self._era, self._other) for lst in b.values() for k, _ in lst}
        folded_sets = {
            f for f in (fold(n) for n in (set_names or [])) if len(f) >= 8 and f not in keys
        }
        self._set_names: list[str] = sorted(folded_sets, key=len, reverse=True)

    def _add(
        self, bucket: dict[str, list[tuple[str, CardMatch]]], alias: str, match: CardMatch
    ) -> None:
        key = fold(alias)
        if not _is_usable_key(key, self.min_key_len, self.min_ascii_key_len):
            return
        bucket.setdefault(key[0], []).append((key, match))

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None, **kw: Any) -> CardIndex:
        """讀本地主檔。**缺檔不是錯誤**——回傳空索引，上層自動降級。

        舊版主檔（沒有 `set_codes` / `set_names`）照樣讀得起來，只是卡號反查與
        套組名剝除是空的——降級成 2026-08-02 之前的純卡名比對，不會爆掉。
        """
        p = Path(path) if path else project_root() / DEFAULT_MASTER_PATH
        if not p.exists():
            return cls([], {}, **kw)
        data = json.loads(p.read_text(encoding="utf-8"))
        kw.setdefault("set_names", data.get("set_names") or [])
        return cls(data.get("cards", []), data.get("out_of_era", {}), **kw)

    @property
    def available(self) -> bool:
        return self.n_cards > 0

    @property
    def n_codes(self) -> int:
        """索引裡有幾個可反查的卡號。0 代表主檔是 2026-08-02 之前的舊格式。"""
        return len(self._codes)

    def __len__(self) -> int:
        return self.n_cards

    # ------------------------------------------------------------------
    def match(self, title: str) -> CardMatch | None:
        """標題 → 卡片。比不到回 None。

        **比不到本身就是訊號**：1998-2004 的卡名幾乎一定會出現在標題裡
        （賣家要靠它被搜到），所以比不到多半代表這不是目標年代的卡，
        或標題只寫了「初期 まとめ」這種沒有單卡資訊的東西。

        判準有三層，依序：**起點最前 → 同起點取最長 → 同長度偏向目標年代**。

        「同起點取最長」是本函式的重點，也是需求指定的規則：卡名互相包含
        （「青眼」⊂「青眼の白龍」、「ブラック・マジシャン・ガール」⊂
        「竜騎士ブラック・マジシャン・ガール」）時，短的那個一定是誤判。

        「起點最前」處理的是另一種情況：一個標題裡出現兩個**不重疊**的卡名
        （實例：「PSA10 神の宣告 2期 スーパーレア ME66 鋼鉄の襲撃者」，後面那個
        是套組名不是商品本體）。賣家一定先寫商品本體，所以取先出現的那個；
        純比長度會挑到後面那張 2017 年的卡，把一張 2 期真品判成年代外。

        兩個索引（目標年代／年代外）同時掃，不能先查目標年代再退而求其次——
        那會讓「竜騎士ブラック・マジシャン・ガール」被判成 2000 年的舊卡。

        卡名全部比不到時，才用卡號反查當**後備**（`match_by_code`）。
        次序與它的兩道閘門為什麼長這樣，見模組頂註第 3 條。
        """
        hit = self.match_by_name(title)
        return hit if hit is not None else self.match_by_code(title)

    def match_by_name(self, title: str) -> CardMatch | None:
        """只走卡名／別名／英文名這條路。比對前先剝掉套組名（頂註第 2 條）。"""
        folded = fold(title)
        for name in self._set_names:
            if name in folded:
                # 換成一個不可能出現在任何鍵裡的字元，而不是刪掉——刪掉會讓
                # 前後兩段黏起來，憑空造出原本不存在的子字串。
                folded = folded.replace(name, "\x00")
        for i in range(len(folded)):
            era_key, era_hit = self._at(self._era, folded, i)
            other_key, other_hit = self._at(self._other, folded, i)
            if era_hit and (other_hit is None or len(era_key) >= len(other_key)):
                return _with_key(era_hit, era_key)
            if other_hit:
                return _with_key(other_hit, other_key)
        return None

    def match_by_code(self, title: str) -> CardMatch | None:
        """卡號反查。標題沒有遊戲王字樣就一律不做（頂註第 3 條的鋸子案例）。"""
        if not self._codes or not looks_like_yugioh(title):
            return None
        for code in extract_title_codes(title):
            hit = self._codes.get(code)
            if hit is not None:
                return _with_key(hit, code, via="code")
        return None

    @staticmethod
    def _at(
        bucket: dict[str, list[tuple[str, CardMatch]]], folded: str, i: int
    ) -> tuple[str, CardMatch | None]:
        """從 folded[i] 起算的最長命中。桶內已按長度遞減，第一個命中就是最長。"""
        for key, match in bucket.get(folded[i], ()):
            if folded.startswith(key, i):
                return key, match
        return "", None


# ---------------------------------------------------------------------------
# 命中率評估（`ygo-sniper match-report`）
# ---------------------------------------------------------------------------
def match_report(
    rows: list[dict[str, Any]],
    index: CardIndex,
    *,
    miss_sample: int = 30,
    hit_sample: int = 30,
    seed: int = 0,
) -> dict[str, Any]:
    """對一堆真實標題跑一次比對，回統計＋抽樣。**改比對規則前後都要跑這支。**

    為什麼要有這支：卡名比不到的標的在估價會退到 L3（整個稀有度的池），而
    `bidding.EvidenceGate` 直接拒絕給 L3 出價上限——也就是說「命中率」是可
    行動標的數量的上游。沒有基線就不知道改動是進步還是退步。

    抽樣用固定種子的 `random.Random`，**同一份資料每次跑抽到同一批**：
    人工核對表要能重跑對照，隨機抽樣會讓「上次那 30 筆」永遠找不回來。

    `hits` 抽樣是給**誤配稽核**用的（標題 ↔ 配到的卡名並列）。誤配比未命中
    危險：未命中誠實地退到 L3 並被閘門擋下，誤配會給出一個有信心但錯誤的估值。
    """
    import random

    per_site: dict[str, dict[str, int]] = {}
    hits: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    for row in rows:
        title = row.get("title") or ""
        site = row.get("site") or "unknown"
        s = per_site.setdefault(
            site, {"n": 0, "hit": 0, "in_era": 0, "out_of_era": 0, "via_name": 0, "via_code": 0}
        )
        s["n"] += 1
        m = index.match(title)
        if m is None:
            misses.append({"site": site, "title": title, "table": row.get("table")})
            continue
        s["hit"] += 1
        s["in_era" if m.in_era else "out_of_era"] += 1
        s[f"via_{m.via}"] += 1
        hits.append({
            "site": site, "title": title, "table": row.get("table"),
            "name_ja": m.name_ja, "name_en": m.name_en,
            "in_era": m.in_era, "via": m.via, "matched_key": m.matched_key,
        })

    total = {k: sum(s[k] for s in per_site.values()) for k in
             ("n", "hit", "in_era", "out_of_era", "via_name", "via_code")}
    rng = random.Random(seed)
    return {
        "cards_indexed": len(index),
        "codes_indexed": index.n_codes,
        "overall": total,
        "by_site": dict(sorted(per_site.items())),
        "miss_samples": rng.sample(misses, min(miss_sample, len(misses))),
        "hit_samples": rng.sample(hits, min(hit_sample, len(hits))),
        "n_misses": len(misses),
        "n_hits": len(hits),
    }


# ---------------------------------------------------------------------------
# 主檔建置（只在 `ygo-sniper refresh-cards` 跑，scan 永遠不碰網路）
# ---------------------------------------------------------------------------
def _fetch_json(url: str, timeout: float) -> Any:
    """串流下載再解析。yaml-yugi 那份約 98MB，整包讀進 bytes 再 loads
    會讓峰值記憶體翻好幾倍，所以落到暫存檔再 json.load。"""
    import tempfile

    import httpx

    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(1 << 20):
                tmp.write(chunk)
        tmp.flush()
        tmp.seek(0)
        return json.load(tmp)


#: yaml-yugi 的 `sets[lang][].set_number` 解析式。地區碼是中間那一段
#: （`LOB-EN001` 的 EN、`DR02-JPC31` 的 JPC），數字才是編號。
_MASTER_CODE_RE = re.compile(r"^([A-Z0-9]{1,5})-([A-Z]{0,3})(\d{1,3})([A-Z]?)$")


def normalize_set_code(raw: str) -> str | None:
    """把資料源的卡號折成索引鍵（`LOB-EN001` → `LOB-1`）。折不動就回 None。

    與 `extract_title_codes` 是**同一個基準**（工程原則 1）：兩邊都是
    「前綴 ＋ 去前導零的編號」，不然主檔存 `LOB-EN001`、標題寫 `LOB-018`
    就永遠對不起來。地區碼白名單也共用同一份。
    """
    m = _MASTER_CODE_RE.fullmatch(raw.strip().upper())
    if not m or m.group(2) not in _CODE_REGION_TAGS:
        return None
    return f"{m.group(1)}-{int(m.group(3))}{m.group(4)}"


def _card_printings(y: dict[str, Any]) -> tuple[list[str], list[str]]:
    """一張卡在 yaml-yugi 裡的（正規化卡號, 套組名）。只看日文與英文兩個地區。"""
    codes: list[str] = []
    names: list[str] = []
    for lang in ("ja", "en"):
        for s in (y.get("sets") or {}).get(lang) or []:
            code = normalize_set_code(str(s.get("set_number") or ""))
            if code:
                codes.append(code)
            name = str(s.get("set_name") or "").strip()
            if name:
                names.append(name)
    return codes, names


def build_master(
    *,
    era_start: str = ERA_START,
    era_end: str = ERA_END,
    timeout: float = 180.0,
    ygoprodeck: Any = None,
    yaml_yugi: Any = None,
) -> dict[str, Any]:
    """組出主檔內容。傳入 `ygoprodeck` / `yaml_yugi` 可跳過下載（測試用）。

    2026-08-02 多產出兩樣東西，兩樣都用**同一條純度規則**篩：

      `set_codes`   每張年代內卡片的卡號。只收「該前綴的每一次印刷都是年代內
                    卡片」的套組——一個前綴只要印過任何一張年代外的卡就整個
                    前綴丟掉（例如 DP16 這種同時收錄復刻與現代卡的套組）。
                    沒有這條，`インセクト女王 DP16-JP025` 會被卡號反查成
                    1998-2004，而它其實是 2016 年的復刻。
      `set_names`   套組名，同一條純度規則。它的用途是**比對前先剝掉**，
                    因為套組名常常內含卡名（Legend of Blue Eyes White Dragon）。

    純度用 ocg_date 判，不用套組發行日——資料源沒有套組發行日，而「這個套組
    印過的卡有沒有現代卡」是同一件事的可觀測代理，且方向保守（誤判只會讓
    某個前綴被剔除，不會讓現代卡混進來）。
    """
    ygo = ygoprodeck if ygoprodeck is not None else _fetch_json(_YGOPRODECK_URL, timeout)
    yugi = yaml_yugi if yaml_yugi is not None else _fetch_json(_YAML_YUGI_URL, timeout)

    rows = ygo["data"] if isinstance(ygo, dict) else ygo
    by_password = {c["password"]: c for c in yugi if c.get("password")}
    by_konami = {c["konami_id"]: c for c in yugi if c.get("konami_id")}

    cards: list[dict[str, Any]] = []
    out_of_era: dict[str, str | None] = {}
    unnamed = 0
    # 前綴／套組名的「純度」統計：印過年代外卡片就出局。
    prefix_era: dict[str, set[bool]] = {}
    set_name_era: dict[str, set[bool]] = {}

    for c in rows:
        misc = (c.get("misc_info") or [{}])[0]
        ocg_date = misc.get("ocg_date")
        y = by_password.get(c.get("id")) or by_konami.get(misc.get("konami_id"))
        raw_ja = ((y or {}).get("name") or {}).get("ja")
        if not raw_ja:
            unnamed += 1
            continue
        name_ja = strip_ruby(raw_ja)
        in_era = bool(ocg_date and era_start <= ocg_date <= era_end)
        codes, set_names = _card_printings(y or {})
        for code in codes:
            prefix_era.setdefault(code.split("-")[0], set()).add(in_era)
        for name in set_names:
            set_name_era.setdefault(name, set()).add(in_era)

        if in_era:
            aliases = [a for a in (reading_alias(raw_ja),) if a]
            cards.append(
                {
                    "id": c.get("id"),
                    "name_ja": name_ja,
                    "name_en": c.get("name", ""),
                    "ocg_date": ocg_date,
                    "aliases": aliases,
                    "set_codes": sorted(set(codes)),
                }
            )
        else:
            # 年代外只留名字＋日期：用來把「現代卡」跟「不認得」分開，
            # 不需要完整卡片資料，多存只是讓每次 scan 讀更大的檔。
            out_of_era.setdefault(name_ja, ocg_date)

    pure_prefixes = {p for p, flags in prefix_era.items() if flags == {True}}
    pure_set_names = sorted(n for n, flags in set_name_era.items() if flags == {True})
    kept_codes = 0
    for card in cards:
        card["set_codes"] = [c for c in card["set_codes"] if c.split("-")[0] in pure_prefixes]
        kept_codes += len(card["set_codes"])

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "era": {"start": era_start, "end": era_end, "basis": "ygoprodeck misc_info.ocg_date"},
        "sources": {"ygoprodeck": _YGOPRODECK_URL, "yaml_yugi": _YAML_YUGI_URL},
        "counts": {
            "scanned": len(rows),
            "in_era": len(cards),
            "out_of_era": len(out_of_era),
            "no_japanese_name": unnamed,
            "set_codes": kept_codes,
            "set_names": len(pure_set_names),
        },
        "cards": cards,
        "out_of_era": out_of_era,
        "set_names": pure_set_names,
    }


def write_master(data: dict[str, Any], path: Path | str | None = None) -> Path:
    p = Path(path) if path else project_root() / DEFAULT_MASTER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p
