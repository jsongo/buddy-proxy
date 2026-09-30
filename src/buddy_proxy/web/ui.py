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

from buddy_proxy.core.state import app, get_state, _get_state_or_none
from buddy_proxy.web.model_list import load_models_from_local_config
from buddy_proxy.codebuddy_provider import forward_chat
from buddy_proxy.benefits import BenefitsManager, read_checkin_settings
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core import cooldown as cooldown_mod

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


def _bare_model_id(model_id: str, provider_id: str) -> str:
    """去掉通道前缀，返回**转发链路上真正使用的**模型名。

    模型列表里的 ``m["id"]`` 带通道前缀（``qoder/qfmodel``），而 ``forward_chat``
    解析 ``provider/model`` 后会把前缀剥掉再转发，``_reject_if_disabled`` 拿到的
    也是裸名。所有「停用/限时窗口」的键都必须按裸名生成，否则永远命中不了。
    """
    prefix = f"{provider_id}/"
    return model_id[len(prefix):] if provider_id and model_id.startswith(prefix) else model_id


def _find_model(provider_id: str, model_id: str, state: Any) -> dict[str, Any] | None:
    """在通道的模型列表里找 ``model_id``，返回条目（找不到返回 None）。

    与 :func:`_normalize_model_ref` 共用同一套「id / 裸名」两种口径的匹配，
    避免两处判断漂移。
    """
    for group in _model_groups(state):
        if group["id"] != provider_id:
            continue
        for m in group["models"]:
            if m["id"] == model_id or _bare_model_id(m["id"], provider_id) == model_id:
                return m
    return None


def _validate_model(provider_id: str, model_id: str, state: Any) -> None:
    """校验 (provider, model) 组合真实可用，否则 400。

    接受两种口径：通道列表里的**带前缀 id**（``qoder/qfmodel``，管理页回传的
    形态）与**裸模型名**（``qfmodel``，客户端实际发给代理的形态）。
    """
    for group in _model_groups(state):
        if group["id"] == provider_id:
            if _find_model(provider_id, model_id, state) is not None:
                return
            raise HTTPException(
                status_code=400,
                detail={"error": {"message": f"模型 {model_id} 不在 {provider_id} 通道的模型列表中"}},
            )
    raise HTTPException(
        status_code=400,
        detail={"error": {"message": f"未知 provider: {provider_id}（未启用或不存在）"}},
    )


def _model_exists_anywhere(model_id: str, state: Any) -> bool:
    """裸模型名能否匹配任一已启用通道。"""
    return any(
        any(m["id"] == model_id for m in group["models"])
        for group in _model_groups(state)
    )


