"""MiMo 上游端点、模型表与请求头常量。

端点从 MiMo Desktop v26.922.220226 的 app.asar 提取（2026-09-22）：

- SSO/免 key 通道：``https://mimo-server-cn.xiaomimimo.com/api/route/chat/completions``
  （注意是 ``/api/route``，不是 ``/api``；asar 里 ``_p() = hN() + "/route"``）
- API key 通道：``https://api.xiaomimimo.com/v1/chat/completions`` 或
  Token Plan 区域端点 ``https://token-plan-{cn,ams,sgp}.xiaomimimo.com/v1``
- 小米账号 SSO：``https://account.xiaomi.com/pass/serviceLogin?sid=mimopc``

模型 id 统一小写，便于 ``forward_chat`` 按 ``provider.models()`` 裸名匹配
（与 zcode 同约定）。
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 上游端点
# ---------------------------------------------------------------------------

#: SSO 通道（走小米账号 cookie，免 API key）
SSO_API_BASE = "https://mimo-server-cn.xiaomimimo.com/api"
#: asar 里的 ``_p()``：chat 等路由挂在 ``/api/route`` 下
SSO_ROUTE_PREFIX = "/route"

#: API key 计费通道（OpenAI 兼容 ``/v1``）
BILLING_API_BASE = "https://api.xiaomimimo.com/v1"

#: Token Plan 订阅通道（按区域）
TOKEN_PLAN_BASES: dict[str, str] = {
    "cn": "https://token-plan-cn.xiaomimimo.com/v1",
    "ams": "https://token-plan-ams.xiaomimimo.com/v1",
    "sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
}

#: 小米账号 serviceLogin（换取 ``mimopc`` serviceToken 的 Phase 1 入口）
SSO_SERVICE_LOGIN = "https://account.xiaomi.com/pass/serviceLogin"
#: MiMo 上游使用的 sid（从 serviceLogin 302 的 callback 参数实测）
SSO_SID = "mimopc"

# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------

#: SSO 通道的来源标识（``wrappedFetch`` 强制写入，上游可能据此分流/风控）
X_SOURCE_SSO = "mimocode-cli-free"
#: API key 通道的来源标识
X_SOURCE_KEY = "mimocode-cli"
#: SSO 换票请求的 UA（asar: ``User-Agent: MiClaw/1.0``）
SSO_UA = "MiClaw/1.0"
#: chat 请求的 UA
CHAT_UA = "mimocode/0.1.0"

# ---------------------------------------------------------------------------
# 模型表
# ---------------------------------------------------------------------------

#: 小写 id → 展示名。与桌面 ``model-catalog.json`` 的 TEXT 模型对齐；
#: ``mimo-auto``/``mimo-pro``/``mimo-flash`` 是 asar 里的别名（会被改写）。
DEFAULT_MODELS: dict[str, str] = {
    "mimo-v2.6-pro": "MiMo V2.6 Pro",
    "mimo-v2.6-flash": "MiMo V2.6 Flash",
    "mimo-pro": "MiMo Pro (别名)",
    "mimo-flash": "MiMo Flash (别名)",
    "mimo-auto": "MiMo Auto (自动路由)",
}

#: 小写 id → 上游正式模型名。``mimo-auto`` 按 asar 的 ``pN``/``l2`` 改写成
#: ``mimo-pro``（未显式偏好 flash 时）。
MODEL_NAME_CANONICAL: dict[str, str] = {
    "mimo-auto": "mimo-pro",
    "mimo-pro": "mimo-pro",
    "mimo-flash": "mimo-flash",
    "mimo-v2.6-pro": "mimo-v2.6-pro",
    "mimo-v2.6-flash": "mimo-v2.6-flash",
}


def sso_chat_url(base: str | None = None) -> str:
    """SSO 通道的 chat completions 完整 URL。"""
    root = (base or SSO_API_BASE).rstrip("/")
    return f"{root}{SSO_ROUTE_PREFIX}/chat/completions"


def key_chat_url(base: str) -> str:
    """API key 通道的 chat completions 完整 URL（base 以 ``/v1`` 结尾）。"""
    return f"{base.rstrip('/')}/chat/completions"


# ---------------------------------------------------------------------------
# 本机路径
# ---------------------------------------------------------------------------


def _default_cookie_db() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Xiaomi MiMo"
        / "Partitions"
        / "xiaomi-account"
        / "Cookies"
    )


def cookie_db_path() -> Path | None:
    """MiMo 桌面账号分区 cookie 库路径；``MIMO_COOKIE_DB`` 可覆盖。"""
    env = os.environ.get("MIMO_COOKIE_DB", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    p = _default_cookie_db()
    return p if p.exists() else None


def mimocode_auth_path() -> Path:
    """MiMo 自己的 auth.json（API key 模式）；``MIMO_AUTH_JSON`` 可覆盖。"""
    env = os.environ.get("MIMO_AUTH_JSON", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".mimocode" / "auth.json"


# ---------------------------------------------------------------------------
# 账号侧只读接口（/ui 额度 Tab 用；asar: Mse 的 gC 调用表）
# ---------------------------------------------------------------------------


def usage_url(base: str | None = None) -> str:
    """今日用量（percent/resetAt）。"""
    return f"{(base or SSO_API_BASE).rstrip('/')}/user/usage"


def subscription_url(base: str | None = None) -> str:
    """当前订阅（current/subscriptions；空表 = 未开通会员）。"""
    return f"{(base or SSO_API_BASE).rstrip('/')}/user/xiaomi/subscription/self"


def profile_url(base: str | None = None) -> str:
    """账号资料（userId/nickname），SSO 换票成功后可用来验活。"""
    return f"{(base or SSO_API_BASE).rstrip('/')}/user/xiaomi/me"
