"""卡片主檔比對。

比對錯了不會有人喊——只會得到一個「卡名欄位有填、看起來很正常」的行情表，
然後估價把兩張不同的卡算進同一個桶。所以規則要一條一條釘死：
最長優先、假名折疊、分隔符折疊、太短的名字不進索引、年代外要標得出來。
"""

from __future__ import annotations

import pytest

from ygo_sniper.cards import CardIndex, fold, reading_alias, strip_ruby


def _card(name_ja, *, id=1, ocg="2000-01-01", aliases=None, en=""):
    return {
        "id": id, "name_ja": name_ja, "name_en": en,
        "ocg_date": ocg, "aliases": aliases or [],
    }


# ---------------------------------------------------------------------------
class TestRubyParsing:
    def test_strip_ruby_keeps_base_text(self):
        raw = "<ruby>青眼の白龍<rt>ブルーアイズ・ホワイト・ドラゴン</rt></ruby>"
        assert strip_ruby(raw) == "青眼の白龍"

    def test_strip_ruby_handles_per_character_furigana(self):
        raw = "<ruby>八<rt>や</rt></ruby>つ<ruby>手<rt>で</rt></ruby>サソリ"
        assert strip_ruby(raw) == "八つ手サソリ"

    def test_whole_name_katakana_reading_is_an_alias(self):
        """「ブルーアイズ・ホワイト・ドラゴン」也是賣家會打的寫法。"""
        raw = "<ruby>青眼の白龍<rt>ブルーアイズ・ホワイト・ドラゴン</rt></ruby>"
        assert reading_alias(raw) == "ブルーアイズ・ホワイト・ドラゴン"

    def test_per_character_furigana_is_not_an_alias(self):
        """逐字注音串起來是「やで」這種沒人會打的東西，不可以進索引。"""
        raw = "<ruby>八<rt>や</rt></ruby>つ<ruby>手<rt>で</rt></ruby>サソリ"
        assert reading_alias(raw) is None
        assert reading_alias("<ruby>白亀<rt>しろカメ</rt></ruby>") is None  # 含平假名


# ---------------------------------------------------------------------------
class TestFolding:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("ブラック・マジシャン・ガール", "ブラックマジシャンガール"),  # 中黑點
            ("ブラック・マジシャン", "ブラック•マジシャン"),               # 賣家用 bullet
            ("はにわ", "ハニワ"),                                          # 平假名／片假名
            ("ｽｰﾊﾟｰ", "スーパー"),                                        # 半形片假名
            ("妖精伝姫－ラチカ", "妖精伝姫ラチカ"),                        # 全形連字號
            ("ホーリー・エルフ", "ホーリー　エルフ"),                      # 全形空白
        ],
    )
    def test_equivalent_writings_fold_together(self, a, b):
        assert fold(a) == fold(b)

    def test_long_vowel_mark_survives(self):
        """ー 是真的音，折掉會讓「エルフ」比中一堆不相干的東西。"""
        assert "ー" in fold("ホーリー・エルフ")


