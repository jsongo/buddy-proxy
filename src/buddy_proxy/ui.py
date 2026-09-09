"""管理 UI：/ui 页面与 /ui/api/* 管理接口。

功能：
- 按 provider 分组的模型列表，一键「设为默认启用模型」（settings.py 持久化）
- 每个模型一键测试：发一条 "hi"，返回延迟 / token / 回复预览
- 按 provider/模型维度聚合的请求统计（metrics.py），含近 14 天图表与最近请求
- provider 健康状态总览

安全约定：/ui/api/* 仅允许本机（127.0.0.1 / ::1）访问；如确需从局域网打开
管理页操作，设置环境变量 ``BUDDY_PROXY_ADMIN_OPEN=1`` 放开（自担风险）。
/v1/* 代理端点不受此限制。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from buddy_proxy.state import app, get_state, _get_state_or_none
from buddy_proxy.model_list import load_models_from_local_config
from buddy_proxy.codebuddy_provider import forward_chat
from buddy_proxy.benefits import BenefitsManager, read_checkin_settings
from buddy_proxy import settings as settings_mod

# 一键测试发送的内容与 token 上限（够穿透 thinking 模型的少量预算）
TEST_PROMPT = "hi"
TEST_MAX_TOKENS = 256
TEST_TIMEOUT_S = 120

_LOCAL_HOSTS = {"127.0.0.1", "::1", "testclient"}


def _ensure_local(request: Request) -> None:
    """管理接口仅限本机访问（防 LAN 内误触计费请求 / 篡改配置）。"""
    if os.getenv("BUDDY_PROXY_ADMIN_OPEN") == "1":
        return
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(
            status_code=403,
            detail={"error": {"message": "管理接口仅限本机访问；如需放开请设 BUDDY_PROXY_ADMIN_OPEN=1"}},
        )


def _err_text(detail: Any) -> str:
    if isinstance(detail, dict):
        detail = (detail.get("error") or {}).get("message") or detail
    return str(detail)


# ---------------------------------------------------------------------------
# 模型分组（provider -> models）
# ---------------------------------------------------------------------------

def _codebuddy_models() -> list[dict[str, Any]]:
    models = []
    for m in load_models_from_local_config():
        if m.get("provider") not in (None, "codebuddy"):
            continue  # 其它 provider 专属条目（如 traepat）由各自分组展示
        models.append({
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "vendor": m.get("vendor"),
            "credits": m.get("credits"),
            "tags": m.get("tags", []),
            "context_window": m.get("max_input"),
            "reasoning": bool(m.get("reasoning")),
        })
    return models


def _provider_models(provider: Any) -> list[dict[str, Any]]:
    models = []
    for m in provider.models():
        models.append({
            "id": m.get("id"),
            "name": m.get("description") or m.get("name") or m.get("id"),
            "vendor": provider.id,
            "credits": m.get("credits"),
            "tier": m.get("tier"),
            "tags": m.get("tags", []),
            "context_window": m.get("context_window") or m.get("max_input"),
            "reasoning": bool(m.get("reasoning")),
        })
    return models


def _model_groups(state: Any) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []

    # 默认 CodeBuddy 通道（模型来自 models_config.json）
    auth = {} if state.mock_dir is not None else (state.client.session.get("auth") or {})
    expires = int(auth.get("expiresAt") or 0)
    groups.append({
        "id": "codebuddy",
        "name": "CodeBuddy（默认通道）",
        "enabled": True,
        "health": {
            "authenticated": bool(auth.get("accessToken")),
            "token_valid": not expires or expires > int(time.time() * 1000),
        },
        "models": _codebuddy_models(),
    })

    for pid, p in getattr(state, "providers", {}).items():
        try:
            health = p.health()
        except Exception:
            health = {}
        groups.append({
            "id": pid,
            "name": p.name,
            "enabled": True,
            "health": health,
            "models": _provider_models(p),
        })
    return groups


def _validate_model(provider_id: str, model_id: str, state: Any) -> None:
    """校验 (provider, model) 组合真实可用，否则 400。"""
    for group in _model_groups(state):
        if group["id"] == provider_id:
            if any(m["id"] == model_id for m in group["models"]):
                return
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"模型 {model_id} 不在 {provider_id} 通道的模型列表中"}},
            )
    raise HTTPException(
        status_code=400,
        detail={"error": {"message": f"未知 provider: {provider_id}（未启用或不存在）"}},
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/ui/api/overview")
async def ui_overview(request: Request):
    _ensure_local(request)
    state = get_state()
    auth = {} if state.mock_dir is not None else (state.client.session.get("auth") or {})
    providers_health = {pid: p.health() for pid, p in getattr(state, "providers", {}).items()}

    default_model = getattr(state, "default_model", None)
    provider = model = None
    if default_model and "/" in default_model:
        provider, model = default_model.split("/", 1)
    elif default_model:
        model = default_model

    return {
        "uptime_seconds": int(time.time() - state.started_at),
        "authenticated": bool(auth.get("accessToken")),
        "providers": providers_health,
        "default_provider": getattr(state, "default_provider", "codebuddy"),
        "default_model": {"provider": provider, "model": model, "raw": default_model},
        "runtime": getattr(state, "runtime_info", {}),
    }


@app.get("/ui/api/models")
async def ui_models(request: Request):
    _ensure_local(request)
    state = get_state()
    groups = _model_groups(state)

    # 附加每个 (provider, model) 的请求统计
    metrics = getattr(state, "metrics", None)
    stat_map: dict[tuple[str, str], dict[str, Any]] = {}
    if metrics is not None:
        for m in metrics.snapshot(days=14)["models"]:
            stat_map[(m["provider"], m["model"])] = m

    default_model = getattr(state, "default_model", None) or ""
    disabled = getattr(state, "disabled_models", set()) or set()
    for group in groups:
        for m in group["models"]:
            st = stat_map.get((group["id"], m["id"])) or {}
            m["stats"] = {
                "count": st.get("count", 0),
                "errors": st.get("errors", 0),
                "avg_ms": st.get("avg_ms", 0),
                "last_ts": st.get("last_ts", 0),
            }
            m["is_default"] = default_model in (f"{group['id']}/{m['id']}", m["id"])
            m["disabled"] = f"{group['id']}/{m['id']}" in disabled
    return {"groups": groups, "default_model": default_model}


@app.get("/ui/api/stats")
async def ui_stats(request: Request):
    _ensure_local(request)
    state = get_state()
    metrics = getattr(state, "metrics", None)
    if metrics is None:
        return {"models": [], "daily": [], "recent": [], "summary": {}, "credits_map": {}}
    snap = metrics.snapshot(days=14)
    # 模型积分倍率，按「通道/模型」为键——同一模型跨通道倍率不同
    # （如 glm-5.3 在 codebuddy 是 x0.79、trae 是 x0.40）。CodeBuddy 走
    # models_config，其余通道取各自 models() 声明的 credits。
    snap["credits_map"] = {}
    for m in load_models_from_local_config():
        if m.get("credits"):
            snap["credits_map"][f"codebuddy/{m['id']}"] = m.get("credits")
    for p in getattr(state, "providers", {}).values():
        for m in p.models():
            if m.get("credits"):
                snap["credits_map"].setdefault(f"{p.id}/{m['id']}", m.get("credits"))
    return snap


@app.get("/ui/api/benefits")
async def ui_benefits(request: Request):
    """打卡状态 + 打卡日历 + 各通道额度（带 5 分钟缓存，避免频打上游）。"""
    _ensure_local(request)
    state = get_state()
    manager = getattr(state, "benefits", None)
    if manager is None:
        return {"providers": [], "calendar": [], "auto_checkin": False,
                "checkin_time": "09:30", "checkin_enabled_providers": []}
    return await manager.snapshot()


@app.post("/ui/api/checkin")
async def ui_checkin(request: Request):
    """立即打卡：向上游领取今日签到积分并记录历史。"""
    _ensure_local(request)
    state = get_state()
    manager = getattr(state, "benefits", None)
    if manager is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "打卡功能未初始化"}})
    body = await request.json()
    provider_id = (body.get("provider") or "").strip()
    if not provider_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 provider"}})
    return await manager.claim_now(provider_id)


@app.post("/ui/api/traepat/model-status")
async def ui_traepat_model_status(request: Request):
    """手动触发 traepat 模型负载查询（10 分钟内重复触发走缓存，避免频打上游）。"""
    _ensure_local(request)
    try:
        from .trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status)


@app.get("/ui/api/traepat/accounts")
async def ui_traepat_accounts(request: Request):
    """traepat 各账号本地凭证/冷却状态 + 后台自愈循环最近一轮结果（纯本地，不触网）。"""
    _ensure_local(request)
    try:
        from .trae.pat import accounts_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(accounts_status)


@app.post("/ui/api/traepat/refresh-tokens")
async def ui_traepat_refresh_tokens(request: Request):
    """立即补签 traepat 缺失/临期 Token；健康账号不强刷。"""
    _ensure_local(request)
    try:
        from .trae.pat import refresh_missing_tokens
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(refresh_missing_tokens)


@app.get("/ui/api/codebuddy/usage-records")
async def ui_codebuddy_usage_records(
    request: Request, days: int = 7, page: int = 1, page_size: int = 20
):
    """CodeBuddy 按请求积分消耗流水（WorkBuddy「使用记录」同源，实扣口径）。

    暂无 UI 消费方，先以管理接口形式备用（curl 即可查），参数：
    days（默认 7）、page、page_size（≤100）。
    """
    _ensure_local(request)
    state = get_state()
    provider = (getattr(state, "providers", {}) or {}).get("codebuddy")
    if provider is None:
        # 未显式启用 codebuddy 通道时回退默认实例（与 BenefitsManager 同口径）
        from buddy_proxy.codebuddy_provider import _default_codebuddy
        provider = _default_codebuddy

    def _fetch():
        end = time.strftime("%Y-%m-%d %H:%M:%S")
        start = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - days * 86400))
        return provider.usage_records(start, end, page_num=page, page_size=page_size)

    try:
        return await asyncio.to_thread(_fetch)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": {"message": str(exc)[:300]}})


# 自动打卡后台循环随应用启停（uvicorn 生命周期）。
# 启动失败只记日志，不阻断代理本身。
async def _start_benefits_loop() -> None:
    try:
        state = _get_state_or_none()
        manager = getattr(state, "benefits", None) if state else None
        if isinstance(manager, BenefitsManager):
            manager.start()
    except Exception as exc:
        print(f"[Benefits] auto checkin loop failed to start: {exc}")


async def _stop_benefits_loop() -> None:
    try:
        state = _get_state_or_none()
        manager = getattr(state, "benefits", None) if state else None
        if isinstance(manager, BenefitsManager):
            await manager.stop()
    except Exception:
        pass


app.router.on_startup.append(_start_benefits_loop)
app.router.on_shutdown.append(_stop_benefits_loop)


@app.get("/ui/api/settings")
async def ui_settings_get(request: Request):
    _ensure_local(request)
    state = get_state()
    cfg = read_checkin_settings()
    return {
        "default_model": getattr(state, "default_model", None),
        "default_provider": getattr(state, "default_provider", "codebuddy"),
        "auto_checkin": cfg["auto_checkin"],
        "checkin_time": cfg["checkin_time"],
        "path": str(settings_mod.settings_path()),
    }


@app.post("/ui/api/settings")
async def ui_settings_post(request: Request):
    _ensure_local(request)
    state = get_state()
    body = await request.json()

    update: dict[str, Any] = {}
    if "default_model" in body:
        raw = settings_mod.normalize_default_model(body.get("default_model") or "")
        if raw:
            if "/" in raw:
                provider_id, model_id = raw.split("/", 1)
                _validate_model(provider_id, model_id, state)
                # 指定了通道的默认模型顺带把兜底通道对齐（显式前缀路由优先级一致）
                update["default_provider"] = provider_id
            elif not _model_exists_anywhere(raw, state):
                raise HTTPException(
                    status_code=400,
                    detail={"error": {"message": f"裸模型 id {raw} 未命中任何已启用通道的模型列表"}},
                )
            update["default_model"] = raw
        else:
            # 清空默认模型：恢复「按客户端请求原样路由」
            update["default_model"] = ""
    if body.get("default_provider"):
        pid = body["default_provider"]
        if pid != "codebuddy" and pid not in getattr(state, "providers", {}):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"provider {pid} 未启用"}},
            )
        update["default_provider"] = pid
    if "auto_checkin" in body:
        update["auto_checkin"] = bool(body["auto_checkin"])
    if "checkin_time" in body:
        raw_time = str(body["checkin_time"] or "").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", raw_time):
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"打卡时间格式应为 HH:MM，收到 {raw_time!r}"}},
            )
        update["checkin_time"] = raw_time

    if not update:
        raise HTTPException(status_code=400, detail={"error": {"message": "没有可更新的设置字段"}})

    saved = settings_mod.save_settings(update)
    # 热更新运行态（"" 与 None 都视为未设置）
    if "default_model" in update:
        state.default_model = update["default_model"] or None
    if "default_provider" in update:
        state.default_provider = update["default_provider"]
    return {"ok": True, "settings": {k: saved.get(k) for k in ("default_model", "default_provider")}}


@app.post("/ui/api/model-toggle")
async def ui_model_toggle(request: Request):
    """停用/启用指定 (provider, model)：停用后该组合调用直接失败。

    请求体：``{"provider": "codebuddy", "model": "glm-4.7", "disabled": true}``
    未带 disabled 时按当前状态取反（切换）。持久化到 settings.json 并热更新运行态。
    """
    _ensure_local(request)
    state = get_state()
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 model"}})
    # 校验组合真实存在，避免写入无效键
    _validate_model(provider or "codebuddy", model, state)

    key = settings_mod.model_key(provider, model)
    current = getattr(state, "disabled_models", set()) or set()
    if not isinstance(current, set):
        current = set(current)
    want_disabled = bool(body["disabled"]) if "disabled" in body else key not in current

    if want_disabled:
        current.add(key)
    else:
        current.discard(key)

    state.disabled_models = current
    settings_mod.save_settings({"disabled_models": sorted(current)})
    return {"ok": True, "model": key, "disabled": want_disabled}


@app.post("/ui/api/test")
async def ui_test(request: Request):
    """一键测试：向指定 (provider, model) 发一条 "hi"，返回延迟与回复预览。"""
    _ensure_local(request)
    get_state()  # 未初始化时抛 503
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 model"}})
    prompt = (body.get("prompt") or TEST_PROMPT).strip() or TEST_PROMPT

    full_model = f"{provider}/{model}" if provider else model
    chat_body = {
        "model": full_model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "max_tokens": TEST_MAX_TOKENS,
    }

    started = time.time()
    try:
        resp = await asyncio.wait_for(forward_chat(chat_body, "openai"), timeout=TEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "latency_ms": _ms(started),
                "error": f"测试超时（>{TEST_TIMEOUT_S}s），通道可能未就绪或上游无响应"}
    except HTTPException as exc:
        return {"ok": False, "status": exc.status_code, "latency_ms": _ms(started),
                "error": _err_text(exc.detail)}
    except Exception as exc:
        return {"ok": False, "latency_ms": _ms(started), "error": str(exc)[:300]}

    latency_ms = _ms(started)
    try:
        payload = json.loads(resp.body)
    except Exception:
        return {"ok": False, "status": resp.status_code, "latency_ms": latency_ms,
                "error": "上游返回了无法解析的响应"}

    if resp.status_code >= 400 or payload.get("error"):
        err = payload.get("error")
        message = err.get("message") if isinstance(err, dict) else str(err)
        return {"ok": False, "status": resp.status_code, "latency_ms": latency_ms,
                "error": message or f"HTTP {resp.status_code}"}

    choices = payload.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    content = message.get("content")
    if isinstance(content, list):  # 兼容分块 content
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "model": payload.get("model") or model,
        "content": (content or "").strip()[:600] or "(空回复)",
        "finish_reason": (choices[0].get("finish_reason") if choices else None),
        "usage": payload.get("usage") or {},
    }


def _ms(started: float) -> int:
    return round((time.time() - started) * 1000)


@app.get("/ui", response_class=HTMLResponse)
async def ui_page():
    # no-cache：页面随代码更新，别让浏览器拿旧缓存（管理页无性能顾虑）
    return HTMLResponse(content=_PAGE_HTML, headers={"Cache-Control": "no-cache"})


@app.get("/")
async def ui_root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/ui")


# ---------------------------------------------------------------------------
# 页面（单文件、零依赖，无外链 CDN）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 页面（单文件、零依赖，无外链 CDN）：源码在 static/index.html，此处读取
# ---------------------------------------------------------------------------

_PAGE_HTML = (Path(__file__).parent / "static" / "index.html").read_text(
    encoding="utf-8")
