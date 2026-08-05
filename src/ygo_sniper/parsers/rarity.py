"""從標題抽取稀有度。

稀有度**不是**年代。這件事踩過坑：`レリーフ`（アルティメットレア 的俗稱）
與 `アルティメット` 一度被放在 watchlist 的 `era_markers.jp_keywords` 裡當作
「1998-2004 的證據」，結果 `レリーフ PSA8 CRMS-JP019`（2009 年卡）與
`PSA9 クリスタルウィング・シンクロ・ドラゴン アルティメット`（2014 年卡）
全被判成候選——現代卡一樣有アルティメットレア。稀有度從此獨立成本模組，
年代證據只由真正的年代標記（初期／期別／舊卡號／年份）提供。

設計：**封閉詞表 + 順序敏感**，第一個命中的就是答案。
順序規則是「長詞先比」——`ノーマルレア` 必須排在 `ノーマル` 前面，
`ウルトラ`／`スーパー` 必須排在 `レア` 前面，否則 `ウルトラレア`
會被 `レア` 搶走變成 rare。

抽不到就回 `None`。實測約 65% 的標題可抽出稀有度，**不猜**——
猜出來的稀有度會直接變成統計建模的假樣本，比缺值更糟。
"""

from __future__ import annotations

from .grade import normalize

#: (needle, rarity) —— **順序即優先序**，第一個命中者勝。
#: 修改時務必維持「長詞在前、含子字串者在前」的不變式，
#: 對應的防呆測試見 tests/test_comps_data.py::TestRarity。
_RARITY_TABLE: tuple[tuple[str, str], ...] = (
    # ultimate（レリーフ 是アルティメットレア的俗稱，同一種稀有度）
    ("レリーフ", "ultimate"),
    ("アルティメット", "ultimate"),
    ("ULTIMATE", "ultimate"),
    # secret（シク 是シークレット的縮寫）
    ("シークレット", "secret"),
    ("シク", "secret"),
    ("SECRET", "secret"),
    # parallel
    ("パラレル", "parallel"),
    ("PARALLEL", "parallel"),
    # ultra
    ("ウルトラ", "ultra"),
    ("ULTRA", "ultra"),
    # super
    ("スーパー", "super"),
    ("SUPER", "super"),
    # normal_rare 必須早於 normal，否則被 ノーマル 搶走
    ("ノーマルレア", "normal_rare"),
    ("ノーマル", "normal"),
    # rare 是最後的兜底：所有 XXレア 都已在上面被更精確的詞攔下
    ("レア", "rare"),
)


def extract_rarity(title: str) -> str | None:
    """回傳稀有度代碼（ultimate/secret/parallel/ultra/super/normal_rare/normal/rare）。

    抽不到回 None。標題先做 NFKC 正規化（半形片假名 ﾚﾘｰﾌ → レリーフ）
    再轉大寫（英文寫法用）；片假名不受 upper() 影響。
    """
    text = normalize(title).upper()
    for needle, rarity in _RARITY_TABLE:
        if needle in text:
            return rarity
    return None
