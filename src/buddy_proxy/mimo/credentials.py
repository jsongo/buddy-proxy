"""MiMo 凭据解析：API key 模式 与 SSO 模式。

优先级：

1. **API key**（platform.xiaomimimo.com 开的 key）
   - 环境变量 ``MIMO_API_KEY``（配 ``MIMO_BASE_URL`` 可切 billing/token-plan）
   - ``~/.mimocode/auth.json`` 里 ``xiaomi.type == "api"`` 的 ``key`` +
     ``metadata.base_url``（MiMo 桌面「API Key」模式会写这份）
   - 状态文件 ``~/.buddy-proxy/mimo_api_key.json``
2. **SSO**（复用 MiMo 桌面的小米账号登录态，免 key）
   - cookie 库里的 ``passToken``/``userId``/``cUserId``
   - 现场两阶段换 ``mimopc`` serviceToken
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from buddy_proxy.core.paths import state_file

from .config import (
    BILLING_API_BASE,
    X_SOURCE_KEY,
    X_SOURCE_SSO,
    CHAT_UA,
    key_chat_url,
    mimocode_auth_path,
    sso_chat_url,
)
from .sso import (
    AccountCookies,
    ServiceToken,
    ensure_service_token,
    load_account_cookies,
)

log = logging.getLogger(__name__)


class AuthError(RuntimeError):
    """凭据缺失或换票失败。"""


@dataclass
class ResolvedUpstream:
    """解析出的上游调用参数。"""

    mode: str  # "key" | "sso"
    chat_url: str
    headers: dict[str, str]
    api_key: str = ""
    base_url: str = ""
    account: AccountCookies | None = None
    service_token: ServiceToken | None = None

    def describe(self) -> str:
        """脱敏描述（只进日志，不打 token 明文）。"""
        if self.mode == "key":
            masked = f"{self.api_key[:4]}…{self.api_key[-2:]}" if self.api_key else ""
            return f"key={masked} base={self.base_url}"
        uid = self.account.user_id if self.account else "?"
        return f"sso userId={uid}"


# ---------------------------------------------------------------------------
# API key 来源
# ---------------------------------------------------------------------------


def _load_mimocode_auth() -> tuple[str, str]:
    """读 MiMo 自己的 auth.json（API key 模式）。"""
    path = mimocode_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    node = (data or {}).get("xiaomi")
    if not isinstance(node, dict) or node.get("type") != "api":
        return "", ""
    key = (node.get("key") or "").strip()
    meta = node.get("metadata") or {}
    base = (meta.get("base_url") or BILLING_API_BASE).strip()
    return key, base.rstrip("/")


def _load_state_key() -> tuple[str, str]:
    path = state_file("mimo_api_key.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    key = (data.get("api_key") or "").strip()
    base = (data.get("base_url") or BILLING_API_BASE).strip()
    return key, base.rstrip("/")


def resolve_api_key() -> tuple[str, str]:
    """解析 ``(api_key, base_url)``；都没有时返回 ``("", "")``。"""
    key = os.environ.get("MIMO_API_KEY", "").strip()
    base = os.environ.get("MIMO_BASE_URL", "").strip().rstrip("/")
    if key:
        return key, base or BILLING_API_BASE
    key, file_base = _load_mimocode_auth()
    if key:
        return key, base or file_base or BILLING_API_BASE
    key, file_base = _load_state_key()
    if key:
        return key, base or file_base or BILLING_API_BASE
    return "", ""


# ---------------------------------------------------------------------------
# 统一解析
# ---------------------------------------------------------------------------


async def resolve_upstream(force_refresh: bool = False) -> ResolvedUpstream:
    """解析出可直接调用的上游参数；都不可用时抛 ``AuthError``。"""
    key, base = resolve_api_key()
    if key:
        return ResolvedUpstream(
            mode="key",
            chat_url=key_chat_url(base),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
                "X-Mimo-Source": X_SOURCE_KEY,
                "User-Agent": CHAT_UA,
            },
            api_key=key,
            base_url=base,
        )

    account = load_account_cookies()
    if account is None:
        raise AuthError(
            "mimo 未配置认证：请设置 MIMO_API_KEY / MIMO_BASE_URL，"
            "或在 MiMo Desktop 登录小米账号（本 provider 会读取其 cookie）"
        )
    try:
        token = await ensure_service_token(force=force_refresh)
    except Exception as exc:  # noqa: BLE001 — 统一转成 AuthError 给上层
        raise AuthError(f"小米 SSO 换票失败: {exc}") from exc

    return ResolvedUpstream(
        mode="sso",
        chat_url=sso_chat_url(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cookie": token.cookie_header(account),
            "X-Mimo-Source": X_SOURCE_SSO,
            "User-Agent": CHAT_UA,
        },
        account=account,
        service_token=token,
    )
