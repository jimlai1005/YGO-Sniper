"""標題解析測試。

測資全部取自 Buyee 搜尋結果的真實標題型態。
這些字串很醜，但這就是現實 —— parser 不能只在乾淨資料上work。
"""

import pytest

from ygo_sniper.domain import Grader
from ygo_sniper.parsers import (
    detect_language,
    extract_set_code,
    is_candidate,
    parse_card,
    parse_grade,
)


@pytest.mark.parametrize(
    "title,grader,grade",
    [
        ("【PSA10】ジェノサイドキングデーモン レリーフ", Grader.PSA, 10.0),
        ("PSA 9 青眼の白龍 初期", Grader.PSA, 9.0),
        ("ＰＳＡ１０ ブラックマジシャン 二期", Grader.PSA, 10.0),
        ("【ARS鑑定 10】遊戯王 バンダイ版", Grader.ARS, 10.0),
        ("ARS10++ 真紅眼の黒竜 初期", Grader.ARS, 10.0),
        ("BGS 9.5 Blue-Eyes LOB-001", Grader.BGS, 9.5),
        ("psa10 Dark Magician SDY-006", Grader.PSA, 10.0),
        ("遊戯王 PSA 鑑定品 レリーフ", Grader.PSA, None),
        ("遊戯王 青眼の白龍 美品", Grader.UNKNOWN, None),
    ],
)
def test_parse_grade(title, grader, grade):
    assert parse_grade(title) == (grader, grade)


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Blue-Eyes White Dragon LOB-001 1st", "LOB-001"),
        ("遊戯王 オベリスクの巨神兵 G4-02 シークレット", "G4-02"),
        ("SDK-001 青眼", "SDK-001"),
        ("遊戯王 レリーフ 美品", None),
        # 分數不是卡號 —— 以前這條會回 "PSA-10"
        ("psa10 Dark Magician SDY-006", "SDY-006"),
    ],
)
def test_extract_set_code(title, expected):
    assert extract_set_code(title) == expected


def test_detect_language():
    assert detect_language("遊戯王 青眼の白龍 初期") == "JP"
    assert detect_language("Blue-Eyes White Dragon 1st Edition") == "EN"
    assert detect_language("遊戯王OCG D-フォース アジア プリズマ") == "ASIA"


