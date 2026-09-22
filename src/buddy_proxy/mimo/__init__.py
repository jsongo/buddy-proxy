"""Xiaomi MiMo Desktop provider 包。

把小米 MiMo 桌面端的登录态（或 platform API key）代理成 OpenAI 兼容
chat 接口，接入 buddy-proxy 统一路由。
"""

from .provider import MimoProvider

__all__ = ["MimoProvider"]
