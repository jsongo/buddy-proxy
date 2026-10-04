"""qoder 多账号 failover：稳定主备 + 内存冷却（kimi/antigravity 同思路）。

永远从优先级最高的可用账号开始（登录顺序即优先级），坏账号靠冷却被临时摘出
候选。冷却状态只放内存（60s / 5min 级，进程重启清零的代价只是每账号重探一次）；
全账号都冷却时 :func:`available_accounts` 为空，转发直接 429——通道级快速失败。

qoder 的上游错误分两类：401（token 失效）与 403/429（额度/权益门——2026-10-03
起三方模型被收回就是带内 code 112 的 403）。403 里既可能是账号级（额度尽、
权益收回）也可能是请求级（模型不存在），按 code 与文案粗分：带 112/pricing 的
按账号冷却；其余 403 与 4xx 原样透传。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .credentials import AccountRef, list_accounts, load_account_cred, cred_to_credential

log = logging.getLogger(__name__)

#: 403 / 401 强刷后仍被拒：账号级问题，短冷却快速重探。
_ACCOUNT_COOLDOWN_S = 60.0
#: 429（额度耗尽）：对齐 antigravity/trae/kimi 首档 5min；Retry-After 可覆盖。
_QUOTA_COOLDOWN_S = 300.0
#: Retry-After 的合理区间（钳到 1s~7d 防御离谱值）。
_RETRY_AFTER_MIN_S = 1.0
_RETRY_AFTER_MAX_S = 7 * 86400.0

_cooldowns: dict[str, tuple[float, str]] = {}  # account_id -> (until_epoch, kind)
_lock = threading.Lock()


def _fmt_left(seconds: float) -> str:
    if seconds >= 90 * 60:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 60:.1f}min"


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。"""
    if quota:
        default, kind = _QUOTA_COOLDOWN_S, "quota"
    else:
        default, kind = _ACCOUNT_COOLDOWN_S, "account"
    seconds = default
    if retry_after:
        try:
            seconds = max(_RETRY_AFTER_MIN_S, min(float(retry_after), _RETRY_AFTER_MAX_S))
        except (TypeError, ValueError):
            pass
    with _lock:
        _cooldowns[account_id] = (time.time() + seconds, kind)
    if reason:
        log.warning("qoder: 账号 %s 冷却 %s（%s）", account_id, _fmt_left(seconds), reason)
    return seconds


def clear_cooldown(account_id: str) -> None:
    """清掉该账号的冷却标记（删除账号后调用，防内存残留）。"""
    with _lock:
        _cooldowns.pop(account_id, None)


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


def available_accounts(region: str | None = None) -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）。

    ``region`` 给出时只回该区账号——qoder 的 CN / 全球版账号不通用（连错域
    401），混域 failover 每轮必败一次，白白多付一次往返。
    """
    out = [a for a in list_accounts() if cooldown_left(a.id)[0] <= 0]
    if region:
        out = [a for a in out if a.region == region]
    return out


def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用）。"""
    parts = []
    for a in list_accounts():
        left, kind = cooldown_left(a.id)
        if left <= 0:
            continue
        label = {"quota": "额度冷却"}.get(kind, "账号冷却")
        parts.append(f"{a.id} {label}剩 {_fmt_left(left)}")
    return "、".join(parts)


def accounts_status() -> dict[str, Any]:
    """UI 账号状态面板数据（纯本地，不触网、不含秘密）。"""
    accounts = list_accounts()
    items: list[dict[str, Any]] = []
    for i, a in enumerate(accounts):
        cred = load_account_cred(a.id) or {}
        exp = cred_to_credential(cred).expires_at_ms
        hours_left = round((exp - time.time()) / 3600, 1) if exp > 0 else None
        left, kind = cooldown_left(a.id)
        items.append({
            "id": a.id,
            "email": a.email,
            "name": a.name or a.email or a.id,
            "region": a.region,
            "index": i + 1,
            "uid": str(cred.get("uid") or ""),
            "token": "ok" if cred.get("refresh_token") else "missing",
            "hours_left": max(hours_left, 0.0) if hours_left is not None else None,
            "cooling": ([{"kind": kind, "minutes_left": round(left / 60, 1)}] if left > 0 else []),
        })
    return {"enabled": bool(items), "accounts": items}
