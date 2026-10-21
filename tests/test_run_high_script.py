"""高價帶 wrapper（scripts/run_high.sh）的端到端測試 ＋ 三支 wrapper 的靜態禁令。

事故（2026-10-21 部署驗收抓到，docs/plans/schedule-pin-taipei.md Task 7）：
`run_high.sh` 的 `exit=$STATUS）` —— 變數後面緊接全形「）」，bash 在多位元組 locale
下把它吃進識別字，`set -u` 讓腳本死在「結束」行：沒有結束行、沒有
`last_run_exit_high`、沒有 log 輪替，而 python exit 0 所以連失敗通知都不會送。
高價帶自 2026-08-22 上線以來每一輪都這樣，因為 `run_high.sh` 從來沒有任何沙盒測試
（既有的 `test_run_daily_script.py` 只蓋 `run_daily.sh`），`bash -n` 又抓不到
`set -u` 的執行期錯誤。這份測試補兩道防線：

1. 每支會跑掃描的 wrapper 都要有一條「stub 成功路徑跑到結束行＋ledger」的沙盒測試；
2. 靜態禁令：`$VAR` 後面不得緊接非 ASCII 字元（一律寫 `${VAR}`）。

沙盒做法與 `test_run_daily_script.py` 完全相同（偽造 `$HOME`，真的腳本一行不改），
直接重用它的 `Sandbox`，只換掉腳本路徑、log／鎖／帳本檔名。

驗證「對修正前要紅」：把舊版放到任意路徑，用環境變數指過去——
    git show 917c74a:scripts/run_high.sh > /path/to/run_high_old.sh
    YGO_RUN_HIGH_SCRIPT=/path/to/run_high_old.sh pytest tests/test_run_high_script.py
"""

import os
import re
from pathlib import Path

import pytest

from test_run_daily_script import (
    _STUB_EXIT_0,
    _STUB_EXIT_7,
    _SUBPROCESS_TIMEOUT,
    REPO_ROOT,
    Sandbox,
)
import subprocess

_SCRIPTS = REPO_ROOT / "scripts"
# 只供「對舊版要紅」的驗收使用；平常不設，測的就是 repo 裡的真檔。
RUN_HIGH_SCRIPT = Path(os.environ.get("YGO_RUN_HIGH_SCRIPT") or _SCRIPTS / "run_high.sh")

# bash 在多位元組 locale 下把緊接在 `$VAR` 後面的非 ASCII 字元當成識別字的一部分。
_VAR_THEN_NON_ASCII = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7F]")


def find_var_followed_by_non_ascii(text: str) -> list[tuple[int, str]]:
    """回傳 [(行號, 該行)]：`$VAR` 後面緊接非 ASCII 字元的所有位置。"""
    return [
        (lineno, line)
        for lineno, line in enumerate(text.splitlines(), start=1)
        if _VAR_THEN_NON_ASCII.search(line)
    ]


# ---------------------------------------------------------------------------
# 靜態禁令
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["run_daily.sh", "run_high.sh", "run_dashboard.sh"])
def test_wrapper_has_no_bare_var_followed_by_non_ascii(name):
    path = RUN_HIGH_SCRIPT if name == "run_high.sh" else _SCRIPTS / name
    hits = find_var_followed_by_non_ascii(path.read_text())
    assert not hits, (
        f"{path.name} 有 `$VAR` 後面緊接非 ASCII 字元（bash 會把它吃進識別字，"
        "`set -u` 下直接 unbound variable，腳本死在那一行）；一律改寫成 `${VAR}`。"
        "見 run_daily.sh 內『變數一律加大括號』那段註解。位置："
        + "；".join(f"{path.name}:{n}: {ln.strip()}" for n, ln in hits)
    )


def test_checker_catches_the_original_incident_line_and_accepts_braced_form():
    """檢查器本身的回歸：事故原句必須被抓、加大括號的寫法必須放行。"""
    bad = 'echo "結束（高價帶，exit=$STATUS） =====" >> "$LOG_FILE"\n'
    good = 'echo "結束（高價帶，exit=${STATUS}） =====" >> "$LOG_FILE"\n'
    assert [n for n, _ in find_var_followed_by_non_ascii(bad)] == [1]
    assert find_var_followed_by_non_ascii(good) == []
    # 後面是 ASCII 標點、或換行結尾的普通用法不能誤報
    assert find_var_followed_by_non_ascii('x="$HOME/a"; echo "$x," "$y"\n') == []


