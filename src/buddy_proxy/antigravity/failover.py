"""antigravity 多账号 failover：稳定主备 + 内存冷却。

选号策略与 trae PAT 同思路：永远从优先级最高的可用账号开始（登录顺序即
优先级），坏账号靠冷却被临时摘出候选——看起来像轮询，实质是主备降级。

冷却状态**只放内存**（与 trae PAT 落盘不同）：trae 有 keeper 进程要跨进
程共享状态、且「冷却至次日」丢了会频打上游；这里冷却只有 60s / 5min 级，
进程重启清零的代价只是每账号重探一次（本来也要探），换来零状态文件管理。
有意不做 trae 的「反复失败升级冷却到次日」：antigravity 是 5h/weekly 双池，
5min 一探自愈正好落在窗口重置上。全账号都冷却时 :func:`available_accounts`
为空，转发直接 429——天然形成通道级快速失败（等价 trae 的 channel-exhausted，
但零持久化状态）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .credentials import (
    AccountRef,
    _expiry_dt,
    list_accounts,
    load_account_cred,
)

log = logging.getLogger(__name__)

#: 403 / 401 强刷后仍被拒：账号级问题，短冷却快速重探。
_ACCOUNT_COOLDOWN_S = 60.0
#: 429（额度耗尽）：对齐 trae 首档 5min；Retry-After 可覆盖。
_QUOTA_COOLDOWN_S = 300.0
#: Retry-After 的合理区间（Google 给秒数；钳到 1s~7d 防御离谱值）。
_RETRY_AFTER_MIN_S = 1.0
_RETRY_AFTER_MAX_S = 7 * 86400.0

_cooldowns: dict[str, tuple[float, str]] = {}  # account_id -> (until_epoch, kind)
_lock = threading.Lock()


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。

    返回实际冷却秒数。``quota=True`` 表示额度类（429），冷却更久；
    否则按账号级问题（403/401）短冷却。
    """
    default = _QUOTA_COOLDOWN_S if quota else _ACCOUNT_COOLDOWN_S
    seconds = default
    if retry_after:
        try:
            seconds = max(_RETRY_AFTER_MIN_S, min(float(retry_after), _RETRY_AFTER_MAX_S))
        except (TypeError, ValueError):
            pass
    kind = "quota" if quota else "account"
    with _lock:
        _cooldowns[account_id] = (time.time() + seconds, kind)
    if reason:
        log.warning("antigravity: 账号 %s 冷却 %.0fs（%s）", account_id, seconds, reason)
    return seconds


def cooldown_left(account_id: str) -> tuple[float, str]:
    """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
    with _lock:
        entry = _cooldowns.get(account_id)
    if not entry:
        return 0.0, ""
    until, kind = entry
    left = until - time.time()
    if left <= 0:
        return 0.0, ""
    return left, kind


def available_accounts() -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）：全部账号剔除冷却中的。"""
    return [a for a in list_accounts() if cooldown_left(a.id)[0] <= 0]


def accounts_status() -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        exp = _expiry_dt(cred)
        hours_left = round((exp.timestamp() - time.time()) / 3600, 1) if exp else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "email": a.email or a.id,
            "index": i + 1,
            "project_id": cred.get("project_id") or "",
            "token": "ok" if cred.get("refresh_token") else "missing",
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}