def _normalize_model_ref(provider: str, model: str, state: Any) -> tuple[str, str]:
    """把管理页回传的模型引用归一成 ``(provider, 裸模型名)``。

    管理页的模型列表里 ``m["id"]`` 带通道前缀（``qoder/qfmodel``），前端会按
    ``provider=<通道id>`` + ``model=<带前缀id>`` 一起回传。若直接拼键就得到
    ``qoder/qoder/qfmodel``，而转发侧一律用剥前缀后的裸名（``qoder/qfmodel``）
    ——两边永远对不上，停用形同虚设。这里统一剥成裸名，保持与转发链路一致。

    裸模型名（如 ``glm-4.7``）不受影响；``provider`` 为空时按原样返回，由调用方
    补默认通道。
    """
    bare = (model or "").strip()
    pid = (provider or "").strip()
    if pid and bare.startswith(f"{pid}/"):
        bare = _bare_model_id(bare, pid)
    # 别名（显示名/大小写变体）归一成通道的规范 id，与转发链路同一口径
    if pid:
        for p in (getattr(state, "providers", {}) or {}).values():
            if getattr(p, "id", None) != pid:
                continue
            resolve = getattr(p, "resolve_model", None)
            if callable(resolve):
                try:
                    resolved = resolve(bare)
                    if isinstance(resolved, str) and resolved:
                        return pid, resolved
                except Exception:  # noqa: BLE001 - 归一失败按原名落键
                    pass
            break
    return pid, bare


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
        # 设置文件健康度：损坏时前端顶部弹红色条幅。load_settings 会静默吞掉
        # 语法错误（容错需要），若不显式告知，用户只会看到「设置项全没了」。
        "settings": settings_mod.settings_health(),
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
    schedules = getattr(state, "model_schedules", {}) or {}
    order = getattr(state, "model_order", None) or {}
    for group in groups:
        for m in group["models"]:
            # 指标也用**裸名**查：埋点记的 model_id 来自转发链路，是剥了通道前缀的
            # 裸名（qoder 目录里的 id 是 `qoder/deepseek-v4.1-flash`，记的是
            # `deepseek-v4.1-flash`）。用带前缀的 m["id"] 查会让这一列的计数几乎
            # 恒为 0——用户反馈「14d 请求数字一直很小」就是这个（实测 qoder 的
            # deepseek-v4.1-flash 真实 2234 次，表里显示 1）。
            bare = _bare_model_id(m["id"], group["id"])
            st = stat_map.get((group["id"], bare)) or {}
            m["stats"] = {
                "count": st.get("count", 0),
                "errors": st.get("errors", 0),
                "avg_ms": st.get("avg_ms", 0),
                "last_ts": st.get("last_ts", 0),
            }
            # 键必须用**剥前缀后的裸名**构造：通道目录里 m["id"] 带前缀
            # （qoder/qoder/qfmodel 形态），而转发侧一律用剥前缀的裸名
            # （_reject_if_disabled 拿到的是裸名），两处口径必须一致，否则
            # 「停用了却还能用」/「时段不生效」。
            key = settings_mod.model_key(group["id"], bare)
            m["is_default"] = default_model in (key, m["id"])
            m["disabled"] = key in disabled
            # 候选上游顺序：有配置则附目标列表 + 各目标当前冷却标记，供前端渲染徽标/编辑器
            # **裸名键优先**（UI 保存的位置，也是用户手写配置的常见形态），再退回
            # `<通道>/<裸名>`——历史上 UI 写的是那个形态，老配置得继续认得。forward
            # 侧两种键都查，这里必须同口径，否则配了却在页面上看不见。
            order_targets = order.get(bare) if isinstance(order, dict) else None
            if not order_targets and isinstance(order, dict):
                order_targets = order.get(key)
            if order_targets:
                marks = []
                for target in order_targets:
                    if "/" not in target:
                        continue
                    t_provider, t_model = target.split("/", 1)
                    left = cooldown_mod.remaining(t_provider, t_model)
                    if left:
                        marks.append({"target": target, "cooldown_s": left})
                m["order"] = {"targets": order_targets, "marks": marks}
            # 限时窗口：有配置则附窗口列表 + 当前是否在开放时段，供前端渲染徽标/编辑器
            windows = schedules.get(key) if isinstance(schedules, dict) else None
            if windows:
                m["schedule"] = {
                    "windows": windows,
                    "open": settings_mod.model_schedule_open(windows),
                }
    return {"groups": groups, "default_model": default_model}


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
        if m.get("credits"):
            snap["credits_map"][f"codebuddy/{m['id']}"] = m.get("credits")
    for p in getattr(state, "providers", {}).values():
        prefix = f"{p.id}/"
        for m in p.models():
            if m.get("credits"):
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
    return await manager.snapshot()


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
        from ..qoder.credentials import auth_state_path, load_state, resolve_credential
        from ..qoder.config import REGIONS

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


@app.get("/ui/api/traepat/model-status")
async def ui_traepat_model_status_cached(request: Request):
    """读取 traepat 模型负载缓存（纯本地，不触网）；无缓存返回空壳供页面默认展示。"""
    _ensure_local(request)
    try:
        from ..trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status, False, True)


