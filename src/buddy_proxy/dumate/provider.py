"""百度搭子（DuMate）provider。

直连本机 DuMate.app 内置的本地 OpenAI 兼容代理（见 ``dumate/discovery.py``），
把请求原样透传给上游 ``dumate-svc.baidu.com`` 网关，无需云端 token。

协议与路由（与 zcode/doubao provider 的多协议约定一致）：
- openai（/v1/chat/completions）→ 透传本地代理，SSE/JSON 原样回传；
- responses（/v1/responses）→ 经通用链路（responses_adapter 转成 chat 后
  落到 openai 路径）；
- anthropic（/v1/messages）→ **不支持**。DuMate 网关只认 OpenAI chat 形态，
  若在这里把 anthropic 直通会拿到上游「invalid_model / api not registered」。

模型：上游无 /v1/models 列表接口，模型名从 DuMate 客户端真实流量里抓包
实测（见下方 ``_MODELS`` 的注释）。``dm-auto-model/text.L0`` 是搭子的智能路由
档，``glm-5`` / ``qwen3.5-35b-a3b`` 是具体模型，``model-text`` 是内部工具模型。

额度：本地 ``GET /api/dumate/points/remaining`` 只回
``{"hasRemainingPoints": bool}``——百度搭子的额度面板在 App 内实现、走
bceConsole 通道，没有可对外的数字余额接口。因此额度只显示「有/无」布尔态。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.errors import describe_exception
from . import discovery

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

# 上游实测可用的模型名（2026-10 抓包 DuMate 客户端真实流量确认）。
# id 一律小写，与全站 models_config.json 命名一致，便于 forward_chat 的
# 「自动匹配 provider.models()」命中本通道，不至于让请求漏到 codebuddy 兜底。
# - dm-auto-model/text.L0：搭子「自动」智能路由档（按请求自动选底层模型），
#   是 App 默认对话模型；对应客户端配置里的 model-text / QianfanPersonalQuota。
# - glm-5 / qwen3.5-35b-a3b：直连具体模型（抓包确认网关接受这些 model 字段）。
# - model-artifact-validate：产物校验工具模型（DuMate opencode.json 内置）。
#
# 注意：id 里**故意保留** ``/``（如 dm-auto-model/text.L0）。BaseProvider
# 约定 provider id 不含 ``/``，但**模型名**不受此限；客户端请求时用
# ``dumate/dm-auto-model/text.L0`` 或让 model_order 解析，剥掉 ``dumate/``
# 前缀后剩下的 ``dm-auto-model/text.L0`` 会原样透传给上游。
_MODELS: dict[str, dict[str, Any]] = {
    "dm-auto-model/text.L0": {
        "name": "百度搭子自动（智能路由）",
        "desc": "DuMate 自动档：按请求智能路由底层模型，App 默认对话模型",
        "reasoning": True,
        "tool_call": True,
    },
    # 2026-10-05 实测确认可路由（抓包 + 逐个试）：deepseek-v4-flash 在网关模型
    # 表里但 401（需内部授权，本账号未开通），deepseek-v4.1 / glm-5.3 / kimi-k2.7
    # 等新款均 invalid_model——DuMate 当前模型池就是 GLM + Qwen + Kimi 这批。
    # 同档模型只保留最新款（用户 2026-10-05：有新模型旧模型就不配上）。
    "kimi-k3": {
        "name": "Kimi K3",
        "desc": "Moonshot Kimi K3（DuMate 直连）",
        "reasoning": True,
        "tool_call": True,
    },
    "qwen3.8-max": {
        "name": "Qwen3.8-Max",
        "desc": "通义千问 3.8 Max（DuMate 直连）",
        "reasoning": True,
        "tool_call": True,
    },
}


class DumateProvider(BaseProvider):
    id = "dumate"
    name = "百度搭子 (DuMate 本地代理)"
    supports_checkin = True  # 每日签到（bceConsole 通道，自动打卡循环消费）

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        # 上次成功发现/探测的端点（health 展示用，不做强缓存——key 每次
        # App 重启都轮换，缓存了反而会拿旧 key 打新进程）。
        self._last_ep: discovery.DumateEndpoint | None = None

    # ------------------------------------------------------------------
    # BaseProvider 接口
    # ------------------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "name": spec["name"],
                "description": spec["desc"],
                "reasoning": spec["reasoning"],
                "tool_call": spec.get("tool_call", False),
                # 上游 opencode.json 标注的上下文/输出上限（192k / 128k）
                "max_input": 192000,
                "max_output": 128000,
            }
            for mid, spec in _MODELS.items()
        ]

    def ensure_auth(self) -> None:
        """确保 DuMate 本地代理可用；失败抛 401/503 带可操作提示。

        与 doubao 一样，这里做**软检查**（只发现、不发请求），真正的连通性由
        ``forward`` 发首个字节前的探测确认，避免在同步 ensure_auth 里阻塞。
        """
        ep = discovery.discover()
        if ep is None:
            state = discovery.describe_state()
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": (
                            "dumate 通道不可用：" + (state.get("hint") or "未找到运行中的 DuMate 本地代理")
                        ),
                        "type": "authentication_error",
                        "hint": state.get("hint", ""),
                        "installed": state.get("installed", False),
                    }
                },
            )
        self._last_ep = ep

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        if protocol == "anthropic":
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": (
                            "dumate 通道暂不支持 Anthropic /v1/messages 协议"
                            "（DuMate 网关只提供 OpenAI chat completions）。"
                            "请改用 /v1/chat/completions 客户端。"
                        ),
                        "type": "unsupported_protocol",
                    }
                },
            )

        ep = discovery.discover()
        if ep is None:
            # App 可能刚被关掉：清掉残留端点，给客户端可操作提示
            self._last_ep = None
            state = discovery.describe_state()
            raise HTTPException(
                status_code=503,
                detail={
                    "error": {
                        "message": "dumate 本地代理不可达：" + (state.get("hint") or "DuMate 未运行"),
                        "type": "upstream_unavailable",
                    }
                },
            )
        self._last_ep = ep

        stream = bool(body.get("stream", False))
        # 剥掉下划线开头字段（内部保留键），模型名透传（上游接受 glm-5 /
        # qwen3.5-35b-a3b / dm-auto-model/text.L0 等，见 _MODELS 注释）
        upstream_body = {k: v for k, v in body.items() if not k.startswith("_")}
        headers = {
            **ep.headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

        client = await self._get_client()
        try:
            resp = await client.post(ep.chat_url(), json=upstream_body, headers=headers)
        except httpx.TimeoutException as exc:
            log.warning("dumate upstream timeout: %s", describe_exception(exc))
            raise HTTPException(status_code=504, detail={
                "error": {"message": "dumate upstream timeout", "type": "timeout"}
            }) from exc
        except httpx.HTTPError as exc:
            log.warning("dumate upstream error: %s", describe_exception(exc))
            raise HTTPException(status_code=502, detail={
                "error": {"message": "dumate upstream error", "type": "bad_gateway"}
            }) from exc

        if resp.status_code >= 400:
            if stream:
                # 先把错误体读完再关流：aclose() 会丢弃未读 body，之后拿不到上游
                # 真实错误（复用 zcode 同套防坑；dumate 上游是同一类 Go 网关）。
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            return StreamingResponse(
                _pass_through_stream(resp),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail={
                "error": {"message": "dumate upstream returned non-JSON", "type": "bad_gateway"}
            }) from exc
        return JSONResponse(content=payload)

    def health(self) -> dict[str, Any]:
        state = discovery.describe_state()
        out: dict[str, Any] = {"id": self.id, "name": self.name, **state}
        if self._last_ep is not None and state.get("ready"):
            out["base_url"] = self._last_ep.base_url
        return out

    # ------------------------------------------------------------------
    # 额度：/ui 管理页消费（同步 httpx，调用方经 asyncio.to_thread 包装）
    # ------------------------------------------------------------------

    def quota(self) -> dict[str, Any] | None:
        """查 DuMate 额度。

        本地 ``/api/dumate/points/remaining`` 只回 ``{"hasRemainingPoints": bool}``，
        没有数字余额（数字面板在 App 内、走 bceConsole 通道，无对外接口）。因此
        这里把布尔态翻译成管理页的额度条目：有额度 → 满额 1/1，无 → 0/1。
        ``sum_items`` 不置位（单条目），``unit="count"`` 触发到期量过滤免刷屏。
        """
        ep = discovery.discover()
        if ep is None:
            raise RuntimeError("dumate 本地代理不可达（DuMate 未运行）")
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(ep.points_url(), headers=ep.headers())
        except httpx.HTTPError as exc:
            raise RuntimeError(f"dumate 额度查询失败: {describe_exception(exc)}") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"dumate 额度查询 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError("dumate 额度返回非 JSON") from exc

        has = bool(data.get("hasRemainingPoints"))
        return {
            "items": [
                {
                    "label": "可用额度",
                    "used": 0 if has else 1,
                    "total": 1,
                    "remaining": 1 if has else 0,
                    "percent": 0.0 if has else 100.0,
                    # 布尔态额度：没有「周期性重置」概念，也不做到期告警
                    "reset_ts": None,
                    "expire_ts": None,
                    "unit": "count",
                }
            ],
            "level": "百度搭子",
        }

    # ------------------------------------------------------------------
    # 签到（/ui 自动打卡循环消费；同步 httpx，调用方经 asyncio.to_thread 包装）
    # ------------------------------------------------------------------

    def checkin_status(self) -> dict[str, Any] | None:
        """今日签到状态；未登录 / App 未装时返回 None（该通道不参与打卡）。"""
        from . import checkin as dumate_checkin

        return dumate_checkin.fetch_checkin_status()

    def checkin_claim(self) -> dict[str, Any] | None:
        from . import checkin as dumate_checkin

        return dumate_checkin.claim_checkin()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client


async def _pass_through_stream(resp: httpx.Response) -> AsyncIterator[bytes]:
    """把上游 SSE/字节流原样泵给客户端；结束后确保连接释放。"""
    try:
        async for chunk in resp.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await resp.aclose()


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)