# ---------------------------------------------------------------------------
# run_high.sh 沙盒端到端
# ---------------------------------------------------------------------------


class HighSandbox(Sandbox):
    def __init__(self, tmp_path: Path):
        super().__init__(tmp_path)
        self.lock_dir = self.project_dir / "data" / "run_high.lock"
        self.last_run_file = self.project_dir / "data" / "last_run_exit_high"

    def env(self, **extra: str) -> dict:
        # 事故只在多位元組 locale 下發作；顯式指定，測試結果才不隨執行者的 shell 而變。
        return super().env(LANG="C.UTF-8", LC_ALL="C.UTF-8", **extra)

    def run(self, **env_extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(RUN_HIGH_SCRIPT)],
            env=self.env(**env_extra),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
        )

    def log_text(self) -> str:
        logs = list(self.log_dir.glob("high-*.log"))
        assert len(logs) == 1, f"預期剛好一份 high log，實際：{logs}"
        return logs[0].read_text()


@pytest.fixture
def high(tmp_path) -> HighSandbox:
    return HighSandbox(tmp_path)


def test_run_high_success_path_reaches_end_line_and_writes_ledger(high):
    """這條就是事故的回歸：stub python 成功，必須一路跑到『結束』行與 ledger。
    舊版死在 `exit=$STATUS）`：stderr 有 `unbound variable`、log 沒有結束行、
    ledger 不存在。"""
    high.set_ygo_sniper_stub(_STUB_EXIT_0)

    result = high.run()

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert high.marker_file.exists(), "ygo-sniper stub 沒有被真的執行到"
    log = high.log_text()
    assert "開始（高價帶）" in log
    assert "結束（高價帶，exit=0）" in log, f"沒有結束行：\n{log}"
    assert not high.lock_dir.exists(), "成功結束後鎖應該被 trap 釋放掉"
    high.assert_no_network_call()

    ledger = high.last_run_ledger()
    assert ledger["exit"] == 0
    assert ledger["notify_attempted"] is False
    # 同一支腳本不得寫到低價帶的帳本（兩條掃描的排程監督互相覆寫的事故形狀）
    assert not (high.project_dir / "data" / "last_run_exit").exists()


def test_run_high_failure_path_records_exit_code_in_end_line_and_ledger(high):
    high.set_ygo_sniper_stub(_STUB_EXIT_7)

    result = high.run()

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 7, result.stderr
    log = high.log_text()
    assert "高價帶掃描失敗，exit=7" in log
    assert "結束（高價帶，exit=7）" in log, f"沒有結束行：\n{log}"
    assert not high.lock_dir.exists()
    high.assert_no_network_call()

    ledger = high.last_run_ledger()
    assert ledger["exit"] == 7
    assert ledger["notify_attempted"] is False


def test_run_high_notify_failure_line_survives_fullwidth_punctuation(high):
    """失敗通知的 curl 自己失敗時，log 那行 `…http=${NOTIFY_HTTP}）——` 後面接全形字，
    是同一顆地雷的第二個藏身處（run_daily.sh 也有過）。"""
    high.set_ygo_sniper_stub(_STUB_EXIT_7)
    high.write_fake_env()
    high.set_curl_stub(
        f'#!/bin/bash\ntouch "{high.curl_marker_file}"\nprintf "000"\nexit 6\n'
    )

    result = high.run()

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 7, result.stderr
    log = high.log_text()
    assert "失敗通知本身也沒送成功（curl exit=6, http=000）" in log
    ledger = high.last_run_ledger()
    assert ledger["notify_attempted"] is True
    assert ledger["notify_curl_exit"] == 6


def test_run_high_slot_gate_unexpected_rc_leaves_a_trace_in_the_log(high):
    """閘門自己壞掉（rc=1）→ 放行照掃，但 log 要有一行 `slot-check 異常 rc=1`。"""
    from test_run_daily_script import _SLOT_CHECK_STUB_TEMPLATE

    high.set_ygo_sniper_stub(_SLOT_CHECK_STUB_TEMPLATE.format(rc=1))

    result = high.run(YGO_SLOT_GATE="1")

    assert "unbound variable" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert high.marker_file.exists(), "異常放行後應該照掃"
    assert "slot-check 異常 rc=1" in high.log_text()
