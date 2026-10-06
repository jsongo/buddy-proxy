"""Trae 签到 / 积分 / 权益用量上游 API（UG 接口，按区域取址）。

两区端点差异（2026-10-05 无凭据实测，判据见 ``config.TraeRegion`` 注释）：
- 额度 ``ide_user_ent_usage``：CN 在 ``api.trae.cn`` 的 **v2**；海外在
  ``growsg-normal.trae.ai`` 的 **v1**（v2 在海外是 TLB 404）。同一接口靠响应
  里的 ``is_dollar_usage_billing`` flag 区分计费口径（CN 积分 / 海外美元）。
- 签到 ``checkin_credits/{status,claim}``：**只有 CN 有**。海外在 grow-normal /
  growsg-normal / api.trae.ai 三处全部回应用级 404，所以海外账号不发请求、
  直接返回「无签到」标记（provider 层 ``supports_checkin=False`` 已挡一层）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .config import resolve_trae_region
from .credentials import _auth

log = logging.getLogger(__name__)

# ───────────────────────── 签到 / 积分 ─────────────────────────

# CN 默认 UG host（向后兼容的模块常量；运行时一律经 resolve_trae_region 取址）。
_UG_API_HOST = "https://api.trae.cn"
# 签到 API 是 device 维度的：device_id 从 JWT 里的稳定 userId 派生（trae2api-cn 方案）
_CHECKIN_DEVICE_IDS: dict[str, str] = {}


def _checkin_identity(token: str, account_id: str = "") -> str:
    """从 JWT 提取稳定 identity（不依赖可能刷新的 token 本身）。"""
    if token:
        try:
            parts = token.split(".")
            if len(parts) >= 2:
                import base64 as _b64

                encoded = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(_b64.urlsafe_b64decode(encoded.encode("ascii")))
                data = payload.get("data")
                if isinstance(data, dict) and data.get("id"):
                    return str(data["id"])
                for key in ("user_id", "userId", "sub"):
                    if payload.get(key):
                        return str(payload[key])
        except Exception:
            pass
    if account_id:
        return str(account_id)
    return token


def checkin_device_id(token: str, account_id: str = "") -> str:
    """返回账号绑定的 16 位稳定 device id（签到 API 需要）。"""
    identity = _checkin_identity(token, account_id)
    if not identity:
        return ""
    cache_key = f"checkin#{identity}"
    if cache_key in _CHECKIN_DEVICE_IDS:
        return _CHECKIN_DEVICE_IDS[cache_key]
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    did = str(int(digest, 16) % 10**16).zfill(16)
    _CHECKIN_DEVICE_IDS[cache_key] = did
    return did


def _build_checkin_headers(token: str, account_id: str = "",
                           region: str | None = None) -> dict[str, str]:
    """UG 接口请求头。``package-type`` 按区域取（CN ``stable_cn`` / 海外
    ``stable_i18n``）——上游按它分流版本能力，填错区会被当异常客户端。"""
    reg = resolve_trae_region(region)
    headers = {
        "Authorization": f"Cloud-IDE-JWT {token}",
        "Content-Type": "application/json",
        "x-device-id": checkin_device_id(token, account_id),
        "x-device-brand": "ASUS TUF Gaming A15 FA507RM_FA507RM",
        "x-device-type": "windows",
        "package-type": reg.package_type,
    }
    return headers


def _post_ug(path: str, token: str = "", account_id: str = "",
             region: str | None = None) -> dict[str, Any]:
    """调用 Trae UG（user growth）签到/积分 API（按区域取 host）。"""
    if not token:
        token, _ = _auth()
    reg = resolve_trae_region(region)
    url = reg.ug_base + path
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers=_build_checkin_headers(token, account_id, region=reg.key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Trae UG {path} [{e.code}]: {e.read().decode()[:300]}")
    return data


#: 海外账号的签到占位结果（无签到系统，不发请求）。provider 侧据此走
#: 「inactive / 不阻塞整页」分支，避免把海外的 404 当成签到失败刷进日历。
_NO_CHECKIN: dict[str, Any] = {
    "enable": False,
    "checked_in": True,
    "message": "海外版无每日签到",
    "no_checkin": True,
}


def fetch_checkin_status(token: str = "", account_id: str = "",
                         region: str | None = None) -> dict[str, Any]:
    """查询今日签到/积分状态。海外区无签到端点，直接返回占位（不发请求）。"""
    reg = resolve_trae_region(region)
    if not reg.has_checkin:
        return dict(_NO_CHECKIN)
    # _post_ug 自己拼 ug_base，这里只给路径（签到固定 v2，仅 CN 有）。
    return _post_ug("/trae/api/v2/ug/checkin_credits/status", token, account_id,
                    region=reg.key)


def claim_checkin_credits(token: str = "", account_id: str = "",
                          region: str | None = None) -> dict[str, Any]:
    """领取今日签到积分。海外区无签到端点，直接返回占位（不发请求）。"""
    reg = resolve_trae_region(region)
    if not reg.has_checkin:
        return dict(_NO_CHECKIN)
    return _post_ug("/trae/api/v2/ug/checkin_credits/claim", token, account_id,
                    region=reg.key)


def fetch_ent_usage(token: str = "", account_id: str = "",
                    region: str | None = None) -> dict[str, Any]:
    """查询权益/额度用量（ide_user_ent_usage：总额度 + 权益包列表）。

    按区域选 ``ug_base`` 与接口版本（CN v2 / 海外 v1），并设 ``X-User-Region``
    头。计费口径分叉（积分 vs 美元）由**调用方**读响应里的
    ``is_dollar_usage_billing`` 决定，本函数只负责取回原始结构。
    """
    if not token:
        token, _ = _auth()
    reg = resolve_trae_region(region)
    headers = _build_checkin_headers(token, account_id, region=reg.key)
    headers["X-User-Region"] = "CN" if reg.key == "cn" else "GLOBAL"
    req = urllib.request.Request(
        reg.usage_url(),
        data=b"{}", headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Trae usage [{e.code}]: {e.read().decode()[:300]}")

