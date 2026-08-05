"""非遊戲王的卡不准進來——但**更不准誤殺真的古董卡**。

事故（2026-08-03，使用者回報）：「我發現裡面有混一些球員卡，棒球跟籃球都有。」
根因是 `PSA 1999` 那條查詢刻意不帶「遊戯王」（為了撈到標題沒寫廠牌的真古董卡），
代價就是撈進棒球／籃球／足球／他 TCG。

⚠️ **這個檔案存在的主要理由是第二節（不誤殺），不是第一節（擋掉）。**
先前試過用 `cards.looks_like_yugioh` 當正面表列的守門，實測會誤殺
`ブラックマジシャンガール 初期 ウルトラ P4-01`、`双頭の雷龍 初期 ウルトラ`、
`クレセントドラゴン 初期 psa9` 這一類——正是花力氣要救的那一批。
所以修法是**負面表列**（他卡種的品牌／聯盟名），而遊戯王字樣只用來
「赦免」（`exclude_keywords_unless_yugioh`），永遠不用來「錄取」。

第三節釘的是幾個**實測會誤殺、所以被剔除**的候選詞。它們看起來都很合理
（NBA、BASEBALL、サッカー…），憑印象加回去是這類清單最典型的退化方式，
所以用測試把「為什麼不能加」變成紅燈而不是一行註解。
"""

from __future__ import annotations

import pytest

from ygo_sniper.parsers import is_candidate, parse_card

# --- 1. 這些必須被擋掉（全部取自 db 裡真的躺著的訊號標題）------------------
OTHER_TCG_TITLES = [
    # 棒球（Calbee／カルビー プロ野球チップス）
    "1999 Calbee 松坂大輔 PSA8 スターカード",
    "PSA8 1999 Calbee 松坂大輔 スターカード S-41",
    "PSA9 イチロー 1999年カルビー T-06 タイトルホルダー",
    # 足球
    "激レア 1999 DS France PSA 8 インサート 中田英寿 ROMA",
    # 籃球
    "1999 ヴィンス・カーター SKYBOX METAL VINCE CARTER PSA9 ",
    # デュエル・マスターズ
    "PSA8 聖鎧亜クイーン・アルカディアス 旧枠 初期 クラシック",
    "PSA8 デュエルマスターズ アルティメット影虎ドラゴン 旧枠 初期 クラシック",
    # デジモン
    "【PSA9】ホーリードラモン 2000 旧デジモンカード",
]

# --- 2. 這些絕對不准被擋（全部是實測會被正面表列誤殺的真古董卡）------------
REAL_VINTAGE_TITLES = [
    "PSA9 ブラックマジシャンガール 初期 ウルトラ P4-01",
    "【 鑑定品 PSA9 】 双頭の雷龍 初期 ウルトラ",
    "クレセントドラゴン　初期　psa9 最安値",
    "PSA9 ダンシング・エルフ UR 初期 PREMIUM PACK 1999 B-1156",
    "【PSA9】カエルスライム 初期 プレミアムパック",
    "【PSA9】大砲だるま 初期 プレミアムパック",
    # 遊戯王賣家自己也在用「旧枠」「クラシック」——有遊戯王字樣就必須放行
    "遊戯王 PSA9 マクロコスモス ウルトラ クラシック 旧テキスト",
    "遊戯王 PSA10 ラーイエロー 旧枠ウルトラ 初期",
]


@pytest.mark.parametrize("title", OTHER_TCG_TITLES)
def test_other_card_games_are_excluded(title, watchlist):
    info = parse_card(title, watchlist)
    assert info.excluded_by, f"沒被擋掉：{title}"
    ok, why = is_candidate(info, watchlist)
    assert ok is False and why.startswith("排除字")


@pytest.mark.parametrize("title", REAL_VINTAGE_TITLES)
def test_real_vintage_cards_survive(title, watchlist):
    """回歸測試：這幾筆是「標題沒寫遊戯王的真古董卡」，誤殺它們等於白做。"""
    info = parse_card(title, watchlist)
    assert info.excluded_by is None, f"誤殺：{title}（被 {info.excluded_by} 擋掉）"
    ok, why = is_candidate(info, watchlist)
    assert ok is True, f"誤殺：{title}（{why}）"