@app.post("/ui/api/traepat/model-status")
async def ui_traepat_model_status(request: Request):
    """手动触发 traepat 模型负载查询（10 分钟内重复触发走缓存，避免频打上游）。"""
    _ensure_local(request)
    try:
        from ..trae.pat import fetch_pat_model_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(fetch_pat_model_status)


@app.get("/ui/api/traepat/accounts")
async def ui_traepat_accounts(request: Request):
    """traepat 各账号本地凭证/冷却状态 + 后台自愈循环最近一轮结果（纯本地，不触网）。"""
    _ensure_local(request)
    try:
        from ..trae.pat import accounts_status
    except Exception:
        raise HTTPException(status_code=503, detail={"error": {"message": "traepat 通道不可用"}})
    return await asyncio.to_thread(accounts_status)


@app.post("/ui/api/traepat/refresh-tokens")
async def ui_traepat_refresh_tokens(request: Request):
    """立即补签 traepat 缺失/临期 Token；健康账号不强刷。"""
    _ensure_local(request)
    try:
        from ..trae.pat import refresh_missing_tokens
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
                provider_id, model_id = _normalize_model_ref(provider_id, model_id, state)
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
    provider, model = _normalize_model_ref(provider, model, state)
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


@app.post("/ui/api/model-schedule")
async def ui_model_schedule(request: Request):
    """设置/清除模型的限时可用窗口。

    请求体：``{"provider": "traepat", "model": "glm-5.3",
              "windows": [["22:00","08:00"], ["12:00","14:00"]]}``
    窗口经 ``normalize_windows`` 校验；``windows`` 为空列表或缺省 → 删除该键
    （模型恢复完全放开，不受时段限制）。持久化到 settings.json 并热更新运行态。
    """
    _ensure_local(request)
    state = get_state()
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 model"}})
    provider, model = _normalize_model_ref(provider, model, state)
    _validate_model(provider or "codebuddy", model, state)

    key = settings_mod.model_key(provider, model)
    windows = settings_mod.normalize_windows(body.get("windows"))
    current = getattr(state, "model_schedules", {}) or {}
    if not isinstance(current, dict):
        current = dict(current)

    if windows:
        current[key] = windows
    else:
        current.pop(key, None)

    state.model_schedules = current
    # 持久化格式与 __main__ 加载对齐：每键存 {"windows": [...]}
    settings_mod.save_settings(
        {"model_schedules": {k: {"windows": v} for k, v in sorted(current.items())}})
    return {"ok": True, "model": key, "windows": windows,
            "open": settings_mod.model_schedule_open(windows) if windows else None}


def _canonical_order_target(provider: str, model: str, state: Any) -> str:
    """把一个候选目标校验并归一成 ``provider/裸模型名``（model_order 目标口径）。

    ``provider`` 留空时用 :func:`_owner_provider` 找归属通道，**但那只用来校验**：
    调用方若用它拼 ``model_order`` 的键，务必自己剥掉前缀存裸名（键的口径见
    :func:`ui_model_order`）。

    **必须按「对外发布的 id」校验，而不是 ``_normalize_model_ref`` 的结果**：后者会把
    名字再过一遍通道的 ``resolve_model``，那是**转发期**的映射（qoder 把对外名
    ``glm-5.3`` 归一成上游内部 key ``gmodel``），拿它去比对通道目录必然失败——
    目录里发布的是 ``glm-5.3``。用 ``_normalize_model_ref`` 校验会让「选择器给出的、
    ``/v1/models`` 也确认存在的」目标被 400 拒掉。

    转发侧会自己对裸名做同样的归一，所以这里存对外名是安全的、也是可读的。
    """
    if not (provider or "").strip():
        provider, model = _split_known_prefix(model)
    if not provider:
        provider = _owner_provider(model, state)
    pid = provider.strip()
    bare = _bare_model_id(model, pid)
    _validate_model(pid, bare, state)
    return settings_mod.model_key(pid, bare)


