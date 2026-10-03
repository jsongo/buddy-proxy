"""antigravity 多账号 failover：稳定主备 + 内存冷却。

选号策略与 trae PAT 同思路：永远从优先级最高的可用账号开始（登录顺序即
优先级），坏账号靠冷却被临时摘出候选——看起来像轮询，实质是主备降级。

冷却状态**只放内存**（与 trae PAT 落盘不同）：trae 有 keeper 进程要跨进
程共享状态、且「冷却至次日」丢了会频打上游；这里冷却只有 60s / 5min /
6h 级，进程重启清零的代价只是每账号重探一次（本来也要探），换来零状态
文件管理。有意不做 trae 的「反复失败升级冷却到次日」：antigravity 是
5h/weekly 双池，5min 一探自愈正好落在窗口重置上。全账号都冷却时
:func:`available_accounts` 为空，转发直接 429——天然形成通道级快速失败
（等价 trae 的 channel-exhausted，但零持久化状态）。

拉黑识别（2026-10-03）：403 文案「Verify your account to continue.」是
Google 风控把账号挡在 antigravity 外（能登录、凭据有效，但上游一律拒）。
这不上额度冷却的 60s 档——那只会每分钟白打一次上游；直接 6h 冷却 +
``blacklist`` 类别，面板副标题标注「疑似拉黑」引导删除（管理页可删账号）。
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
#: 403「Verify your account」= Google 风控拉黑：短冷却只会反复白打上游，
#: 直接 6h（重启清零后可重新探测——万一哪天解封了）。
_BLACKLIST_COOLDOWN_S = 6 * 3600.0
#: Retry-After 的合理区间（Google 给秒数；钳到 1s~7d 防御离谱值）。
_RETRY_AFTER_MIN_S = 1.0
_RETRY_AFTER_MAX_S = 7 * 86400.0

#: 上游 403 文案的拉黑指纹（不区分大小写匹配）。
_BLACKLIST_MSG = "verify your account"

_cooldowns: dict[str, tuple[float, str]] = {}  # account_id -> (until_epoch, kind)
_lock = threading.Lock()


def _fmt_left(seconds: float) -> str:
    """剩余冷却的展示时长：90min 以上按小时（6h 档读着不像「360min」）。"""
    if seconds >= 90 * 60:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 60:.1f}min"


def is_blacklist_signal(code: int, message: str) -> bool:
    """403 +「Verify your account to continue.」= Google 风控拉黑该账号。

    与普通 403（PERMISSION_DENIED 等）的区别：账号本身能登录、凭据有效，
    但 antigravity 上游一律拒——重试无意义，冷却按拉黑档走。
    """
    return code == 403 and _BLACKLIST_MSG in (message or "").lower()


def mark_cooldown(account_id: str, *, retry_after: str | None = None,
                  quota: bool = False, blacklist: bool = False,
                  reason: str = "") -> float:
    """把账号冷却一段时间。``retry_after``（Retry-After 头）优先于默认时长。

    返回实际冷却秒数。三档：``blacklist``（Google 风控拉黑，6h）>
    ``quota``（429 额度，默认 5min）> 其余账号级问题（403/401，60s）。
    Retry-After 只覆盖后两档——拉黑是风控决定，头部给不出恢复时间。
    """
    if blacklist:
        default, kind = _BLACKLIST_COOLDOWN_S, "blacklist"
    elif quota:
        default, kind = _QUOTA_COOLDOWN_S, "quota"
    else:
        default, kind = _ACCOUNT_COOLDOWN_S, "account"
    seconds = default
    if retry_after and not blacklist:
        try:
            seconds = max(_RETRY_AFTER_MIN_S, min(float(retry_after), _RETRY_AFTER_MAX_S))
        except (TypeError, ValueError):
            pass
    with _lock:
        _cooldowns[account_id] = (time.time() + seconds, kind)
    if reason:
        log.warning("antigravity: 账号 %s 冷却 %s（%s）",
                    account_id, _fmt_left(seconds), reason)
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


def available_accounts() -> list[AccountRef]:
    """当前可用的账号（按 failover 顺位）：全部账号剔除冷却中的。"""
    return [a for a in list_accounts() if cooldown_left(a.id)[0] <= 0]


def cooldown_report() -> str:
    """全部账号冷却状态的一句话画像（通道耗尽报错用），如：

    「u@x.com 额度冷却剩 2.1min、v@y.com 疑似拉黑剩 6.0h」

    只列冷却中的账号——报「全部不可用」时不在冷却的账号要么成功了（走不到
    报错）要么是无冷却失败（EOF 之类，由报错里的「最后错误」兜底说明）。
    """
    parts = []
    for a in list_accounts():
        left, kind = cooldown_left(a.id)
        if left <= 0:
            continue
        label = {"quota": "额度冷却", "blacklist": "疑似拉黑"}.get(kind, "账号冷却")
        parts.append(f"{a.id} {label}剩 {_fmt_left(left)}")
    return "、".join(parts)


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
