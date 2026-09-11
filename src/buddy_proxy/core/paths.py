"""buddy-proxy 本地状态目录：所有运行时状态 JSON 统一放在 ~/.buddy-proxy/。

历史上各模块把状态散落在 ~/.ethan/ 下（trae_pat_token.json、trae_work.json、
buddy_client_names.json…），与 ethan 生态其它工具的文件混在一起。2026-09-09
起统一收敛到本模块管理的状态目录：

- 默认 ``~/.buddy-proxy/``，可用 ``BUDDY_PROXY_STATE_DIR`` 整体覆盖；
- 各状态文件仍保留原有的专用环境变量覆盖（TRAE_PAT_TOKEN_FILE、
  TRAE_WORK_CRED_PATH、BUDDY_CLIENT_NAMES_FILE），优先级高于本模块默认值；
- ``state_file()`` 提供一次性自动迁移：新位置不存在而 ~/.ethan/ 下的遗留
  文件存在时，自动复制过来（copy 而非 move——原文件留作备份，且避免
  多实例/回滚场景丢状态）。凭证类文件迁移后强制 0600。
"""

from __future__ import annotations

import os
import pathlib
import shutil

_LEGACY_DIR = pathlib.Path.home() / ".ethan"
_DEFAULT_STATE_DIR = pathlib.Path.home() / ".buddy-proxy"


def state_dir() -> pathlib.Path:
    """buddy-proxy 运行时状态目录（可用 BUDDY_PROXY_STATE_DIR 覆盖）。"""
    return pathlib.Path(
        os.environ.get("BUDDY_PROXY_STATE_DIR", str(_DEFAULT_STATE_DIR))
    ).expanduser()


def state_file(name: str, *, legacy: str | None = None) -> pathlib.Path:
    """状态文件在统一目录下的路径；首次访问时自动从遗留位置迁移。

    - ``name``：统一目录下的文件名；
    - ``legacy``：~/.ethan/ 下的历史文件名（省略则不做迁移）。

    迁移是 copy 而非 move：遗留文件保留原位作备份，回滚旧版本代理时
    仍能读到状态；凭证文件复制后强制 0600。
    """
    target = state_dir() / name
    if legacy and not target.exists():
        old = _LEGACY_DIR / legacy
        if old.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old, target)
                os.chmod(target, 0o600)
            except OSError:
                # 迁移失败不阻断业务：回落到遗留路径继续读。
                return old
    return target
