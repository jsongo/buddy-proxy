"""DuMate 签到（bceConsole 通道）。

接口（2026-10-05 实测）：

- 查询：``GET  https://console.bce.baidu.com/api/dumate/points/loginBonusInfo``
  → ``{"result": {"totalTimes": N, "totalPoints": N, "hasIssued": bool,
       "signInDays": ["YYYY-MM-DD", ...]}}``
- 领取：``POST https://console.bce.baidu.com/api/dumate/points/loginBonus``
  （body ``{}``，需 ``csrftoken`` = ``bce-user-info`` cookie 值）
  → ``{"result": true}``

认证走 :mod:`buddy_proxy.dumate.cookies` 解出的百度云 cookie（App 登录态），
**不是** inapp key 通道。App 未登录 / cookie 过期时返回 None，由调用方兜底。

``totalPoints`` 是**累计签到所得**（不是当前余额——余额面板走 quotaOverview，
无公开接口）。签到积分与模型消耗积分是两个独立池，别拿 totalPoints 当余额。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .cookies import resolve_bceconsole_auth

log = logging.getLogger(__name__)

_BCE_CONSOLE = "https://console.bce.baidu.com"
_BONUS_INFO = f"{_BCE_CONSOLE}/api/dumate/points/loginBonusInfo"
_BONUS_CLAIM = f"{_BCE_CONSOLE}/api/dumate/points/loginBonus"
_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


def fetch_checkin_status(timeout: float = 20.0) -> dict[str, Any] | None:
    """查询签到状态；未登录 / 失败返回 None。

    返回 ``{"checked_in": bool, "claimable": bool, "total_points": int,
    "total_times": int}``（total_points/total_times 拿不到时为 0）。

    ``timeout`` 默认给足（签到调度器调用）；面板状态接口这类高频只读场景
    传小值（如 3s）快速失败，别拖慢页面首渲。
    """
    auth = resolve_bceconsole_auth()
    if auth is None:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.get(_BONUS_INFO, headers=auth.headers())
    except httpx.HTTPError as exc:
        log.debug("dumate checkin status fetch failed: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    result = data.get("result") or {}
    if not data.get("success", True) and not result:
        return None
    return _parse_status_payload(data)


def _parse_status_payload(data: dict[str, Any]) -> dict[str, Any]:
    """loginBonusInfo 响应 → 统一状态 dict（签领与查询共用解析）。"""
    result = data.get("result") or {}
    return {
        "checked_in": bool(result.get("hasIssued")),
        "claimable": not bool(result.get("hasIssued")),
        "total_points": int(result.get("totalPoints") or 0),
        "total_times": int(result.get("totalTimes") or 0),
        # 每日签到所得：totalPoints/totalTimes（已签次数），单次拿不到就 0。
        # 仅用于界面「每日 +X」展示，不是精确口径。
        "daily_credit": round(int(result.get("totalPoints") or 0)
                              / max(int(result.get("totalTimes") or 1), 1)),
        # signInDays 是真实签到日期列表，交给 BenefitsManager 记日历
        "sign_in_days": result.get("signInDays") or [],
    }


def _bce_get(path: str, *, timeout: float = 20.0) -> dict[str, Any] | None:
    """带 cookie 的 bceConsole GET；未登录 / 失败返回 None。"""
    auth = resolve_bceconsole_auth()
    if auth is None:
        return None
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout)) as client:
            resp = client.get(f"{_BCE_CONSOLE}{path}", headers=auth.headers())
    except httpx.HTTPError as exc:
        log.debug("dumate bce GET %s failed: %s", path, type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not data.get("success", True):
        return None
    return data


def fetch_quota_overview(timeout: float = 20.0) -> dict[str, Any] | None:
    """查积分余额（quota_overview，bceConsole 通道，数字面板同口径）。

    实测（2026-10-05）桌面端抓包：App 的「积分」面板走
    ``GET /api/dumate/points/quota_overview?timezone=...&clientType=...&ignoreLoginBonus=true``
    （**下划线**版；camelCase ``quotaOverview`` 是另一个端点、怎么调都是 500）。
    返回 dict 含 ``total_points`` / ``used_points`` / ``remaining_points`` /
    ``is_subscribed`` / ``packages``（incremental 包列表）等；未登录 / 失败 None。
    """
    data = _bce_get(
        "/api/dumate/points/quota_overview"
        "?timezone=Asia%2FShanghai&clientType=mac&ignoreLoginBonus=true",
        timeout=timeout,
    )
    if data is None:
        return None
    result = data.get("result") or {}
    if not result:
        return None
    try:
        total = float(result.get("totalPoints") or 0)
        used = float(result.get("usedPoints") or 0)
    except (TypeError, ValueError):
        return None
    packages = []
    for p in result.get("incremental") or []:
        try:
            packages.append({
                "package_id": p.get("packageId") or "",
                "used_points": float(p.get("usedPoints") or 0),
                "total_points": float(p.get("totalPoints") or 0),
                "start_ts": p.get("startDate"),
                "expire_ts": p.get("expireDate"),
                "source": p.get("source") or "",
                "status": p.get("status") or "",
            })
        except (TypeError, ValueError):
            continue
    return {
        "total_points": total,
        "used_points": used,
        "remaining_points": max(total - used, 0.0),
        "is_subscribed": bool(result.get("isSubscribed")),
        "packages": packages,
        "throttled": bool((result.get("modelThrottleInfo") or {}).get("throttled")),
    }


def claim_checkin() -> dict[str, Any] | None:
    """领取今日签到；成功返回状态 dict，失败 / 未登录返回 None。"""
    auth = resolve_bceconsole_auth()
    if auth is None:
        return None
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(
                _BONUS_CLAIM, headers=auth.headers(), json={},
            )
    except httpx.HTTPError as exc:
        log.debug("dumate checkin claim failed: %s", type(exc).__name__)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not data.get("success") or not data.get("result"):
        return None
    return fetch_checkin_status() or {"checked_in": True, "claimable": False}
