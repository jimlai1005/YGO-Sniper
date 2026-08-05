"""全語料載入器與 `corpus-diff` 的比對邏輯。

這支工具存在的理由是「讓 `CLAUDE.md` 第一節那條驗收協定可以被執行」，
所以本檔要守住的不變式只有三件事：

1. **語料不可以悄悄縮水**——解不出標題的檔案必須被算進 `failures`。
   靜默縮水的後果是比對結果「假性乾淨」（被誤殺的那筆根本沒進語料）。
2. **判定改變抓得到**——尤其是「排除字命中真實卡名」這一類，
   因為市場上不是每天都有那張卡在賣，只掃在架標題會漏掉它。
3. **語料輪替不可以被讀成判定改變**——快取有 TTL，兩次跑的語料本來就不同。
"""

from __future__ import annotations

import json

import pytest

from ygo_sniper.corpus import (
    Baseline,
    CorpusError,
    Verdict,
    diff_verdicts,
    extract_titles,
    judge,
    load_card_names,
    load_corpus,
    name_hits,
    save_baseline,
)

_YAHOO_SEARCH = """
<html><body><ul>
<li class="Product"><a class="Product__titleLink" href="/auction/x1">遊戯王 初期 青眼の白龍 PSA10</a></li>
<li class="Product"><a class="Product__titleLink" href="/auction/x2">遊戯王 二期 ブラックマジシャン ARS9</a></li>
</ul></body></html>
"""

_BUYEE_SEARCH = """
<html><body>
<div><a href="/mercari/item/m123" title="遊戯王 初期 ホーリーナイトドラゴン PSA9">
<img alt="遊戯王 初期 ホーリーナイトドラゴン PSA9"></a><span>3,000円</span></div>
</body></html>
"""


def _next_data(payload: dict) -> str:
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


class TestExtractTitles:
    """每一種快取檔都要落進一個具名 kind——「不知道」不是合法結果。"""

    def test_yahoo_search_page(self):
        kind, titles = extract_titles(_YAHOO_SEARCH)
        assert kind == "yahoo_search"
        assert titles == ("遊戯王 初期 青眼の白龍 PSA10", "遊戯王 二期 ブラックマジシャン ARS9")

    def test_buyee_search_page(self):
        kind, titles = extract_titles(_BUYEE_SEARCH)
        assert kind == "buyee_search"
        assert titles == ("遊戯王 初期 ホーリーナイトドラゴン PSA9",)

    def test_yahoo_closed_next_data(self):
        node = {
            "totalResultsAvailable": 2,
            "items": [{"title": "落札A"}, {"title": "落札B"}],
        }
        html = _next_data(
            {"props": {"pageProps": {"initialState": {"search": {"items": {"listing": node}}}}}}
        )
        assert extract_titles(html) == ("yahoo_closed", ("落札A", "落札B"))

    def test_paypay_next_data(self):
        node = {"totalResultsAvailable": 1, "items": [{"title": "フリマA"}]}
        html = _next_data(
            {"props": {"initialState": {"searchState": {"search": {"result": node}}}}}
        )
        assert extract_titles(html) == ("paypay", ("フリマA",))

    def test_ruten_detail_json(self):
        body = json.dumps([{"ProdId": "2261", "ProdName": "遊戲王 初期 PSA9"}], ensure_ascii=False)
        assert extract_titles(body) == ("ruten_detail", ("遊戲王 初期 PSA9",))

    @pytest.mark.parametrize(
        "text,kind",
        [
            # 露天搜尋回應只有 Id，標題在詳情呼叫裡：本來就沒有標題，不算失敗
            ('{"TotalRows":87,"Rows":[{"Id":"2253"}]}', "ruten_search_ids"),
            ('{"TotalRows":0,"Rows":[],"LimitedTotalRows":0}', "ruten_empty"),
            # 頁面自述查無結果
            ("<html><body>該当する商品が見つかりません</body></html>", "buyee_empty"),
        ],
    )
    def test_no_titles_by_design_is_not_a_failure(self, text, kind):
        got_kind, titles = extract_titles(text)
        assert (got_kind, titles) == (kind, ())

    @pytest.mark.parametrize(
        "text,kind",
        [
            ("", "empty_file"),
            ("<html><body><div>completely unknown page</div></body></html>", "unknown_page"),
            # JS 沒跑完的骨架：測試路徑必須等於生產路徑（CLAUDE.md 第六節）
            ('<html><body><img src="loading-spinner.gif"></body></html>',
             "unrendered_skeleton"),
        ],
    )
    def test_unparsable_pages_get_a_failing_kind(self, text, kind):
        got_kind, titles = extract_titles(text)
        assert (got_kind, titles) == (kind, ())