# ---------------------------------------------------------------------------
class TestLongestMatch:
    def test_longer_name_wins_over_contained_name(self):
        """需求指定的規則：「青眼の白龍」不可以被「青眼」搶走。"""
        idx = CardIndex([_card("青眼", id=1), _card("青眼の白龍", id=2)])
        m = idx.match("【PSA10】遊戯王 初期 青眼の白龍 LOB-001")
        assert m is not None and m.name_ja == "青眼の白龍" and m.card_id == 2

    def test_insertion_order_does_not_matter(self):
        """長詞優先必須來自排序，不是來自「誰先被加進去」。"""
        for cards in (
            [_card("青眼の白龍", id=2), _card("青眼", id=1)],
            [_card("青眼", id=1), _card("青眼の白龍", id=2)],
        ):
            assert CardIndex(cards).match("初期 青眼の白龍").name_ja == "青眼の白龍"

    def test_alias_matches_and_reports_canonical_name(self):
        idx = CardIndex([_card("青眼の白龍", aliases=["ブルーアイズ・ホワイト・ドラゴン"])])
        m = idx.match("PSA9 ブルーアイズホワイトドラゴン 初期")
        assert m is not None and m.name_ja == "青眼の白龍"

    def test_earliest_card_name_wins_over_a_later_longer_one(self):
        """一個標題出現兩個不重疊卡名時取先出現的——賣家先寫商品本體。

        實例：「PSA10 神の宣告 2期 スーパーレア ME66 鋼鉄の襲撃者」的後半
        是套組名。純比長度會挑到後面那張現代卡，把一張 2 期真品判成年代外。
        """
        idx = CardIndex(
            [_card("神の宣告", id=1)], {"鋼鉄の襲撃者": "2017-11-11"}
        )
        m = idx.match("PSA10 神の宣告 2期 スーパーレア ME66 鋼鉄の襲撃者")
        assert m is not None and m.name_ja == "神の宣告" and m.in_era

    def test_modern_card_containing_a_vintage_name_is_not_vintage(self):
        """「竜騎士ブラック・マジシャン・ガール」是 2014 年卡，不可以被判成舊卡。"""
        idx = CardIndex(
            [_card("ブラック・マジシャン・ガール")],
            {"竜騎士ブラック・マジシャン・ガール": "2014-05-17"},
        )
        m = idx.match("竜騎士ブラック・マジシャン・ガール PSA10")
        assert m is not None and not m.in_era

    def test_no_match_returns_none(self):
        """比不到本身就是訊號（多半不是 1998-2004 的單卡），不可以硬湊。"""
        idx = CardIndex([_card("青眼の白龍")])
        assert idx.match("遊戯王 初期 まとめ 10枚セット PSA9") is None


class TestDangerousKeys:
    def test_single_character_names_are_excluded(self):
        """「闇」「山」「海」是真的卡名，但它出現在幾乎每個標題裡——
        收進索引等於每一筆都誤判。"""
        idx = CardIndex([_card("闇"), _card("山"), _card("７")])
        assert idx.match("遊戯王 闇の支配者 初期 PSA9 山盛り") is None

    def test_short_ascii_names_are_excluded(self):
        """純英數短名會被 PSA10 / LOB-001 這種字串誤觸發。"""
        idx = CardIndex([_card("A2")])
        assert idx.match("遊戯王 PSA10 A2 初期") is None


class TestEraFlag:
    def test_out_of_era_match_is_reported_as_such(self):
        idx = CardIndex([_card("青眼の白龍")], {"妖精伝姫－ラチカ": "2019-07-13"})
        m = idx.match("【PSA10】遊戯王 妖精伝姫－ラチカ")
        assert m is not None
        assert m.in_era is False and m.ocg_date == "2019-07-13"

    def test_in_era_match_carries_ocg_date(self):
        idx = CardIndex([_card("はにわ", ocg="1999-02-04")])
        m = idx.match("ARS10 遊戯王 はにわ 初期 Vol.1")
        assert m is not None and m.in_era and m.ocg_date == "1999-02-04"


class TestLoading:
    def test_missing_master_degrades_quietly(self, tmp_path):
        """主檔不在（新 clone、還沒跑 refresh-cards）不是錯誤——
        估價會自動退到不看卡名的那一層，但不可以整個程式炸掉。"""
        idx = CardIndex.load(tmp_path / "nope.json")
        assert not idx.available and len(idx) == 0
        assert idx.match("遊戯王 初期 青眼の白龍 PSA10") is None

    def test_roundtrip_through_disk(self, tmp_path):
        from ygo_sniper.cards import write_master

        data = {
            "cards": [_card("青眼の白龍", aliases=["ブルーアイズ・ホワイト・ドラゴン"])],
            "out_of_era": {"妖精伝姫－ラチカ": "2019-07-13"},
        }
        p = write_master(data, tmp_path / "m.json")
        idx = CardIndex.load(p)
        assert idx.available and len(idx) == 1
        assert idx.match("初期 青眼の白龍 PSA9").in_era
        assert idx.match("妖精伝姫ラチカ PSA10").in_era is False


