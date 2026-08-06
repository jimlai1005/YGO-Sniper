"""判定層：「這筆標的還在不在架上」。

兩種判準的語意不同，測試也分開寫——這不是形式主義：
`end_time` 已過是確定事實，`disappeared_at` 是推論（2026-08-06 實測誤判率
56.5%）。任何把兩者合成同一個布林值的改動都應該讓這裡紅掉。
"""

from datetime import UTC, datetime, timedelta

from ygo_sniper.expiry import ExpiryStatus, expiry_status

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)


def _row(**kw) -> dict:
    """一列 signals（已 JOIN listing_obs）。預設是「還在架上的一口價」。"""
    row = {
        "key": "buyee_yahoo:x1",
        "site": "buyee_yahoo",
        "state": "watching",
        "payload": "{}",
        "obs_disappeared_at": None,
        "obs_window_exit_at": None,
        "obs_revived_count": 0,
    }
    row.update(kw)
    return row


def _payload_with_end(end_time: str) -> str:
    import json

    return json.dumps({"listing": {"end_time": end_time}})


def test_ended_auction_is_certain():
    row = _row(payload=_payload_with_end("2026-08-06T11:00:00+00:00"))
    st = expiry_status(row, now=NOW)
    assert st.kind == "ended"
    assert st.confidence == "certain"
    assert st.detail == "已結標"


def test_future_end_time_is_live():
    row = _row(payload=_payload_with_end("2026-08-07T11:00:00+00:00"))
    assert expiry_status(row, now=NOW).kind == "live"


def test_disappeared_is_gone_not_ended():
    """消失是推論，不能冒充結標。"""
    row = _row(obs_disappeared_at=(NOW - timedelta(hours=6)).isoformat())
    st = expiry_status(row, now=NOW)
    assert st.kind == "gone"
    assert "6 小時" in st.detail


def test_ended_beats_gone():
    """兩者同時成立時，確定事實壓過推論。"""
    row = _row(
        payload=_payload_with_end("2026-08-06T11:00:00+00:00"),
        obs_disappeared_at=(NOW - timedelta(hours=6)).isoformat(),
    )
    assert expiry_status(row, now=NOW).kind == "ended"


def test_window_exit_alone_does_not_expire():
    """被擠出觀測窗是右設限，沒有結論——不能當成離場。"""
    row = _row(obs_window_exit_at=(NOW - timedelta(days=3)).isoformat())
    assert expiry_status(row, now=NOW).kind == "live"


def test_offer_sent_gets_different_wording():
    """已出價的標的消失，很可能代表你標下了，不能寫成「疑似已售出」。"""
    row = _row(state="offer_sent", obs_disappeared_at=(NOW - timedelta(hours=2)).isoformat())
    assert "標下" in expiry_status(row, now=NOW).detail


def test_offer_sent_ended_auction_also_asks_whether_you_won():
    """已出價的東西**幾乎一定是競標**，而競標的正常結局是走 `ended` 這一支。

    提醒只寫在 `gone` 那一支的話，最需要它的那一類反而看不到中性的「已結標」，
    而這是唯一「清錯了無法自癒」的類別——真的標下的標的不會再出現在搜尋結果，
    自動還原永遠救不回來（設計文件第 6 節第 6 點）。
    """
    row = _row(state="offer_sent", payload=_payload_with_end("2026-08-06T11:00:00+00:00"))
    st = expiry_status(row, now=NOW)
    assert st.kind == "ended"           # 判定不變：結標仍是確定事實
    assert st.confidence == "certain"
    assert "標下" in st.detail


def test_live_status_has_empty_detail():
    st = expiry_status(_row(), now=NOW)
    assert st == ExpiryStatus(kind="live", confidence="certain", detail="", note=None)


def test_naive_timestamp_is_treated_as_utc():
    """庫裡的時間戳都帶 +00:00；naive 當本地時間會產生 8 小時靜默偏移。"""
    row = _row(payload=_payload_with_end("2026-08-06T11:00:00"))
    assert expiry_status(row, now=NOW).kind == "ended"


def test_broken_payload_does_not_crash():
    """payload 壞掉不能讓整張卡片消失——回 live，讓它繼續顯示。"""
    assert expiry_status(_row(payload="not json"), now=NOW).kind == "live"
    assert expiry_status(_row(payload=None), now=NOW).kind == "live"


def test_confidence_table_is_consulted():
    row = _row(site="buyee_yahoo", obs_disappeared_at=(NOW - timedelta(hours=2)).isoformat())
    st = expiry_status(row, now=NOW, gone_confidence={"buyee_yahoo": "medium", "_default": "low"})
    assert st.confidence == "medium"
    assert st.note is None          # medium 不加警語


def test_unknown_source_falls_back_to_default():
    row = _row(site="brand_new_site", obs_disappeared_at=(NOW - timedelta(hours=2)).isoformat())
    st = expiry_status(row, now=NOW, gone_confidence={"buyee_yahoo": "medium", "_default": "low"})
    assert st.confidence == "low"
    assert "復活率偏高" in st.note


def test_gone_confidence_from_config_reads_scan_block():
    from ygo_sniper.expiry import gone_confidence_from_config

    class _Cfg:
        scan = {"gone_confidence": {"buyee_yahoo": "medium", "_default": "low"}}

    assert gone_confidence_from_config(_Cfg())["buyee_yahoo"] == "medium"


def test_gone_confidence_defaults_when_config_missing():
    """設定沒寫時不能炸，也不能假裝有信心——退回全部 low。"""
    from ygo_sniper.expiry import gone_confidence_from_config

    class _Cfg:
        scan = {}

    assert gone_confidence_from_config(_Cfg()) == {"_default": "low"}
