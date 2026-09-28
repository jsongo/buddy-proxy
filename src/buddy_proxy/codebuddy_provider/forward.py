"""多 provider 路由入口：显式前缀 / 模型自动匹配 / 兜底通道。

所有路径统一经 :func:`observability._instrument` 记录请求指标（/ui 图表数据源）。
"""

from __future__ import annotations

import functools
import json
import pathlib
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


@functools.lru_cache(maxsize=1)
def _codebuddy_static_ids() -> frozenset[str]:
    """CodeBuddy 静态表（models_config.json）的小写 id 集合（进程内缓存）。

    刻意**不用** ``model_list.load_models_from_local_config()``：那个函数会经
    ``diagnostic()`` 调 ``get_state()``（代理未初始化时抛 503），且它在路由热路径
    上每次都要读文件 + 归一化全表。这里只读一次原始 JSON 并缓存。

    兜底：读失败时返回空集——宁可不做这层保护，也不能让路由挂掉。
    """
    try:
        config = pathlib.Path(__file__).resolve().parent.parent / "web" / "models_config.json"
        data = json.loads(config.read_text("utf-8"))
        return frozenset(
            str(m["id"]).lower() for m in data.get("models", []) if m.get("id")
        )
    except Exception:  # noqa: BLE001 - 配置读不出来不该阻断路由
        return frozenset()


def _is_codebuddy_model(model: str) -> bool:
    """该名字是否是 CodeBuddy 静态表里的模型 id。

    用途只有一个：在**别名轮**挡住「别的通道想用别名认领一个本该归 CodeBuddy
    的名字」——``auto`` 正是 CodeBuddy 的默认模型，Qoder 的档位模型也叫 ``auto``，
    不挡就会静默改道；CodeBuddy 是兜底通道、不在 ``providers`` 里，别名轮无从
    靠「有没有别的通道精确认领」判断归属，故直接查静态表。

    只按**原样**比对，不做小写化：静态表里的 id 本身就是小写，而 ``Qwen3.8-Max``
    / ``Kimi-K3`` 这类**官方显示名**虽然小写化后与静态表撞车，但它们确实是通道
    目录里真实存在的模型名（Qoder 的 display_name 就是这么写的）。把它们也挡掉
    会让「按官方文档写模型名」直接 502，代价远大于收益——真正需要防的是裸名
    ``auto`` 被别名改道，而那种输入本来就没有大小写变体。
    """
    return model in _codebuddy_static_ids()


def _resolved_model_id(provider: Any, model: str) -> str:
    """把请求里的模型名归一成**停用/时段配置使用的键名**。

    Qoder 之类的通道接受别名（显示名 ``Qwen3.8-Flash`` / 内部 key ``qfmodel``），
    两者必须落到同一把键上，否则「停用了却还能用别名调通」。通道没提供
    ``resolve_model`` 时原样返回（多数通道的 id 就是上游名）。
    """
    resolve = getattr(provider, "resolve_model", None)
    if callable(resolve):
        try:
            resolved = resolve(model)
            if isinstance(resolved, str) and resolved:
                return resolved
        except Exception:  # noqa: BLE001 - 归一失败不阻断转发
            pass
    return model


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

    # 2) 自动匹配模型 id（仅在未显式指定前缀时；前缀路由的结果不能被覆盖）
    #
    # 分两轮：**先全部按 id 精确匹配**（含剥前缀裸名），都未命中再退回别名。
    # 不能一轮搞定——多个通道可能"认识"同一个名字（qoder 的显示名 GLM-5.3 与
    # zcode 的模型名 glm-5.3 撞车），若让别名与精确匹配同等参与、按注册序先到
    # 先得，注册靠前的通道就会把别人的模型抢走。别名只能是兜底，不能是抢占。
    #
    # 别名轮（``allow_aliases=True``）额外跳过「名字就是 CodeBuddy 静态表 id」的
    # 情况：CodeBuddy 是兜底通道、不在 ``providers`` 里，别名轮无从靠「别的通道
    # 能否精确认领」判断归属。``auto`` 正是 CodeBuddy 的默认模型，而 Qoder 本地
    # 合成的档位模型（TIER_MODELS）也叫 ``auto``，不挡住就会静默改道。
    #
    # ⚠️ 只挡别名轮，不挡精确轮：精确轮是各通道按自己发布的 id 认领，trae/zcode
    # 等通道的模型名可能与静态表重合（两边都真有这个模型），让他们照常先认领，
    # 与既有路由行为一致。
    if provider is None:
        for allow_aliases in (False, True):
            if allow_aliases and _is_codebuddy_model(requested_model):
                break  # 归 CodeBuddy 静态表，别名不得认领
            for p in providers.values():
                try:
                    if p.accepts_model(requested_model, aliases=allow_aliases):
                        provider = p
                        break
                except Exception:  # noqa: BLE001 - 单个通道判断失败不该阻断路由
                    continue
            if provider is not None:
                break

    if provider is not None:
        # 非默认 provider（Trae/豆包等）：由各自 forward 决定协议支持范围。
        # Trae 已支持 anthropic 协议（/v1/messages 客户端如 Claude Code 可直连）；
        # 豆包等仅 openai 协议透传（doubao2api 只支持 OpenAI chat completions）。
        # 停用/时段键按 provider 归一后的 id 生成，与转发链路口径一致
        # （别名请求如「Qwen3.8-Flash」也要能被停用规则命中）。
        gate_model = _resolved_model_id(provider, requested_model)
        _reject_if_disabled(state, provider.id, gate_model)
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