class TestEraDetection:
    def test_accepts_jp_early_era(self, watchlist):
        info = parse_card("【PSA10】遊戯王 青眼の白龍 初期 レリーフ", watchlist)
        assert info.in_era
        assert info.era_evidence
        ok, why = is_candidate(info, watchlist)
        assert ok, why

    def test_accepts_english_2002_set(self, watchlist):
        info = parse_card("PSA 9 Blue-Eyes White Dragon LOB-001 1st Edition", watchlist)
        assert info.in_era
        assert is_candidate(info, watchlist)[0]

    def test_rejects_modern_25th(self, watchlist):
        """現代卡是最大的噪音來源，必須被排除字擋掉。"""
        info = parse_card("【PSA10】閃刀姫-レイ 25thシークレットレア アーコレ", watchlist)
        assert info.excluded_by is not None
        assert not is_candidate(info, watchlist)[0]

    def test_rejects_prismatic(self, watchlist):
        info = parse_card("遊戯王 I:Pマスカレーナ プリシク PSA10", watchlist)
        assert not is_candidate(info, watchlist)[0]

    def test_rejects_ungraded_even_if_vintage(self, watchlist):
        info = parse_card("遊戯王 青眼の白龍 初期 レリーフ 美品", watchlist)
        assert info.in_era
        ok, why = is_candidate(info, watchlist)
        assert not ok
        assert "機構" in why

    def test_rejects_modern_without_era_marker(self, watchlist):
        info = parse_card("PSA10 遊戯王 妖精伝姫－ラチカ ウルトラレア", watchlist)
        assert not is_candidate(info, watchlist)[0]

    def test_rejects_bgs_not_in_accepted_graders(self, watchlist):
        """需求只要 PSA / ARS，BGS 不收。"""
        info = parse_card("BGS 9.5 Blue-Eyes LOB-001 1st Edition", watchlist)
        assert info.in_era
        ok, why = is_candidate(info, watchlist)
        assert not ok
        assert "BGS" in why

    def test_rejects_low_grade(self, watchlist):
        info = parse_card("PSA 5 遊戯王 青眼の白龍 初期", watchlist)
        ok, why = is_candidate(info, watchlist)
        assert not ok
        assert "分數" in why

    def test_year_in_title_counts_as_evidence(self, watchlist):
        info = parse_card("PSA 10 Yu-Gi-Oh 2002 promo card", watchlist)
        assert info.in_era
        assert any(e.startswith("year:") for e in info.era_evidence)

    def test_year_followed_by_kanji_counts_as_evidence(self, watchlist):
        """`1999年` 必須算年代證據（2026-08-03 事故）。

        漢字在 Python `re` 裡算 `\\w`，所以舊的 `\\b(199[89])\\b` 在 `9` 與 `年`
        之間找不到 word boundary，整個判定對日文寫法失效——實例
        `遊戯王 1999年 大砲だるま PSA9 プレミアムパック` 被判「無年代證據」
        而整筆丟掉，而年份是最硬的年代證據之一。
        （與 `\\bPSA\\b` 對「PSA鑑定品」失效是同一個坑。）
        """
        info = parse_card("遊戯王 1999年 大砲だるま PSA9 プレミアムパック", watchlist)
        assert "year:1999" in info.era_evidence
        assert is_candidate(info, watchlist)[0]

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("遊戯王 1999年 青眼の白龍 PSA9", "year:1999"),      # 後接漢字
            ("遊戯王 1999 青眼の白龍 PSA9", "year:1999"),        # 兩側空白
            ("遊戯王 2004年初期 青眼の白龍 PSA9", "year:2004"),   # 區間上緣
            ("遊戯王1998 青眼の白龍 PSA9", "year:1998"),         # 前接漢字
        ],
    )
    def test_year_boundaries_that_must_hit(self, watchlist, title, expected):
        assert expected in parse_card(title, watchlist).era_evidence

    @pytest.mark.parametrize(
        "title",
        [
            "遊戯王 x1999 青眼の白龍 PSA9",    # 型號的一部分，不是年份
            "遊戯王 11999 青眼の白龍 PSA9",    # 數字中段
            "遊戯王 1999A 青眼の白龍 PSA9",    # 後接英文
            "遊戯王 2005年 青眼の白龍 PSA9",   # 區間外
        ],
    )
    def test_year_boundaries_that_must_not_hit(self, watchlist, title):
        """放寬只針對「相鄰字是 CJK」；英數邊界的舊行為一個都不能鬆。"""
        assert not any(
            e.startswith("year:") for e in parse_card(title, watchlist).era_evidence
        )

    # -- 年份否決（2026-08-04）-------------------------------------------
    @pytest.mark.parametrize(
        "title,year",
        [
            # 事故原型：標題明寫 2007，卻靠「初期」二字通過 1998-2004 判定
            ("【初期】遊戯王 2007 レリーフ PSA9", "2007"),
            # 賣家對「初期」的用法很鬆：初期遊戯王英語版 = 早期英文版（2002 以後）
            ("【初期遊戯王英語版】レッドアイズ 2005 1st Edition PSA10", "2005"),
            # 舊卡號也擋不住明確年份（TLM 是 2005 年的 ザ・ロストミレニアム）
            ("【PSA9】アルティメットインセクト LV7 旧レリーフ TLM-JP010 2005年", "2005"),
            # 復刻版：關鍵字全中，年份是唯一說真話的欄位
            ("【psa9】封印されしエクゾディア 復刻版 初期 ウルトラレア 2024 遊戯王", "2024"),
            # 年份區間整段落在域外 → 否決（域內那一半見下面的反例）
            ("2010-2017 YU-GI-OH! LOB EYES WHITE DRAGON REPRINT PSA 7", "2010"),
            # 1990-1997：遊戲王還沒出，這一定是別的東西（實例是ドラゴンボール）
            ("ドラゴンボール 本弾 第8章 初期 1995 カードダス PSA10", "1995"),
        ],
    )
    def test_out_of_era_year_vetoes_loose_keywords(self, watchlist, title, year):
        """明確寫出的年份比關鍵字硬。**這 6 種在實測語料裡都真的出現過。**"""
        info = parse_card(title, watchlist)
        assert info.era_veto == f"year:{year}"
        assert not info.in_era
        ok, why = is_candidate(info, watchlist)
        assert not ok
        assert year in why and "無 1998-2004 年代證據" not in why

    @pytest.mark.parametrize(
        "title",
        [
            # 域內年份取消否決：年份區間跨越 1998-2004 時寧可收下
            "遊戯王 1999-2005 青眼の白龍 初期 PSA10",
            "遊戯王 2004年 2005年 ブラックマジシャン PSA10",
            # `20th`／`10期` 含數字但不是四位數年份（20th 另有排除字擋，
            # 這裡驗的是**年份 tokenizer 不可以誤認它**）
            "遊戯王 10期 初期 青眼の白龍 PSA10",
            # 卡號、商品編號、圖片尺寸：四位數但不是年份
            "遊戯王 初期 青眼の白龍 PSA10 TLM-JP010 i-img1200x902",
            "遊戯王 初期 青眼の白龍 PSA10 LOB-K0 型番x2005",
            # 攻守值：`19[0-8]\\d` 段刻意不納入否決範圍就是為了這一類
            "遊戯王 アックス・レイダー 初期 攻撃力1900 守備力1800 PSA9",
            # 標題開頭的整理番號不是年份（實測語料 10 筆全是番號）
            "2015【遊戯王】 初期 青眼の白龍 PSA10",
        ],
    )
    def test_year_veto_does_not_misfire(self, watchlist, title):
        """誤殺一筆真卡的代價遠大於漏擋一筆假卡，所以這些一個都不准被否決。"""
        info = parse_card(title, watchlist)
        assert info.era_veto is None, f"{title!r} 被誤判為域外年份"
        assert is_candidate(info, watchlist)[0], f"{title!r} 被誤殺"

    def test_lot_number_prefix_is_not_era_evidence_either(self, watchlist):
        """整理番號在**兩個方向**都不算年份——域內域外必須同一個 tokenizer。

        `2002【遊戯王】…` 的 2002 是賣家的整理番號，拿它當域內年代證據
        就是假證據；而假證據與「否決被取消」是同一件事的兩面（工程原則 1）。
        """
        info = parse_card("2002【遊戯王】 青眼の白龍 PSA10 ウルトラ", watchlist)
        assert "year:2002" not in info.era_evidence

    def test_english_words_are_not_set_codes(self, watchlist):
        """LAST 裡的 AST、GLOBAL 裡的 LOB 都不是卡號。

        純子字串比對會把這種現代卡誤判成 2002-2004 英版，
        那是最貴的錯 —— 假年代證據會直接變成推播。
        """
        info = parse_card("Yugioh LAST WEEK GLOBAL sale PSA 9 modern card", watchlist)
        assert info.era_evidence == []
        assert not is_candidate(info, watchlist)[0]

    def test_real_jp_code_still_matches(self, watchlist):
        """收緊比對之後，真的卡號還是要認得出來（PS 後面接數字是合法寫法）。"""
        info = parse_card("PSA 10 遊戯王 青眼の白龍 PS-01", watchlist)
        assert "jp_code:PS" in info.era_evidence
        assert is_candidate(info, watchlist)[0]

    def test_unknown_grade_is_not_rejected(self, watchlist):
        """標題沒寫分數不代表卡不好，不要因為賣家懶得打字就漏掉。"""
        info = parse_card("遊戯王 PSA鑑定品 青眼の白龍 初期 レリーフ", watchlist)
        assert is_candidate(info, watchlist)[0]