def _split_known_prefix(model: str) -> tuple[str, str]:
    """把 ``<已知通道>/<名>`` 拆成 ``(通道, 名)``；不是这个形态就返回 ``("", 原串)``。

    为什么要拆：老配置里的键是 ``zcode/glm-5.3``，顺序页照着配置原样展示，用户
    点点改改再保存时提交回来的就是那个带前缀的串。不拆的话 ``_owner_provider``
    会去目录里找一个**名字真叫** ``zcode/glm-5.3`` 的模型，必然 400。

    **只认 :data:`settings.KNOWN_PROVIDER_IDS` 里的通道名**：``provider/model``
    本身也是合法的普通模型 id（``openrouter/gpt-5``），见不得斜杠就拆会把它们
    拆坏。别名 ``workbuddy`` 借 :func:`settings.model_key` 一并归一。
    """
    head, sep, tail = (model or "").strip().partition("/")
    if sep and tail.strip():
        pid, _, bare = settings_mod.model_key(head, tail).partition("/")
        if pid in settings_mod.KNOWN_PROVIDER_IDS:
            return pid, bare
    return "", (model or "").strip()


def _owner_provider(model: str, state: Any) -> str:
    """模型名没带通道时，找出**哪个通道认识它**（用于校验/拼 ``target``）。

    只用于「校验这个模型名是真实存在的」，**不参与 ``model_order`` 的落键**——
    键一律是裸名（`fake-model`），运行时对所有发布它的通道生效。挑第一个认识的
    通道即可：与转发侧 ``_resolve_auto`` 的「精确匹配轮」顺序一致，于是
    「能保存的」正好是「能路由到的」。
    """
    for group in _model_groups(state):
        gid = group.get("id") or ""
        if not gid:
            continue
        for item in group.get("models") or []:
            if not isinstance(item, dict):
                continue
            if _bare_model_id(item.get("id") or "", gid) == model:
                return gid
    raise HTTPException(
        status_code=400,
        detail={"error": {"message": f"没有通道发布名为 {model} 的模型"}})


