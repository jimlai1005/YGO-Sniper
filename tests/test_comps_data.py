"""行情資料地基：稀有度抽取、年代判定、入庫過濾、結構化欄位。

這四件事是後續統計建模的前提。錯了不會有人喊——只會得到一個
看起來很正常、但中位數被垃圾樣本拉歪的行情表。所以每一條都釘死。
"""

import sqlite3

import pytest
from conftest import make_listing

from ygo_sniper.comps import CompsEngine
from ygo_sniper.domain import Currency, Grader, Site
from ygo_sniper.parsers import extract_rarity, is_candidate, parse_card
from ygo_sniper.store import Store


# ---------------------------------------------------------------------------
class TestRarity:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("レリーフ", "ultimate"),
            ("アルティメットレア", "ultimate"),
            ("シークレット", "secret"),
            ("ウルトラ", "ultra"),
            ("スーパーレア", "super"),
            ("ノーマルレア", "normal_rare"),
            ("ノーマル", "normal"),
            ("遊戯王 初期 美品", None),
        ],
    )
    def test_extract(self, title, expected):
        assert extract_rarity(title) == expected

    @pytest.mark.parametrize(
        "title,expected",
        [
            # 長詞優先：短詞排在後面才不會把長詞搶走
            ("ノーマルレア", "normal_rare"),      # 不可被 ノーマル 搶走
            ("ウルトラレア", "ultra"),            # 不可被 レア 搶走
            ("スーパーレア", "super"),            # 不可被 レア 搶走
            ("アルティメットレア", "ultimate"),    # 不可被 レア 搶走
            ("シークレットレア", "secret"),        # 不可被 レア 搶走
            ("パラレルレア", "parallel"),          # 不可被 レア 搶走
            ("レア", "rare"),                     # 兜底仍然要能命中
        ],
    )
    def test_longest_word_wins(self, title, expected):
        assert extract_rarity(title) == expected

    @pytest.mark.parametrize(
        "title,expected",
        [
            ("PSA10 Blue-Eyes ULTRA RARE LOB-001", "ultra"),
            ("Dark Magician SECRET RARE", "secret"),
            ("Exodia SUPER RARE 1st Edition", "super"),
            ("ULTIMATE RARE Blue-Eyes", "ultimate"),
            ("PARALLEL RARE promo", "parallel"),
        ],
    )
    def test_english_forms(self, title, expected):
        assert extract_rarity(title) == expected

    def test_halfwidth_katakana_normalized(self):
        """全形／半形片假名走 NFKC，賣家怎麼打都要認得。"""
        assert extract_rarity("遊戯王 ｽｰﾊﾟｰﾚｱ PSA9") == "super"

    def test_no_guessing(self):
        """抽不到就回 None。猜出來的稀有度會變成統計建模的假樣本。"""
        assert extract_rarity("遊戯王 青眼の白龍 PSA10 鑑定品") is None

    def test_parse_card_fills_rarity(self, watchlist):
        info = parse_card("【PSA10】遊戯王 初期 青眼の白龍 シークレット", watchlist)
        assert info.rarity == "secret"

    def test_rarity_survives_exclusion(self, watchlist):
        """被排除的標的也要留下屬性，否則沒辦法回頭分析擋掉了什麼。"""
        info = parse_card("25th ブラックマジシャン アルティメットレア PSA10", watchlist)
        assert info.excluded_by is not None
        assert info.rarity == "ultimate"


