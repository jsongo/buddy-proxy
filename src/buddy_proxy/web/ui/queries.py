"""管理 UI：状态查询接口（总览/统计/日志/打卡与额度）。

打卡后台循环的生命周期也挂在这里（uvicorn 启停钩子，原 ``web/ui.py`` 拆分）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import HTTPException, Request

from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.state import app, get_state, _get_state_or_none
from buddy_proxy.web.model_list import load_models_from_local_config
from buddy_proxy.benefits import BenefitsManager

from .common import _ensure_local


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
        # 设置文件健康度：损坏时前端顶部弹红色条幅。load_settings 会静默吞掉
        # 语法错误（容错需要），若不显式告知，用户只会看到「设置项全没了」。
        "settings": settings_mod.settings_health(),
    }


@app.get("/ui/api/stats")
async def ui_stats(request: Request):
    _ensure_local(request)
    state = get_state()
    metrics = getattr(state, "metrics", None)
    if metrics is None:
        return {"models": [], "daily": [], "model_daily": [], "recent": [], "summary": {}, "credits_map": {}}
    snap = metrics.snapshot(days=30)

    def _credits_tag(v: Any) -> Any:
        """倍率统一成展示字符串：数字（如 qoder 的 price_factor 0.2）补「x」前缀，
        目录里本就是 "x1.83 credits" 这类字符串的原样保留。"""
        if isinstance(v, (int, float)):
            return f"x{v:g}"
        return v

    # 模型积分倍率，按「通道/模型」为键——同一模型跨通道倍率不同
    # （如 glm-5.3 在 codebuddy 是 x0.79、trae 是 x0.40）。CodeBuddy 走
    # models_config，其余通道取各自 models() 声明的 credits。
    snap["credits_map"] = {}
    for m in load_models_from_local_config():
        # `is not None` 而非真值判断：0.0 是合法倍率（免费模型），真值判断会吞掉
        if m.get("credits") is not None:
            snap["credits_map"][f"codebuddy/{m['id']}"] = m.get("credits")
    for p in getattr(state, "providers", {}).values():
        prefix = f"{p.id}/"
        for m in p.models():
            if m.get("credits") is not None:
                mid = str(m.get("id") or "")
                # models() 的 id 可能已带「provider/」前缀（如 qoder 的
                # to_openai_model），剥掉再拼键——否则键变成 qoder/qoder/x，
                # 前端按「provider/裸名」查永远落空，倍率从来不显示
                if mid.startswith(prefix):
                    mid = mid[len(prefix):]
                snap["credits_map"].setdefault(
                    settings_mod.model_key(p.id, mid), _credits_tag(m.get("credits")))
    return snap


@app.get("/ui/api/logs")
async def ui_logs(request: Request, start: str = "", end: str = "",
                  page: int = 1, page_size: int = 20,
                  provider: str = "", model: str = "", client: str = ""):
    """请求日志分页查询：按日期范围直接读 metrics.jsonl + 30 天归档（服务端分页）。

    provider/model/client：逗号分隔白名单（UI 快速筛选，组内 OR、组间 AND），空 = 不筛。
    """
    _ensure_local(request)
    state = get_state()
    metrics = getattr(state, "metrics", None)
    if metrics is None:
        return {"rows": [], "total": 0, "page": 1, "page_size": page_size,
                "pages": 1, "clients": [], "from_disk": False}
    provs = [p.strip() for p in provider.split(",") if p.strip()] or None
    mds = [m.strip() for m in model.split(",") if m.strip()] or None
    cls = [c.strip() for c in client.split(",") if c.strip()] or None
    return await asyncio.to_thread(
        metrics.query_logs, start or None, end or None, page, page_size, provs, mds, cls)


@app.get("/ui/api/benefits")
async def ui_benefits(request: Request):
    """打卡状态 + 打卡日历 + 各通道额度（带 5 分钟缓存，避免频打上游）。"""
    _ensure_local(request)
    state = get_state()
    manager = getattr(state, "benefits", None)
    if manager is None:
        return {"providers": [], "calendar": [], "auto_checkin": False,
                "checkin_time": "09:30", "checkin_enabled_providers": []}
    return await manager.snapshot(getattr(state, "disabled_providers", set()) or set())


@app.post("/ui/api/benefits/refresh")
async def ui_benefits_refresh(request: Request):
    """单通道额度刷新（管理页卡片「↻」按钮）：作废该通道缓存后重查快照。

    只作废目标通道的 quota 缓存，其余通道照走各自的缓存——按钮要的是
    「这一家的新数据」，没必要把所有通道的上游都打一遍。返回整份新快照，
    前端渲染省一次 GET。
    """
    _ensure_local(request)
    state = get_state()
    manager = getattr(state, "benefits", None)
    if manager is None:
        raise HTTPException(status_code=503, detail={"error": {"message": "额度功能未初始化"}})
    body = await request.json()
    provider_id = (body.get("provider") or "").strip()
    if not provider_id:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 provider"}})
    manager.invalidate_quota(provider_id)
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
