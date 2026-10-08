"""Trae PAT 静态模型目录的公开 ID 与上游映射契约。"""

from __future__ import annotations

import json
from pathlib import Path

import buddy_proxy.trae.pat as pat


_CONFIG = Path(__file__).parents[1] / "src/buddy_proxy/web/models_config.json"


def _traepat_rows() -> dict[str, dict]:
    data = json.loads(_CONFIG.read_text("utf-8"))
    return {
        str(row["id"]): row
        for row in data.get("models", [])
        if row.get("provider") == "traepat"
    }


def test_new_public_ids_are_lowercase_and_map_to_exact_upstream_names():
    """客户端只见小写 ID；大小写敏感的上游拼写留在边界映射。"""
    expected = {
        "deepseek-v4.1-flash": ("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
        "glm-5.3-flashx": ("glm-5.3-flashx", "glm-5.3-flashx"),
        "kimi-k2.8-preview": ("kimi-k2.8-preview", "kimi-k2.8-preview"),
        "step-5-preview": ("step-5-preview", "step-5-preview"),
        "qwen3.8-flash": ("qwen3.8-flash", "qwen3.8-flash"),
        "doubao-seed-evolving": ("Doubao-Seed-Evolving", "Doubao-Seed-Evolving"),
    }
    rows = _traepat_rows()

    assert all(model_id == model_id.lower() for model_id in expected)
    assert {
        model_id: (rows[model_id]["upstream_model"], rows[model_id]["config_name"])
        for model_id in expected
    } == expected

    pat._reload_pat_models()
    assert {model_id: pat.PAT_PUBLIC_MODELS[model_id] for model_id in expected} == expected


def test_retired_kimi_k26_is_absent_from_static_and_runtime_catalogs():
    rows = _traepat_rows()
    pat._reload_pat_models()

    assert "kimi-k2.6" not in rows
    assert "kimi-k2.6" not in pat.PAT_MODELS
