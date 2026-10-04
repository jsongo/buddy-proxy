"""Qoder 上游流转换辅助（COSY 信封拆解 -> Anthropic 事件流）。

2026-10 从 ``provider.py`` 拆出；旧路径 ``buddy_proxy.qoder.provider``
对以下名字保持 re-export 兼容。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from .errors import _describe_upstream_error

log = logging.getLogger(__name__)


async def _to_anthropic_stream(
    openai_stream: AsyncIterator[bytes],
    model: str,
    converter_cls: Any,
) -> AsyncIterator[bytes]:
    """OpenAI chat chunk SSE 流 -> Anthropic Messages 事件流。

    ``openai_stream`` 是本 provider 已拆掉 COSY 外壳的 OpenAI SSE 字节流。
    转换器复用 ``anthropic_adapter.AnthropicStreamConverter``（与 trae/mimo
    同一个），因此 reasoning_content -> thinking 块、tool_calls -> tool_use
    块的行为与其它通道一致。

    转换途中任何异常都**先收尾再抛**：``feed_chunk`` 对畸形 chunk 会抛，
    不兜的话客户端拿到「内容块悬空、没有 message_stop」的残流，比直接报错
    更难排查。
    """
    converter = converter_cls(model)

    def _emit(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _close_open() -> list[str]:
        try:
            return [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001 - 收尾失败不能盖掉原错误
            return []

    def _abort(msg: str) -> list[str]:
        out = _close_open()
        out.append(_emit("error", {"type": "error",
                                   "error": {"type": "api_error", "message": msg}}))
        return out

    buffer = ""
    try:
        async for raw in openai_stream:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            buffer += text
            # 按 SSE 帧切：只处理完整的 ``data: ...\n\n``，最后一段留在 buffer。
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                        for event in _abort(msg):
                            yield event
                        return
                    for name, payload in converter.feed_chunk(chunk):
                        yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001 - 上游畸形数据不该让客户端只收到半截流
        log.warning("qoder anthropic 流中断: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    yield "data: [DONE]\n\n"


def _unwrap(payload: str) -> tuple[str | None, str | None, bool]:
    """拆一层 SSE 信封。

    返回 ``(inner_json | None, error | None, done)``：

    - ``event:finish`` 之类的尾帧（无 ``body``）-> ``(None, None, False)``
    - ``body == "[DONE]"`` -> ``(None, None, True)``
    - 带内业务错误（``code``/``message`` 且无 ``choices``/``usage``）-> 错误
    """
    if payload == "[DONE]":
        return None, None, True
    try:
        frame = json.loads(payload)
    except ValueError:
        return None, None, False
    if not isinstance(frame, dict):
        return None, None, False
    body = frame.get("body")
    if not isinstance(body, str):
        # 顶层带 code 的错误帧（真实权益门帧：
        # ``{"code":"112","message":…}`` 没有 body）——不能当尾帧忽略。
        if frame.get("code") is not None:
            return None, _describe_upstream_error(frame), False
        # 尾帧（计时统计）等：无 body 无 code，直接忽略
        return None, None, False
    if body == "[DONE]":
        return None, None, True
    try:
        chunk = json.loads(body)
    except ValueError:
        return None, f"上游返回非法 JSON: {body[:200]}", False
    if not isinstance(chunk, dict):
        return None, None, False
    if "choices" not in chunk and "usage" not in chunk and (
        chunk.get("code") is not None or isinstance(chunk.get("message"), str)
    ):
        return None, _describe_upstream_error(chunk), False
    return body, None, False


def _normalize_message(message: Any) -> Any:
    """单条消息的上游适配（Qoder 专属，不改公共转换器）。

    三条上游硬性要求，实测（2026-09）：

    1. **``developer`` role 整请求被拒**（反序列化阶段就挂），转 ``system``。
    2. **带 ``tool_calls`` 的消息，``content`` 不能是 ``null``**。
       Anthropic 的 ``tool_use`` only 回合转出来正是 ``content: null``，
       上游会拒单——而且**报错文案误导**：它说「role 'tool' 必须回应带
       tool_calls 的消息」，害得往 tool 配对方向排查。实际把它改成 ``""``
       即可通过（``content=""`` 实测 200）。这条**不绑 role**：绑了
       ``assistant`` 的话，``developer`` 那条先被改成 ``system`` 就永远命中
       不了（而且必须在摘 ``tool_calls`` 之前做，否则条件同样不成立）。
    3. **``tool_calls`` 只能挂在 ``assistant`` 上**。``system`` 带 ``tool_calls``
       一样被那句误导文案拒掉（实测：``system`` + ``content:""`` 仍 ❌，
       ``assistant`` + ``content:""`` ✅）——因为其后的 ``tool`` 没有
       ``assistant`` 可配对。所以 ``developer`` 转 ``system`` 时要把
       ``tool_calls`` 摘掉（系统消息本就不该发起工具调用，摘掉不丢信息）。

    这三条只影响本通道：其它 provider 共用同一个转换器，不能在那里改。
    """
    if not isinstance(message, dict):
        return message
    out = message
    # ⚠️ content 的修正必须排在摘 ``tool_calls`` **之前**（见 ``developer`` 分支）：
    # 一旦先摘掉 tool_calls，下面「有没有 tool_calls」就再也不成立，
    # ``content: null`` 会原样出站。
    if out.get("tool_calls") and out.get("content") is None:
        out = {**out, "content": ""}
    if out.get("role") == "developer":
        # role 改成 system，同时摘掉不可能属于系统消息的 tool_calls
        out = {k: v for k, v in out.items() if k != "tool_calls"}
        out["role"] = "system"
    return out