# ---------------------------------------------------------------------------
# 從商品描述補抓鑑定分數（parsers.grade.resolve_grade）
#
# 這一組守的是一條紅線：補抓到的分數會直接乘進公允價、再反推出價上限，
# 使用者照著它下真錢的單。**抓錯比抓不到危險得多**，所以每一條測試問的都是
# 「這個情境下它有沒有乖乖說『無法判定』」，而不是「它抓到了嗎」。
# ---------------------------------------------------------------------------
#: 2026-08-02 從 `auctions.yahoo.co.jp/jp/auction/k1238516579` 實抓的描述，
#: 只截掉無關的後段。真正的分數是 **PSA5**，關鍵字堆裡卻有 PSA10／PSA9／ARS10。
#: 整段丟給 regex 會抓到 PSA10——高估 11 倍，而且方向正好是「上限開太高」。
REAL_SPAM_DESCRIPTION = (
    "PSA5ですが、とてもキレイな状態です！\n"
    "カードの状態は、掲載写真にてご確認ください。\n"
    "ご検討よろしくお願いいたします！\n"
    "\n"
    "●検索用ワード\n"
    "掛軸 掛け軸 シリアルナンバー 白き幻獣 グランドマスターレア\n"
    "遊戯王 PSA10 PSA9 PSA鑑定 ARS10 ARS9 ARS鑑定 BGS10 BGS ブラックラベル\n"
)


