"""``buddy update`` 子命令的行为测试。

这条命令会在别人的仓库里跑 `git pull`，所以「不该动的时候一动不动」比
「能更新成功」更重要。这里用一个一次性的裸仓库+克隆来跑真实 git（不 mock），
因为它要防的恰恰是真实 git 的行为差异：

- **工作树脏 → 必须拒绝，且 HEAD 不变**（不 stash、不 merge、不碰在制品）
- **分叉 → 必须失败并退出非零**（`--ff-only`，不造意外 merge commit）
- **脚本里不能出现 ``"$var中文"`` 写法**：bash 会把多字节字符的首字节并进
  变量名，`set -u` 下直接报 `before\\xef: unbound variable`（真踩过）。
  这条纯静态检查也要留，因为它在 dry-run 里不会暴露。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
BUDDY = REPO / "buddy"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="需要 git 与 bash",
)


def _git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _make_repo(root: pathlib.Path) -> pathlib.Path:
    """建一个「上游裸仓库 + 工作克隆」，克隆里放好 buddy 脚本。"""
    upstream = root / "upstream.git"
    work = root / "work"
    _git("init", "--quiet", "--bare", str(upstream), cwd=root)
    _git("clone", "--quiet", str(upstream), str(work), cwd=root)
    _git("config", "user.email", "t@example.com", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "pyproject.toml").write_text('[project]\nname = "b"\nversion = "0.0.0"\n')
    # 真仓库里 uv.lock/.venv 是被忽略的；这里照抄，否则 uv sync 一跑工作树就脏了
    (work / ".gitignore").write_text("uv.lock\n.venv/\nlogs/\n")
    shutil.copy2(BUDDY, work / "buddy")
    (work / "buddy").chmod(0o755)
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "init", cwd=work)
    _git("push", "-q", "origin", "HEAD:main", cwd=work)
    _git("branch", "-u", "origin/main", cwd=work)
    return work


def _run_update(work: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["./buddy", "update"], cwd=work, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(work),
             "BUDDY_HOME": str(work)},
    )


def _head(work: pathlib.Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                          capture_output=True, text=True).stdout.strip()


def _advance_upstream(work: pathlib.Path, text: str) -> None:
    """在上游追加一个提交，让工作克隆变成「落后一版」。

    必须**经由另一个克隆**去提交：直接在这个工作克隆里 commit + push 的话，
    本地 HEAD 就跟着变了，克隆永远不落后——那样测到的是「已是最新」分支，
    而不是「拉到了新代码」（我第一版就是这么写错的）。
    """
    side = work.parent / "side"
    if not side.exists():
        _git("clone", "--quiet", str(work.parent / "upstream.git"), str(side),
             cwd=work.parent)
        _git("config", "user.email", "t@example.com", cwd=side)
        _git("config", "user.name", "t", cwd=side)
    (side / "marker.txt").write_text(text)
    _git("add", "-A", cwd=side)
    _git("commit", "-qm", text, cwd=side)
    _git("push", "-q", "origin", "HEAD:main", cwd=side)
    _git("fetch", "-q", "origin", cwd=work)


def test_refuses_to_touch_a_dirty_tree(tmp_path: pathlib.Path) -> None:
    """工作树脏 → 拒绝执行，且 HEAD 一动不动。

    「更新」这类命令最坏的失败不是报错，而是**悄悄把你的在制品覆盖掉**。
    所以这里断言的不只是退出码，还有 HEAD 完全没变、脏文件内容还在。
    """
    work = _make_repo(tmp_path)
    dirty = work / "marker.txt"
    dirty.write_text("work in progress")
    before = _head(work)

    proc = _run_update(work)

    assert proc.returncode != 0, f"脏工作树竟然成功了:\n{proc.stdout}"
    assert "未提交改动" in proc.stdout, proc.stdout
    assert _head(work) == before, "HEAD 不该被改动"
    assert dirty.read_text() == "work in progress", "在制品被覆盖了"


def test_fast_forwards_when_clean(tmp_path: pathlib.Path) -> None:
    """干净且落后一版 → 真的快进到上游 HEAD，新文件落地。"""
    work = _make_repo(tmp_path)
    _advance_upstream(work, "new upstream commit")
    upstream_head = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=work,
        capture_output=True, text=True).stdout.strip()
    # 落后了才测得出「更新」，否则测的是「已是最新」那条分支
    assert _head(work) != upstream_head, "前置条件：本地应落后于上游"

    proc = _run_update(work)

    assert _head(work) == upstream_head, f"没快进到上游:\n{proc.stdout}\n{proc.stderr}"
    assert (work / "marker.txt").read_text() == "new upstream commit"


def test_diverged_branch_fails_loudly(tmp_path: pathlib.Path) -> None:
    """本地有分叉提交 → 明确失败，不偷偷造一个 merge commit。

    用 ``--ff-only`` 就是这个目的；换成默认的 ``git pull`` 会凭空多出一个
    merge 提交，把线性历史搞乱且不易察觉。
    """
    work = _make_repo(tmp_path)
    _advance_upstream(work, "upstream side")
    (work / "local.txt").write_text("local side")
    _git("add", "-A", cwd=work)
    _git("commit", "-qm", "local side", cwd=work)   # 造成分叉
    before = _head(work)

    proc = _run_update(work)

    assert proc.returncode != 0, f"分叉竟然成功了:\n{proc.stdout}"
    assert _head(work) == before, "分叉时不该改动本地历史"
    parents = subprocess.run(["git", "rev-list", "--parents", "-n", "1", "HEAD"],
                             cwd=work, capture_output=True, text=True).stdout.split()
    assert len(parents) == 2, "不该产生 merge commit"


def test_no_bare_variable_before_a_multibyte_char() -> None:
    """脚本里不能写 ``$var中文`` —— bash 会把中文字节并进变量名。

    实测 ``log "当前 $before，拉取中…"`` 直接报
    ``before\\xef: unbound variable``（``set -u`` 下必炸）。JSON/终端里都看
    不出问题，只有真跑那行才暴露，所以用静态检查把它钉住。

    注意不能只查 **引号紧跟变量** 的写法（``"$var中文``）：踩到的那次是
    ``"当前 $branch @ $before，拉取中"``，变量在字符串中间。第一版正则要求
    前面是 ``"``，于是漏掉了它——变异测试（把 ``${before}`` 改回裸 ``$before``）
    照样全绿才发现。
    """
    text = BUDDY.read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):        # 注释里提到这个坑是合理的
            continue
        # 只要 $name 后面紧跟非 ASCII 且没写花括号就中招，与它在字符串里
        # 什么位置无关
        for m in re.finditer(r"\$([A-Za-z_][A-Za-z0-9_]*)([^\s\x00-\x7F])", line):
            offenders.append(f"{i}: ${m.group(1)}{m.group(2)}  <- {line.strip()}")
    assert not offenders, "变量名后紧跟多字节字符，需改用 ${var}：\n" + "\n".join(offenders)


def test_no_duplicate_function_definitions() -> None:
    """顶层函数不能重复定义。

    bash 对重复定义不报错，**后面的静默覆盖前面的**——改动时不小心把已有
    函数连带复制一份，功能看起来正常（跑的是后一个），但前一份成了死代码，
    下次改「那个函数」很可能改错地方。我加 cmd_update 时就这么复制出过一份
    重复的 open_ui，靠肉眼读 diff 才发现。
    """
    text = BUDDY.read_text(encoding="utf-8")
    names = re.findall(r"(?m)^([a-z_][a-z0-9_]*)\(\)\s*\{", text)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"这些函数被重复定义（后者会静默覆盖前者）: {dupes}"


def test_update_is_registered_as_a_subcommand() -> None:
    """``buddy update`` 得真的挂在分派表里、也写进 help。"""
    text = BUDDY.read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*update\)\s+cmd_update", text), "分派表里没有 update"
    assert re.search(r"(?m)^\s*cmd_update\(\)\s*\{", text), "没有 cmd_update 定义"
    assert "buddy update" in text.split("set -euo pipefail")[0], "help 头部没写 update"