# ---------------------------------------------------------------------------
# 2026-08-02 命中率修法。每一條測試都對應一個實測過的失敗（或誤配）案例，
# 案例出處見 cards.py 頂註。**這一整區的重點是防退化，不是防呆。**
# ---------------------------------------------------------------------------
def _en_card(name_ja, name_en, *, id=1, ocg="2000-01-01", codes=None):
    return {
        "id": id, "name_ja": name_ja, "name_en": name_en, "ocg_date": ocg,
        "aliases": [], "set_codes": codes or [],
    }


class TestEnglishNames:
    def test_english_card_name_matches_an_english_title(self):
        """eBay 標題全是英文。實測基線：103 筆 eBay 標題命中 0 筆。"""
        idx = CardIndex([_en_card("眠り子", "Nemuriko")])
        m = idx.match("PSA 9 Nemuriko LOB-013 1st Edition Yu-Gi-Oh! Mint 2002")
        assert m is not None and m.name_ja == "眠り子" and m.via == "name"

    def test_english_punctuation_is_folded(self):
        """「Fiend Reflection #2」的 # 與「Ray & Temperature」的 & 賣家寫法不一。"""
        idx = CardIndex([_en_card("ミラージュ", "Fiend Reflection #2")])
        assert idx.match("1st Edition Yugioh PSA 8 Fiend Reflection 2 LOB-021") is not None

    def test_short_english_names_stay_out_of_the_index(self):
        """「Yami」只有四個字母，會被標題裡的「YAMI YUGI」誤觸發。

        漏掉一張 Yami 的代價是退回 L3；配錯的代價是拿別張卡的行情去出價。
        """
        idx = CardIndex([_en_card("闇", "Yami")])
        assert idx.match("2002 YU-GI-OH! 1ST ED #051 YAMI PSA 9") is None

    def test_japanese_title_still_prefers_the_japanese_name(self):
        """英文名進索引不可以改變日文標題的既有行為。"""
        idx = CardIndex([_en_card("青眼の白龍", "Blue-Eyes White Dragon")])
        m = idx.match("【PSA10】遊戯王 初期 青眼の白龍 LOB-001")
        assert m is not None and m.name_ja == "青眼の白龍"


class TestSetNameStripping:
    #: 實測的災難案例：套組名「Legend of Blue Eyes White Dragon」內含卡名
    #: 「Blue-Eyes White Dragon」，60 筆幾百塊的雜魚卡全被配成全庫最貴的那張卡。
    TITLE = "2002 YU-GI-OH! LOB-LEGEND OF BLUE EYES WHITE DRAGON 1ST ED MAN EATER PSA 9"

    def _index(self, **kw):
        return CardIndex(
            [_en_card("青眼の白龍", "Blue-Eyes White Dragon", id=1),
             _en_card("マンイーター", "Man Eater", id=2)],
            **kw,
        )

    def test_without_stripping_the_set_name_wins_and_that_is_the_bug(self):
        """記錄「不剝套組名會怎樣」——這條在測試裡釘死，才不會有人把剝除拿掉。"""
        m = self._index().match(self.TITLE)
        assert m is not None and m.name_ja == "青眼の白龍"

    def test_stripping_the_set_name_recovers_the_real_card(self):
        m = self._index(set_names=["Legend of Blue Eyes White Dragon"]).match(self.TITLE)
        assert m is not None and m.name_ja == "マンイーター"

    def test_a_real_blue_eyes_in_that_set_still_matches(self):
        """剝除只吃掉套組名那一段，同一個標題裡另外寫的卡名要留著。"""
        idx = self._index(set_names=["Legend of Blue Eyes White Dragon"])
        m = idx.match(
            "2002 YU-GI-OH! LOB-LEGEND OF BLUE EYES WHITE DRAGON 1ST ED "
            "BLUE-EYES WHITE DRAGON PSA 9"
        )
        assert m is not None and m.name_ja == "青眼の白龍"

    def test_set_name_that_is_also_a_card_name_is_not_stripped(self):
        """套組名剛好等於卡名時不可以剝——那會把卡名一起吃掉。"""
        idx = CardIndex([_en_card("青眼の白龍", "Blue-Eyes White Dragon")],
                        set_names=["Blue-Eyes White Dragon"])
        assert idx.match("PSA9 Blue-Eyes White Dragon LOB-001") is not None


