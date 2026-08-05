"""推播訊息格式。

這裡釘的是「你在手機上看到什麼」——格式退化不會讓任何東西壞掉，
所以沒有測試的話會無聲地退化（少了網址、少了行情區間），
而你要等到某天想點進去看標的時才發現。
"""

from __future__ import annotations

import json

from ygo_sniper.notify import format_signal

DASH = "http://127.0.0.1:8321"


def _row(**over) -> dict:
    """一筆典型的 signals 表列（欄位名與 store.upsert_signal 寫入的一致）。"""
    row = {
        "title": "【大人気/ARS9】ブラックマジシャンガール 初期 ウルトラ P4-01",
        "url": "https://buyee.jp/mercari/item/m48967074463",
        "landed_twd": 2055.0,
        "price_native": 8399.0,
        "currency": "JPY",
        "route": "buyee_consolidated",
        "comps_n": 5,
        "comps_median": 4669.0,
        "discount_pct": 0.56,
        "score": 34.0,
        "flags": json.dumps(["discount"]),
        "payload": json.dumps(
            {"comps": {"n": 5, "median_twd": 4669.0, "p25_twd": 1283.0, "p75_twd": 6090.0}}
        ),
    }
    row.update(over)
    return row


def test_listing_link_is_present_as_anchor():
    """標的連結用「看標的」錨點——訊息要短，裸網址會把版面撐爛。

    這條釘的是連結沒有消失，不是它長什麼樣子。
    """
    msg = format_signal(_row(), DASH)
    assert '<a href="https://buyee.jp/mercari/item/m48967074463">看標的</a>' in msg
    assert f'<a href="{DASH}">開 dashboard</a>' in msg


def test_shows_p25_p75_range_when_comps_exist():
    """中位數只是一個點；要判斷「多少錢算合理」得看區間。"""
    msg = format_signal(_row(), DASH)
    assert "合理區間 NT$1,283–6,090" in msg
    assert "行情 NT$4,669（n=5）" in msg


def test_no_range_line_when_payload_lacks_percentiles():
    """舊資料的 payload 沒有 p25/p75——降級顯示，不可以炸。"""
    msg = format_signal(_row(payload=json.dumps({"comps": {"n": 5}})), DASH)
    assert "合理區間" not in msg
    assert "行情 NT$4,669" in msg


def test_broken_payload_does_not_crash():
    """payload 壞掉（截斷、非 JSON）時照樣要送得出訊息。

    推播是通知管路，不能因為一個欄位解析失敗就整批發不出去。
    """
    msg = format_signal(_row(payload="{不是 JSON"), DASH)
    assert "合理區間" not in msg
    assert "到手 <b>NT$2,055</b>" in msg


def test_no_comps_says_so_explicitly():
    """沒有行情樣本時要講清楚判斷依據，不要讓人誤以為比過價了。"""
    msg = format_signal(_row(comps_median=None, comps_n=0, payload="{}"), DASH)
    assert "無足夠樣本" in msg
    assert "只根據到手成本" in msg
