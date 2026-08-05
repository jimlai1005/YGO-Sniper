"""從標題判斷這張卡是不是 1998-2004。

這是整個 pipeline 訊噪比的關鍵。實測 Buyee「遊戯王 PSA」的搜尋結果裡，
大概八成以上是現代高價卡（25th / プリシク / クオシク），
沒有排除規則的話你每天會收到一堆完全不相關的推播然後三天後關掉通知。

策略是「先排除，再證明」：
  1. 命中 exclude_keywords → 直接丟掉
  2. 命中任一 era marker（時期詞 / 日版卡號 / 英版 set code）→ 收下
  3. 都沒有 → 不收，但記錄下來，方便你之後看 miss 掉什麼再補 marker
  4. **標題明寫域外年份（1990-1997／2005-2029）且沒有任何域內年份 → 否決**
     （2026-08-04 加）。第 2 步的關鍵字是賣家的用詞習慣，而年份是事實：
     `初期遊戯王英語版` 指的是早期英文版（2002 以後）、`初期絵` 是畫工版本、
     `復刻版 初期` 是 2024 年的重印。關鍵字擋不住這些，年份可以。
     實測：`【初期】遊戯王 2007 レリーフ PSA9` 原本靠「初期」二字過關；
     行情庫裡同型污染 6 列（重跑 `backfill-era` 已清）。
"""

from __future__ import annotations

import re

from ..domain import CardInfo, Grader
from .grade import SOURCE_TITLE, normalize, parse_grade
from .rarity import extract_rarity

# 前綴允許「字母＋數字」（日版的 G4- / P1- 這類），並排除鑑定機構名，
# 否則 "PSA10 ... SDY-006" 會把分數當成卡號回傳 "PSA-10"。
# 前後邊界用 lookaround 不用 `\b`（2026-08-03 修，同 grade.py 的理由）：
# 漢字算 `\w`，所以 `遊戯王LOB-001` 的 `王`／`L` 之間、`LOB-001初期` 的 `1`／`初`
# 之間都沒有 word boundary，兩者都抓不到卡號。卡號抓不到不會擋掉標的，
# 但會讓它在估價時退到 L3（沒有同卡成交可比），也就是拿不到出價上限。
_SET_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?!PSA|ARS|BGS)([A-Z]{1,4}\d?)[-‐]?(?:JP|EN|E)?(\d{2,3})(?![A-Za-z0-9])"
)
#: 標題裡「四位數年份形狀」的 token。**全專案只有這一個年份 tokenizer**：
#: 域內年份（收，1998-2004）與域外年份（否決，見 `_OUT_OF_ERA_YEARS`）
#: 必須從**同一份 token 清單**切出來。兩邊各寫一條 regex 的話，邊界規則
#: 一旦分岔，「有沒有域內年份」與「有沒有域外年份」就不是同一個基準的
#: 比較，而整條否決規則的安全網正是「域內年份可以取消否決」（工程原則 1）。
#:
#: ⚠️ **不可以用 `\b`**（2026-08-03 事故）：漢字在 Python `re` 裡算 `\w`，
#: 所以 `1999年` 的 `9` 與 `年` 之間**沒有** word boundary，`\b(199[89])\b`
#: 對它完全失效——實例 `遊戯王 1999年 大砲だるま PSA9 プレミアムパック`
#: 被判「無 1998-2004 年代證據」而整筆丟掉，而年份是最硬的年代證據之一。
#: 這與 `parsers/grade.py` 的 `\bPSA\b` 對「PSA鑑定品」失效是同一個坑，
#: 修法也照抄那裡：lookaround 只擋英數，讓年份後面直接接日文時照樣命中。
#: 邊界擋掉 `11999`（數字中段）、`x1999`／`1999A`（型號的一部分）、
#: `i-img1200x902`（圖片尺寸）、`TLM-JP010`（卡號只有三位數）。
_YEAR_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(\d{4})(?![A-Za-z0-9])")

