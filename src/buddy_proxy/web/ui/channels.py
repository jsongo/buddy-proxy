"""管理 UI：各通道专属管理接口（qoder / traepat / codebuddy）。

原 ``web/ui.py`` 拆分：通道相关的接口都在这里，后续新通道的管理面板接口
（如 antigravity 账号状态）也加到本模块。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import HTTPException, Request

from buddy_proxy.core.state import app, get_state

from .common import _ensure_local


@app.get("/ui/api/qoder/auth")
async def ui_qoder_auth(request: Request):
    """Qoder 登录/鉴权状态 + 账号信息（供管理页鉴权面板展示）。

    纯本地读取，不触网：返回区域、账号、token 过期时间与来源，前端据此
    提示「未登录 / 即将过期 / 正常」，并可给出重新登录的命令。
    """
    _ensure_local(request)
    state = get_state()
    provider = getattr(state, "providers", {}).get("qoder")
    if provider is None:
        return {"enabled": False, "authenticated": False}

    try:
        from ...qoder.credentials import auth_state_path, load_state, resolve_credential
        from ...qoder.config import REGIONS

        region = provider.region()
        info: dict[str, Any] = {
            "enabled": True,
            "region": region.key,
            "region_label": region.label,
            "regions": sorted(REGIONS),
            "endpoint": region.infer_base,
            "state_file": str(auth_state_path()),
            "login_command": "buddy login qoder",
        }
    except Exception as exc:  # noqa: BLE001 - 状态面板不该因读取失败而 500
        return {"enabled": True, "authenticated": False, "error": str(exc)[:200]}

    try:
        cred = resolve_credential(region)
    except Exception:
        cred = None

    saved = load_state()
    # 内存凭据里的 plan 是额度接口回填的，可能比状态文件新（同一进程内）。
    live_plan = getattr(cred, "plan", "") if cred else ""
    now_ms = int(time.time() * 1000)
    expires_at = (cred.expires_at_ms if cred else 0) or int(saved.get("expires_at_ms") or 0)
    info.update({
        "authenticated": cred is not None,
        "uid": (cred.uid if cred else "") or saved.get("uid") or "",
        "name": (cred.name if cred else "") or saved.get("name") or "",
        "email": (cred.email if cred else "") or saved.get("email") or "",
        "plan": live_plan or saved.get("plan") or "",
        "source": cred.source if cred else "",
        "expires_at_ms": expires_at or None,
        "expires_in_days": (
            round((expires_at - now_ms) / 86400000, 1) if expires_at else None
        ),
        "expired": bool(expires_at and expires_at <= now_ms),
        "has_refresh_token": bool(
            (cred.refresh_token if cred else "") or saved.get("refresh_token")
        ),
        "updated_at_ms": saved.get("updated_at_ms"),
    })
    return info


@app.post("/ui/api/qoder/models/refresh")
async def ui_qoder_models_refresh(request: Request):
    """从上游刷新 Qoder 模型目录（管理页「刷新模型」按钮）。"""
    _ensure_local(request)
    state = get_state()
    provider = getattr(state, "providers", {}).get("qoder")
    if provider is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道未启用"}})
    models = await provider.refresh_models(force=True)
    return {"ok": True, "count": len(models), "models": models}


@app.get("/ui/api/traepat/model-status")
async def ui_traepat_model_status_cached(request: Request):
    """读取 traepat 模型负载缓存（纯本地，不触网）；无缓存返回空壳供页面默认展示。"""
    _ensure_local(request)
    try:
        from ...trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status, False, True)


@app.post("/ui/api/traepat/model-status")
async def ui_traepat_model_status(request: Request):
    """手动触发 traepat 模型负载查询（10 分钟内重复触发走缓存，避免频打上游）。"""
    _ensure_local(request)
    try:
        from ...trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status)


@app.get("/ui/api/traepat/accounts")
async def ui_traepat_accounts(request: Request):
    """traepat 各账号本地凭证/冷却状态 + 后台自愈循环最近一轮结果（纯本地，不触网）。"""
    _ensure_local(request)
    try:
        from ...trae.pat import accounts_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(accounts_status)


@app.get("/ui/api/antigravity/accounts")
async def ui_antigravity_accounts(request: Request):
    """antigravity 各账号本地凭证/冷却状态（纯本地，不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...antigravity import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/antigravity/accounts/order")
async def ui_antigravity_accounts_order(request: Request):
    """调整 antigravity 账号的 failover 顺位（管理页上移/下移按钮）。

    提交完整的账号 id 顺序列表，重写 index.json 的 priority；返回重排后的
    账号状态（与 GET 同构，前端直接重渲染）。quota 缓存键带 priority
    （``quota_epoch``），重排后旧额度快照自动失效、下一轮刷新即换新顺位。
    """
    _ensure_local(request)
    try:
        from ...antigravity import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...antigravity import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/traepat/refresh-tokens")
async def ui_traepat_refresh_tokens(request: Request):
    """立即补签 traepat 缺失/临期 Token；健康账号不强刷。"""
    _ensure_local(request)
    try:
        from ...trae.pat import refresh_missing_tokens
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
