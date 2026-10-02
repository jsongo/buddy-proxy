"""管理 UI 公共件：本机校验、错误文案、一键测试常量。

原 ``web/ui.py``（1000+ 行）按领域拆成包（2026-10-03）：各子模块都往
``core.state.app`` 上挂路由，import 即注册（见 ``__init__.py``）。
"""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import HTTPException, Request

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


def _ms(started: float) -> int:
    return round((time.time() - started) * 1000)