# ---------------------------------------------------------------------------
class TestEraNotRarity:
    """稀有度不是年代。這四筆是實測抓到的誤判與正確案例，釘死不許回退。"""

    @pytest.mark.parametrize(
        "title",
        [
            # 2009 年卡（CRMS = クリムゾン・クライシス），只因寫了「レリーフ」被誤收
            "レリーフ PSA8 CRMS-JP019 デスカイザー・ドラゴン",
            # 2014 年卡，只因寫了「アルティメット」被誤收
            "PSA9 クリスタルウィング・シンクロ・ドラゴン アルティメット",
        ],
    )
    def test_rarity_alone_is_not_era_evidence(self, watchlist, title):
        info = parse_card(title, watchlist)
        assert info.era_evidence == [], f"稀有度不該產生年代證據：{info.era_evidence}"
        ok, why = is_candidate(info, watchlist)
        assert not ok
        assert "年代" in why

    @pytest.mark.parametrize(
        "title",
        [
            "遊戯王 初期 悪魔鏡の儀式 スーパーレア PSA8 鑑定品",
            "ARS10 遊戯王 はにわ 初期 Vol.1",
        ],
    )
    def test_real_era_markers_still_pass(self, watchlist, title):
        info = parse_card(title, watchlist)
        ok, why = is_candidate(info, watchlist)
        assert ok, why

    def test_watchlist_has_no_rarity_in_era_markers(self, watchlist):
        """防呆：不准有人再把稀有度塞回年代標記。"""
        kws = watchlist["era_markers"]["jp_keywords"]
        for bad in ("レリーフ", "アルティメット", "シークレット", "ウルトラ", "スーパー"):
            assert bad not in kws, f"{bad} 是稀有度，不是年代標記"

    def test_min_grade_focuses_on_7_to_10(self, watchlist):
        assert watchlist["min_grade"] == {"PSA": 7, "ARS": 7}
        assert is_candidate(parse_card("PSA7 遊戯王 初期 青眼の白龍", watchlist), watchlist)[0]
        assert not is_candidate(parse_card("PSA6 遊戯王 初期 青眼の白龍", watchlist), watchlist)[0]


class TestEraKeywordIsNotIllustrationVersion:
    """「初期絵」是**畫工版本**，不是年代——現代復刻卡最愛用這個詞。

    實例（2026-08-01 從行情庫抓到的真汙染）：
      `遊戯王 PSA10 ブラック・マジシャン・ガール 初期絵 20thSE 赤文字 DMMD-JP001`
      成交 NT$42,630，是 2020 年後的卡，卻因為「初期」被判成 1998-2004。
    這種假證據直接把公允價往上拉，而且完全沒有外顯症狀。
    """

    @pytest.mark.parametrize(
        "title",
        [
            "遊戯王 PSA10 ブラックマジシャンガール 初期絵 ウルトラ",
            "遊戯王 PSA9 青眼の白龍 初期イラスト シークレット",
        ],
    )
    def test_illustration_variant_is_not_era_evidence(self, watchlist, title):
        info = parse_card(title, watchlist)
        assert info.era_evidence == [], f"初期絵/初期イラスト 不是年代證據：{info.era_evidence}"
        assert not is_candidate(info, watchlist)[0]

    def test_plain_hatsuki_still_counts(self, watchlist):
        """只擋帶後綴的那一處，不准連真的「初期」一起誤殺。"""
        info = parse_card("遊戯王 PSA9 青眼の白龍 初期 ウルトラ", watchlist)
        assert "jp_kw:初期" in info.era_evidence

    def test_both_in_one_title_keeps_the_real_one(self, watchlist):
        """同一個標題裡兩種寫法都有時，單獨的那一處仍然算數（逐處判斷）。"""
        info = parse_card("遊戯王 PSA9 初期絵 復刻 と 初期 の比較 ウルトラ", watchlist)
        assert "jp_kw:初期" in info.era_evidence


class TestModernAndBundleExclusions:
    """兩類汙染：現代確認詞、多卡整包。

    整包尤其要擋——一筆整包的價格是「好幾張卡的總價」卻被當成一張卡的行情，
    對區間寬度的傷害遠大於它的筆數。實例：
      `PSA10 遊戯王 青眼の白龍 ブラック・マジシャン 連番2枚セット 初期` NT$451,111。
    """

    @pytest.mark.parametrize(
        "title,expect",
        [
            ("PSA10 遊戯王 青眼の白龍 ブラック・マジシャン 連番2枚セット 初期", "枚セット"),
            ("遊戯王 初期 ウルトラ PSA9 セット売り", "セット売り"),
            ("遊戯王 初期 レリーフ PSA9 重複あり", "重複"),
            ("遊戯王 PSA10 ブラック・マジシャン・ガール 初期絵 20thSE 赤文字", "20th"),
            ("遊戯王 初期 PSA10 ホログラフィックレア", "ホログラフィック"),
            ("遊戯王 初期 PSA10 レアリティコレクション", "レアリティコレクション"),
            ("遊戯王 PSA10 初期 DMMD-JP001", "DMMD"),
        ],
    )
    def test_excluded(self, watchlist, title, expect):
        info = parse_card(title, watchlist)
        assert info.excluded_by == expect
        ok, why = is_candidate(info, watchlist)
        assert not ok and expect in why

    def test_single_card_is_not_caught_by_the_bundle_words(self, watchlist):
        """排除字是子字串比對，誤殺單張卡就是把好貨一起丟掉。"""
        assert is_candidate(
            parse_card("PSA10 遊戯王 初期 青眼の白龍 ウルトラ 1枚", watchlist), watchlist
        )[0]

    def test_exclusions_have_no_duplicates(self, watchlist):
        kws = watchlist["exclude_keywords"]
        assert len(kws) == len(set(kws)), "排除字重複＝有人沒先看清單就往下加"


