"""PAT 额度查询：按账号拉取 entitlement 包并归一为 UI 额度项。"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from typing import Any

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _PLUS_GATEWAY, PatProfile, ensure_pat_config
from .credentials import _pat_headers
from .store import _account_state, _mutate_account

log = logging.getLogger(__name__)

# ───────────────────────── 额度查询 ─────────────────────────

def _cached_quota(profile: PatProfile, quota_class: str) -> list[dict[str, Any]] | None:
    state = _account_state(profile.cache_key)
    quotas = state.get("quota") if isinstance(state.get("quota"), dict) else {}
    value = quotas.get(quota_class) if isinstance(quotas, dict) else None
    items = value.get("items") if isinstance(value, dict) else None
    return items if isinstance(items, list) else None


def _save_quota(profile: PatProfile, quota_class: str, items: list[dict[str, Any]]) -> None:
    def store(state: dict[str, Any]) -> None:
        quotas = state.setdefault("quota", {})
        quotas[quota_class] = {"items": items, "fetched_at": time.time()}
    _mutate_account(profile.cache_key, store)


def _quota_items(data: dict[str, Any], profile: PatProfile, multi: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pack in data.get("user_entitlement_pack_list") or []:
        base = pack.get("entitlement_base_info") or {}
        quota = base.get("quota") or {}
        usage = pack.get("usage") or {}
        limit = quota.get("basic_usage_limit")
        used = usage.get("basic_usage_amount") or 0
        if not isinstance(limit, (int, float)) or limit <= 0:
            continue
        entitlement_id = str(base.get("entitlement_id") or "pack")
        if "weekly" in entitlement_id:
            label = "PAT 周包（通用额度）"
        elif "daily" in entitlement_id:
            # 同一账号会同时返回多个日包（例如 GPT-5.6 与 GPT-6），不能都叫
            # “高级模型共享”，否则 UI 看起来像重复额度。只从 entitlement_id 的
            # 已知后缀提取公开模型系列，不展示账号标识或其它原始字段。
            family = ""
            if entitlement_id.endswith("_gpt_56_sol"):
                family = "GPT-5.6 Sol"
            elif entitlement_id.endswith("_gpt_6"):
                family = "GPT-6"
            label = f"PAT 日包（{family or '高级模型'}）"
        else:
            label = "PAT 额度包"
        if multi:
            label = f"PAT #{profile.index + 1} · {label[4:]}"
        end_time = base.get("end_time") or 0
        items.append({"label": label, "used": round(used, 2), "total": limit,
                      "remaining": round(limit - used, 2),
                      "percent": round(used / limit * 100),
                      "reset_ts": int(end_time) if end_time else None})
    return items


def fetch_pat_ent_usage() -> list[dict[str, Any]]:
    """按账号查询 advanced 额度；失败只使用该账号自己的缓存。"""
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 通道未配置 TRAE_PAT_PLUS_GATEWAY")
    profiles = ensure_pat_config()
    all_items: list[dict[str, Any]] = []
    failures = 0
    for profile in profiles:
        try:
            credentials = _ns._get_profile_credentials(profile)
            request = urllib.request.Request(
                f"{plus}/trae/api/v1/pay/ide_user_ent_usage", data=b"{}", method="POST",
                headers=_pat_headers(credentials, "application/json"))
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            items = _quota_items(data, profile, len(profiles) > 1)
            _save_quota(profile, "advanced", items)
            all_items.extend(items)
        except Exception as exc:
            failures += 1
            cached = _cached_quota(profile, "advanced")
            if cached:
                all_items.extend(dict(item, label=f"{item['label']}·缓存") for item in cached)
            log.warning("PAT 额度查询失败，账号序号=%d（%s）", profile.index, type(exc).__name__)
    if not all_items and failures:
        raise HTTPException(status_code=502, detail="PAT 额度查询失败")
    return all_items
