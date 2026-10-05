"""trae work 多账号 failover：稳定主备 + 内存冷却。

核心冷却状态机收敛到 :class:`buddy_proxy.core.account_failover.CooldownTracker`
（antigravity/kimi/qoder 共用）。本模块保留 trae 自己的
:func:`accounts_status`（字段口径：uid/nickname）并持有一个模块级实例。

永远从优先级最高的可用账号开始（登录顺序即优先级），坏账号靠冷却被临时摘出
候选。冷却状态只放内存（60s / 5min 级，进程重启清零的代价只是每账号重探一次）；
全账号都冷却时 :func:`available_accounts` 为空，转发直接 429——通道级快速失败。

trae work 的上游错误：401（token 失效）与 403/429（额度/权益）。trae work
不分 region（全 CN）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..core.account_failover import CooldownTracker
from .credentials import AccountRef, list_accounts, load_account_cred

log = logging.getLogger(__name__)

#: 模块级冷却状态机。
_tracker = CooldownTracker()

#: 活引用 tracker 内部 dict（同 ``_tracker._cooldowns``）：测试直接
#: ``failover._cooldowns.clear()`` / 赋值造过期条目，保留入口。
_cooldowns = _tracker._cooldowns

#: 兼容导出（原模块常量，外部可能引用）。
_ACCOUNT_COOLDOWN_S = 60.0
_QUOTA_COOLDOWN_S = 300.0


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。"""
    return _tracker.mark(account_id, retry_after=retry_after,
                         kind="quota" if quota else "account",
                         reason=reason, log=log)


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。"""
    _tracker.clear(account_id)


def cooldown_left(account_id: str) -> tuple[float, str]:
    """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
    return _tracker.left(account_id)


def available_accounts() -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）：全部账号剔除冷却中的。"""
    return [a for a in list_accounts() if cooldown_left(a.id)[0] <= 0]


def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用）。"""
    return _tracker.report([a.id for a in list_accounts()])


def accounts_status() -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        exp = int(cred.get("expires_at") or 0)
        hours_left = round((exp - time.time()) / 3600, 1) if exp > 0 else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "uid": a.uid,
            "nickname": a.alias or a.nickname or a.uid or a.id,
            "alias": a.alias,
            "index": i + 1,
            "token": "ok" if cred.get("refresh_token") else "missing",
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}