# ---------------------------------------------------------------------------
@pytest.fixture
def engine(cfg, fx, tmp_path):
    return CompsEngine(cfg, fx, Store(tmp_path / "t.db"))


def _sold(title, price=5000, ext="x1"):
    return make_listing(price=price, title=title, external_id=ext, site=Site.BUYEE_MERCARI)


class TestIngestFilter:
    def test_blocks_pokemon_and_sleeves(self, engine):
        """實測汙染源：405 筆歷史 comps 有 43% 是這種東西。"""
        junk = [
            _sold("メガリザードンXex MA 223/193 PSA10 MEGAドリームex", ext="p1"),
            _sold("フクオカのピカチュウ PSA10 プロモ", ext="p2"),
            _sold("CARDBOARD GOLD PSA鑑定品用スリーブ 10枚", ext="p3"),
            _sold("２枚 Card Saver カードセーバー 1 PSA 鑑定 提出 スリーブ", ext="p4"),
        ]
        report = engine.ingest_sold(junk)
        assert report.kept == 0
        assert report.rejected == 4
        assert sum(report.reasons.values()) == 4

    def test_blocks_modern_cards(self, engine):
        report = engine.ingest_sold([
            _sold("レリーフ PSA8 CRMS-JP019 デスカイザー・ドラゴン", ext="m1"),
            _sold("PSA9 クリスタルウィング・シンクロ・ドラゴン アルティメット", ext="m2"),
        ])
        assert report.kept == 0
        assert report.reasons == {"無 1998-2004 年代證據": 2}

    def test_keeps_real_vintage(self, engine):
        report = engine.ingest_sold([
            _sold("遊戯王 初期 悪魔鏡の儀式 スーパーレア PSA8 鑑定品", ext="g1"),
            _sold("ARS10 遊戯王 はにわ 初期 Vol.1", ext="g2"),
        ])
        assert report.kept == 2
        assert report.rejected == 0

    def test_reason_distribution_is_reported(self, engine):
        report = engine.ingest_sold([
            _sold("遊戯王 初期 青眼の白龍 ポケモン", ext="r1"),          # 排除字
            _sold("PSA3 遊戯王 初期 青眼の白龍", ext="r2"),               # 分數
            _sold("BGS9 遊戯王 初期 青眼の白龍", ext="r3"),               # 機構
            _sold("遊戯王 現代カード PSA10", ext="r4"),                   # 年代
            _sold("遊戯王 初期 青眼の白龍 美品", ext="r5"),               # 沒鑑定機構
        ])
        assert report.kept == 0
        assert report.reasons == {
            "排除字 ポケモン": 1,
            "分數低於門檻": 1,
            "機構 BGS 不在名單": 1,
            "無 1998-2004 年代證據": 1,
            "未偵測到鑑定機構": 1,
        }
        assert "擋 5 筆" in report.summary()

    def test_rejected_rows_never_reach_the_db(self, engine):
        engine.ingest_sold([_sold("メガリザードンXex PSA10", ext="z1")])
        assert engine.store.comps_by() == []


