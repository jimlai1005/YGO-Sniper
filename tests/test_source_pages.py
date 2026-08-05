"""逐來源的翻頁數覆寫（`sources.<name>.max_pages`）。

背景：`fetch.max_pages_per_query` 是全域的，但翻頁的邊際效益取決於單頁容量——
Yahoo 一頁 50 筆、Mercari 100 筆，一小時的新增量塞不滿第一頁；PayPay 一頁只有
40 筆而在架量大，只掃 1 頁等於系統性地看不見它的新貨。

⚠️ 這一組測試同時是紅線的守衛：加重的是**抓取覆蓋**，不是評分。
`test_override_does_not_touch_scoring_config` 釘住「調頁數不會順手動到 scoring」。
"""

from __future__ import annotations

import dataclasses

import pytest
from conftest import FakeFx, make_listing

import ygo_sniper.pipeline as pipeline_mod
from ygo_sniper.domain import Site
from ygo_sniper.pipeline import Pipeline
from ygo_sniper.sources.health import SearchResult


# ---------------------------------------------------------------------------
# 1. Config.max_pages_for 的解析
# ---------------------------------------------------------------------------
def _cfg_with(cfg, sources: dict, global_pages: int = 1):
    return dataclasses.replace(
        cfg, sources=sources, fetch={**cfg.fetch, "max_pages_per_query": global_pages}
    )


def test_falls_back_to_global_when_not_overridden(cfg):
    c = _cfg_with(cfg, {"yahoo_direct": {"canary_keyword": "遊戯王"}}, global_pages=1)
    assert c.max_pages_for("yahoo_direct") == 1
    assert c.max_pages_for("完全沒設定過的來源") == 1


def test_source_override_wins(cfg):
    c = _cfg_with(cfg, {"buyee_paypay": {"max_pages": 3}}, global_pages=1)
    assert c.max_pages_for("buyee_paypay") == 3
    assert c.max_pages_for("yahoo_direct") == 1


@pytest.mark.parametrize("bad", [0, -2, "三頁", [], {"a": 1}])
def test_illegal_override_falls_back_loudly(cfg, capsys, bad):
    """非法值退回全域值**並印警告**——靜默當成 1 的話，設定打錯字會表現成
    『PayPay 好像沒什麼貨』，而你永遠不會去看設定檔。"""
    c = _cfg_with(cfg, {"buyee_paypay": {"max_pages": bad}}, global_pages=2)
    assert c.max_pages_for("buyee_paypay") == 2
    assert "max_pages" in capsys.readouterr().out


def test_shipped_config_has_no_stale_page_overrides(cfg):
    """實際出貨的設定檔：目前沒有任何來源需要覆寫頁數。

    2026-08-02 之前 `buyee_paypay` 覆寫成 3 頁，理由是它一頁只有 40 筆、
    裝不下一小時的新增量。改成原站直抓（`paypay_direct`）之後一頁 100 筆，
    那個理由消失了——**覆寫值必須跟著它的理由一起消失**，否則設定檔會留下
    一串沒有人記得為什麼存在的數字，而每一個都在多打請求。
    """
    global_pages = int(cfg.fetch["max_pages_per_query"])
    for name in cfg.sources:
        assert cfg.max_pages_for(name) == global_pages, (
            f"sources.{name}.max_pages 覆寫了全域值——"
            "覆寫要有理由（單頁容量），改管道時記得重算"
        )


#: 日本站的三條發現管道（eBay 是英文關鍵字、不同站，不在此列）。
_JP_SOURCES = {"yahoo_direct", "buyee_mercari", "paypay_direct"}
#: 以「遊戯王」開頭的通用查詢：它們是覆蓋面最寬的那幾條，三個平台都要跑。
_BROAD_PREFIX = "遊戯王 "


def test_query_sources_are_real_registry_names(cfg):
    """每個 query 的 sources 都必須是 registry 認得的名字，而且不可為空。

    這條守的是**打錯字**：pipeline 對不認得的來源名只印一行 warn 就跳過，
    症狀是「那個平台好像今天沒貨」，而不是任何錯誤。
    （原本這條測試寫成「paypay 必須出現在每一條日文查詢」，2026-08-03 放寬——
    見 `test_narrow_queries_may_target_fewer_sources`——但「名字必須真實存在」
    這一半是打錯字的唯一防線，只能加嚴不能拿掉。）
    """
    known = _JP_SOURCES | {"ebay", "yahoo_closed", "ruten"}
    for q in cfg.watchlist["queries"]:
        srcs = q.get("sources") or []
        assert srcs, f"{q['name']} 沒有任何 sources，等於設了不掃"
        for name in srcs:
            assert name in known, f"{q['name']} 的來源 {name!r} 不是已知管道（打錯字？）"