class TestGradeFromDescription:
    def test_recovers_the_grade_the_title_left_out(self):
        """標題只寫機構、描述寫了分數 → 補到，而且來源要標成 description。"""
        from ygo_sniper.parsers import SOURCE_DESCRIPTION, resolve_grade

        r = resolve_grade("PSA 遊戯王 トランプコレクション 暗黒の竜王 初期 希少",
                          "PSA5ですが、とてもキレイな状態です！")
        assert (r.grader, r.grade) == (Grader.PSA, 5.0)
        assert r.source == SOURCE_DESCRIPTION and not r.conflict

    def test_seo_keyword_spam_does_not_become_the_grade(self):
        """**本組最重要的一條**：真實描述尾巴的關鍵字堆不准變成分數。

        這一段是實抓的（見 `REAL_SPAM_DESCRIPTION` 的註）。沒有這道防線，
        這張 PSA5 的卡會被讀成 PSA10，公允價高估 11 倍。
        """
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade("PSA 遊戯王 暗黒の竜王 初期", REAL_SPAM_DESCRIPTION)
        assert r.grade == 5.0, "關鍵字堆裡的 PSA10 蓋掉了真正的 PSA5"

    def test_a_line_full_of_grades_is_treated_as_spam_even_without_a_marker(self):
        """沒寫「検索用」但一行塞三個分數的，一樣當關鍵字堆丟掉。"""
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade(
            "PSA 遊戯王 初期",
            "PSA6の美品です。\n遊戯王 PSA10 PSA9 ARS10 BGS10 レリーフ 初期",
        )
        assert r.grade == 6.0

    def test_description_contradicting_the_title_voids_both(self):
        """描述與標題矛盾 → **兩個都不採信**，一律「無法判定」。

        我們分不出是標題打錯還是描述打錯，猜哪一邊都是在賭。
        """
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade("【PSA10】遊戯王 青眼の白龍 初期", "実際はPSA8です、写真をご確認ください")
        assert r.grade is None and r.conflict
        assert "無法判定" in r.note

    def test_two_different_grades_in_the_description_is_also_unknown(self):
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade("遊戯王 PSA鑑定品 初期", "PSA9の1枚とPSA7の1枚、状態はお写真で")
        assert r.grade is None and r.conflict

    def test_a_different_grader_in_the_description_is_not_borrowed(self):
        """標題寫 ARS、描述只有 PSA9 → 那個分數多半在講別張卡，不採用。"""
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade("遊戯王 ARS 鑑定品 初期", "同梱可能です。他にPSA9も出品中")
        assert r.grade is None and r.conflict
        assert r.grader is Grader.ARS

    def test_authenticity_only_slabs_are_a_fact_not_a_gap(self):
        """「真贋鑑定のみ」的殼上本來就沒有分數——不是賣家漏寫。

        實例：`auctions.yahoo.co.jp/jp/auction/t1239317122`（ARS 鑑定品）。
        """
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade(
            "遊戯王 ホーリーナイトドラゴン 初期 シークレットレア ARS 鑑定品",
            "真贋鑑定のみ（本物であると鑑定されています）のものとなります。",
        )
        assert r.grade is None and not r.conflict
        assert "真贋鑑定" in r.note

    def test_no_description_tells_the_user_to_look_at_the_slab(self):
        """描述不可得時要給出**下一步**，不是只說「沒有」。"""
        from ygo_sniper.parsers import resolve_grade

        r = resolve_grade("遊戯王 PSA鑑定品 初期", None)
        assert r.grade is None and r.source is None
        assert "鑑定殼" in r.note

    def test_title_grade_keeps_its_source_label(self):
        from ygo_sniper.parsers import SOURCE_TITLE, parse_card, resolve_grade

        r = resolve_grade("【PSA10】遊戯王 青眼の白龍 初期", "コレクション整理です")
        assert (r.grade, r.source) == (10.0, SOURCE_TITLE)
        # parse_card 只看標題，所以它給的來源永遠是 title（抽不到就是 None）
        assert parse_card("【PSA10】遊戯王 青眼の白龍 初期", {}).grade_source == SOURCE_TITLE
        assert parse_card("遊戯王 PSA鑑定品 初期", {}).grade_source is None

    def test_japanese_text_right_after_the_number_still_matches(self):
        """`\\b` 對 CJK 無效——「PSA5ですが」的 5 與 で 之間沒有 word boundary。

        標題那組 pattern 因此在描述上命中率是 0%（2026-08-02 實測），
        描述這條路徑必須用放寬版。這條測試就是釘那個差別。
        """
        from ygo_sniper.parsers import grades_in_description

        assert grades_in_description("PSA5ですが") == [(Grader.PSA, 5.0)]
        # 但放寬不等於放行：後面接數字仍然不算（PSA123 不是 PSA1）
        assert grades_in_description("整理番号 PSA123") == []

    def test_strip_keyword_spam_keeps_the_head(self):
        from ygo_sniper.parsers import strip_keyword_spam

        kept = strip_keyword_spam(REAL_SPAM_DESCRIPTION)
        assert "PSA5" in kept
        assert "検索用" not in kept and "ARS10" not in kept