# ---------------------------------------------------------------------------
class TestCompsColumns:
    def test_structured_attributes_are_persisted(self, engine):
        engine.ingest_sold([
            _sold("【PSA9】遊戯王 初期 青眼の白龍 LOB-001 シークレット", price=8000, ext="c1")
        ])
        rows = engine.store.comps_by()
        assert len(rows) == 1
        row = rows[0]
        assert row["rarity"] == "secret"
        assert row["grader"] == Grader.PSA.value
        assert row["grade"] == 9.0
        assert row["set_code"] == "LOB-001"
        assert "jp_kw:初期" in row["era_evidence"]
        assert row["card_name"] is None          # 這輪先留空欄位
        # 原始標題永久保留：分桶是查詢時的決定，不是寫入時的決定
        assert row["title"].startswith("【PSA9】")
        assert row["signature"]                  # signature 保留（相容）

    def test_comps_by_filters_combine(self, engine):
        engine.ingest_sold([
            _sold("PSA10 遊戯王 初期 青眼の白龍 シークレット", price=9000, ext="a1"),
            _sold("PSA9 遊戯王 初期 ブラックマジシャン シークレット", price=7000, ext="a2"),
            _sold("PSA9 遊戯王 初期 真紅眼の黒竜 ウルトラ", price=6000, ext="a3"),
            _sold("ARS9 遊戯王 初期 死者蘇生 ウルトラ", price=5000, ext="a4"),
        ])
        store = engine.store
        assert len(store.comps_by()) == 4
        assert len(store.comps_by(rarity="secret")) == 2
        assert len(store.comps_by(rarity="ultra")) == 2
        assert len(store.comps_by(grader="PSA")) == 3
        assert len(store.comps_by(grader="PSA", grade=9.0)) == 2
        assert len(store.comps_by(rarity="ultra", grader="ARS", grade=9.0)) == 1
        assert store.comps_by(rarity="ultra", grader="ARS")[0]["price_native"] == 5000

    def test_comps_by_since_days_window(self, engine):
        engine.ingest_sold([_sold("PSA10 遊戯王 初期 青眼の白龍", ext="w1")])
        assert len(engine.store.comps_by(since_days=90)) == 1
        # 把 sold_at 推到 200 天前 → 90 天窗口撈不到
        with sqlite3.connect(engine.store.db_path) as c:
            c.execute("UPDATE comps SET sold_at = '2020-01-01T00:00:00+00:00'")
        assert engine.store.comps_by(since_days=90) == []
        assert len(engine.store.comps_by()) == 1        # 沒有窗口就撈得到

    def test_comps_by_returns_raw_rows_not_stats(self, engine):
        """store 層只交出原始列，統計是上層的決定。"""
        engine.ingest_sold([_sold("PSA10 遊戯王 初期 青眼の白龍", price=1234, ext="s1")])
        rows = engine.store.comps_by()
        assert isinstance(rows, list) and isinstance(rows[0], dict)
        assert rows[0]["price_native"] == 1234
        assert rows[0]["currency"] == Currency.JPY.value

    def test_load_comps_roundtrip_keeps_attributes(self, engine):
        engine.ingest_sold([
            _sold("PSA10 遊戯王 初期 青眼の白龍 レリーフ", price=9000, ext="rt1")
        ])
        engine.load_from_store()
        rows = [r for lst in engine._index.values() for r in lst]
        assert rows and rows[0]["rarity"] == "ultimate"


# ---------------------------------------------------------------------------
class TestSchemaMigration:
    """既有 db 是舊 schema。開機補欄位必須**零資料遺失**、且可重複執行。"""

    OLD_SCHEMA = """
    CREATE TABLE comps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signature TEXT NOT NULL, title TEXT, price_twd REAL, price_native REAL,
        currency TEXT, url TEXT, site TEXT, sold_at TEXT, confidence TEXT,
        UNIQUE(signature, url)
    );
    """

    def test_migration_preserves_existing_rows(self, tmp_path):
        db = tmp_path / "old.db"
        with sqlite3.connect(db) as c:
            c.executescript(self.OLD_SCHEMA)
            c.executemany(
                "INSERT INTO comps (signature, title, price_twd, url) VALUES (?,?,?,?)",
                [(f"SIG{i}", f"舊資料{i}", 100.0 + i, f"https://x.test/{i}") for i in range(7)],
            )

        store = Store(db)                       # 觸發補欄位

        with sqlite3.connect(db) as c:
            c.row_factory = sqlite3.Row
            assert c.execute("SELECT COUNT(*) n FROM comps").fetchone()["n"] == 7
            cols = {r["name"] for r in c.execute("PRAGMA table_info(comps)")}
            row = c.execute("SELECT * FROM comps WHERE signature='SIG3'").fetchone()
        assert {"rarity", "grader", "grade", "set_code", "era_evidence", "card_name"} <= cols
        assert row["title"] == "舊資料3" and row["price_twd"] == 103.0
        assert row["rarity"] is None            # 舊列的新欄位是 NULL，不是被填假值

        Store(db)                               # 冪等：重跑不炸、不掉資料
        assert len(store.comps_by(limit=100)) == 7


