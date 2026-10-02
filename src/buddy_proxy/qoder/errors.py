"""Qoder 上游错误的归因与报错构造。

2026-10 从 ``provider.py`` 拆出；旧路径 ``buddy_proxy.qoder.provider``
对以下名字保持 re-export 兼容。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException


def _describe_upstream_error(chunk: dict[str, Any]) -> str:
    """带内错误帧 -> 可读原因。

    上游把真正的失败原因放在 ``details`` 里（JSON 字符串），顶层 ``message``
    只有一句没用的 ``Error in upstream response``。只取 ``message`` 会让
    「模型不存在」「参数非法」「渠道校验拦截」全都退化成同一句话，线上只能
    靠猜——所以这里把 ``details.error.message`` 一并挖出来。
    """
    parts = [str(chunk.get("message") or "")] if chunk.get("message") else []
    code = chunk.get("code")
    if code:
        parts.append(f"code={code}")
    details = chunk.get("details")
    detail_msg = ""
    if isinstance(details, str) and details.strip():
        try:
            parsed = json.loads(details)
        except ValueError:
            detail_msg = details.strip()
        else:
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict):
                detail_msg = str(err.get("message") or "")
            elif isinstance(parsed, dict):
                detail_msg = str(parsed.get("message") or "")
    elif isinstance(details, dict):
        err = details.get("error")
        detail_msg = str((err or {}).get("message") or "") if isinstance(err, dict) else ""
    if detail_msg:
        parts.append(detail_msg)
    return " | ".join(p for p in parts if p)[:500] or "上游返回未知错误"


def _sse_error(message: str) -> bytes:
    """构造 OpenAI 风格的 SSE 错误帧。"""
    payload = json.dumps({"error": {"message": message, "type": "upstream_error"}},
                         ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


def _upstream_error(error: str) -> HTTPException:
    """上游带内错误 -> HTTPException（502）。

    Anthropic 客户端（Claude Code）只认 ``{"type":"error","error":{...}}``，
    收到我们原来的 ``{"detail": ...}`` 会把它当**未知可重试错误**，于是对着
    同一个请求重试到上限（线上表现为 ``Retrying in 15s · attempt 7/10``）。
    这里补上 Anthropic 形状，让它能正确识别并停止无谓重试。
    """
    return HTTPException(
        status_code=502,
        detail={
            "type": "error",
            "error": {"type": "api_error", "message": f"qoder 上游错误: {error}"},
        },
    )


def _last_user_text(messages: list[Any]) -> str:
    """取最后一条 user 消息的纯文本（多模态时拼接 text part）。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return ""
    return ""