#: 標題**開頭**的四位數＋`【`／`[` 是賣家自己的整理番號，不是年份。
#: 2026-08-04 實測語料 15,446 個標題，這個形狀命中 10 筆，**10 筆全是番號**
#: （`1924【遊戯王】 シーラカンス`、`1997【遊戯王】 迷宮兄弟`、
#: `2015【遊戯王】 ジャンド`——同一個賣家的「ゲートボールデッキ」系列）。
#: 不擋掉的話 `2015【遊戯王】…初期…PSA10` 會被年份否決誤殺，而
#: `2002【遊戯王】…` 會拿到一個假的域內年代證據——兩個方向都會錯。
_LOT_PREFIX_RE = re.compile(r"^(\d{4})\s*[【\[]")

#: 目標年代（含頭尾）。
_ERA_YEARS = (1998, 2004)

#: **域外**年份：寫出這些年份就代表這不是 1998-2004 的卡。
#:
#: 為什麼下界是 1990 而不是 1900（2026-08-04 實測決定）：遊戲王 ATK／DEF
#: 常見值一律是 50 的倍數，`攻撃力1900 守備力1800` 這種標題在語料裡有 26 筆，
#: 而 1900／1800／1950 全部落在 1900-1989 這一段——把下界開到 1900 等於
#: 讓攻擊力變成年份證據。1990-1997 與 2005-2029 這兩段**不含任何 50 的倍數**
#: （2000 是 50 的倍數但它在域內段，本來就不會觸發否決），所以這兩段與
#: 攻守值結構上不相交。代價：1900-1989 的古書（實測 5 筆【ARS書店】的舊書
#: 靠「初期」二字過關）擋不到——那是書店賣家的問題，不是年代判定的問題。
_OUT_OF_ERA_YEARS = ((1990, 1997), (2005, 2029))
_JP_HINT = re.compile(r"[ぁ-んァ-ヶ一-龯]")
_ASIA_HINT = re.compile(r"アジア|ASIA|亞洲版", re.I)
_EN_HINT = re.compile(r"英語版?|EN版|English|1st\s*Edition", re.I)


def detect_language(title: str) -> str | None:
    t = normalize(title)
    if _ASIA_HINT.search(t):
        return "ASIA"
    if _EN_HINT.search(t):
        return "EN"
    if _JP_HINT.search(t):
        return "JP"
    return None


def extract_set_code(title: str) -> str | None:
    t = normalize(title).upper()
    m = _SET_CODE_RE.search(t)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _hit_keyword(text: str, needles: list[str]) -> str | None:
    """關鍵字比對：純子字串。needle 是日文詞，word boundary 對 CJK 無意義。"""
    for n in needles:
        if n.upper() in text:
            return n
    return None


#: 排除字的第二層：**只在標題沒有遊戯王字樣時**才生效
#: （`watchlist.exclude_keywords_unless_yugioh`）。
#:
#: 為什麼需要第二層（2026-08-03，事故驅動）：`PSA 1999` 這條查詢刻意不帶「遊戯王」
#: （為了撈到標題沒寫廠牌的真古董卡），代價是撈進棒球／籃球／足球／他 TCG。
#: 用 `cards.looks_like_yugioh` 當**正面表列**的守門實測會誤殺 5 筆真卡
#: （`ブラックマジシャンガール 初期 ウルトラ P4-01`、`双頭の雷龍 初期 ウルトラ`…）
#: ——正是這條查詢要救的那一類，所以正面表列不可用。
#:
#: 負面表列（他卡種的品牌／聯盟名）解決了大部分，但有一類詞卡在中間：
#: 「旧枠」是デュエルマスターズ／MTG 的行話，**遊戯王賣家偶爾也會借用**
#: （實測語料 11 筆命中裡有 2 筆是真遊戲王：`遊戯王 ネオスペーシアン E・HERO 旧枠
#: 旧テキスト`、`遊戯王 ラーイエロー 旧枠ウルトラ`）。無條件排除會誤殺，
#: 完全不排除就擋不掉 `PSA8 聖鎧亜クイーン・アルカディアス 旧枠 初期`（DM 卡）。
#:
#: 這一層的形狀正好對上問題：**遊戯王字樣只用來「赦免」，不用來「錄取」**。
#: 誤殺面因此縮到「標題沒寫遊戯王、又寫了他卡種行話的真遊戲王卡」——
#: 實測語料 15,557 個標題、comps 1,729 列、卡名主檔 15,396 個名字，該交集為 0。
def _hit_soft_keyword(text: str, title: str, needles: list[str]) -> str | None:
    from ..cards import looks_like_yugioh

    if not needles or looks_like_yugioh(title):
        return None
    return _hit_keyword(text, needles)