def test_broad_japanese_queries_still_cover_all_three_platforms(cfg):
    """寬查詢（「遊戯王 …」那幾條）三個日本平台都要跑，一個都不能少。

    它們是主力覆蓋面；少一個平台的後果是那個平台的新貨系統性看不見，
    而且外顯症狀是「今天沒推薦」——與市場真的很冷長得一模一樣。
    """
    for q in cfg.watchlist["queries"]:
        if not q["keyword"].startswith(_BROAD_PREFIX):
            continue
        missing = _JP_SOURCES - set(q["sources"])
        assert not missing, f"{q['name']} 少了 {sorted(missing)}"


def test_narrow_queries_may_target_fewer_sources(cfg):
    """窄查詢（2026-08-03 新增那三條）可以只掛有實測貢獻的來源。

    背景：使用者買到兩張我們從沒掃到的卡（`【PSA9】カエルスライム 初期
    プレミアムパック`），根因是五條查詢全部以「遊戯王」開頭，而 Mercari／
    PayPay 是 AND 語意——標題沒寫「遊戯王」的標的從來沒被搜到過。修法是加
    查詢，而請求預算是硬約束（每輪每查詢每來源一個請求，排程一天 15 次）。
    所以新查詢的 sources 是**按實測淨增量**逐條指派的，不是照抄三件套：
    實測 0 貢獻的來源掛上去只是每天多打幾百個請求。

    這條測試不檢查「掛了哪幾個」（那是會隨市場變的判斷），只釘住兩件事：
    每個日本管道至少還在某條查詢裡活著（沒有整個平台被悄悄拔掉），
    而且每條查詢的來源都真實存在（上一條測試）。
    """
    used = {s for q in cfg.watchlist["queries"] for s in q["sources"]}
    for name in _JP_SOURCES:
        assert name in used, f"{name} 已經不在任何查詢裡——整個平台被拔掉了"


def test_titles_without_the_yugioh_token_are_still_reachable(cfg):
    """**事故回歸測試**：標題沒有「遊戯王」三個字的標的必須有查詢搆得到。

    這兩個標題是使用者 2026-08 自己在站上找到並買下的真實商品，dashboard
    從來沒出現過它們。解析端沒問題（`parse_card` 給 `jp_kw:初期`＋PSA9＋
    is_candidate=True），是**搜尋詞**的問題：每一條查詢都要求標題含「遊戯王」。

    這裡用 AND 語意模擬（Mercari／PayPay 的搜尋就是 AND；Yahoo 更寬鬆，
    所以這個模擬是保守的下界）：至少要有一條日本站查詢，它的每個關鍵字
    都出現在標題裡。哪天有人把這幾條窄查詢刪掉，這裡會紅。
    """
    missed = [
        "【PSA9】カエルスライム 初期 プレミアムパック",
        "【PSA9】大砲だるま 初期 プレミアムパック",
    ]
    for title in missed:
        hits = [
            q["name"]
            for q in cfg.watchlist["queries"]
            if set(q["sources"]) & _JP_SOURCES
            and all(tok in title for tok in q["keyword"].split())
        ]
        assert hits, f"沒有任何查詢搆得到 {title!r}（AND 語意）"


# ---------------------------------------------------------------------------
# 2. pipeline 真的把頁數傳下去
# ---------------------------------------------------------------------------
class _RecordingDetailed:
    """新介面（search_detailed）的假來源，記下每次拿到的 pages。"""

    supports_sold = False

    def __init__(self, name, site=Site.BUYEE_PAYPAY):
        self.name, self.site = name, site
        self.pages_seen: list[int] = []
        self.max_price_seen: list[float | None] = []

    def search_detailed(self, keyword, *, max_price=None, pages=None):
        self.pages_seen.append(pages)
        self.max_price_seen.append(max_price)
        return SearchResult(source=self.name, site=self.site.value, query=keyword)


