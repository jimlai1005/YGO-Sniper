"""卡名主檔／年代判定的三個誤殺洞（2026-08-07 逐筆重放通知時發現）。

三個洞的共同形狀：**正中目標輪廓的真標的，被主檔比對層靜默丟掉**——
CLAUDE.md 第一節的那一類錯。每一區先釘「錯的樣子 → 對的樣子」（紅燈測試），
再釘該守衛原本就在做對的事（防止修洞時把守衛整個拆掉）。

  洞 1  三幻神特典（G4-01/02/03、GBI-001/002/003）：主檔的 ocg_date 是量產版
        首發日（2008-2011），name-match 一律判 out_of_era，蓋過標題裡的
        特典卡號。實際誤殺 4 筆 signals（本檔用的就是那 4 筆原始標題）。
  洞 2  『守備』封じ：官方卡名帶 『』 引號，賣家不寫引號，`fold()` 不折掉
        引號 → 鍵永遠對不上。同類還有『攻撃』封じ、死のメッセージ「Ａ」等
        （主檔實測 6 個年代內、52 個年代外卡名帶 『』「」）。
  洞 3  一字卡名（山／森／氷／海／闇）被 `_is_usable_key` 的長度門檻整批
        擋在索引外。守衛本身是對的（「闇」出現在幾乎每個標題），
        修法是窄放行：一字鍵只在**緊接稀有度／機構／期別詞**時才算命中
        （全語料 36,691 筆實測：規則命中 12 筆，全是真一字卡標的或
        本來就被「枚セット」排除的多卡 lot，誤配 0）。
"""

from __future__ import annotations

import pytest

from ygo_sniper.cards import CardIndex, fold


def _card(name_ja, *, id=1, ocg="2000-01-01", aliases=None, en="", codes=None):
    return {
        "id": id, "name_ja": name_ja, "name_en": en,
        "ocg_date": ocg, "aliases": aliases or [], "set_codes": codes or [],
    }


#: 三幻神在主檔的樣子：out_of_era、日期是量產版首發日。
_GODS_OUT_OF_ERA = {
    "オシリスの天空竜": "2011-12-17",
    "オベリスクの巨神兵": "2008-12-20",
    "ラーの翼神竜": "2009-12-19",
}


# ---------------------------------------------------------------------------
# 洞 1：年代外量產卡的「年代內特典印刷」
# ---------------------------------------------------------------------------
class TestEraPrintings:
    """G4-（DM4 GB 特典，主檔內部證據：15 張年代內卡帶 G4- 卡號、ocg_date
    全部 = 2000-12-07）與 GBI-（同家族海外版特典，語料旁證見 cards.py 註記）
    是 1998-2004 的印刷；標題帶這些卡號時，量產版的 ocg_date 不可以蓋過它。"""

    def _index(self):
        return CardIndex([], dict(_GODS_OUT_OF_ERA))

    @pytest.mark.parametrize(
        "title,name",
        [
            # ↓ 這 4 筆是 2026-08-07 重放時實際被誤殺的 signals 原始標題
            ("psa9 オベリスクの巨神兵 G4-02 シークレット 2期 遊戯王.",
             "オベリスクの巨神兵"),
            ("遊戯王 オベリスクの巨神兵 G4-02 ウルトラ　psa9",
             "オベリスクの巨神兵"),
            ("遊戯王 PSA10 オベリスクの巨神兵 三幻神 GBI-002 ウルトラレア初期 God 英語版 通常盤",
             "オベリスクの巨神兵"),
            ("遊戯王 オシリスの天空竜 G4-01 シークレットレア PSA8 鑑定品",
             "オシリスの天空竜"),
            # ↓ comps 語料裡同形狀的ラー（洞是同一個）
            ("ARS9 遊戯王 ラーの翼神竜 シークレット SE G4-03",
             "ラーの翼神竜"),
        ],
    )
    def test_god_promo_with_era_set_code_is_in_era(self, title, name):
        m = self._index().match(title)
        assert m is not None and m.name_ja == name
        assert m.in_era, (
            f"{name} 帶特典卡號卻被量產版 ocg_date 判成年代外：{title!r}"
        )

    def test_g4_printing_carries_the_promo_date_not_the_reprint_date(self):
        """回傳的 ocg_date 必須是特典印刷的日期（同源：主檔 G4 系列 15 張
        年代內卡的一致日期），不是 2008 量產版的。"""
        m = self._index().match("遊戯王 オベリスクの巨神兵 G4-02 ウルトラ　psa9")
        assert m is not None and m.ocg_date == "2000-12-07"

    # --- 守衛的另一份工作：沒有特典卡號的現代量產版必須維持年代外 ---
    def test_god_without_a_set_code_stays_out_of_era(self):
        m = self._index().match("遊戯王 オベリスクの巨神兵 PSA10 美品")
        assert m is not None and m.in_era is False

    def test_god_with_an_unrelated_set_code_stays_out_of_era(self):
        """卡號要在**這張卡自己的**特典清單裡才算，別張卡的年代卡號不算。"""
        m = self._index().match("遊戯王 オベリスクの巨神兵 LOB-001 PSA10")
        assert m is not None and m.in_era is False

    def test_other_modern_card_with_a_g4_code_stays_out_of_era(self):
        """特典清單以卡名為鍵：不在清單上的年代外卡，帶 G4 卡號也不翻案。"""
        idx = CardIndex([], {"竜騎士ブラック・マジシャン・ガール": "2014-05-17"})
        m = idx.match("竜騎士ブラック・マジシャン・ガール G4-02 PSA10")
        assert m is not None and m.in_era is False