# --- 3. 實測會誤殺、所以刻意不在清單裡的候選詞 ------------------------------
@pytest.mark.parametrize(
    "word,collides_with",
    [
        ("NBA", "CANNONBALL SPEAR SHELLFISH"),        # 子字串 CA-NBA-LL
        ("BASEBALL", "ULTIMATE BASEBALL KID"),        # 真的有這張卡
        ("サッカー", "ブラッド・サッカー"),              # 另有 4 張同構卡名
        ("スターカード", "モンスターカード"),            # 是它的子字串
        ("クラシック", "遊戯王 マクロコスモス クラシック"),  # 遊戯王賣家自己在用
        # 2026-08-05：這條不是「差點加進去」，是**已經加進去、活著誤殺**的。
        # 由 `ygo-sniper corpus-diff` 抓到：它殺掉了語料裡的
        # 「PSA9 遊戯王 プリズマン 初期 ノーマル Vol.6」——PSA9 初期 Vol.6，
        # 正中目標輪廓。要擋的現代稀有度用 "プリズマティック" 就夠，前綴形式純屬冗餘。
        ("プリズマ", "プリズマン / Ｅ・ＨＥＲＯ プリズマー"),
    ],
)
def test_rejected_candidates_stay_rejected(word, collides_with, watchlist):
    """這些詞看起來都該加，但每一個都實測會誤殺——用紅燈擋住「憑印象加回去」。"""
    assert word not in watchlist.get("exclude_keywords", []), (
        f"{word} 會誤殺 {collides_with}，不可以放進 exclude_keywords"
    )
    assert word not in watchlist.get("exclude_keywords_unless_yugioh", []), (
        f"{word} 會誤殺 {collides_with}"
    )


# --- 4. 第二層的機制本身 -----------------------------------------------------
def test_soft_list_only_fires_without_a_yugioh_marker():
    """`exclude_keywords_unless_yugioh`：遊戯王字樣只用來赦免，不用來錄取。"""
    wl = {
        "exclude_keywords": [],
        "exclude_keywords_unless_yugioh": ["旧枠"],
        "era_markers": {"jp_keywords": ["初期"]},
    }
    assert parse_card("PSA9 なんとかドラゴン 旧枠 初期", wl).excluded_by == "旧枠"
    assert parse_card("遊戯王 PSA9 なんとか 旧枠 初期", wl).excluded_by is None
    # 沒有這個鍵的舊 watchlist 照樣運作（不是必填欄位）
    assert parse_card("PSA9 なんとか 旧枠 初期", {"era_markers": {}}).excluded_by is None


def test_hard_list_wins_regardless_of_yugioh_marker():
    """無條件清單不受遊戯王字樣影響——「遊戯王 … ポケモン」那種混賣照樣擋。"""
    wl = {"exclude_keywords": ["デジモン"], "era_markers": {"jp_keywords": ["初期"]}}
    assert parse_card("遊戯王 デジモン 初期 PSA9 まとめ", wl).excluded_by == "デジモン"


# --- 5. 把新規則套回既有資料（cli.recheck-signals 的核心）-------------------
def _seed_signals(tmp_path):
    from ygo_sniper.store import Store

    store = Store(tmp_path / "t.db")
    with store._conn() as c:
        for key, title, state in (
            ("buyee_mercari:m1", "1999 Calbee 松坂大輔 PSA8 スターカード", "new"),
            ("buyee_mercari:m2", "PSA9 遊戯王 青眼の白龍 初期 ウルトラ", "new"),
            ("buyee_mercari:m3", "【PSA9】ホーリードラモン 2000 旧デジモンカード", "in_bundle"),
        ):
            c.execute(
                "INSERT INTO signals (key, site, external_id, title, url, state, score) "
                "VALUES (?,?,?,?,?,?,1)",
                (key, "buyee_mercari", key.split(":")[1], title, "https://x/1", state),
            )
        c.execute(
            "INSERT INTO listing_obs (key, source, site, title, url, first_seen, last_seen, "
            "seen_count) VALUES ('buyee_mercari:m1','s','buyee_mercari','t','u','a','a',1)"
        )
    return store


def _doomed(store, watchlist):
    """cli.recheck_signals 的核心（同一套判準），這裡不啟動 typer。"""
    return [
        r["key"]
        for r in store.all_signal_titles()
        if not is_candidate(parse_card(r["title"], watchlist), watchlist)[0]
    ]


def test_recheck_purges_contamination_and_keeps_the_real_card(tmp_path, watchlist):
    store = _seed_signals(tmp_path)
    doomed = _doomed(store, watchlist)
    assert sorted(doomed) == ["buyee_mercari:m1", "buyee_mercari:m3"]

    rep = store.purge_signals(doomed)
    # m3 是使用者手動丟進湊單籃的 → 程式不准刪，只回報
    assert rep == {"deleted": 1, "kept_manual": 1, "obs_deleted": 1}
    left = {r["key"] for r in store.all_signal_titles()}
    assert left == {"buyee_mercari:m2", "buyee_mercari:m3"}


def test_recheck_is_idempotent(tmp_path, watchlist):
    """重跑一定是 0 筆可刪——冪等要看得出來，不是用信的。"""
    store = _seed_signals(tmp_path)
    store.purge_signals(_doomed(store, watchlist))
    second = _doomed(store, watchlist)
    assert second == ["buyee_mercari:m3"], "手動標過的那筆會一直被列出來（提醒你自己處理）"
    assert store.purge_signals(second)["deleted"] == 0
    assert store.purge_signals([])["deleted"] == 0