#: 年代關鍵字的「否定後綴」：命中之後**緊接著**這些字就不算年代證據。
#:
#: 「初期絵」／「初期イラスト」是**畫工版本**，不是年代——現代復刻卡最愛用
#: 這個詞（賣點是「用回初代插圖」）。實例：
#:   `遊戯王 PSA10 ブラック・マジシャン・ガール 初期絵 20thSE 赤文字 DMMD-JP001`
#: 成交 NT$42,630，是 2020 年後的卡，卻因為「初期」被判成 1998-2004 並進了
#: 行情庫。同一類的假證據會直接把公允價往上拉，而且完全沒有外顯症狀。
_ERA_KEYWORD_ANTI_SUFFIX: dict[str, tuple[str, ...]] = {
    "初期": ("絵", "イラスト", "ILLUST"),
}


def _hit_era_keyword(text: str, needles: list[str]) -> str | None:
    """年代關鍵字比對。與 `_hit_keyword` 只差一件事：帶否定後綴的那一處不算命中。

    逐個出現位置檢查，不是整個詞一票否決——標題裡同時寫了「初期絵」與另一處
    單獨的「初期」時，後者仍然是有效的年代證據。寧可逐處判斷也不要用
    「有初期絵就整個標題作廢」，那會誤殺真的初期卡。
    """
    for n in needles:
        anti = _ERA_KEYWORD_ANTI_SUFFIX.get(n)
        needle = n.upper()
        if anti is None:
            if needle in text:
                return n
            continue
        start = 0
        while (i := text.find(needle, start)) != -1:
            tail = text[i + len(needle):]
            if not tail.startswith(anti):
                return n
            start = i + len(needle)
    return None


# 卡號 needle 的 regex cache，避免每個 listing 重編譯數十條 pattern
_CODE_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _code_pattern(needle: str) -> re.Pattern[str]:
    pat = _CODE_RE_CACHE.get(needle)
    if pat is None:
        # 前面不接英數（卡號不會是英文單字的尾巴）；後面只擋字母，不擋數字 ——
        # "PS" 後面接數字才是合法卡號，"G4-" 這種 needle 後面本來就是數字。
        pat = re.compile(rf"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z])", re.I)
        _CODE_RE_CACHE[needle] = pat
    return pat


def year_tokens(text: str) -> list[int]:
    """標題裡所有「年份形狀」的四位數，依出現順序。**域內與域外共用這一支。**

    `text` 必須是 `grade.normalize()` 之後的字串（全形數字要先折成半形，
    否則 `２００７年` 抓不到）。開頭的整理番號（`2015【…`）會被跳過。
    """
    lot = _LOT_PREFIX_RE.match(text)
    start = lot.end(1) if lot else 0
    return [int(m.group(1)) for m in _YEAR_TOKEN_RE.finditer(text, start)]


def _in_range(year: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= year <= hi for lo, hi in ranges)


def _hit_code(text: str, needles: list[str]) -> str | None:
    """卡號比對：純子字串會被英文單字誤觸發（LAST→AST、GLOBAL→LOB、PSA10→PS），
    那會製造假的年代證據，直接汙染推播品質。所以卡號一律用邊界比對。
    """
    for n in needles:
        if _code_pattern(n).search(text):
            return n
    return None


