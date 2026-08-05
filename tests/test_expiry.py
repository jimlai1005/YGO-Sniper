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
