"""Qoder 活动面的机器指纹（桌面端 ``runtime-info`` 同款）。

``/sash/**`` 活动面按**机器指纹**定向发放签到活动（CN 与全球区一致，2026-10-07
实测）：签到条目只发给指纹被上游「认识」的设备。只带静态 ``Cosy-MachineId``
时，从未被代理领过的账号（如只在桌面端签到的号）返回的活动列表里**没有**
CLAIM_BENEFIT 条目——桌面端能看到、我们看不到，界面误报「有活动，暂无可领
签到奖励」，就是这个原因。

桌面端主进程在每次活动请求前调 App 内置的原生二进制生成指纹::

    {app}/Contents/Resources/umid/runtime-info <environment> --account-stdin
    stdin:  {"account": "<uid>"}\\n
    stdout: {"machineToken": "...", "machineType": "...", "machineCode": "...", ...}

指纹对同一账号**逐次稳定**（实测多次运行同值），请求头为::

    Cosy-MachineToken / Cosy-MachineCode / Cosy-MachineType   ← 指纹三件套
    Cosy-MachineOS / Cosy-MachineHostname                     ← 客户端标识
    Cosy-MachineId                                            ← 静态 machine_id

这里复刻同一套：找得到二进制就生成真实指纹，找不到（没装 Qoder 桌面端）
回退旧行为（纯静态头）——已被代理领过的账号静态指纹也能看到条目（上游已
登记该指纹），新号则看不到。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: 单次二进制调用的超时（正常毫秒级）。
IDENTITY_TIMEOUT_S = 10.0

#: 指纹缓存时长（桌面端 1h 刷新一次，实测逐次同值，缓存久点无害）。
_IDENTITY_TTL_S = 3600.0

#: 生成失败后的负缓存（避免每个请求都白起一次子进程）。
_FAILURE_TTL_S = 60.0

#: 各区域的 ``runtime-info`` 候选路径（按顺序探测；环境变量可覆盖）。
_UMID_ENV = {"global": "QODER_UMID_BIN", "cn": "QODER_UMID_BIN_CN"}
_UMID_DEFAULTS = {
    "global": ("/Applications/Qoder.app/Contents/Resources/umid/runtime-info",),
    "cn": ("/Applications/Qoder CN.app/Contents/Resources/umid/runtime-info",),
}

#: 二进制的 environment 参数（桌面端生产安装通道）。
_UMID_ENVIRONMENT = "stable"


@dataclass(frozen=True)
class MachineIdentity:
    """一次生成的机器指纹（进 ``Cosy-Machine*`` 头，勿落日志）。"""

    token: str
    code: str
    type: str


_cache: dict[tuple[str, str], tuple[float, MachineIdentity | None]] = {}


def _binary_candidates(region_key: str) -> list[str]:
    """按环境变量 → /Applications → ~/Applications 的顺序找二进制。"""
    out = []
    env = os.environ.get(_UMID_ENV.get(region_key, ""), "").strip()
    if env:
        out.append(env)
    for base in _UMID_DEFAULTS.get(region_key, ()):
        out.append(base)
        # 用户级安装（~/Applications）的同款 App：把前缀换掉再试一次
        if "/Applications/" in base:
            out.append(str(Path.home() / "Applications" / base.split("/Applications/", 1)[1]))
    return out


def _find_binary(region_key: str) -> str | None:
    for cand in _binary_candidates(region_key):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _run_runtime_info(binary: str, account: str) -> MachineIdentity | None:
    proc = subprocess.run(  # noqa: S603 - 路径来自白名单/环境变量，非用户输入拼接
        [binary, _UMID_ENVIRONMENT, "--account-stdin"],
        input=json.dumps({"account": account}) + "\n",
        capture_output=True, text=True, timeout=IDENTITY_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"exit={proc.returncode} {proc.stderr[:120]}")
    line = proc.stdout.strip().splitlines()[0]
    data = json.loads(line)
    token = str(data.get("machineToken") or "").strip()
    code = str(data.get("machineCode") or "").strip()
    mtype = str(data.get("machineType") or "").strip()
    if not (token and code and mtype):
        raise RuntimeError("缺少 machineToken/machineCode/machineType 字段")
    return MachineIdentity(token=token, code=code, type=mtype)


def machine_identity(region_key: str, account: str) -> MachineIdentity | None:
    """取一个账号的机器指纹；二进制缺失/失败返回 ``None``（回退静态头）。

    进程内缓存：成功 1h、失败 1min——与桌面端的刷新节奏对齐，也避免每次
    活动查询都起子进程。
    """
    key = (region_key, account)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None:
        expires, value = hit
        if now < expires:
            return value
    value: MachineIdentity | None = None
    binary = _find_binary(region_key)
    if binary is not None:
        try:
            value = _run_runtime_info(binary, account)
        except BaseException as exc:  # noqa: BLE001 - 指纹失败只降级，绝不阻断签到
            # 连 BaseException 都吞：这里跑在 to_thread 工作线程里，任何异常
            # 泄漏都会炸掉整页签到状态聚合；指纹是纯增益，拿不到就回退静态头。
            log.warning("qoder runtime-info 指纹生成失败（%s）: %s", region_key, exc)
    ttl = _IDENTITY_TTL_S if value is not None else _FAILURE_TTL_S
    _cache[key] = (now + ttl, value)
    return value


def identity_headers(identity: MachineIdentity) -> dict[str, str]:
    """指纹对应的请求头片段（与桌面端 ``nativeCampaignRequestService`` 一致）。"""
    return {
        "Cosy-MachineOS": sys.platform,
        "Cosy-MachineHostname": socket.gethostname(),
        "Cosy-MachineToken": identity.token,
        "Cosy-MachineCode": identity.code,
        "Cosy-MachineType": identity.type,
    }


def clear_cache() -> None:
    """清空指纹缓存（测试用）。"""
    _cache.clear()