# ---------------------------------------------------------------------------
class TestEraEvidenceBackfill:
    """`era_evidence` 是**寫入時**算好存進去的，而估價的取樣就是靠它過濾。

    所以改了 watchlist 之後不回填，新判準只對「之後才抓到的資料」生效——
    庫裡的舊污染（現代卡、多卡整包、把「初期絵」當年代證據的那些）
    會原封不動繼續參與估價。這個類別釘住回填的兩條性質：判準與入庫同一組、
    重跑冪等。
    """

    def _seed(self, tmp_path):
        store = Store(tmp_path / "b.db")
        rows = {
            "SIG": [
                # 舊判準收下的污染：整包、現代卡、初期絵，全都有假的年代證據
                {"title": "PSA10 遊戯王 青眼の白龍 連番2枚セット 初期", "price_twd": 451111.0,
                 "url": "u1", "era_evidence": "jp_kw:初期"},
                {"title": "遊戯王 PSA10 ブラマジガール 初期絵 20thSE 赤文字", "price_twd": 42630.0,
                 "url": "u2", "era_evidence": "jp_kw:初期"},
                # 入庫門檻擋得住、但年代證據還在的（分數過低）
                {"title": "ARS6 遊戯王 初期 青眼の白龍 ウルトラ", "price_twd": 3000.0,
                 "url": "u3", "era_evidence": "jp_kw:初期"},
                # 真貨：回填後必須原封不動
                {"title": "PSA9 遊戯王 初期 青眼の白龍 ウルトラ", "price_twd": 5000.0,
                 "url": "u4", "era_evidence": "jp_kw:初期"},
            ]
        }
        store.save_comps(rows)
        return store

    def _backfill(self, store, watchlist):
        """cli.backfill_era 的核心（同一套判準），這裡不啟動 typer。"""
        updates = []
        for r in store.comps_by(limit=1000):
            info = parse_card(r["title"], watchlist)
            ok, _why = is_candidate(info, watchlist)
            new = ",".join(info.era_evidence) if ok else ""
            if new != (r["era_evidence"] or ""):
                updates.append((r["id"], new))
        return store.set_era_evidence(updates)

    def test_backfill_clears_contamination_and_keeps_the_real_one(self, tmp_path, watchlist):
        store = self._seed(tmp_path)
        assert store.count_era_verified_comps() == 4
        assert self._backfill(store, watchlist) == 3
        assert store.count_era_verified_comps() == 1
        kept = [r for r in store.comps_by(limit=100) if r["era_evidence"]]
        assert [r["price_twd"] for r in kept] == [5000.0]

    def test_backfill_is_idempotent(self, tmp_path, watchlist):
        """重跑一定是 0 列改動——冪等要看得出來，不是用信的。"""
        store = self._seed(tmp_path)
        assert self._backfill(store, watchlist) > 0
        assert self._backfill(store, watchlist) == 0
        assert self._backfill(store, watchlist) == 0

    def test_backfill_applies_the_same_gate_as_ingest(self, tmp_path, watchlist):
        """回填只看年代關鍵字是不夠的：實測庫裡有 8 列有年代證據卻過不了入庫
        門檻（PSA1／ARS6／BGS，含一筆 NT$357,280 的 BGS7），一直在參與估價。"""
        store = self._seed(tmp_path)
        self._backfill(store, watchlist)
        low = [r for r in store.comps_by(limit=100) if r["url"] == "u3"][0]
        assert low["era_evidence"] == ""