# ---------------------------------------------------------------------------
# 洞 2：官方卡名裡的 『』「」 引號
# ---------------------------------------------------------------------------
class TestQuotedNames:
    def test_quotes_fold_away(self):
        assert fold("『守備』封じ") == fold("守備封じ")
        assert fold("死のメッセージ「Ａ」") == fold("死のメッセージA")

    def test_stop_defense_matches_the_unquoted_seller_spelling(self):
        """實際誤殺的 signals 標題：賣家寫 守備封じ，主檔是 『守備』封じ。"""
        idx = CardIndex([_card("『守備』封じ", ocg="1999-05-27", en="Stop Defense")])
        m = idx.match("遊戯王　初期　守備封じ　スーパーレア　PSA8　鑑定品　")
        assert m is not None and m.name_ja == "『守備』封じ" and m.in_era
        assert m.ocg_date == "1999-05-27"

    def test_quoted_out_of_era_name_still_flags_modern_cards(self):
        """同一條折疊規則也要幫年代外那一側：「Ａ」細胞… 是 2007 年卡，
        賣家不寫引號時要認得出來（in_era=False 是明確訊號，不是雜訊）。"""
        idx = CardIndex([], {"「Ａ」細胞増殖装置": "2007-02-15"})
        m = idx.match("遊戯王 A細胞増殖装置 PSA10")
        assert m is not None and m.in_era is False


