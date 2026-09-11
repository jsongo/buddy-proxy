"""多 provider 路由入口：显式前缀 / 模型自动匹配 / 兜底通道。

所有路径统一经 :func:`observability._instrument` 记录请求指标（/ui 图表数据源）。
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.core.state import (
    diagnostic,
    get_state,
)
from buddy_proxy.core import settings as settings_mod

from .observability import _instrument
from .provider import _default_codebuddy


def _reject_if_disabled(state: Any, provider_id: str, model_id: Any) -> None:
    """命中管理页停用或「不在可用时段」的 (provider, model) 组合时返回 403 拒绝转发。

    判定优先级：永久停用（disabled_models）> 限时窗口（model_schedules）。
    两者都未命中才放行。
    """
    if not isinstance(model_id, str):
        return
    # 键必须与配置加载/UI 保存同口径（settings.model_key）：legacy 的
    # workbuddy/* 归一成 codebuddy/*，否则 workbuddy 进来的请求查不到已保存
    # 的停用/时段配置，窗口外照样放行。
    key = settings_mod.model_key(provider_id, model_id)
    disabled = getattr(state, "disabled_models", None)
    if disabled and key in disabled:
        diagnostic("model_disabled_reject", provider=provider_id, model=model_id)
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": f"模型 {key} 已被停用，请在管理页 /ui 重新启用后再调用",
                    "type": "model_disabled",
                }
            },
        )
    schedules = getattr(state, "model_schedules", None)
    windows = schedules.get(key) if isinstance(schedules, dict) else None
    if windows is not None and not settings_mod.model_schedule_open(windows):
        diagnostic("model_scheduled_reject", provider=provider_id, model=model_id)
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "message": (f"模型 {key} 当前不在可用时段（开放："
                                f"{settings_mod.format_windows(windows)}），窗口外调用被拒绝"),
                    "type": "model_scheduled",
                }
            },
        )


async def forward_chat(
    body: dict[str, Any],
    protocol: str,
    original: dict[str, Any] | None = None,
) -> StreamingResponse | JSONResponse:
    """转发 chat 请求到上游，支持流式和非流式。

    多 provider 路由（三种方式，优先级从高到低）：
    1. 显式前缀：``model: "<provider-id>/<model-name>"``，如
       ``trae/DeepSeek-V4-Flash``、``doubao/doubao-think``、
       ``codebuddy/glm-5.3``；前缀命中已启用 provider（或 codebuddy）时，
       剥掉前缀再转发，强制走指定通道。
    2. 自动匹配：模型 id 命中某个非默认 provider 的 models() 列表
       （如豆包/Trae 模型），走该 provider 的 forward。
    3. 兜底通道：都未命中时，走 ``state.default_provider``（默认 codebuddy，
       可通过 --default-provider / PROXY_DEFAULT_PROVIDER 改为 trae 等）。

    请求未带 ``model`` 字段时，用管理页设置的默认启用模型
    （``state.default_model``，settings.py 持久化）补齐后再路由。
    所有路径统一经 :func:`_instrument` 记录请求指标（/ui 图表数据源）。
    """
    state = get_state()

    # ---- 默认模型：客户端未指定 model 时补齐 ----
    requested_model = body.get("model")
    if not requested_model:
        default_model = getattr(state, "default_model", None)
        if default_model:
            body = {**body, "model": default_model}
            requested_model = default_model

    # ---- 多 provider 路由 ----
    providers = getattr(state, "providers", {}) or {}
    provider = None

    # 1) 显式 provider 前缀：provider/model
    if isinstance(requested_model, str) and "/" in requested_model:
        prefix, real_model = requested_model.split("/", 1)
        if prefix in providers:
            # 严格分离：扩展目录模型（仅 traepat 有）写 trae/ 前缀时直接报错提示，
            # 不做静默克底；个人目录模型两边都有，trae/ 前缀仍走个人账号
            if prefix == "trae" and "traepat" in providers:
                from buddy_proxy.trae.pat import pat_gateway_is_plus
                if pat_gateway_is_plus(real_model):
                    raise HTTPException(
                        status_code=400,
                        detail=f"模型 {real_model} 属于 traepat 通道（服务账号），"
                               f"请使用 model: traepat/{real_model}")
            provider = providers[prefix]
            # 剥掉前缀后再转发（上游不认识 "trae/" 前缀）
            body = {**body, "model": real_model}
            requested_model = real_model
        elif prefix == "codebuddy":
            # 显式强制走默认 CodeBuddy 通道，跳过 provider 自动匹配与兜底
            body = {**body, "model": real_model}
            _reject_if_disabled(state, "codebuddy", real_model)
            diagnostic("provider_route", provider="codebuddy", model=real_model,
                       protocol=protocol, via="prefix")
            return await _instrument(
                state, _default_codebuddy.forward(body, protocol, original),
                provider_id="codebuddy", model_id=real_model, protocol=protocol,
                stream=bool(body.get("stream")),
            )

    # 2) 自动匹配模型 id
    if provider is None:
        for p in providers.values():
            if any(m.get("id") == requested_model for m in p.models()):
                provider = p
                break

    if provider is not None:
        # 非默认 provider（Trae/豆包等）：由各自 forward 决定协议支持范围。
        # Trae 已支持 anthropic 协议（/v1/messages 客户端如 Claude Code 可直连）；
        # 豆包等仅 openai 协议透传（doubao2api 只支持 OpenAI chat completions）。
        _reject_if_disabled(state, provider.id, requested_model)
        diagnostic("provider_route", provider=provider.id, model=requested_model, protocol=protocol)
        provider.ensure_auth()
        return await _instrument(
            state, provider.forward(body, protocol, original),
            provider_id=provider.id, model_id=requested_model, protocol=protocol,
            stream=bool(body.get("stream")),
        )

    # 3) 兜底通道：未命中任何 provider 模型列表时，按配置的默认通道转发
    default_provider_id = getattr(state, "default_provider", "codebuddy")
    if default_provider_id in providers:
        default_provider = providers[default_provider_id]
        _reject_if_disabled(state, default_provider.id, requested_model)
        diagnostic("provider_route", provider=default_provider.id,
                   model=requested_model, protocol=protocol, via="default")
        default_provider.ensure_auth()
        return await _instrument(
            state, default_provider.forward(body, protocol, original),
            provider_id=default_provider_id, model_id=requested_model, protocol=protocol,
            stream=bool(body.get("stream")),
        )

    # 默认 CodeBuddy 路径（对称封装，与其它 provider 一致）
    _reject_if_disabled(state, "codebuddy", requested_model)
    return await _instrument(
        state, _default_codebuddy.forward(body, protocol, original),
        provider_id="codebuddy", model_id=requested_model, protocol=protocol,
        stream=bool(body.get("stream")),
    )