class _RecordingLegacy:
    """舊介面（search）的假來源，同樣記下 pages。"""

    supports_sold = False

    def __init__(self, name, site=Site.BUYEE_MERCARI):
        self.name, self.site = name, site
        self.pages_seen: list[int] = []

    def search(self, keyword, **kw):
        self.pages_seen.append(kw.get("pages"))
        return [make_listing(price=1500, site=self.site, external_id="x1")]


def _pipeline(monkeypatch, tmp_path, cfg, registry, queries, sources_cfg, global_pages=1):
    test_cfg = dataclasses.replace(
        cfg,
        root=tmp_path,
        watchlist={**cfg.watchlist, "queries": queries},
        sources=sources_cfg,
        fetch={**cfg.fetch, "max_pages_per_query": global_pages},
    )
    monkeypatch.setattr(pipeline_mod, "build_sources", lambda _cfg, _f=None: registry)
    monkeypatch.setattr(pipeline_mod, "FxRates", lambda _cfg: FakeFx())
    return Pipeline(test_cfg)


def test_pipeline_passes_per_source_pages(monkeypatch, tmp_path, cfg):
    deep = _RecordingDetailed("deep_src")
    shallow = _RecordingLegacy("shallow_src")
    pipe = _pipeline(
        monkeypatch, tmp_path, cfg,
        {"deep_src": deep, "shallow_src": shallow},
        [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["deep_src", "shallow_src"]}],
        sources_cfg={"deep_src": {"max_pages": 3}},   # shallow 沒設 → 用全域
        global_pages=1,
    )
    try:
        pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    assert deep.pages_seen == [3], "來源層覆寫沒有傳到抓取層"
    assert shallow.pages_seen == [1], "沒覆寫的來源不該跟著變深"


def test_canary_always_uses_one_page(monkeypatch, tmp_path, cfg):
    """canary 問的是『這條管道還活著嗎』，翻頁對這個問題沒有貢獻——
    但每輪每來源都會跑，跟著 max_pages 一起漲就是白付三倍請求。"""
    deep = _RecordingDetailed("deep_src")
    pipe = _pipeline(
        monkeypatch, tmp_path, cfg,
        {"deep_src": deep},
        [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["deep_src"]}],
        sources_cfg={"deep_src": {"max_pages": 3, "canary_keyword": "遊戯王",
                                  "canary_min_results": 10}},
        global_pages=1,
    )
    try:
        pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    # 第一次是正常查詢（3 頁），第二次是 canary（固定 1 頁）
    assert deep.pages_seen == [3, 1]


def test_canary_never_carries_a_price_ceiling(monkeypatch, tmp_path, cfg):
    """🚩 canary 不帶 max_price。

    價格上限（`_price_ceiling_jpy`，由「到手成本 = 鑑定費」反推）是**商業過濾**，
    canary 問的是「這條管道還活著嗎」。把商業過濾套在健康探針上，回傳筆數就會
    跟著平台當下的價格分布跳動——答案只會更不穩定，而不穩定的健康指標就是
    假警報的來源（同 2026-08-01 parsed_count 那次事故的根因）。
    正常查詢照舊要帶上限（省流量），兩者必須分得開。
    """
    deep = _RecordingDetailed("deep_src")
    pipe = _pipeline(
        monkeypatch, tmp_path, cfg,
        {"deep_src": deep},
        [{"name": "t", "keyword": "遊戯王 PSA", "sources": ["deep_src"]}],
        sources_cfg={"deep_src": {"canary_keyword": "遊戯王", "canary_min_results": 10}},
        global_pages=1,
    )
    try:
        pipe.scan(skip_comps=True, dry_run=True)
    finally:
        pipe.close()

    normal, canary = deep.max_price_seen
    assert normal is not None and normal > 0, "正常查詢仍要帶價格上限（省流量）"
    assert canary is None, "canary 不該帶 max_price"


def test_override_does_not_touch_scoring_config(cfg):
    """🚩 紅線：多掃幾頁 ≠ 評分加權。scoring 區塊裡不該出現任何來源名字。"""
    blob = repr(cfg.scoring) + repr(cfg.valuation.get("venue_premium_prior", {}))
    assert "max_pages" not in blob
    # venue 先驗可以有平台名（那是市場結構係數），但不可以來自翻頁設定
    assert cfg.valuation["venue_premium_prior"]["buyee_paypay"] == 2.0
