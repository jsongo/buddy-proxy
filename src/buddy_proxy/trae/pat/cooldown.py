"""PAT 账号/额度冷却：撞码分级冷却、通道级 4031 快速失败与可用账号序。"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _ACCOUNT_COOLDOWN_S, PatProfile, ensure_pat_config
from .models import pat_gateway_is_plus
from .store import _account_state, _mutate_account

log = logging.getLogger(__name__)

# ───────────────────────── 账号/额度冷却 ─────────────────────────


def _quota_class(model: str) -> str:
    return "advanced" if pat_gateway_is_plus(model) else "standard"


def _cooldown_until(profile: PatProfile, quota_class: str) -> float:
    state = _account_state(profile.cache_key)
    cooldowns = state.get("cooldowns") if isinstance(state.get("cooldowns"), dict) else {}
    try:
        return max(float(cooldowns.get("account") or 0),
                   float(cooldowns.get(quota_class) or 0))
    except (TypeError, ValueError):
        return 0


def _next_day_timestamp(now: float) -> float:
    current = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
    return (current.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).timestamp()


def _retry_after_seconds(value: str | None, now: float) -> int | None:
    if not value:
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                return None
            seconds = int(target.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(seconds, 1), 7 * 86400)


def _quota_reset_timestamp(now: float, quota_class: str) -> float:
    # 两类模型池都按已确认的日界自动恢复；池仍独立记录，互不影响。
    del quota_class
    return _next_day_timestamp(now)


# 白名单额度码的分级冷却：首次撞码只做短冷却并立即换号（防上游偶发误报
# 毁掉整个通道一整天）；短窗口内同一账号同类反复撞码才升级为「冷却到次日」
#（真额度耗尽）。内存计数即可——重启后重新探测一次的代价远小于误冷却。
_QUOTA_CODE_HIT_WINDOW_S = 300
_QUOTA_CODE_ESCALATE_HITS = 3
_quota_code_hits: dict[tuple[str, str], list[float]] = {}
_quota_code_hits_lock = threading.Lock()

# 通道级额度限制标记：4031（日额度包耗尽）实测会在同一窗口内拦截全部账号
# —— 判定维度不是单账号余额（余额 0/58 的账号同样 4031，疑似按 IP/租户维度）。
# 此时逐个探测 10 个账号毫无意义，改为快速失败 + 短 TTL 定期重探（默认 5 分钟）：
# 上游限制解除后自动恢复；若真有个别账号仍有余量，最多 5 分钟后也会被发现。
_channel_exhausted: dict[str, float] = {}
_channel_exhausted_lock = threading.Lock()
_CHANNEL_EXHAUSTED_TTL_S = 300


def _channel_exhausted_until(quota_class: str) -> float | None:
    with _channel_exhausted_lock:
        until = float(_ns._channel_exhausted.get(quota_class) or 0)
    return until if until > time.time() else None


def _mark_channel_exhausted(quota_class: str) -> None:
    until = time.time() + _CHANNEL_EXHAUSTED_TTL_S
    with _channel_exhausted_lock:
            _ns._channel_exhausted[quota_class] = until


def _raise_channel_exhausted(quota_class: str) -> None:
    until = _channel_exhausted_until(quota_class)
    if not until:
        return
    retry = max(0, int(until - time.time()))
    raise HTTPException(
        status_code=429,
        detail=(f"PAT {quota_class} 全账号当前均被上游 4031 限额拦截（判定维度疑似非账号级，"
                f"切号无效），{retry}s 后自动重探；日包重置 00:00，期间可改用 trae/ 或 codebuddy/ 通道"))


def _record_quota_code_hit(cache_key: str, quota_class: str) -> int:
    """记录一次额度码撞码，返回窗口内累计次数。"""
    now = time.monotonic()
    key = (cache_key, quota_class)
    with _quota_code_hits_lock:
        hits = [t for t in _ns._quota_code_hits.get(key, []) if now - t < _QUOTA_CODE_HIT_WINDOW_S]
        hits.append(now)
        _ns._quota_code_hits[key] = hits
        return len(hits)


def _mark_cooldown(
    profile: PatProfile, quota_class: str, *, code: int | None = None,
) -> None:
    """额度码冷却：首次短冷却（5分钟）换号，反复撞码升级为冷却到次日。"""
    hits = _record_quota_code_hit(profile.cache_key, quota_class)
    if hits >= _QUOTA_CODE_ESCALATE_HITS:
        until = _quota_reset_timestamp(time.time(), quota_class)
        until_desc = "至次日"
    else:
        until = time.time() + _ACCOUNT_COOLDOWN_S * 5
        until_desc = "5分钟（首次，未升级）"

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get(quota_class) or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns[quota_class] = max(old, until)

    _mutate_account(profile.cache_key, store)
    log.warning(
        "PAT 账号序号=%d %s 类冷却%s（触发码=%s，窗口内第%d次）",
        profile.index, quota_class, until_desc,
        code if code is not None else "unknown", hits)


def _mark_account_cooldown(profile: PatProfile, *, retry_after: str | None = None) -> None:
    now = time.time()
    until = now + (_retry_after_seconds(retry_after, now) or _ACCOUNT_COOLDOWN_S)

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get("account") or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns["account"] = max(old, until)

    _mutate_account(profile.cache_key, store)


def _ordered_available_profiles(quota_class: str) -> tuple[PatProfile, ...]:
    now = time.time()
    return tuple(profile for profile in ensure_pat_config()
                 if _cooldown_until(profile, quota_class) <= now)