# ---------------------------------------------------------------------------
# 標題分數 pattern：CJK 邊界與「相當於」宣稱
# ---------------------------------------------------------------------------
class TestTitleGradeBoundary:
    """2026-08-03：把標題那組的結尾 `\\b` 換成 `(?!\\d)` 時撞到的兩件事。

    `\\b` 對 CJK 無效造成兩種漏抓，但它**同時**意外擋住了「PSA10相当」這種
    賣家宣稱——拿掉邊界就得把後者明講出來，否則會憑空造出不存在的鑑定，
    而且方向是高估（PSA10 的分數溢價是 ×2.10）。
    """

    @pytest.mark.parametrize(
        "title,grader,grade",
        [
            ("PSA9初期 遊戯王", "PSA", 9.0),        # 數字後直接接漢字
            ("PSA9.5初期", "PSA", 9.5),             # 半分不可以被截成 9.0
            ("BGS9.5初期", "BGS", 9.5),
            ("PSA 9 初期", "PSA", 9.0),             # 原本就會過的，不可退化
            ("ARS10+ 初期", "ARS", 10.0),
            ("遊戯王 PSA10 初期", "PSA", 10.0),
        ],
    )
    def test_grade_survives_cjk_after_the_number(self, title, grader, grade):
        g, v = parse_grade(title)
        assert g.value == grader and v == grade

    def test_digits_after_the_grade_are_still_rejected(self):
        """`PSA123` 不可以被讀成 PSA1——放寬 CJK 不等於放寬數字。"""
        g, v = parse_grade("PSA123 遊戯王")
        assert g is Grader.UNKNOWN and v is None

    @pytest.mark.parametrize(
        "title",
        [
            "ARS10 遊戯王 はにわ 初期 Vol.1 PSA10相当",
            "【ARS10】青眼の白龍　復刻シークレット　海馬セット　PSA10並",
            "遊戯王 初期 ARS9 PSA9相當",
            "遊戯王 ARS10 PSA10 クラス",
        ],
    )
    def test_equivalent_to_claims_are_not_a_real_grade(self, title):
        """「PSA10相当」是賣家說「品相相當於 PSA10」，不是 PSA 鑑定過。

        這幾筆的真實鑑定都是 ARS，讀成 PSA 會憑空生出一個不存在的鑑定結果。
        實測 1,922 筆真實標題裡「相当」12 筆、「並」15 筆。
        """
        g, _ = parse_grade(title)
        assert g is Grader.ARS, f"{title!r} 的宣稱詞沒有被擋掉"