# ---------------------------------------------------------------------------
# 洞 3：一字卡名的窄放行
# ---------------------------------------------------------------------------
class TestSingleCharNames:
    def _index(self):
        return CardIndex([
            _card("山", id=1, ocg="1999-03-06"),
            _card("氷", id=2, ocg="1999-09-23"),
            _card("闇", id=3, ocg="1999-03-06"),
            _card("海", id=4, ocg="1999-03-06"),
            _card("７", id=5, ocg="2004-02-26"),
        ])

    @pytest.mark.parametrize(
        "title,name",
        [
            # ↓ 重放時實際被誤殺的 signals 標題
            ("遊戯王　初期　山　スーパーレア　PSA7　鑑定品", "山"),
            # ↓ 語料裡同形狀的真標的（全語料量測的命中樣本）
            ("【ARS鑑定 8】山 スーパーレア Super Rare 遊戯王 OCG 鑑定書付き PSA BGS ARS 鑑定品 STARTER BOX スターターボックス 1999", "山"),
            ("遊戯王　氷　ノーマル　Vol.５　カード", "氷"),
            ("PSA9 遊戯王 山 初期 STARTER BOX", "山"),
            ("遊戯王　初期　山　ARS8", "山"),
        ],
    )
    def test_single_char_card_next_to_rarity_grader_or_era_word_matches(self, title, name):
        m = self._index().match(title)
        assert m is not None and m.name_ja == name and m.in_era, (
            f"一字卡名 {name} 緊鄰稀有度／機構／期別詞仍比不到：{title!r}"
        )

    # --- 守衛的另一份工作：不緊鄰限定詞的一字不可以進來 ---
    def test_single_char_inside_ordinary_words_does_not_match(self):
        """「闇の支配者」「山盛り」——一字後面接的不是限定詞就不算
        （與 tests/test_cards_match.py::TestDangerousKeys 同一條線）。"""
        assert self._index().match("遊戯王 闇の支配者 初期 PSA9 山盛り") is None

    def test_single_char_tail_of_a_token_does_not_match(self):
        """PSA7 的 7 不是卡「７」：一字鍵前面是英數字就不算。"""
        assert self._index().match("PSA7 スーパーレア 遊戯王") is None

    def test_ascii_single_char_stays_out_entirely(self):
        """「７」NFKC 後是 ASCII 的 7，出現在每個分數與卡號裡——維持不進索引。"""
        assert self._index().match("遊戯王 ７ スーパーレア 初期") is None

    def test_kaiba_is_not_the_card_umi(self):
        """海馬（人名）的「海」後面接「馬」，不是限定詞——不可以比中卡「海」。"""
        assert self._index().match("遊戯王 海馬 デッキ PSA10 鑑定") is None

    def test_set_name_tail_char_is_not_the_card(self):
        """全語料 diff 抓到的實際誤配：ポケモン e5「神秘なる山」PSA9。

        引號折掉後「山」在折疊字串裡緊鄰 PSA，但它是套組名的**詞尾**，
        不是獨立 token——所以確認必須在原始標題上做（「山」前一字是 る）。
        這筆若放進來，會拿寶可夢的成交去污染卡「山」的行情。"""
        title = "ヒメグマ ● :1ED [e5 066/088](拡張パック第5弾「神秘なる山」)PSA９"
        assert self._index().match(title) is None


# ---------------------------------------------------------------------------
# 整合：真實主檔 + 重放的 6 筆誤殺標題（與 tests/test_corpus.py 同樣直讀 data/）
# ---------------------------------------------------------------------------
class TestReplayedMissesAgainstRealMaster:
    """單元測試用合成索引；這裡用真實主檔再走一次同樣的 6 筆，
    確保修法在生產資料上真的接得起來（測試路徑＝生產路徑，CLAUDE.md 第六節）。"""

    @pytest.fixture()
    def idx(self, cfg):
        idx = CardIndex.load(cfg.root / "data/cards_1998_2004.json")
        if not idx.available:
            pytest.skip("卡名主檔不存在（data/ 不進版控）")
        return idx

    @pytest.mark.parametrize(
        "title,name",
        [
            ("psa9 オベリスクの巨神兵 G4-02 シークレット 2期 遊戯王.", "オベリスクの巨神兵"),
            ("遊戯王 オベリスクの巨神兵 G4-02 ウルトラ　psa9", "オベリスクの巨神兵"),
            ("遊戯王 PSA10 オベリスクの巨神兵 三幻神 GBI-002 ウルトラレア初期 God 英語版 通常盤",
             "オベリスクの巨神兵"),
            ("遊戯王 オシリスの天空竜 G4-01 シークレットレア PSA8 鑑定品", "オシリスの天空竜"),
            ("遊戯王　初期　守備封じ　スーパーレア　PSA8　鑑定品　", "『守備』封じ"),
            ("遊戯王　初期　山　スーパーレア　PSA7　鑑定品", "山"),
        ],
    )
    def test_replayed_title_is_now_in_era(self, idx, title, name):
        m = idx.match(title)
        assert m is not None, f"仍然比不到：{title!r}"
        assert m.name_ja == name and m.in_era, (
            f"比到 {None if m is None else (m.name_ja, m.in_era)}，"
            f"期望 ({name!r}, in_era=True)：{title!r}"
        )
