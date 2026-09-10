"""PAT 凭证自愈：后台循环主动补签缺失/临期 token，状态透出 UI。

背景故障模式：换 token 端点只在特定网络环境可达；离线期间新加的账号永远
换不到 token。请求路径被动补签会让第一个撞上的用户吃到 502——改为后台
主动补：环境恢复后自动补齐，故障可自愈也可观测。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.parse
from typing import Any

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _AUTH_URL, _REFRESH_MARGIN_S, _TOKEN_URL, _PatConfigError, _load_profiles, pat_enabled
from .credentials import _credentials_from_state
from .cooldown import _clear_standard_cooldowns
from .store import _account_state

log = logging.getLogger(__name__)

# ───────────────────────── 凭证自愈（后台保活） ─────────────────────────
#
# 背景故障模式：换 token 端点只在特定网络环境可达；离线期间新加的账号永远
# 换不到 token，缓存 token 过期后也只能等网络恢复。靠请求路径被动补签会让
# 第一个撞上的用户吃到 502——改为后台循环主动补：环境恢复后自动把缺失/
# 临期的 token 补齐，并把状态透出给 UI，故障可自愈也可观测。

_KEEPALIVE_INTERVAL_S = max(0, int(os.environ.get("WB_TRAE_TOKEN_KEEPALIVE_S", "600")))
_keeper_thread: threading.Thread | None = None
_keeper_lock = threading.Lock()
_keeper_round_lock = threading.Lock()
_keeper_last: dict[str, Any] = {"at": 0.0, "env_ready": None, "refreshed": [], "waiting": []}

# 一次性迁移标记：清理旧 4031 逻辑误写的账号级 standard 次日级冷却（见
# cooldown._clear_standard_cooldowns）。进程内只跑一次；跨进程用标记文件保证
# 全生命周期只清一轮——不能每次启动都重放，否则会反复误删现行正常短冷却。
_standard_cooldown_migrated = False


def _migrate_standard_cooldowns_once() -> None:
    global _standard_cooldown_migrated
    if _standard_cooldown_migrated:
        return
    _standard_cooldown_migrated = True
    from buddy_proxy.paths import state_file

    marker = state_file("trae_pat_standard_cooldown_cleanup.done")
    if marker.exists():
        return
    try:
        _clear_standard_cooldowns()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(time.strftime("%Y-%m-%dT%H:%M:%S%z"), encoding="utf-8")
    except Exception as exc:  # 迁移失败不应阻断保活循环启动（下次启动重试）
        log.warning("PAT standard 冷却清理失败（%s）", type(exc).__name__)


def _exchange_env_ready(timeout: float = 3.0) -> bool:
    """探测换 token 端点当前是否可达（DNS+TCP 层）；不可达说明本轮无法签发。"""
    for key in (_AUTH_URL, _TOKEN_URL):
        raw = os.environ.get(key, "").strip()
        host = urllib.parse.urlsplit(raw).hostname if raw else None
        if not host:
            continue
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            return False
        for info in infos[:2]:
            try:
                # sockaddr 可能是 IPv6 四元组，create_connection 只接受 (host, port)
                with socket.create_connection(info[4][:2], timeout=timeout):
                    break
            except OSError:
                continue
        else:
            return False
    return True


def _needs_refresh(state: dict[str, Any], now: float) -> bool:
    current = _credentials_from_state(state)
    if current is None:
        return True
    try:
        expires_at = float(state.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return expires_at - now <= _REFRESH_MARGIN_S


def _keeper_round() -> dict[str, Any]:
    """一轮自愈：环境可达时补签缺失/临期 token；不可达时短路与记录。"""
    now = time.time()
    profiles = _load_profiles()
    refreshed: list[str] = []
    waiting: list[str] = []
    if not _ns._exchange_env_ready():
        for profile in profiles:
            if _needs_refresh(_account_state(profile.cache_key), now):
                waiting.append(profile.id)
        return {"at": time.time(), "env_ready": False, "refreshed": [], "waiting": waiting}
    for profile in profiles:
        if not _needs_refresh(_account_state(profile.cache_key), now):
            continue
        try:
            _ns._get_profile_credentials(profile)
        except Exception as exc:
            waiting.append(profile.id)
            log.warning("PAT 凭证自愈未成功，账号 %s（%s）", profile.id, type(exc).__name__)
        else:
            refreshed.append(profile.id)
            log.info("PAT 凭证自愈：账号 %s 已补签 token", profile.id)
    return {"at": time.time(), "env_ready": True, "refreshed": refreshed, "waiting": waiting}


def refresh_missing_tokens() -> dict[str, Any]:
    """立即执行一轮凭证补签并返回脱敏状态，供 UI 手动触发。

    与后台循环共用互斥锁，避免用户连点、定时轮询和真实请求同时惊群。
    只处理缺失或临期账号；健康 token 不会被强制刷新。
    """
    with _keeper_round_lock:
        result = _keeper_round()
        with _keeper_lock:
            _keeper_last.update(result)
    return {**result, "accounts": accounts_status(start_keeper=False)["accounts"]}


def _keeper_loop(interval: int) -> None:
    while True:
        try:
            refresh_missing_tokens()
        except Exception as exc:
            log.warning("PAT 凭证自愈循环异常（%s）", type(exc).__name__)
        time.sleep(interval)


def start_token_keeper() -> None:
    """启动后台凭证自愈循环（幂等）。``WB_TRAE_TOKEN_KEEPALIVE_S=0`` 可关闭。"""
    global _keeper_thread
    if _KEEPALIVE_INTERVAL_S <= 0 or not pat_enabled():
        return
    with _keeper_lock:
        if _keeper_thread is not None and _keeper_thread.is_alive():
            return
        _migrate_standard_cooldowns_once()
        _keeper_thread = threading.Thread(
            target=_keeper_loop, args=(_KEEPALIVE_INTERVAL_S,),
            name="pat-token-keeper", daemon=True)
        _keeper_thread.start()


def accounts_status(*, start_keeper: bool = True) -> dict[str, Any]:
    """各账号本地凭证/冷却状态 + 自愈循环最近一轮结果（不触网、无秘密）。"""
    if start_keeper:
        start_token_keeper()  # 幂等：兜底保证查看状态时循环一定已拉起
    now = time.time()
    accounts: list[dict[str, Any]] = []
    try:
        profiles = _load_profiles()
    except _PatConfigError:
        profiles = ()
    for profile in profiles:
        state = _account_state(profile.cache_key)
        current = _credentials_from_state(state)
        try:
            expires_at = float(state.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        cooling = []
        cooldowns = state.get("cooldowns") if isinstance(state.get("cooldowns"), dict) else {}
        for kind, until in cooldowns.items():
            try:
                minutes_left = (float(until) - now) / 60
            except (TypeError, ValueError):
                continue
            if minutes_left > 0:
                cooling.append({"kind": kind, "minutes_left": round(minutes_left)})
        hours_left = (expires_at - now) / 3600 if current else None
        accounts.append({
            "id": profile.id,
            "priority": profile.priority,
            "token": ("ok" if current and hours_left > _REFRESH_MARGIN_S / 3600
                      else "expiring" if current else "missing"),
            "hours_left": round(hours_left, 1) if current else None,
            "refreshed_at": state.get("refreshed_at"),
            "cooling": cooling,
        })
    with _keeper_lock:
        keeper = dict(_keeper_last)
    return {"accounts": accounts, "keeper": keeper,
            "keepalive_s": _KEEPALIVE_INTERVAL_S, "enabled": pat_enabled()}

