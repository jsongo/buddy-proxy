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

    纯本地读取，不触网：返回区域、首个账号的 token 过期时间与来源，前端据此
    提示「未登录 / 即将过期 / 正常」，并可给出重新登录的命令。多账号明细走
    ``/ui/api/qoder/accounts``。
    """
    _ensure_local(request)
    state = get_state()
    provider = getattr(state, "providers", {}).get("qoder")
    if provider is None:
        return {"enabled": False, "authenticated": False}

    try:
        from ...qoder.credentials import (
            cred_to_credential,
            list_accounts,
            load_account_cred,
            qoder_state_dir,
        )
        from ...qoder.config import REGIONS

        region = provider.region()
        info: dict[str, Any] = {
            "enabled": True,
            "region": region.key,
            "region_label": region.label,
            "regions": sorted(REGIONS),
            "endpoint": region.infer_base,
            "state_dir": str(qoder_state_dir()),
            "login_command": "buddy login qoder",
        }
    except Exception as exc:  # noqa: BLE001 - 状态面板不该因读取失败而 500
        return {"enabled": True, "authenticated": False, "error": str(exc)[:200]}

    accounts = list_accounts()
    first = (load_account_cred(accounts[0].id) if accounts else None) or {}
    first_cred = cred_to_credential(first)
    now_ms = int(time.time() * 1000)
    expires_at = first_cred.expires_at_ms
    info.update({
        "authenticated": bool(accounts),
        "uid": first_cred.uid,
        "name": first_cred.name,
        "email": first_cred.email,
        "plan": first_cred.plan,
        "source": first_cred.source,
        "expires_at_ms": expires_at or None,
        "expires_in_days": (
            round((expires_at - now_ms) / 86400000, 1) if expires_at else None
        ),
        "expired": bool(expires_at and expires_at <= now_ms),
        "has_refresh_token": bool(first_cred.refresh_token),
        "account_count": len(accounts),
    })
    return info


@app.get("/ui/api/qoder/accounts")
async def ui_qoder_accounts(request: Request):
    """qoder 各账号本地凭证/冷却状态（纯本地不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...qoder import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/qoder/accounts/order")
async def ui_qoder_accounts_order(request: Request):
    """调整 qoder 账号的 failover 顺位（管理页上移/下移按钮）。

    提交完整账号 id 顺序列表，重写 index.json 的 priority；返回重排后的账号
    状态（与 GET 同构）。quota 缓存键带 priority（``quota_epoch``），重排后旧
    额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...qoder import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...qoder import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/qoder/accounts/delete")
async def ui_qoder_accounts_delete(request: Request):
    """删除一个 qoder 账号（索引条目 + cred 文件 + 冷却标记）。

    不再使用或凭据作废的账号从轮换里摘掉——留着每轮 failover 白打一次上游。
    返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...qoder import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "qoder 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...qoder import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


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


@app.post("/ui/api/antigravity/accounts/delete")
async def ui_antigravity_accounts_delete(request: Request):
    """删除一个 antigravity 账号（索引条目 + cred 文件 + 冷却标记）。

    给「能登录但被 Google 拉黑（403 Verify your account，面板副标题显示
    疑似拉黑）」或不再使用的账号准备——留在轮换里只会每轮 failover 白打
    一次。返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...antigravity import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "antigravity 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...antigravity import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.get("/ui/api/kimi/accounts")
async def ui_kimi_accounts(request: Request):
    """kimi 各账号本地凭证/冷却状态（纯本地，不触网，不含秘密）。"""
    _ensure_local(request)
    try:
        from ...kimi import failover
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/order")
async def ui_kimi_accounts_order(request: Request):
    """调整 kimi 账号的 failover 顺位（管理页上移/下移按钮）。

    与 antigravity 同款：提交完整账号 id 顺序列表，重写 index.json 的
    priority；返回重排后的账号状态（与 GET 同构）。quota 缓存键带 priority
    （``quota_epoch``），重排后旧额度快照自动失效。
    """
    _ensure_local(request)
    try:
        from ...kimi import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 ids（账号 id 的完整顺序列表）"}})
    try:
        await asyncio.to_thread(creds.reorder_accounts, ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
    from ...kimi import failover
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/delete")
async def ui_kimi_accounts_delete(request: Request):
    """删除一个 kimi 账号（索引条目 + cred 文件 + 冷却标记）。

    refresh_token 作废/不再使用的账号从轮换里摘掉——留着每轮 failover 白打
    一次上游。返回删除后的账号状态（与 GET 同构，前端直接重渲染）。
    """
    _ensure_local(request)
    try:
        from ...kimi import credentials as creds
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    aid = body.get("id")
    if not isinstance(aid, str) or not aid.strip():
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 id（要删除的账号 id）"}})
    aid = aid.strip()
    removed = await asyncio.to_thread(creds.delete_account, aid)
    if not removed:
        raise HTTPException(status_code=404,
                            detail={"error": {"message": f"账号不存在: {aid}"}})
    from ...kimi import failover
    await asyncio.to_thread(failover.clear_cooldown, aid)
    return await asyncio.to_thread(failover.accounts_status)


@app.post("/ui/api/kimi/accounts/import")
async def ui_kimi_accounts_import(request: Request):
    """导入 kimi cli 导出的 token JSON（面板「导入账号」入口，CLI 共用）。

    body ``{"payload": <str|dict>}``：粘贴的 JSON 文本或已解析对象。同
    refresh_token 更新原账号（顺位不变），新的追加为备用号。补充信息
    （/v1/me）失败不白费导入。返回导入后的账号状态（与 GET 同构）。
    """
    _ensure_local(request)
    try:
        from ...kimi import failover, login
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "kimi 通道不可用"}})
    body = await request.json()
    payload = body.get("payload")
    if payload is None:
        raise HTTPException(status_code=400,
                            detail={"error": {"message": "缺少 payload（kimi cli 导出的 token JSON）"}})
    try:
        await asyncio.to_thread(login.import_cred_payload, payload)
    except login.LoginError as exc:
        raise HTTPException(status_code=400, detail={"error": {"message": str(exc)}})
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
