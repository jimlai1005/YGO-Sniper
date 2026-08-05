"""`sold_at` 是入庫時間這件事，必須一路露到使用者眼前（2026-08-06）。

實測（`data/sniper.db`，2026-08-06）：

    site            comps 筆數   sold_at 是入庫時間
    buyee_mercari      1046          1046  （100%）
    buyee_paypay        563            77
    buyee_yahoo         957             0

Buyee 系的已售出頁不含日期，`comps.py` 於是退回 `now()` 並標
`sold_at_is_ingest=1`。旗標本身早就存在，破口在**它沒有跟著資料走到畫面上**：
dashboard 的「成交日」欄直接印 `sold_at`，於是入庫日被當成成交日給使用者看。
一個假的事實被講得跟真的一樣，而且沒有任何痕跡——CLAUDE.md 第五節那一類。

這一組測試守的是那條「一路露出去」的鏈，不是計算：
    comps 列 → `Comparable.sold_at_is_ingest` → API（`asdict`）→ 畫面標記。
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ygo_sniper.appraise import collect_comparables

ROOT = Path(__file__).resolve().parents[1]


def _row(price, *, card="はにわ", sold="2026-07-01", site="buyee_mercari", ingest=0):
    return {
        "title": f"{card} ultra PSA9",
        "price_twd": price,
        "rarity": "ultra",
        "grade": 9.0,
        "card_name": card,
        "url": f"https://buyee.jp/mercari/item/m{int(price)}",
        "sold_at": sold,
        "era_evidence": "jp_kw:初期",
        "site": site,
        "sold_at_is_ingest": ingest,
    }


def test_the_ingest_time_flag_follows_the_row_to_the_report():
    """Mercari 那一筆要帶著「這不是成交日」的旗標，Yahoo 那一筆不帶。

    兩筆一起測是刻意的：只測 True 的那一半，把旗標寫死成 `True` 也會過。
    """
    rows = [
        _row(100, site="buyee_mercari", ingest=1),
        _row(200, site="buyee_yahoo", ingest=0),
    ]
    shown, _stats = collect_comparables(
        rows, None, card_name="はにわ", rarity="ultra", grade=9.0
    )
    by_site = {c.site: c for c in shown}
    assert by_site["buyee_mercari"].sold_at_is_ingest is True
    assert by_site["buyee_yahoo"].sold_at_is_ingest is False


def test_the_flag_survives_serialisation_to_the_api():
    """報告是用 `asdict` 送出去的——欄位掉了的話畫面就標不出來，而且沒有錯誤。"""
    shown, _ = collect_comparables(
        [_row(100, ingest=1)], None, card_name="はにわ", rarity="ultra", grade=9.0
    )
    payload = asdict(shown[0])
    assert payload["sold_at_is_ingest"] is True
    assert payload["sold_at"] == "2026-07-01"      # 值本身不動，只是多一個註記


def test_a_row_without_the_column_defaults_to_trusting_the_timestamp():
    """舊列（欄位還沒回填）不要憑空變成「假時間」——`None` 不是 1。

    回填是冪等的，但測試不該假設它跑過。預設值錯的方向要選「不亂標」，
    因為亂標會讓使用者對真的警告麻痺。
    """
    row = _row(100)
    del row["sold_at_is_ingest"]
    shown, _ = collect_comparables(
        [row], None, card_name="はにわ", rarity="ultra", grade=9.0
    )
    assert shown[0].sold_at_is_ingest is False


def test_the_dashboard_marks_the_ingest_dates():
    """畫面必須真的讀那個旗標。

    這是本檔唯一擋得住「資料備好了但沒人用」的一條——旗標送到前端卻沒有被
    讀，外顯與完全沒做一模一樣（CLAUDE.md 第五節：靜默失敗）。
    """
    html = (ROOT / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert "sold_at_is_ingest" in html, "dashboard 沒有讀入庫時間旗標"
    assert "入庫日" in html, "dashboard 沒有把入庫日標示給使用者看"