def parse_card(title: str, watchlist: dict) -> CardInfo:
    t = normalize(title)
    upper = t.upper()

    grader, grade = parse_grade(t)
    info = CardInfo(
        grader=grader,
        grade=grade,
        # 這一支只看標題，所以來源永遠是 title；描述補抓走 parsers.resolve_grade
        # （由有商品頁的呼叫端負責，見 appraise.py）。
        grade_source=SOURCE_TITLE if grade is not None else None,
        set_code=extract_set_code(t),
        language=detect_language(t),
        # 稀有度在排除前就抽好：被排除的標的也要留下屬性，
        # 之後才能回頭分析「我到底擋掉了什麼」。稀有度不參與 is_candidate。
        rarity=extract_rarity(t),
    )

    # --- 1. 排除 ---
    excluded = _hit_keyword(upper, watchlist.get("exclude_keywords", []))
    if excluded is None:
        excluded = _hit_soft_keyword(
            upper, t, watchlist.get("exclude_keywords_unless_yugioh", [])
        )
    if excluded:
        info.excluded_by = excluded
        return info

    # --- 2. 年代證據 ---
    markers = watchlist.get("era_markers", {})
    evidence: list[str] = []

    if hit := _hit_era_keyword(upper, markers.get("jp_keywords", [])):
        evidence.append(f"jp_kw:{hit}")
    if hit := _hit_code(upper, markers.get("jp_set_codes", [])):
        evidence.append(f"jp_code:{hit}")
    if hit := _hit_code(upper, markers.get("en_set_codes", [])):
        evidence.append(f"en_code:{hit}")

    # --- 3. 年份直寫：域內是證據，域外是**否決** ---
    #
    # 明確寫出的年份比關鍵字硬：賣家對「初期」的用法很鬆（`初期遊戯王英語版`
    # 指的是早期英文版，那是 2002 年以後；`初期絵` 是畫工版本），但沒有人會
    # 在 1999 年的卡上寫 2007。所以標題**只**寫了域外年份時，即使命中了
    # 初期／二期／舊卡號，這一筆也不是目標年代。
    #
    # 2026-08-04 事故：`【初期】遊戯王 2007 レリーフ PSA9` 靠「初期」二字
    # 通過 1998-2004 判定；行情庫（comps）裡同型污染 14 筆（2006 的 5 期
    # UTR レリーフ、2024/2025 的復刻版…），它們一直在當「初期卡的行情」用。
    #
    # **域內年份可以取消否決**（`2010-2017` 這種年份區間就是靠這條處理：
    # `1999-2005` 含域內年份 → 不否決，`2010-2017` 全在域外 → 否決）。
    # 這個方向是刻意的保守——寧可少擋一筆，不要誤殺真卡。
    years = year_tokens(t)
    in_era_years = [y for y in years if _ERA_YEARS[0] <= y <= _ERA_YEARS[1]]
    out_years = [y for y in years if _in_range(y, _OUT_OF_ERA_YEARS)]

    if in_era_years:
        evidence.append(f"year:{in_era_years[0]}")

    info.era_evidence = evidence
    if out_years and not in_era_years:
        # 證據留著（診斷用：看得出「它本來靠哪個關鍵字過關」），
        # 但 in_era 一律 False——是否收下只看 in_era 與 era_veto。
        info.era_veto = f"year:{out_years[0]}"
        info.in_era = False
    else:
        info.in_era = bool(evidence)
    return info


def is_candidate(info: CardInfo, watchlist: dict) -> tuple[bool, str]:
    """最終收不收。回傳 (收不收, 原因) —— 原因要留著，
    不然你調 config 的時候完全不知道為什麼某張卡沒出現。
    """
    if info.excluded_by:
        return False, f"排除字 {info.excluded_by}"
    # 年份否決要有自己的原因字串：否則它會被吞進「無 1998-2004 年代證據」，
    # 而這兩件事在調 watchlist 時要做的處置完全不同（一個是補 marker，
    # 一個是「標題自己就寫了不是」）。
    if info.era_veto:
        return False, f"標題年份 {info.era_veto.split(':', 1)[1]} 不在 1998-2004"
    if not info.in_era:
        return False, "無 1998-2004 年代證據"

    accepted = [g.upper() for g in watchlist.get("accepted_graders", [])]
    if info.grader is Grader.UNKNOWN:
        return False, "未偵測到鑑定機構"
    if accepted and info.grader.value not in accepted:
        return False, f"機構 {info.grader.value} 不在名單"

    floors = watchlist.get("min_grade", {})
    floor = floors.get(info.grader.value)
    if info.grade is not None and floor is not None and info.grade < floor:
        return False, f"分數 {info.grade} 低於 {floor}"

    return True, "ok"
