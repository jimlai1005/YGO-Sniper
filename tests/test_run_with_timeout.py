"""supervisor 的行為測試：真開子行程（sh/sleep），但零網路。"""

import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_with_timeout.py"


def _run(timeout: str, *cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), timeout, *cmd],
        capture_output=True, text=True, timeout=30,
    )


def _is_gone(pid: int) -> bool:
    """判斷孫行程是否已經死透。

    os.kill(pid, 0) 對殭屍行程（父行程已死、還沒被 launchd 回收）一樣會成功——
    殭屍仍佔著行程表的一格。所以光看 os.kill 拋不拋例外會誤判「還活著」。
    改用 `ps -o stat=` 看真實狀態：查無此行程，或狀態是 Z（殭屍），都算已死——
    訊號已經生效，剩下只是核心多久才回收那格記憶體，不影響「行程不再消耗資源」
    這個我們真正關心的行為。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True, text=True,
    )
    stat = result.stdout.strip()
    return stat == "" or stat.startswith("Z")


def test_exit_code_passthrough():
    assert _run("30", "sh", "-c", "exit 7").returncode == 7


def test_timeout_returns_124_and_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "pid"
    r = _run("1", "sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait")
    assert r.returncode == 124
    assert "watchdog" in r.stdout
    gpid = int(pidfile.read_text())
    # 用 poll loop 取代固定 sleep：固定 0.3s 在忙碌機器上是機率性的猜測，
    # poll 到真的死透（或殭屍）為止才是在測「行為」而不是在測「時序運氣」。
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _is_gone(gpid):
        time.sleep(0.1)
    assert _is_gone(gpid), f"grandchild pid={gpid} 在 5s 後仍存活且非殭屍"
