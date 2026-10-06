"""GLM 官方 (BigModel Coding Plan) provider —— ``glm`` 渠道。

上游与 :mod:`.zcode` 完全同源（智谱 BigModel 的 Anthropic 兼容 coding 端点
``https://open.bigmodel.cn/api/anthropic``，协议/模型表/额度端点全部继承），
所以直接做 ``ZcodeProvider`` 子类——转发直通、SSE 泵、错误透传零复制。

与 zcode 的唯一差异是**凭据来源独立**：

- zcode 的兜底链末级读本机 ZCode CLI 的 ``~/.zcode/v2/config.json``——凭据
  生命周期绑在 CLI 登录态上（CLI 里重登/换号，通道凭据跟着变）；
- glm 是用户直接在智谱控制台买的套餐 key，只认自己的两处：
  1. 环境变量 ``GLM_API_KEY``
  2. 本项目 key 文件 ``~/.buddy-proxy/glm_api_key``（首行裸 key，0600）

  绝不读 ``~/.zcode``——两渠道 key 各自独立计费套餐，串了会把 A 套餐的
  用量算到 B 头上，额度卡也就对不上了。

为什么独立成渠道而不并进 zcode：两条 key 对应两个独立购买的套餐
（额度、有效期、可用模型都可能不同），管理页各一张额度卡、路由上
``glm/<模型>`` 前缀可显式指定走官方 key，与 zcode 互不干扰。

套餐权限（2026-10-06 实测，lite 档新 key）：``glm-5.3`` / ``glm-5.3-flash`` /
``glm-5-turbo`` 均 200；``glm-5.3-flashx`` 仍 ``429 code 1311「当前订阅套餐
暂未开放GLM-5.3-FlashX权限」``——与 zcode 的 2026-09-19 实测一致。模型表
保留 flashx 作**预备接入**：套餐升级后无需改代码即可使用。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from .base import BaseProvider  # noqa: F401  -- 仅为类型提示可读性
from .zcode import BIGMODEL_ANTHROPIC_BASE, ZcodeProvider, _load_secret_file_impl
from ..core.paths import state_file


def secret_file_path() -> Path:
    """本渠道自己的 key 文件：``~/.buddy-proxy/glm_api_key``。

    与 zcode 的 ``zcode_api_key`` 同目录不同名——两渠道 key 独立落盘，
    覆盖/轮换互不影响。可用 ``BUDDY_PROXY_STATE_DIR`` 整体挪走。
    """
    return state_file("glm_api_key")


def resolve_credentials() -> tuple[str, str]:
    """解析 (api_key, anthropic_base_url)。只认 env 与自有 key 文件。

    与 :func:`zcode.resolve_credentials` 的差异就在这：没有第三级
    ``~/.zcode`` CLI 兜底（见模块 docstring——两渠道套餐独立，不串）。
    """
    key = os.environ.get("GLM_API_KEY", "").strip()
    if not key:
        key = _load_secret_file_impl(secret_file_path())
    return key, BIGMODEL_ANTHROPIC_BASE


class GlmProvider(ZcodeProvider):
    id = "glm"
    name = "GLM 官方 (BigModel Coding Plan)"

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        # 绕开父类 __init__ 的三级凭据链：glm 只走自己的 resolve_credentials
        # （父类实现会掉进 ~/.zcode CLI 配置，见模块 docstring）。
        if api_key or base_url:
            self._api_key = api_key or ""
            self._base = (base_url or BIGMODEL_ANTHROPIC_BASE).rstrip("/")
        else:
            key, base = resolve_credentials()
            self._api_key = key
            self._base = base
        self._client = None

    def ensure_auth(self) -> None:
        if not self._api_key:
            self._api_key, self._base = resolve_credentials()
        if not self._api_key:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": (
                            "glm 未配置认证：请设置 GLM_API_KEY 或 "
                            "~/.buddy-proxy/glm_api_key（智谱控制台签发的 "
                            "coding-plan API key，bigmodel.cn → API Keys）"
                        ),
                        "type": "authentication_error",
                    }
                },
            )

    def quota(self) -> dict[str, Any]:
        """额度查询（继承父类端点），仅补一道 glm 自己的 key 校验。

        父类实现在 ``self._api_key`` 为空时会 fallback 到 *zcode* 的
        凭据链——对 glm 渠道那意味着可能拿 zcode 的 key 查出 zcode
        套餐的额度、渲染进 glm 的额度卡。这里凭据未就绪就直接报 glm
        文案，绝不借别家 key。
        """
        if not self._api_key:
            raise RuntimeError("glm 未配置 API key，无法查询额度")
        return super().quota()


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.providers.glm
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    from .zcode import _auth_headers

    import httpx

    key, base = resolve_credentials()
    if not key:
        print("no api key found (env GLM_API_KEY / ~/.buddy-proxy/glm_api_key)")
        raise SystemExit(1)
    print(f"key: {key[:6]}***{key[-4:]}  base: {base}")
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{base}/v1/messages",
            headers=_auth_headers(key),
            json={
                "model": "glm-5.3-flash",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            },
        )
        print(f"status: {r.status_code}")
        print(r.text[:600])