class TestSetCodeLookup:
    def _index(self):
        return CardIndex([
            _en_card("ビッグ・シールド・ガードナー", "Big Shield Gardna", id=1, codes=["G5-2"]),
            _en_card("ランドスターの剣士", "Swordsman of Landstar", id=2, codes=["JY-2"]),
        ])

    def test_code_rescues_a_title_whose_card_name_is_misspelled(self):
        """實測：賣家把「ビッグ」打成「ビック」，卡名比不到、卡號救得回來。"""
        m = self._index().match(
            "遊戯王 1円スタート　ビックシールドガードナー　シークレット　PSA10 初期 G5-02"
        )
        assert m is not None and m.name_ja == "ビッグ・シールド・ガードナー"
        assert m.via == "code" and m.matched_key == "G5-2"

    def test_leading_zeros_and_region_tags_fold_to_the_same_key(self):
        idx = CardIndex([_en_card("火炎草", "Firegrass", codes=["LOB-18"])])
        for title in ("Yugioh PSA 8 Firegrass LOB-018", "遊戯王 火炎草 LOB-EN018 PSA8"):
            assert idx.match(title) is not None

    def test_card_name_wins_over_a_conflicting_code(self):
        """卡名與卡號指到不同卡時**一律以卡名為準**。

        實測案例：`ホルスの黒炎竜LV8 SOD-JP007` 的卡號在資料源指向 LV6；
        賣家寫的卡名才是他要賣的東西。
        """
        idx = CardIndex([
            _en_card("ホルスの黒炎竜 ＬＶ６", "Horus LV6", id=1, codes=["SOD-7"]),
            _en_card("ホルスの黒炎竜 ＬＶ８", "Horus LV8", id=2, codes=["SOD-8"]),
        ])
        m = idx.match("PSA8 遊戯王 レリーフ ホルスの黒炎竜LV8 SOD-JP007 アルティメット")
        assert m is not None and m.name_ja == "ホルスの黒炎竜 ＬＶ８" and m.via == "name"

    def test_code_is_ignored_when_the_title_is_not_a_yugioh_listing(self):
        """實測誤配：一把園藝鋸的型號 PS-18 被配成「クラブ・タートル」。"""
        idx = CardIndex([_en_card("クラブ・タートル", "Crab Turtle", codes=["PS-18"])])
        assert idx.match(
            "★【未使用】ARS アルスコーポレーション ピストル型鋸 剪定・枝打ち 替刃式 "
            "L荒目 PS-18 のこぎり ノコギリ"
        ) is None

    def test_ambiguous_codes_are_dropped_entirely(self):
        """一個卡號指到兩張卡（歐版／美版撞號）時整條丟掉，不猜。"""
        idx = CardIndex([
            _en_card("伝説の剣", "Legendary Sword", id=1, codes=["LOB-40"]),
            _en_card("闇の護符", "Dark Talisman", id=2, codes=["LOB-40"]),
        ])
        assert idx.n_codes == 0
        assert idx.match("遊戯王 PSA9 LOB-040") is None

    def test_grader_scores_are_never_read_as_set_codes(self):
        idx = CardIndex([_en_card("クラブ・タートル", "Crab Turtle", codes=["PSA-10"])])
        assert idx.match("遊戯王 初期 PSA-10 鑑定品") is None


class TestFoldingExtras:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("スロットマシーンＡＭ－７", "スロットマシーンAM−7"),   # U+2212 減號
            ("Ｄ．Ｄ．クロウ", "DDクロウ"),                        # 半形句點
            ("Yu-Gi-Oh!", "yugioh"),                              # 英文標點
            ("Ray & Temperature", "Ray and Temperature".replace(" and ", " ")),
        ],
    )
    def test_extra_separators_fold_together(self, a, b):
        assert fold(a) == fold(b)

    def test_long_vowel_mark_still_survives(self):
        """新增的分隔符不可以誤傷長音符——它是真的音。"""
        assert "ー" in fold("ホーリー・エルフ")


