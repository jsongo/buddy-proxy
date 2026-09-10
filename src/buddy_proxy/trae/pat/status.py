"""PAT 模型负载查询（plus 网关 get_detail_param，短 TTL 缓存）。"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _EXCHANGE_TIMEOUT_S, _PLUS_GATEWAY, ensure_pat_config
from .credentials import _pat_headers
from .models import pat_gateway_is_plus, pat_model_meta, pat_model_names

# ───────────────────────── 模型负载查询 ─────────────────────────

_MODEL_STATUS_TTL_S = 600
_model_status_cache: dict[str, Any] = {"fetched_at": 0.0, "data": None}


def fetch_pat_model_status(force: bool = False, cached_only: bool = False) -> dict[str, Any]:
    """手动触发的模型负载查询（用首个账号凭证；结果短 TTL 缓存）。

    返回 {models: [{id, workload(0-100%或None), credits, max_input}], fetched_at, cached}；
    只有 plus 网关目录接口提供负载数据。workload 为 None 表示该模型无负载信息。

    cached_only=True 时只读缓存、绝不触网：有缓存返回之（cached=True），
    无缓存返回空壳（fetched_at=0），供页面默认展示、按钮再强制刷新。
    """
    now = time.time()
    cached = _model_status_cache.get("data")
    if cached_only:
        if cached:
            return {**cached, "cached": True}
        return {"models": [], "fetched_at": 0.0, "cached": False}
    if (not force and cached and now - float(_model_status_cache.get("fetched_at") or 0)
            < _MODEL_STATUS_TTL_S):
        return {**cached, "cached": True}
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 模型服务未配置")
    profile = ensure_pat_config()[0]
    credentials = _ns._get_profile_credentials(profile)
    request = urllib.request.Request(
        f"{plus}/api/ide/v1/get_detail_param",
        data=json.dumps({"function": os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3"),
                         "need_prompt": False, "poly_prompt": False}).encode("utf-8"),
        method="POST",
        headers=_pat_headers(credentials, "*/*"),
    )
    try:
        with urllib.request.urlopen(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        raise HTTPException(status_code=502,
                            detail=f"PAT 模型负载查询失败（HTTP {exc.code}）") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HTTPException(status_code=502,
                            detail=f"PAT 模型负载查询失败（网络错误：{reason}）") from None
    meta = pat_model_meta()
    hot_by_config: dict[str, float | None] = {}
    for entry in raw.get("config_info_list") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("config_name")
        if not isinstance(name, str) or not name:
            continue
        hot = ((entry.get("display_config") or {}).get("hot_info") or {}).get("hot")
        hot_by_config[name] = round(min(max(float(hot), 0.0), 100.0), 1) if isinstance(hot, (int, float)) else None
    models: list[dict[str, Any]] = []
    for model_id in pat_model_names():
        if not pat_gateway_is_plus(model_id):
            continue
        info = meta.get(model_id) or {}
        _, config_name = _ns.PAT_PLUS_MODELS.get(model_id, ("", model_id))
        models.append({
            "id": model_id,
            "name": info.get("name") or model_id,
            "workload": hot_by_config.get(config_name),
            "credits": info.get("credits"),
            "max_input": info.get("max_input"),
            "reasoning": bool(info.get("reasoning")),
        })
    result = {"models": models, "fetched_at": now}
    _model_status_cache.update({"fetched_at": now, "data": result})
    return {**result, "cached": False}
