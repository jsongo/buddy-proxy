"""Kimi Code OAuth Device Flow（RFC 8628）纯 HTTP 层。

端点与参数来自 MoonshotAI/kimi-code 官方 CLI（``packages/oauth/src``），
2026-10-03 用真实账号实测通过：

- ``POST {oauth_host}/api/oauth/device_authorization``（form: ``client_id``）
  → ``user_code`` / ``device_code`` / ``verification_uri`` /
  ``verification_uri_complete`` / ``expires_in`` / ``interval``（默认 5s）
- 轮询 ``POST {oauth_host}/api/oauth/token``（form: ``client_id`` +
  ``device_code`` + ``grant_type=urn:ietf:params:oauth:grant-type:device_code``）：
  200 带 ``access_token`` 即成功；``error=authorization_pending`` 继续等、
  ``slow_down`` 加间隔继续等、``expired_token`` / ``access_denied`` 终止。
- 刷新走同一 token 端点（``grant_type=refresh_token``）。

access_token 寿命 15 分钟、refresh_token 30 天（实测），刷新因此是转发
主路径的常态操作（见 credentials.ensure_account_token），不是登录时的一次性
动作。

mainland-cn 与 global 两套部署共用 client_id，只差 oauth_host
（auth.kimi.com / auth.kimi.ai）；host 按账号存进 cred，混用不串。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
#: mainland-cn 的 OAuth host；global 区账号的 cred 里另存 auth.kimi.ai。
DEFAULT_OAUTH_HOST = "https://auth.kimi.com"

DEFAULT_POLL_INTERVAL_S = 5.0
#: 授权端点没给 expires_in 时的兜底轮询预算（RFC 8628 常见值 600s）。
DEFAULT_POLL_TIMEOUT_S = 600.0
#: slow_down 后的加时（RFC 8628 建议值）。
_SLOW_DOWN_STEP_S = 5.0


class OAuthError(RuntimeError):
    """Device Flow 失败（被拒/过期/HTTP 错误）。"""


@dataclass(frozen=True)
class DeviceCode:
    """device_authorization 响应（RFC 8628 §3.2）。

    ``verification_uri_complete`` 是带好 user_code 的完整链接，浏览器打开
    即见预填的授权码；``user_code`` 留作手动输入的兜底。
    """

    user_code: str
    device_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: float
    interval: float


def _post_form(
    url: str,
    params: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST form-encoded 并解析 JSON 响应；传输层失败统一抛 :class:`OAuthError`。

    HTTP 400 也照常返回 body——device flow 的 pending/denied 都以 400 +
    ``{"error": ...}`` 表达，由调用方按语义分诊（照 kimi cli 的做法）。
    """
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # 400 是 device flow 的语义状态码：pending/slow_down/denied/invalid_grant
        # 都以 400 + {"error": ...} 表达（实测真机如此；测试桩全用 200 覆盖不到）。
        # 读出 body 交调用方按语义分诊，不当传输错误抛——否则 device flow 在
        # 第一个 pending 响应上就崩。
        if exc.code == 400:
            try:
                payload = json.loads(exc.read())
            except Exception:  # noqa: BLE001 - 400 但 body 不是 JSON，按传输错误处理
                raise OAuthError(f"Kimi OAuth 请求失败（{url}）: {exc}") from exc
            return payload if isinstance(payload, dict) else {}
        raise OAuthError(f"Kimi OAuth 请求失败（{url}）: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - urllib 报错种类多，统一转 OAuthError
        raise OAuthError(f"Kimi OAuth 请求失败（{url}）: {exc}") from exc
    return payload if isinstance(payload, dict) else {}


def start_device_authorization(
    oauth_host: str = DEFAULT_OAUTH_HOST,
    *,
    device_id: str = "",
    timeout: float = 30.0,
) -> DeviceCode:
    """发起设备授权，拿到 user_code + 完整验证链接。"""
    from .upstream import device_flow_headers

    url = f"{oauth_host.rstrip('/')}/api/oauth/device_authorization"
    payload = _post_form(
        url,
        {"client_id": CLIENT_ID},
        headers=device_flow_headers(device_id),
        timeout=timeout,
    )
    missing = [
        k for k in ("user_code", "device_code", "verification_uri_complete")
        if not str(payload.get(k) or "").strip()
    ]
    if missing:
        raise OAuthError(f"Kimi device_authorization 响应缺字段: {', '.join(missing)}")
    try:
        expires_in = float(payload.get("expires_in") or DEFAULT_POLL_TIMEOUT_S)
    except (TypeError, ValueError):
        expires_in = DEFAULT_POLL_TIMEOUT_S
    try:
        interval = float(payload.get("interval") or DEFAULT_POLL_INTERVAL_S)
    except (TypeError, ValueError):
        interval = DEFAULT_POLL_INTERVAL_S
    return DeviceCode(
        user_code=str(payload["user_code"]),
        device_code=str(payload["device_code"]),
        verification_uri=str(payload.get("verification_uri") or ""),
        verification_uri_complete=str(payload["verification_uri_complete"]),
        expires_in=expires_in,
        interval=max(interval, 1.0),
    )


def poll_token(
    oauth_host: str,
    device_code: str,
    *,
    device_id: str = "",
    interval: float = DEFAULT_POLL_INTERVAL_S,
    expires_in: float = DEFAULT_POLL_TIMEOUT_S,
    on_wait: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """轮询 token 端点直到成功/超时/被拒，返回完整 token payload。

    ``on_wait(秒)`` 在每次 sleep 前回调（CLI 打印进度 tick 用）。用户拒绝
    （access_denied）、授权码过期（expired_token）、整体超时抛
    :class:`OAuthError`；Ctrl-C 直接冒泡（登录入口负责收尾文案）。
    """
    from .upstream import device_flow_headers

    url = f"{oauth_host.rstrip('/')}/api/oauth/token"
    deadline = time.monotonic() + max(expires_in, 30.0)
    wait = max(interval, 1.0)
    while True:
        payload = _post_form(
            url,
            {
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers=device_flow_headers(device_id),
        )
        if str(payload.get("access_token") or "").strip():
            return payload
        error = str(payload.get("error") or "").strip()
        if error == "authorization_pending":
            pass  # 继续等
        elif error == "slow_down":
            wait += _SLOW_DOWN_STEP_S
        elif error == "expired_token":
            raise OAuthError("授权码已过期，请重新发起登录")
        elif error == "access_denied":
            raise OAuthError("用户拒绝了授权")
        else:
            detail = str(payload.get("error_description") or payload.get("message") or error)
            raise OAuthError(f"Kimi OAuth 轮询失败: {detail or 'unknown error'}")
        if time.monotonic() > deadline:
            raise OAuthError("等待授权超时（浏览器侧一直未确认）")
        if on_wait is not None:
            on_wait(wait)
        time.sleep(wait)


def refresh_token(
    oauth_host: str,
    refresh_token: str,
    *,
    device_id: str = "",
    timeout: float = 30.0,
) -> dict[str, Any]:
    """refresh_token 换新 token payload（含 access_token / expires_in）。"""
    from .upstream import device_flow_headers

    url = f"{oauth_host.rstrip('/')}/api/oauth/token"
    payload = _post_form(
        url,
        {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers=device_flow_headers(device_id),
        timeout=timeout,
    )
    if not str(payload.get("access_token") or "").strip():
        error = str(payload.get("error") or "")
        raise OAuthError(f"Kimi token 刷新被拒: {error or '响应缺 access_token'}")
    return payload