@app.post("/ui/api/model-order")
async def ui_model_order(request: Request):
    """设置/清除模型的候选上游顺序（按序尝试、未提交即失败就换下一档）。

    请求体：``{"model": "deepseek-v4.1-flash",
              "targets": [{"provider": "qoder", "model": "deepseek-v4.1-flash"},
                          {"provider": "codebuddy", "model": "deepseek-v4.1-flash"}]}``

    键是**裸模型名**（不带 ``provider/``）：用户表达的是「指定这个模型名时按这个
    顺序选通道」。每个目标经 :func:`_canonical_order_target` 校验并归一成
    ``provider/对外发布名``——**不是** ``_normalize_model_ref``：那会套用通道转发期的
    ``resolve_model`` 映射，qoder 上会把 ``glm-5.3`` 变成上游内部 key ``gmodel``，
    再拿去比对通道目录必然 400（见 helper 的注释）。非法目标 400。``targets`` 为空
    列表或缺省 → 删除该键（恢复历史路由）并顺带清掉该模型的冷却标记——用户显式改顺序
    = 重置健康判断。持久化到 settings.json 的 ``model_order`` 并热更新运行态。
    """
    _ensure_local(request)
    state = get_state()
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail={"error": {"message": "缺少 model"}})

    # 键与目标不同口径：**键是裸模型名，目标是 provider/模型名**。
    #
    # 键就是用户嘴里的「这个模型」：配一条 `deepseek-v4.1-flash`，请求该名字时
    # 一律按后面的顺序选实际发请求的通道。一个名字一条配置（写两遍没有意义，
    # 反而会按请求解析到哪个通道而行为不一）。所以这里接受（并忽略）前端可能
    # 传来的归属通道，只存裸名。
    #
    # **model 里自带的 `<通道>/` 也要剥掉**：老配置的键就是那个形态，页面照原样
    # 展示、用户改完提交回来的就是 `zcode/glm-5.3`；带着它落键会写出一个转发侧
    # 永远查不到的双前缀死键。
    #
    # 目标仍按对外发布名校验后拼 `provider/模型名`（**不是** `_normalize_model_ref`：
    # 那会套用通道转发期的 `resolve_model` 映射，qoder 上把 `glm-5.3` 变成上游内部
    # key `gmodel`，转发侧按 `qoder/glm-5.3` 查不到，条目成死键）。
    _, model = _split_known_prefix(model)
    key = _canonical_order_target("", model, state).split("/", 1)[-1]
    raw_targets = body.get("targets")
    targets: list[str] = []
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            t_model = (item.get("model") or "").strip()
            if not t_model:
                continue
            # 按对外发布名校验（不走过 resolve_model 的上游 key 映射，见 helper 注释）
            targets.append(_canonical_order_target(
                (item.get("provider") or "").strip(), t_model, state))
    targets = settings_mod.normalize_order(list(dict.fromkeys(targets)))

    current = getattr(state, "model_order", None) or {}
    if not isinstance(current, dict):
        current = dict(current)
    # 清顺序时顺带清标记：用户显式改 = 重置健康判断，避免刚配完还是被冷却挡着。
    # 必须在 pop 之前取旧目标列表，否则读到的是已被删空的值。
    cleared = 0
    # **收敛同名的遗留键**：老配置里同一个模型可能既有 `<通道>/<名>` 又有裸名
    # （用户手改过、或从旧版本升上来）。保存裸名那一份时必须把带前缀的旧键一起
    # 删掉，否则一个模型在页面上就是两张卡、配置里两条规则，而哪条生效取决于
    # 转发侧的查找顺序——用户会看到「改了一条，行为没变」。
    # 只删**同名**且前缀是**已知通道**的（`x/glm-5.3` 对 `glm-5.3`）；不同模型
    # 互不影响，`openrouter/...` 这种普通模型 id 也不会被误伤。
    # 放在「写新值」之前：下面 `targets` 为空时就是「删掉这个模型」，遗留旧键
    # 也该一起走。
    for k in [k for k in current if k != key and _split_known_prefix(k)[1] == key]:
        for target in current.get(k) or []:
            if isinstance(target, str) and "/" in target:
                # 旧键上的冷却标记一并清掉：那条规则本身要没了，标记只会误导
                cleared += cooldown_mod.clear(*target.split("/", 1))
        current.pop(k, None)
    if targets:
        current[key] = targets
    else:
        for target in current.get(key) or []:
            if "/" in target:
                cleared += cooldown_mod.clear(*target.split("/", 1))
        current.pop(key, None)

    state.model_order = current
    # 存储形状与用户手写配置一致：直接存字符串数组（不像 model_schedules 包一层）
    settings_mod.save_settings({"model_order": {k: v for k, v in sorted(current.items())}})
    return {"ok": True, "model": key, "targets": targets, "marks_cleared": cleared}


@app.get("/ui/api/model-order/options")
async def ui_model_order_options(request: Request):
    """候选目标选择器数据源：``[{provider, models: [...]}, ...]``。

    只在「添加目标」时拉一次，让用户**选**而不是拼错。刻意**不**复用
    ``/ui/api/models``：那个接口带停用/时段/指标等重量级字段，这里只需要
    「有哪些通道、每个通道认识哪些模型名」，请求体越小越好。

    每个模型给两样东西：

    - ``id``：**用来写进 model_order 的规范名**（剥掉 ``qoder/<public>`` 这类
      通道内前缀，因为转发侧按裸名路由）。
    - ``label``：给人看的名（display_name / 别名），与 ``id`` 不同才显示。

    刻意**不过滤**停用/不在时段的目标：候选里放一个今天不想用的通道是合法配置
    （运行期会跳过并继续下一个），把它从选择器里藏掉只会让人以为不支持。
    """
    _ensure_local(request)
    state = get_state()
    groups: list[dict[str, Any]] = []
    for group in _model_groups(state):
        gid = group.get("id") or ""
        if not gid:
            continue
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in group.get("models") or []:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or ""
            mid = _bare_model_id(raw_id, gid)
            if not mid or mid in seen:
                continue
            seen.add(mid)
            label = (item.get("name") or "").strip()
            entry = {"id": mid, "target": settings_mod.model_key(gid, mid)}
            if label and label != mid:
                entry["label"] = label
            models.append(entry)
        if models:
            groups.append({"provider": gid, "models": models})
    return {"groups": groups}