class TestNoRegressionOnMisMatchGuards:
    def test_toon_black_magician_girl_is_still_not_the_base_card(self):
        """最長匹配的邊界：既有那條紅線在新規則下必須原樣成立。"""
        idx = CardIndex(
            [_en_card("ブラック・マジシャン・ガール", "Dark Magician Girl")],
            {"トゥーン・ブラック・マジシャン・ガール": "2015-11-14"},
        )
        m = idx.match("トゥーン・ブラック・マジシャン・ガール PSA10 遊戯王")
        assert m is not None and not m.in_era

    def test_old_master_without_new_fields_still_loads(self, tmp_path):
        """2026-08-02 之前的主檔（沒有 set_codes / set_names）不可以讀不起來。"""
        from ygo_sniper.cards import write_master

        p = write_master({"cards": [_card("青眼の白龍")], "out_of_era": {}}, tmp_path / "m.json")
        idx = CardIndex.load(p)
        assert idx.available and idx.n_codes == 0
        assert idx.match("初期 青眼の白龍 PSA9").in_era


class TestMasterBuild:
    """`build_master` 的卡號／套組名純度規則。"""

    @staticmethod
    def _sources():
        ygo = {"data": [
            {"id": 1, "name": "Firegrass",
             "misc_info": [{"ocg_date": "1999-02-04", "konami_id": 11}]},
            {"id": 2, "name": "Insect Queen",
             "misc_info": [{"ocg_date": "2002-11-21", "konami_id": 22}]},
            {"id": 3, "name": "Some Modern Card",
             "misc_info": [{"ocg_date": "2016-07-09", "konami_id": 33}]},
        ]}
        yugi = [
            {"password": 1, "konami_id": 11, "name": {"ja": "火炎草"},
             "sets": {"en": [{"set_number": "LOB-EN018", "set_name": "Legend of Blue Eyes"}]}},
            {"password": 2, "konami_id": 22, "name": {"ja": "インセクト女王"},
             "sets": {"ja": [{"set_number": "DL4-136", "set_name": "Duelist Legacy 4"},
                             {"set_number": "DP16-JP025", "set_name": "Duelist Pack 16"}]}},
            {"password": 3, "konami_id": 33, "name": {"ja": "現代のなにか"},
             "sets": {"ja": [{"set_number": "DP16-JP001", "set_name": "Duelist Pack 16"}]}},
        ]
        return ygo, yugi

    def test_prefixes_that_also_printed_modern_cards_are_excluded(self):
        """DP16 同時收錄復刻與現代卡 → 整個前綴丟掉。

        沒有這條，`インセクト女王 DP16-JP025` 會被卡號反查成 1998-2004，
        而它其實是 2016 年的復刻。
        """
        from ygo_sniper.cards import build_master

        ygo, yugi = self._sources()
        data = build_master(ygoprodeck=ygo, yaml_yugi=yugi)
        codes = {c["name_ja"]: c["set_codes"] for c in data["cards"]}
        assert codes["インセクト女王"] == ["DL4-136"]
        assert codes["火炎草"] == ["LOB-18"]
        assert "Duelist Pack 16" not in data["set_names"]
        assert "Legend of Blue Eyes" in data["set_names"]

    def test_normalize_set_code_shares_a_basis_with_title_extraction(self):
        """主檔端與標題端必須折到同一個鍵，不然永遠對不起來（工程原則 1）。"""
        from ygo_sniper.cards import extract_title_codes, normalize_set_code

        assert normalize_set_code("LOB-EN001") == "LOB-1"
        assert extract_title_codes("Yugioh PSA9 LOB-001 1st") == ["LOB-1"]
        assert normalize_set_code("LOB-E040") is None      # 歐版編號不同步，不收
