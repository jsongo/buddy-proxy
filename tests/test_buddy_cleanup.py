"""``buddy cleanup`` 子命令的行为测试。

按天轮删历史日志看着简单，但踩过两个坑，都靠这里锁住：

- **BSD find 没有 ``-regextype``/``-regex``**：本机是 macOS，写成 GNU 那套时
  find 直接报错（stderr 被 ``2>/dev/null`` 吞了），stdout 空 → 一个文件都
  没命中，命令假装「无需清理」。改用 ``-name`` 通配 + ``case`` 校验形状。
- **不能碰活跃文件**：``buddy-proxy.jsonl``（无日期后缀、正在写）必须原样
  保留；只删尾部严格是 ``.YYYY-MM-DD`` 的轮转文件。
- **边界**：文件名日期**等于**截止日要保留（``<`` 而非 ``<=``）。

用真实 bash 跑真实脚本（不 mock）：要防的正是 find/date/字符串比较的真实
行为差异。``BUDDY_HOME`` 指向临时目录，脚本自会去 `<BUDDY_HOME>/logs` 里删。
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BUDDY = REPO / "buddy"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="需要 bash"
)


def _setup(tmp_path: pathlib.Path) -> pathlib.Path:
    """造一个 BUDDY_HOME：拷脚本 + 建 logs/。返回 logs 目录。"""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    shutil.copy(BUDDY, home / "buddy")
    (home / "buddy").chmod(0o755)
    return home / "logs"


def _run(home: pathlib.Path, *args: str, keep: int | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "BUDDY_HOME": str(home),
           "HOME": str(home)}  # 脚本顶层会读 $HOME 拼 plist 路径
    if keep is not None:
        env["BUDDY_LOG_KEEP_DAYS"] = str(keep)
    return subprocess.run(["bash", str(home / "buddy"), *args],
                          capture_output=True, text=True, timeout=30, env=env)


def _touch(logs: pathlib.Path, name: str) -> pathlib.Path:
    p = logs / name
    p.write_text("x")
    return p


def _days_ago(n: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - n * 86400))


def test_deletes_only_files_older_than_window(tmp_path):
    """窗口外的轮转文件删掉，窗口内的保留，活跃文件一律不碰。"""
    logs = _setup(tmp_path)
    old = _touch(logs, f"buddy-proxy.jsonl.{_days_ago(30)}")
    recent = _touch(logs, f"buddy-proxy.jsonl.{_days_ago(2)}")
    active = _touch(logs, "buddy-proxy.jsonl")  # 无日期后缀：正在写
    unrelated = _touch(logs, "notes.txt")

    proc = _run(logs.parent, "cleanup", keep=15)
    assert proc.returncode == 0, proc.stderr
    assert not old.exists(), "30 天前的该删"
    assert recent.exists(), "2 天前的该留"
    assert active.exists(), "活跃文件不能删"
    assert unrelated.exists(), "非轮转文件不能删"


def test_keeps_file_exactly_on_cutoff(tmp_path):
    """文件名日期正好等于截止日 → 保留（边界用 <，不是 <=）。"""
    logs = _setup(tmp_path)
    boundary = _touch(logs, f"metrics.jsonl.{_days_ago(15)}")
    older = _touch(logs, f"metrics.jsonl.{_days_ago(16)}")

    proc = _run(logs.parent, "cleanup", keep=15)
    assert proc.returncode == 0, proc.stderr
    assert boundary.exists(), "正好 15 天该保留"
    assert not older.exists(), "16 天该删"


def test_matches_bsd_find_shape(tmp_path):
    """必须真的命中 BSD/macOS 的 find（GNU 的 -regextype/-regex 在本机报错）。

    这是真踩过的回归：用 -regextype 时 find 报错、stdout 为空，命令静默
    变成「无需清理」。这条保证「窗口外的文件确实被数到并删掉」。
    """
    logs = _setup(tmp_path)
    for n in (20, 25, 40):
        _touch(logs, f"proxy.log.{_days_ago(n)}")
    proc = _run(logs.parent, "cleanup", keep=10)
    assert proc.returncode == 0, proc.stderr
    assert "已删除 3 个" in proc.stdout, f"应命中 3 个文件: {proc.stdout}"
    assert not list(logs.glob("proxy.log.20*")), "窗口外的都该删"


def test_noop_when_nothing_to_delete(tmp_path):
    """没有过期文件时报「无需清理」，退出码 0（幂等，可反复跑）。"""
    logs = _setup(tmp_path)
    _touch(logs, f"buddy-proxy.jsonl.{_days_ago(1)}")
    proc = _run(logs.parent, "cleanup", keep=15)
    assert proc.returncode == 0, proc.stderr
    assert "无需清理" in proc.stdout


def test_multiple_log_families(tmp_path):
    """三类轮转文件（buddy-proxy.jsonl / metrics.jsonl / proxy.log）都覆盖。"""
    logs = _setup(tmp_path)
    names = [f"buddy-proxy.jsonl.{_days_ago(30)}",
             f"metrics.jsonl.{_days_ago(31)}",
             f"proxy.log.{_days_ago(32)}"]
    for n in names:
        _touch(logs, n)
    proc = _run(logs.parent, "cleanup", keep=15)
    assert proc.returncode == 0, proc.stderr
    assert "已删除 3 个" in proc.stdout, proc.stdout