class TestLoadCorpus:
    def test_failed_files_are_counted_not_skipped(self, tmp_path):
        """解不出標題的檔案必須報數。靜默跳過 = 比對結果假性乾淨。"""
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "ok.html").write_text(_YAHOO_SEARCH, encoding="utf-8")
        (cache / "broken.html").write_text("<html><body>???</body></html>", encoding="utf-8")
        (cache / "empty.html").write_text("", encoding="utf-8")
        # 設計上就沒有標題的那種，不可以被算成失敗
        (cache / "ids.html").write_text('{"TotalRows":9,"Rows":[{"Id":"1"}]}', encoding="utf-8")

        db = tmp_path / "sniper.db"
        import sqlite3

        con = sqlite3.connect(db)
        for table in ("signals", "comps", "listing_obs"):
            con.execute(f"create table {table} (title text)")
        con.execute("insert into signals values ('遊戯王 初期 PSA10 デビル・ドラゴン')")
        con.commit()
        con.close()

        master = tmp_path / "cards.json"
        master.write_text(
            json.dumps({"cards": [{"name_ja": "ブラッド・サッカー", "name_en": "Blood Sucker"}],
                        "out_of_era": {"シャーク・サッカー": "2010-01-01"},
                        "set_names": ["決闘者の遺産"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        corpus = load_corpus(db_path=db, cache_dir=cache, master_path=master)

        assert corpus.n_files == 4
        assert {f.path.name for f in corpus.failures} == {"broken.html", "empty.html"}
        assert corpus.kind_counts["ruten_search_ids"] == 1
        assert corpus.n_db_titles == 1
        assert corpus.n_cache_titles == 2
        assert len(corpus.titles) == 3
        assert corpus.era_card_names == frozenset({"ブラッド・サッカー", "Blood Sucker"})
        assert set(corpus.card_names) >= {"シャーク・サッカー", "決闘者の遺産"}

    def test_missing_db_is_loud(self, tmp_path):
        (tmp_path / "cache").mkdir()
        with pytest.raises(CorpusError):
            load_corpus(
                db_path=tmp_path / "nope.db",
                cache_dir=tmp_path / "cache",
                master_path=tmp_path / "cards.json",
            )

    def test_real_corpus_is_not_empty(self, cfg):
        """真語料的下限檢查：載入器整個壞掉時，0 筆會被讀成「規則沒改壞」。

        門檻寫得很寬（10,000／10,000）是刻意的——這條要抓的是「載入器死了」，
        不是「今天的快取少了幾百筆」，寫太緊只會變成每天紅一次的假警報。
        """
        corpus = load_corpus(
            db_path=cfg.db_path,
            cache_dir=cfg.cache_dir,
            master_path=cfg.root / "data/cards_1998_2004.json",
        )
        assert len(corpus.titles) > 10_000
        assert len(corpus.card_names) > 10_000
        assert corpus.n_db_titles > 0
        assert corpus.n_cache_titles > 0
        # 解不出標題的比例失控 = 語料悄悄縮水
        assert corpus.n_failures < corpus.n_files * 0.1


class TestDiff:
    def _baseline(self, verdicts, hits=None, universe=None):
        return Baseline(
            verdicts=verdicts,
            name_hits=hits or {},
            name_universe=frozenset(universe or []),
            meta={"taken_at": "2026-08-04T00:00:00+00:00"},
        )

    def test_newly_blocked_is_detected(self):
        before = Verdict("PSA", 9.0, True, "")
        after = Verdict("PSA", 9.0, False, "排除字 サッカー")
        diff = diff_verdicts(self._baseline({"t": before}), {"t": after})
        assert [c.title for c in diff.newly_blocked] == ["t"]
        assert diff.newly_allowed == () and diff.n_changed == 1

    def test_newly_allowed_and_grade_change_are_separated(self):
        base = self._baseline({
            "a": Verdict("PSA", 9.0, False, "排除字 X"),
            "b": Verdict("UNKNOWN", None, False, "未偵測到鑑定機構"),
        })
        cur = {
            "a": Verdict("PSA", 9.0, True, ""),
            "b": Verdict("ARS", 10.0, False, "未偵測到鑑定機構"),
        }
        diff = diff_verdicts(base, cur)
        assert [c.title for c in diff.newly_allowed] == ["a"]
        assert [c.title for c in diff.grade_changed] == ["b"]
        assert diff.newly_blocked == ()

    def test_reason_only_change_is_not_a_behaviour_change(self):
        base = self._baseline({"t": Verdict("UNKNOWN", None, False, "無 1998-2004 年代證據")})
        diff = diff_verdicts(base, {"t": Verdict("UNKNOWN", None, False, "排除字 ポケモン")})
        assert diff.n_changed == 0

    def test_corpus_rotation_is_not_a_verdict_change(self):
        """快取輪替掉的標題不可以被算成「判定改變」（同源同基準，第三節）。"""
        base = self._baseline({"gone": Verdict("PSA", 9.0, True, "")})
        diff = diff_verdicts(base, {"fresh": Verdict("PSA", 9.0, True, "")})
        assert diff.n_changed == 0
        assert diff.only_in_baseline == ("gone",) and diff.only_in_current == ("fresh",)
        assert diff.n_compared == 0

    def test_name_newly_excluded_is_flagged_by_era(self):
        base = self._baseline({}, hits={}, universe=["ブラッド・サッカー", "メタボ・サッカー"])
        hits = {"ブラッド・サッカー": "サッカー", "メタボ・サッカー": "サッカー"}
        diff = diff_verdicts(
            base, {}, current_name_hits=hits, era_names=frozenset({"ブラッド・サッカー"})
        )
        assert diff.n_era_names_killed == 1
        # 年代內排最前面：它是誤殺的鐵證，不該被年代外的雜訊淹掉
        assert [c.name for c in diff.names_newly_excluded][0] == "ブラッド・サッカー"

    def test_names_not_in_baseline_universe_are_not_compared(self):
        """基準沒掃過的卡名，不可以被讀成「當時沒命中」。"""
        base = self._baseline({}, hits={}, universe=["A"])
        diff = diff_verdicts(base, {}, current_name_hits={"A": "", "B": "新詞"})
        assert diff.n_names_compared == 1 and diff.names_newly_excluded == ()


class TestKnownMisfireIsCaught:
    """這支工具唯一真正的驗收：抓不抓得到我們**已知**的誤殺。

    `config/watchlist.yaml` 記載 `サッカー` 會命中 5 個真實卡名
    （`ブラッド・サッカー` 等）。工具必須列得出它們——而且注意：
    今天的在架／成交標題語料裡一筆真卡都沒有，所以只掃標題是抓不到的。
    """

    def test_sakka_would_be_caught(self, cfg):
        era, other = load_card_names(cfg.root / "data/cards_1998_2004.json")
        names = tuple(sorted(era | other))
        before = name_hits(names, cfg.watchlist)
        assert not [n for n, w in before.items() if w and "サッカー" in n], (
            "前提破了：現行規則已經命中サッカー卡名"
        )

        poisoned = json.loads(json.dumps(cfg.watchlist))
        poisoned["exclude_keywords"].append("サッカー")
        after = name_hits(names, poisoned)

        base = Baseline(
            verdicts={}, name_hits={n: w for n, w in before.items() if w},
            name_universe=frozenset(names), meta={},
        )
        diff = diff_verdicts(
            base, {}, current_name_hits=after, era_names=frozenset(era)
        )
        killed = {c.name for c in diff.names_newly_excluded}
        assert "ブラッド・サッカー" in killed
        assert len(killed) == 5, killed
        assert diff.n_era_names_killed >= 1

    def test_no_era_card_name_is_hit_by_an_exclude_keyword(self, cfg):
        """不變式：**1998-2004 的真卡名，一個都不准被排除字命中。**

        年代內卡名被排除字打到＝那張卡永遠不會出現在推播裡，而且是靜默的
        （見 CLAUDE.md 第一節）。所以這裡的正確值只有一個：空集合。

        本測試由一個真實誤殺催生（2026-08-05）：排除字 `プリズマ` 是真卡名
        `プリズマン` 的子字串，殺掉了語料裡的「PSA9 遊戯王 プリズマン 初期
        ノーマル Vol.6」——PSA9 初期 Vol.6，正中目標輪廓。`ygo-sniper
        corpus-diff` 抓到後移除該詞（`プリズマティック` 已涵蓋其用途）。

        這條會擋住整**類**錯誤，不只那一個詞：任何新增的排除字只要是某張
        真卡名的子字串就會在這裡亮紅燈，不必等到有人發現「最近沒好貨」。
        """
        era, _ = load_card_names(cfg.root / "data/cards_1998_2004.json")
        hits = {n: w for n, w in name_hits(sorted(era), cfg.watchlist).items() if w}
        assert hits == {}, (
            f"排除字誤殺了 {len(hits)} 個年代內真卡名：{hits}\n"
            "每一個都代表那張卡永遠不會被推播。請縮小該排除字的範圍"
            "（通常是改用更長的完整詞），並在 tests/test_exclude_other_tcg.py "
            "的紅燈清單留下反例。"
        )


class TestBaselineRoundTrip:
    def test_baseline_survives_save_and_load(self, tmp_path, watchlist):
        from ygo_sniper.corpus import load_baseline

        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "ok.html").write_text(_YAHOO_SEARCH, encoding="utf-8")
        db = tmp_path / "sniper.db"
        import sqlite3

        con = sqlite3.connect(db)
        for table in ("signals", "comps", "listing_obs"):
            con.execute(f"create table {table} (title text)")
        con.commit()
        con.close()
        master = tmp_path / "cards.json"
        master.write_text(json.dumps({"cards": [{"name_ja": "ブラッド・サッカー"}]}), encoding="utf-8")

        corpus = load_corpus(db_path=db, cache_dir=cache, master_path=master)
        verdicts = {t: judge(t, watchlist) for t in corpus.titles}
        hits = name_hits(corpus.card_names, watchlist)

        path = tmp_path / "baseline.json"
        save_baseline(path, corpus, verdicts, hits)
        loaded = load_baseline(path)

        assert loaded.verdicts == verdicts
        assert loaded.name_universe == frozenset(corpus.card_names)
        assert diff_verdicts(loaded, verdicts, current_name_hits=hits).n_changed == 0

    def test_stale_version_is_rejected(self, tmp_path):
        from ygo_sniper.corpus import load_baseline

        path = tmp_path / "old.json"
        path.write_text(json.dumps({"version": 0, "verdicts": {}}), encoding="utf-8")
        with pytest.raises(CorpusError):
            load_baseline(path)
