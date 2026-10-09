"""Antigravity 模型目录加载与动态 id 归一化。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

log = logging.getLogger(__name__)

_MODELS_JSON = Path(__file__).with_name("models.json")


def _load_models(path: Path = _MODELS_JSON) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("antigravity models.json 读取失败（%s），使用内置兜底表", exc)
        return [{
            "id": "gemini-3.8-flash",
            "group": "gemini",
            "description": "Gemini 3.8 Flash (fallback)",
        }]
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("id")] or [{
        "id": "gemini-3.8-flash",
        "group": "gemini",
        "description": "Gemini 3.8 Flash (fallback)",
    }]


MODELS: list[dict[str, Any]] = _load_models()
DEFAULT_MODELS: dict[str, str] = {
    m["id"]: str(m.get("description") or m["id"]) for m in MODELS
}
_MODEL_BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in MODELS}

# 只去掉已知 effort 后缀；thinking/image/agent 等是模型身份的一部分。
_EFFORT_SUFFIXES = ("-extra-low", "-low", "-medium", "-high", "-tiered")
_EFFORTS = tuple(suffix.removeprefix("-") for suffix in _EFFORT_SUFFIXES)
_INTERNAL_MODEL_PREFIXES = ("chat_", "tab_")


def _split_effort_suffix(name: str) -> tuple[str, str | None]:
    for suffix in _EFFORT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix.removeprefix("-")
    return name, None


def _strip_effort_suffix(name: str) -> str:
    return _split_effort_suffix(name)[0]


def _is_public_upstream_model(name: str) -> bool:
    return bool(name) and not name.lower().startswith(_INTERNAL_MODEL_PREFIXES)


def _default_effort(efforts: Sequence[str]) -> str:
    """为动态条目选择保守且可路由的默认档位。"""
    for preferred in ("medium", "tiered", "low", "high", "extra-low"):
        if preferred in efforts:
            return preferred
    return efforts[0]


def _group_for_model(model_id: str) -> str:
    return "claude-gpt" if model_id.startswith(("claude-", "gpt-")) else "gemini"
