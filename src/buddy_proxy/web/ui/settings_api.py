"""管理 UI：settings.json 的读写接口（默认模型/通道、打卡设置）。

原 ``web/ui.py`` 拆分。模型引用的校验/归一 helper 在 ``models_api``。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, Request

from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.state import app, get_state
from buddy_proxy.benefits import read_checkin_settings

from .common import _ensure_local
from .models_api import _model_exists_anywhere, _normalize_model_ref, _validate_model


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
