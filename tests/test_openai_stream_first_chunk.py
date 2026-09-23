"""openai 流式首 chunk 只有 role、无 content 时不得炸（PR #42 回归）。

PR #42 把 openai 分支的 DSML 注入条件从 detected_tool_calls（循环前初始化）
改成 chunk_tool_calls，但赋值在 if chunk_content: 里、注入块在 guard 外——
首 chunk 无 content 时 UnboundLocalError，被 except Exception 包成
"stream error: cannot access local variable 'chunk_tool_calls'..." 吐给客户端。
日志实证：两条 stream_error 均 protocol=openai、chunks=1、error 87 字节。
"""
import asyncio
import json
import sys
import time
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from buddy_proxy.codebuddy_provider import pipeline as cbp  # noqa: E402
from buddy_proxy.core import state as st  # noqa: E402


def _install_fake_state():
    """get_state() 要求已初始化，塞一个最小假 state（只用到 write_log 等）。"""
    st.proxy_state = SimpleNamespace(
        providers={},
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        verbose_llm=False,
        write_log=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
    )


class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class _FakeStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    """接管 httpx.AsyncClient，回放固定 SSE 行。"""

    lines: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _FakeStreamCM(_FakeResp(list(self.lines)))


async def _drive(monkeypatch, lines: list[str]) -> str:
    _install_fake_state()
    _FakeAsyncClient.lines = lines
    monkeypatch.setattr(cbp.httpx, "AsyncClient", _FakeAsyncClient)
    out = []
    async for payload in cbp.stream_upstream(
        "http://fake/upstream", {}, {"model": "deepseek-v4.1-flash"}, "openai",
        {"model": "deepseek-v4.1-flash"},
    ):
        out.append(payload.decode("utf-8"))
    return "".join(out)


def test_role_only_first_chunk_no_unbound_error(monkeypatch):
    """首 chunk 仅 role、无 content —— 此前必炸的形态。"""
    lines = [
        "data: " + json.dumps({
            "id": "c1", "object": "chat.completion.chunk", "created": 1,
            "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }),
        "data: " + json.dumps({
            "id": "c1",
            "choices": [{"index": 0, "delta": {"content": "你好"}}],
        }),
        "data: " + json.dumps({
            "id": "c1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        "data: [DONE]",
    ]
    text = asyncio.run(_drive(monkeypatch, lines))
    assert "chunk_tool_calls" not in text, f"UnboundLocalError 泄漏: {text}"
    assert "internal_error" not in text, f"流被错误中断: {text}"
    assert "你好" in text, f"正文丢失: {text}"
    assert "data: [DONE]" in text


def test_role_only_first_chunk_with_dsml_still_parses(monkeypatch):
    """修复不伤及 DSML 注入：后续 chunk 出现文本工具调用仍要注入。"""
    p = "｜｜DSML｜｜"
    dsml = (
        f'<{p} calls> <{p} invoke name="shell"> '
        f'<{p} parameter name="command" string="true">ls'
        f'</{p} parameter> </{p} invoke> </{p} calls>'
    )
    lines = [
        "data: " + json.dumps({
            "id": "c2", "object": "chat.completion.chunk", "created": 1,
            "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "delta": {"role": "assistant"}}],
        }),
        "data: " + json.dumps({
            "id": "c2",
            "choices": [{"index": 0, "delta": {"content": dsml}}],
        }),
        "data: " + json.dumps({
            "id": "c2",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }),
        "data: [DONE]",
    ]
    text = asyncio.run(_drive(monkeypatch, lines))
    assert "chunk_tool_calls" not in text, text
    assert "internal_error" not in text, text
    # 标记不泄漏 + 注入了 tool_calls + finish_reason 被改写
    assert "DSML" not in text.replace("deepseek-v4.1-flash", ""), text
    assert '"tool_calls"' in text, text
    assert '"finish_reason": "tool_calls"' in text, text
    assert '"name": "shell"' in text, text