@app.get("/ui/api/model-order")
async def ui_model_order_get(request: Request):
    """**顺序页唯一的数据源**：原样返回 ``model_order`` 字段。

    刻意不做任何「按通道展开」「按目录标注」——那些会让同一个模型名在多个通道
    各冒一份，页面上看起来就是一堆重复（用户原话：「配置什么就展示什么，别搞
    这么复杂」）。这里给什么，页面就画几张卡：键 → 卡片，逐字对应。

    每个键附上各目标的冷却剩余秒数（``marks``），供卡片显示 ⏸，不改变键的集合。
    """
    _ensure_local(request)
    state = get_state()
    order = getattr(state, "model_order", None)
    if not isinstance(order, dict):
        order = {}
    items: list[dict[str, Any]] = []
    for key in order:
        targets = order.get(key) or []
        if not isinstance(targets, list):
            targets = []
        marks = []
        for target in targets:
            if not isinstance(target, str) or "/" not in target:
                continue
            t_provider, t_model = target.split("/", 1)
            left = cooldown_mod.remaining(t_provider, t_model)
            if left:
                marks.append({"target": target, "cooldown_s": left})
        items.append({"model": key, "targets": targets, "marks": marks})
    return {"items": items}


@app.post("/ui/api/model-order/mark-clear")
async def ui_model_order_mark_clear(request: Request):
    """清除冷却标记（不动顺序），给「上游已恢复、现在就重试」用。

    请求体：``{"provider": "zcode", "model": "glm-5.3", "target": "zcode/glm-5.3"}``
    —— ``target`` 可选；缺省则清该模型候选列表里全部目标的标记。两者都缺省则全清。
    """
    _ensure_local(request)
    state = get_state()
    body = await request.json()
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    target = (body.get("target") or "").strip()

    if target:
        if "/" not in target:
            raise HTTPException(
                status_code=400, detail={"error": {"message": "target 应为 provider/model 形态"}})
        cleared = cooldown_mod.clear(*target.split("/", 1))
        return {"ok": True, "target": target, "marks_cleared": cleared}

    if not model:
        cleared = cooldown_mod.clear()
        return {"ok": True, "marks_cleared": cleared}

    # 用规范化后的名字（对外发布名，不套 resolve_model）去查刚存的键，否则查不到。
    # 但**校验失败不报错**：清冷却是个纯缓存操作，跟「模型是否还在目录里」无关——
    # 报「模型不在通道的模型列表中」会让人以为保存失败了跑去翻配置，而实际什么都没坏
    # （模型下架后想清掉残留标记是完全合理的）。所以校验不过就退回原始名字清一次。
    try:
        key = _canonical_order_target(provider, model, state)
    except HTTPException:
        key = settings_mod.model_key(provider or "codebuddy", model)
    order = getattr(state, "model_order", None) or {}
    # 裸名键优先（UI 现在的落键位置），再退回 `<通道>/<名>`（老配置）。
    order_targets = order.get(model) or order.get(key) or []
    cleared = 0
    for item in order_targets:
        if "/" in item:
            cleared += cooldown_mod.clear(*item.split("/", 1))
    if not order_targets:
        # 没配顺序的模型：按裸名清一次（转发侧 mark_failed 用的就是裸名）
        pid, _, bare = key.partition("/")
        cleared = cooldown_mod.clear(pid, bare)
    return {"ok": True, "model": key, "marks_cleared": cleared}


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