class TestGraderIsNotAShopName:
    """2026-08-04：`【ARS書店】` 這種**店名**不可以被讀成 ARS 鑑定。

    事故：舊書店「ARS書店」的古書（1937／1953／1978／1986 年）通過了遊戲王
    鑑定卡的篩選——店名給了「機構」、書名裡的「明治初期」給了「年代證據」。
    這是 CJK 邊界坑的第四次現形，但方向相反：不是漏抓，是**多抓**
    （`(?![A-Za-z0-9])` 只擋英數，擋不住後面直接接漢字的店名）。

    本組的重點是**兩個方向都測**：擋掉店名的那幾條，與「不可以順手擋掉真標的」
    的那幾條。實測 32,180 個真實標題，本規則只改變 6 筆的 `parse_grade`
    （全部是【ARS書店】的古書），真鑑定卡誤殺 0 筆。
    """

    @pytest.mark.parametrize(
        "title",
        [
            # 語料實例（各縮短保留形狀）
            "【ARS書店】「東京土木建築業組合沿革誌」1937年.東京土木建築業組合／明治初期時代の業界",
            "【ARS書店】仏教『初期仏教の思想』三枝充悳.著・1978年・東洋哲学研究所",
            "【ARS書店】アイヌ民族『ハウカセの大きな石』北海道2000年代初期の年代史",
            # 同一家店的近鄰寫法（語料 0 命中，但與「鑑定書」不會相撞）
            "【ARS書房】明治初期の商業史 1953年",
        ],
    )
    def test_shop_name_is_not_a_grader(self, title):
        assert parse_grade(title) == (Grader.UNKNOWN, None)

    def test_the_old_books_no_longer_pass_the_filter(self, watchlist):
        """端到端：擋除必須發生在 `is_candidate`，不是只有 parse_grade 變乾淨。"""
        info = parse_card(
            "【ARS書店】『明治前期・岩手県農業発達史』著者：森嘉兵衛・1953・岩手県/明治初期の土地改革",
            watchlist,
        )
        ok, why = is_candidate(info, watchlist)
        assert not ok and why == "未偵測到鑑定機構"

    @pytest.mark.parametrize(
        "title,grader",
        [
            # 「鑑定書付き」的『書』不是店名——規則只看**緊接**機構名的那幾個字
            ("女剣士カナン 初期 ウルトラレア ARS 鑑定書付き", Grader.ARS),
            ("遊戯王　トライホーン・ドラゴン 初期ARS 鑑定書付き", Grader.ARS),
            # 「書店」出現在標題**別的位置**（書名／出版社）不影響機構判定
            ("遊戯王 青眼の白龍 初期 ARS鑑定品 ※紀伊国屋書店で購入した保護ケース付", Grader.ARS),
            # 既有行為：只有機構、沒有分數 → 機構仍要抓到（`_GRADER_ONLY` 的存在理由）
            ("遊戯王 PSA鑑定品 青眼の白龍 初期", Grader.PSA),
            ("遊戯王 ホーリーナイトドラゴン 初期 シークレットレア ARS 鑑定品", Grader.ARS),
            ("遊戯王 初期 ホーリーナイトドラゴン シークレット PSA", Grader.PSA),
        ],
    )
    def test_real_graded_cards_are_not_killed(self, title, grader):
        g, v = parse_grade(title)
        assert (g, v) == (grader, None), f"{title!r} 被誤殺"

    def test_arusu_is_not_in_the_vendor_list(self):
        """規則的反例：`アルス` **不可以**進詞表——賣家用它稱呼 ARS 鑑定。

        實例是 comps id=25480（真實成交 NT$19,366）。看起來 `アルス` 只出現在
        園藝鋸的品牌名上，加進詞表能多擋幾筆雜訊，但這一筆證明它會誤殺真標的。
        """
        t = "遊戯王 旧アジア　ARS9 カオスエンペラードラゴン　シークレットアルス　鑑定書付き　鑑定品"
        assert parse_grade(t) == (Grader.ARS, 9.0)
        assert parse_grade("遊戯王 青眼の白龍 初期 ARS アルス 鑑定書付き") == (Grader.ARS, None)


class TestSetCodeBoundary:
    """卡號的前後邊界同樣不能用 `\\b`——漢字算 `\\w`。

    卡號抓不到不會擋掉標的，但會讓它在估價時退到 L3（沒有同卡成交可比），
    也就是拿不到出價上限。
    """

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("遊戯王LOB-001", "LOB-001"),       # 前面直接接漢字
            ("LOB-001初期", "LOB-001"),         # 後面直接接漢字
            ("遊戯王 LOB-001 初期", "LOB-001"),  # 原本就會過的
            ("遊戯王 オベリスクの巨神兵 G4-02", "G4-02"),
            ("PSA10 Dark Magician SDY-006", "SDY-006"),  # 分數不可被當卡號
            ("LOB-0011", None),                 # 位數不符仍然不收
        ],
    )
    def test_set_code_survives_cjk_neighbours(self, title, expected):
        assert extract_set_code(title) == expected
