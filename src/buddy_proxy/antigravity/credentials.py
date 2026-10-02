"""Antigravity OAuth 凭证：存储、刷新、上游账户信息。

凭证落盘 ``~/.buddy-proxy/antigravity_oauth.json``（0600），格式：

    {
      "access_token": "...",
      "refresh_token": "...",
      "expiry": "2026-10-03T12:00:00+00:00",   # ISO，UTC
      "token_type": "Bearer",
      "scope": "...",
      "email": "you@gmail.com",
      "project_id": "...",                      # onboardUser 分配的托管项目
      "tier": "free-tier",
      "saved_at": 1730000000
    }

OAuth 三方与 Antigravity 客户端一致（安装型应用公开 client）：
client ``1071006060591-tmhssin2h21lcre235vtolojh4g403ep``，scopes 比 gemini
多 ``cclog`` + ``experimentsandconfigs`` 共 5 个。refresh_token 不轮换
（Google 对安装型应用默认如此），长期持有即可。

agy CLI 的登录态在系统 keyring（黑盒、跨应用读取要弹权限框），不做互通，
独立走 OAuth。
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import pathlib
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from buddy_proxy.core.paths import state_file

log = logging.getLogger(__name__)

CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]

#: 提前这个量刷新 access_token，避免临界点请求带着过期票出门。
_EXPIRY_SKEW = timedelta(minutes=2)


def cred_path() -> "pathlib.Path":
    """凭证文件路径（``ANTIGRAVITY_OAUTH_JSON`` 可覆盖）。"""
    env = os.environ.get("ANTIGRAVITY_OAUTH_JSON", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_file("antigravity_oauth.json")


def save_cred(cred: dict[str, Any]) -> "pathlib.Path":
    """写凭证（0600；先建临时文件再原子替换，与 gemini/save_cred 同款）。"""
    path = cred_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(cred)
    payload.setdefault("saved_at", int(time.time()))
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def load_cred() -> dict[str, Any] | None:
    """读凭证；没有/损坏/缺 refresh_token 返回 None。"""
    try:
        data = json.loads(cred_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("refresh_token") or "").strip():
        return None
    return data


def has_cred() -> bool:
    return load_cred() is not None


class AuthError(RuntimeError):
    """刷新失败（网络/被拒/refresh_token 作废）。"""


def _token_request(data: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - urllib 报错种类多，统一转 AuthError
        raise AuthError(f"Google token 接口请求失败: {exc}") from exc
    if "error" in payload and "access_token" not in payload:
        raise AuthError(f"Google token 接口拒绝: {payload.get('error')}")
    return payload


def _expiry_from(expires_in: Any) -> str:
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        seconds = 3600.0
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def refresh_cred(cred: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """用 refresh_token 换新 access_token，更新并落盘。

    返回更新后的 cred（原 dict 就地更新）。刷新失败抛 :class:`AuthError`。
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": cred["refresh_token"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    payload = _token_request(data, timeout=timeout)
    cred["access_token"] = payload["access_token"]
    cred["expiry"] = _expiry_from(payload.get("expires_in"))
    cred["token_type"] = payload.get("token_type") or "Bearer"
    if payload.get("scope"):
        cred["scope"] = payload["scope"]
    # Google 偶尔会滚动新的 refresh_token；没给就保留旧的（常态）。
    if payload.get("refresh_token"):
        cred["refresh_token"] = payload["refresh_token"]
    save_cred(cred)
    return cred


def _expiry_dt(cred: dict[str, Any]) -> datetime | None:
    raw = str(cred.get("expiry") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def access_token_valid(cred: dict[str, Any]) -> bool:
    """access_token 是否还在有效期内（含 2 分钟提前量）。"""
    exp = _expiry_dt(cred)
    if exp is None:
        return False
    return datetime.now(timezone.utc) < exp - _EXPIRY_SKEW


def ensure_access_token(cred: dict[str, Any] | None = None) -> str:
    """拿一个可用的 access_token：内存有效直接用，否则刷新。"""
    cred = cred or load_cred()
    if cred is None:
        raise AuthError("antigravity 未登录：请先运行 `buddy login antigravity`")
    if not access_token_valid(cred):
        refresh_cred(cred)
    return cred["access_token"]


def fetch_user_email(access_token: str, timeout: float = 15.0) -> str:
    """拉账号邮箱（展示用；失败返回空串，不阻断登录）。"""
    req = urllib.request.Request(
        USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - 展示字段，拿不到就算了
        return ""
    return str(data.get("email") or "")
