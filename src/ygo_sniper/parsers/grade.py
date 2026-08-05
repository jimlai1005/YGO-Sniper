"""從標題解析鑑定機構與分數，必要時再從**商品描述**補抓。

日文賣家的寫法非常自由，實測看過的變體：
    【PSA10】  PSA 10  PSA１０  psa10  ＰＳＡ10
    【ARS鑑定 10】  ARS10+  ARS 10++  ARS鑑定10
    BGS9.5  BGS 9.5 (Quad)
還有一堆只寫「鑑定品」「鑑定書付き」不寫機構的。

── 從描述補抓分數（`resolve_grade`）───────────────────────────────────
標題只寫「鑑定品」的標的，分數多半寫在**商品描述**裡。補抓它的價值很直接：
`bidding.EvidenceGate.require_known_grade` 對 grade=None 一律拒絕給出價上限，
所以「抽不到分數」＝這一整類標的不可行動。

但補抓比不抓危險，因為**日文賣家的描述尾巴幾乎都有 SEO 關鍵字堆**。
2026-08-02 實測（`auctions.yahoo.co.jp/jp/auction/k1238516579`，標題只寫 PSA）：

    PSA5ですが、とてもキレイな状態です！        ← 真正的分數
    ●検索用ワード
    遊戯王 PSA10 PSA9 PSA鑑定 ARS10 ARS9 ARS鑑定 BGS10 BGS …   ← 關鍵字堆

整段描述丟給 regex 會抓到 **PSA10**——實際是 **PSA5**，而且錯的方向正好是
「公允價被高估、上限開太高」，也就是會付錢的那一邊。所以本模組的補抓有三層防線：

  1. 砍掉關鍵字堆（`_KEYWORD_SPAM_RE` 之後的整段丟掉）。
  2. 一行裡出現 ≥3 個不同的鑑定分數 → 整行當關鍵字堆丟掉（沒有正常賣家會在
     同一行寫三個分數）。
  3. **剩下的分數必須全體一致**，只要有兩個不同的值就回「無法判定」。
     與標題矛盾（同機構不同分數、或機構本身對不上）也一律「無法判定」。

三層都是同一個立場：**抓錯比抓不到危險得多**，寧可維持 grade=None，
讓報告叫使用者自己去看照片上的鑑定殼。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..domain import Grader

# ARS 的 10+ / 10++ 要優先於純 10 被捕捉，所以分數 pattern 帶上尾綴
#
# 結尾用 `(?!\d)` 而不是 `\b`（2026-08-03 修）：`\b` 對 CJK 無效——日文與漢字在
# `re` 裡算 `\w`，所以數字後面直接接日文時沒有 word boundary。兩個實測症狀：
#   parse_grade("PSA9初期")   → (UNKNOWN, None)  分數整個抓不到
#   parse_grade("BGS9.5初期") → (BGS, 9.0)       **靜默截斷成錯誤的值**
# 後者比前者危險：抓不到會誠實地退化成「分數未知」並被出價閘門擋下，
# 抓錯會拿一個看起來正常的數字一路走完估價。
# `(?!\d)` 保留原本要擋的東西（`PSA123` 仍不會被讀成 PSA1），只是不再被
# 日文字擋住。
#
# ⚠️ 但拿掉 `\b` 會**放進一類原本被意外擋掉的東西**：賣家的「相當於」宣稱。
#     ARS10 遊戯王 はにわ 初期 Vol.1 PSA10相当   ← 真實鑑定是 ARS10
#     【ARS10】青眼の白龍 復刻シークレット PSA10並  ← 真實鑑定是 ARS10
# 舊的 `\b` 剛好擋住「PSA10相当」（`10` 後面接漢字沒有 boundary），於是
# fall-through 到 ARS 得到正確答案——那是**副作用而不是設計**，但它是對的。
# 拿掉 `\b` 之後就得把這件事明講：`_CLAIM_SUFFIX` 列出宣稱詞，命中就不算數。
# 實測 1,922 筆真實標題裡「相当」12 筆、「並」15 筆，都是同一個形狀：
# 「這張卡（用另一家鑑定）的品相相當於 PSA10」。把它讀成 PSA10 會憑空造出
# 一個不存在的鑑定，而且方向是**高估**（PSA10 的溢價是 ×2.10）。
#: 「相當於某某分」的宣稱詞。跟在分數後面時，那個分數是賣家的比喻不是鑑定結果。
_CLAIM_SUFFIX = r"相当|相當|並み|並|級|クラス|レベル"

_GRADE_PATTERNS: list[tuple[Grader, re.Pattern[str]]] = [
    (Grader.PSA, re.compile(
        rf"PSA[\s\-鑑定]{{0,4}}(10|[1-9](?:\.5)?)(?!\d)(?!\s*(?:{_CLAIM_SUFFIX}))", re.I)),
    (Grader.ARS, re.compile(
        rf"ARS[\s\-鑑定]{{0,4}}(10\+{{0,2}}|[1-9](?:\.5)?)(?!\d)(?!\s*(?:{_CLAIM_SUFFIX}))", re.I)),
    (Grader.BGS, re.compile(
        rf"BGS[\s\-鑑定]{{0,4}}(10|[1-9](?:\.5)?)(?!\d)(?!\s*(?:{_CLAIM_SUFFIX}))", re.I)),
]

#: 機構名**後面緊接著**這些詞時，那三個字母是店名或別家品牌，不是鑑定機構。
#:
#: 2026-08-04 事故：`【ARS書店】「東京土木建築業組合沿革誌」1937年…明治初期時代の…`
#: ——舊書店的店名叫「ARS書店」，而書名裡的「初期」剛好是年代標記，於是
#: 1937/1953/1978/1986 年的古書一路通過遊戲王鑑定卡的篩選（實測 6 筆）。
#: 下面的 `(?![A-Za-z0-9])` 只擋英數，擋不住後面直接接漢字的店名——這是
#: 同一個 CJK 邊界坑的第四次現形，只是這次方向相反：不是「該命中卻沒命中」，
#: 而是「不該命中卻命中」。
#:
#: 詞表**逐條有語料依據**（32,180 個真實標題，這個形狀的機構名 token 出現
#: 3,074 次，後面接的字共 29 種：空白 1,518、`鑑` 1,307、字尾 133，其餘
#: 全部 ≤24 次）。唯一的商號字是：
#:   `書店` 6 次，全部是【ARS書店】的古書。
#: `書房`／`書院`／`古書` 是同一家店的近鄰寫法，語料 0 命中、也不會與「鑑定書」
#: 相撞（那是 ARS 後接**空白或「鑑」**的形狀，不是 ARS 後接「書房」）。
#: **刻意不寫**「店」「堂」「屋」這種單字：語料 0 命中，純屬想像，而每多一個
#: 通用字就多一分誤殺真標題的機率（本檔的紅線是誤殺比漏擋貴）。
#:
#: ❌ 試過但**不採用**：`アルス`。語料裡 9 筆全是 ARS コーポレーション（園藝
#: 鋸／剪定鋏）與戰前出版社アルス，看起來很安全——但 comps id=25480
#: `遊戯王 旧アジア ARS9 カオスエンペラードラゴン シークレットアルス 鑑定書付き`
#: 證明**賣家會用「アルス」稱呼 ARS 鑑定**（那家公司的日文名兩種寫法都有人用）。
#: 擋掉它換來的是一把 102 元的鋸子不再進 comps，賠上的是「ARS アルス 鑑定書付き」
#: 這種真標的整筆消失——這個檔案的取捨永遠是寧可漏擋。
_VENDOR_SUFFIX = r"書店|書房|書院|古書"

# \b 對 CJK 無效（漢字在 re 裡算 \w，所以 "PSA鑑定品" 的 A 與 鑑 之間沒有邊界）。
# 改用 lookaround 只擋英數，讓機構名後面直接接日文時仍能命中。
# 尾巴再加一個 `_VENDOR_SUFFIX` 的否定 lookahead：店名不是鑑定。
_GRADER_ONLY = {
    Grader.PSA: re.compile(
        rf"(?<![A-Za-z0-9])PSA(?![A-Za-z0-9])(?!\s{{0,2}}(?:{_VENDOR_SUFFIX}))", re.I),
    Grader.ARS: re.compile(
        rf"(?<![A-Za-z0-9])ARS(?![A-Za-z0-9])(?!\s{{0,2}}(?:{_VENDOR_SUFFIX}))", re.I),
    Grader.BGS: re.compile(
        rf"(?<![A-Za-z0-9])BGS(?![A-Za-z0-9])(?!\s{{0,2}}(?:{_VENDOR_SUFFIX}))", re.I),
}

_GRADED_HINT = re.compile(r"鑑定(品|書|済|付)|graded|slab", re.I)


def normalize(text: str) -> str:
    """全形轉半形 + 統一空白。日文標題裡的 ＰＳＡ１０ 不處理就抓不到。"""
    t = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", t).strip()


def parse_grade(title: str) -> tuple[Grader, float | None]:
    """回傳 (機構, 分數)。分數抓不到時回 None，不要猜。

    ARS 的 10+ / 10++ 一律折成 10.0 —— 它們在成交價上確實有溢價，
    但那應該由 comps 反映，不該混進分數欄位造成排序錯亂。
    """
    t = normalize(title)

    for grader, pattern in _GRADE_PATTERNS:
        m = pattern.search(t)
        if m:
            raw = m.group(1).rstrip("+")
            try:
                return grader, float(raw)
            except ValueError:
                return grader, None

    # 有機構名但沒抓到分數
    for grader, pattern in _GRADER_ONLY.items():
        if pattern.search(t):
            return grader, None

    return Grader.UNKNOWN, None


def looks_graded(title: str) -> bool:
    """連機構都沒寫，但看起來是鑑定品。留著當弱訊號，不直接排除。"""
    t = normalize(title)
    grader, _ = parse_grade(t)
    return grader is not Grader.UNKNOWN or bool(_GRADED_HINT.search(t))


def passes_min_grade(grader: Grader, grade: float | None, min_grade: dict[str, int]) -> bool:
    """分數未知時放行 —— 寧可多看一眼，也不要漏掉標題寫得爛的好貨。"""
    if grade is None:
        return True
    floor = min_grade.get(grader.value)
    return floor is None or grade >= floor


# ---------------------------------------------------------------------------
# 從商品描述補抓分數
# ---------------------------------------------------------------------------
#: 分數來源標籤。**必須跟著分數一起傳到下游**：標題寫的分數與描述裡撈出來的
#: 分數可信度不同，混成同一個欄位就再也分不出來（工程原則 1）。
SOURCE_TITLE = "title"
SOURCE_DESCRIPTION = "description"

#: 描述尾巴的 SEO 關鍵字堆起點。命中之後**整段丟掉**（含這一行）。
#: 這些標記是實測抄下來的，不是想像的：日文賣家的模板幾乎都用「検索用」開頭。
_KEYWORD_SPAM_RE = re.compile(
    r"(検索\s*(用|ワード|キーワード)|以下.{0,4}検索|search\s+(word|keyword)s?)",
    re.I,
)

#: 一行裡出現幾個**不同**的鑑定分數就當關鍵字堆。3 是刻意的：
#: 「PSA9かPSA10か迷いましたが」這種正常句子最多兩個，三個以上一定是關鍵字堆。
_SPAM_LINE_DISTINCT_GRADES = 3

#: 「真贋鑑定のみ」＝只鑑真偽、殼上根本沒有分數。抓不到分數時要分得出
#: 「賣家沒寫」與「這張卡本來就沒有分數」——後者不是資料缺口，是事實。
_AUTHENTICITY_ONLY_RE = re.compile(r"真贋鑑定|真贋のみ|authenticity[\s\-]?only", re.I)

_LINE_SPLIT_RE = re.compile(r"[\r\n]+|<\s*br\s*/?\s*>", re.I)


@dataclass(slots=True, frozen=True)
class GradeResolution:
    """一次「這張卡幾分」的判定，**連同它的來歷與為什麼**。

    `grade is None` 有三種完全不同的意思，全部靠 `note` 分辨：沒人寫、
    寫了但互相矛盾、或這張卡本來就只有真偽鑑定沒有分數。三者對使用者的
    下一步不一樣（去看照片／別信標題／這張卡沒分數），所以不能合成一個 None。
    """

    grader: Grader
    grade: float | None
    #: `SOURCE_TITLE` / `SOURCE_DESCRIPTION` / None（沒抓到）
    source: str | None
    #: True ＝ 有分數被抓到但互相矛盾，**刻意降級成無法判定**
    conflict: bool
    note: str

    @property
    def resolved_from_description(self) -> bool:
        return self.source == SOURCE_DESCRIPTION


def strip_keyword_spam(text: str) -> str:
    """砍掉描述尾巴的 SEO 關鍵字堆。見模組頂註的實測案例。

    兩道：先在「検索用」這類標記處截斷，再逐行丟掉「一行三個以上不同分數」的。
    第二道是給沒寫標記的關鍵字堆用的——賣家模板百百種，只擋標記會漏。
    """
    if not text:
        return ""
    m = _KEYWORD_SPAM_RE.search(text)
    head = text[: m.start()] if m else text
    kept: list[str] = []
    for line in _LINE_SPLIT_RE.split(head):
        if len({g for g in _iter_grades(line) if g[1] is not None}) >= _SPAM_LINE_DISTINCT_GRADES:
            continue
        kept.append(line)
    return "\n".join(kept)


#: 描述用的分數 pattern。與標題那組（`_GRADE_PATTERNS`）只差**結尾的邊界條件**，
#: 而那個差別是必要的：`\b` 對 CJK 無效——「PSA5ですが」的 `5` 與 `で` 之間沒有
#: word boundary（日文假名在 `re` 裡算 `\w`），所以標題那組在描述裡完全抓不到
#: 「PSA5ですが、とてもキレイな状態です！」這種最常見的寫法（2026-08-02 實測，
#: 兩筆 Yahoo 標的都是這樣寫的，用 `\b` 版本命中率 0%）。
#: 改成 `(?!\d)`：仍然擋掉「PSA123」被讀成 PSA1（後面接數字就不算），
#: 但後面接日文時照樣命中。標題那組**刻意不動**——它跑在全部 pipeline 上，
#: 這裡只放寬描述這一條新路徑。
#: 逐條寫出來而不是對 `_GRADE_PATTERNS` 的字串做手術：手術式改寫看起來省事，
#: 但只要哪天有一條的結尾長得不一樣（ARS 那條本來就沒有 `\b`），它就會安靜地
#: 少改一條，而症狀是「某個機構的分數偶爾抓錯」——最難發現的那種錯。
_GRADE_PATTERNS_LOOSE: tuple[tuple[Grader, re.Pattern[str]], ...] = (
    (Grader.PSA, re.compile(r"PSA[\s\-鑑定]{0,4}(10|[1-9](?:\.5)?)(?!\d)", re.I)),
    (Grader.ARS, re.compile(r"ARS[\s\-鑑定]{0,4}(10\+{0,2}|[1-9](?:\.5)?)(?!\d)", re.I)),
    (Grader.BGS, re.compile(r"BGS[\s\-鑑定]{0,4}(10|[1-9](?:\.5)?)(?!\d)", re.I)),
)


def _iter_grades(text: str) -> list[tuple[Grader, float | None]]:
    """文字裡**所有**的 (機構, 分數)。不去重、不排序，呼叫端自己收斂。"""
    t = normalize(text)
    out: list[tuple[Grader, float | None]] = []
    for grader, pattern in _GRADE_PATTERNS_LOOSE:
        for m in pattern.finditer(t):
            raw = m.group(1).rstrip("+")
            try:
                out.append((grader, float(raw)))
            except ValueError:  # pragma: no cover - regex 已保證是數字
                out.append((grader, None))
    return out


def grades_in_description(description: str) -> list[tuple[Grader, float]]:
    """描述裡（去掉關鍵字堆之後）出現過的 (機構, 分數)，去重保序。"""
    seen: list[tuple[Grader, float]] = []
    for grader, grade in _iter_grades(strip_keyword_spam(description)):
        if grade is not None and (grader, grade) not in seen:
            seen.append((grader, grade))
    return seen


def resolve_grade(title: str, description: str | None = None) -> GradeResolution:
    """標題 ＋ 商品描述 → 最終的 (機構, 分數, 來源)。**矛盾一律回無法判定。**

    決策順序（每一步都寧可退回 None，不猜）：

      1. 標題有分數 → 用它（`source="title"`）。但描述若出現**同機構不同分數**，
         判定為矛盾 → 分數作廢。標題寫錯分數是實際存在的情形，而我們分不出
         是標題錯還是描述錯，所以兩個都不採信。
      2. 標題沒分數 → 看描述。描述裡的分數必須**只有一個值**才採用；
         而且標題若已經寫了機構（「ARS 鑑定品」），描述的機構必須對得上，
         否則那個分數多半在講別張卡。
      3. 其餘一律 `grade=None`，`note` 說明是哪一種缺口。
    """
    grader, title_grade = parse_grade(title)
    found = grades_in_description(description or "")

    if title_grade is not None:
        clash = [(g, v) for g, v in found if g is grader and v != title_grade]
        if clash:
            others = "、".join(f"{g.value}{v:g}" for g, v in clash)
            return GradeResolution(
                grader=grader, grade=None, source=None, conflict=True,
                note=(
                    f"**無法判定**：標題寫 {grader.value}{title_grade:g}，"
                    f"商品描述卻出現 {others}。兩者矛盾時本工具不猜——"
                    "請自己看商品照片上的鑑定殼"
                ),
            )
        return GradeResolution(
            grader=grader, grade=title_grade, source=SOURCE_TITLE, conflict=False,
            note=f"分數來自**標題**（{grader.value}{title_grade:g}）",
        )

    if not found:
        if _AUTHENTICITY_ONLY_RE.search(description or ""):
            return GradeResolution(
                grader=grader, grade=None, source=None, conflict=False,
                note=(
                    "商品描述寫的是**真贋鑑定（只鑑真偽）**，這種鑑定殼上本來就沒有分數——"
                    "不是賣家漏寫。分數溢價無從估計，請自己看照片與殼上的標示"
                ),
            )
        return GradeResolution(
            grader=grader, grade=None, source=None, conflict=False,
            note=(
                "標題與商品描述都抽不到鑑定分數（描述可能根本沒提，或這個平台"
                "不提供描述）——**請自己看商品照片上的鑑定殼**，分數就印在殼上"
            ),
        )

    values = {v for _g, v in found}
    if len(values) > 1:
        listed = "、".join(f"{g.value}{v:g}" for g, v in found)
        return GradeResolution(
            grader=grader, grade=None, source=None, conflict=True,
            note=(
                f"**無法判定**：商品描述裡出現多個不一致的分數（{listed}），"
                "分不出哪一個是這張卡的——請自己看商品照片上的鑑定殼"
            ),
        )

    desc_grader, desc_grade = found[0]
    if grader is not Grader.UNKNOWN and desc_grader is not grader:
        return GradeResolution(
            grader=grader, grade=None, source=None, conflict=True,
            note=(
                f"**無法判定**：標題寫的機構是 {grader.value}，描述裡卻只有 "
                f"{desc_grader.value}{desc_grade:g}——機構對不上，那個分數多半"
                "在講別張卡。請自己看商品照片上的鑑定殼"
            ),
        )
    return GradeResolution(
        grader=desc_grader, grade=desc_grade, source=SOURCE_DESCRIPTION, conflict=False,
        note=(
            f"分數來自**商品描述**（{desc_grader.value}{desc_grade:g}），標題沒寫。"
            "描述是賣家自由文字，可信度低於標題——下單前請對一次照片上的鑑定殼"
        ),
    )
