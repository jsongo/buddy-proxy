"""请求指纹：对齐 Antigravity 客户端的请求头形态。

antigravity-cli（agy，Go）不开源，指纹以社区验证过的 antigravity-claude-proxy /
CLIProxyAPI / gcli2api 的形态为基准（JSON REST 已被服务端大量用户验证接受），
版本号取值策略同参考实现：

1. 环境变量覆盖；
2. 本机安装的 Antigravity.app 的 product.json（``ideVersion`` 进 User-Agent、
   ``version`` 进 X-Client-Version——后者是 API 版本门）；
3. 写死 fallback（UA 2.0.3 / X-Client-Version 1.110.0，参考实现同款）。

与 gemini 通道的关键差异：antigravity 多 ``X-Client-Name`` / ``X-Client-Version``
头，且 ``x-goog-api-client`` 模拟 Node 客户端环境（gl-node + fire + grpc）。
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

#: product.json 读不到时的兜底（antigravity-claude-proxy 同款值，已验证可用）。
FALLBACK_UA_VERSION = "2.0.3"
FALLBACK_CLIENT_VERSION = "1.110.0"

#: 模拟 Google Node.js 客户端环境的 x-goog-api-client（参考实现写死值）。
GOOG_API_CLIENT = "gl-node/18.18.2 fire/0.8.6 grpc/1.10.x"

_UA_VERSION: str | None = None
_CLIENT_VERSION: str | None = None


def _product_json_paths() -> list[Path]:
    app = Path("/Applications/Antigravity.app/Contents/Resources/app/product.json")
    user_app = Path.home() / "Applications/Antigravity.app/Contents/Resources/app/product.json"
    return [app, user_app]


def _detect_versions() -> tuple[str, str]:
    """(UA 版本, X-Client-Version)：env > 本机 product.json > fallback。"""
    ua = os.environ.get("ANTIGRAVITY_UA_VERSION", "").strip()
    client = os.environ.get("ANTIGRAVITY_CLIENT_VERSION", "").strip()
    if not (ua and client):
        try:
            for path in _product_json_paths():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue
                ua = ua or str(data.get("ideVersion") or "").strip()
                client = client or str(data.get("version") or "").strip()
                if ua and client:
                    break
        except (OSError, json.JSONDecodeError):
            pass
    return ua or FALLBACK_UA_VERSION, client or FALLBACK_CLIENT_VERSION


def ua_version() -> str:
    global _UA_VERSION
    if _UA_VERSION is None:
        _UA_VERSION = _detect_versions()[0]
    return _UA_VERSION


def client_version() -> str:
    global _CLIENT_VERSION
    if _CLIENT_VERSION is None:
        _CLIENT_VERSION = _detect_versions()[1]
    return _CLIENT_VERSION


def user_agent(model: str) -> str:
    """``antigravity/<version> <os>/<arch>[ <model>]``（参考实现同款格式）。"""
    plat = platform.system().lower()  # darwin / linux / windows
    if plat == "windows":
        plat = "win32"
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    out = f"antigravity/{ua_version()} {plat}/{arch}"
    model = (model or "").strip()
    if model:
        out += f" {model}"
    return out


def auth_headers(access_token: str, model: str = "", stream: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": user_agent(model),
        "X-Client-Name": "antigravity",
        "X-Client-Version": client_version(),
        "x-goog-api-client": GOOG_API_CLIENT,
    }
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def metadata_headers() -> dict[str, str]:
    """loadCodeAssist / onboardUser 等管理接口请求头（与对话请求同款）。"""
    return auth_headers(access_token="")
