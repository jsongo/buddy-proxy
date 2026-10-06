"""zcode-start 风控 system 注入：ZCode 块永远在最前，客户端 system 合并在后。

背景（2026-10-06 ``/model glm-5.3-flash`` 实测）：请求被上游拒为
``405 {"code":3012,"msg":"request has been blocked due to unusual activity"}``。
根因是 anthropic 路径只在**没有** system 时才注入 ZCode 风控要求的 3 个系统块——
而 Claude Code 的请求一律自带 system（探测和正常对话都是），注入被跳过、
风控指纹对不上就整条拦截。openai→anthropic 转换路径一直是合并口径且实测可过。

本文件锁两件事：
1. 带客户端 system 的请求也必须合并出 ZCode 前置块（不能被"已有 system"短路）；
2. 合并形状与风控指纹一致：前置 3 块各带一个 cache_control，客户端块不带断点。
"""

from __future__ import annotations

import asyncio

from buddy_proxy.providers import zcode_start as zs
from buddy_proxy.providers.zcode_start import (
    _SYSTEM_BLOCKS,
    _client_system_blocks,
    _merged_system,
    _required_system_blocks,
    ZCodeStartPlanProvider,
)


# -- 纯函数：合并口径 -----------------------------------------------------------


def test_template_is_three_blocks():
    """风控指纹就是「3 个 system 块」——模板条数变了要同步改风控认知。"""
    assert len(_SYSTEM_BLOCKS) == 3


def test_required_blocks_each_carry_cache_control():
    out = _required_system_blocks()
    assert [b["type"] for b in out] == ["text"] * 3
    assert [b["cache_control"] for b in out] == [{"type": "ephemeral"}] * 3
    assert [b["text"] for b in out] == list(_SYSTEM_BLOCKS)


def test_client_system_merged_after_required_blocks():
    """自带 system 不许顶掉 ZCode 块——那是 3012 的直接根因。"""
    merged = _merged_system("You are Claude Code")
    assert merged[:3] == _required_system_blocks()
    assert merged[3:] == [{"type": "text", "text": "You are Claude Code"}]


def test_client_system_string_and_block_forms():
    """str / 块数组 / 空值都要出同样形状的纯文本块。"""
    assert _client_system_blocks("hi") == [{"type": "text", "text": "hi"}]
    assert _client_system_blocks("") == []
    assert _client_system_blocks(None) == []
    assert _client_system_blocks([{"type": "text", "text": "a",
                                   "cache_control": {"type": "ephemeral"}},
                                  {"type": "text", "text": ""}]) == [
        {"type": "text", "text": "a"}]  # 客户端 cache_control 被剥掉


def test_merged_without_client_system_is_just_required():
    assert _merged_system(None) == _required_system_blocks()


# -- forward() 真实派发：别再被「已有 system」短路 -------------------------------


class _FakeResp:
    status_code = 200

    def json(self):
        return {"id": "msg_x", "type": "message", "role": "assistant",
                "model": "glm-5.3-flash", "content": [],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1}}

    async def aclose(self):
        pass


class _FakeClient:
    """只抓 build_request 的 json 出参——被送到上游的 body 就是断言对象。"""

    def __init__(self):
        self.body: dict | None = None

    def build_request(self, method, url, json=None, headers=None):
        self.body = json
        return object()

    async def send(self, req, stream=False):
        return _FakeResp()


def _forward_capture(body: dict, protocol: str = "anthropic") -> dict:
    provider = ZCodeStartPlanProvider(base_url="http://upstream.test", api_key="k")
    fake = _FakeClient()

    async def _get_client():
        return fake

    provider._get_client = _get_client  # type: ignore[method-assign]
    # deviceMid 来自本机 ~/.zcode/v2/telemetry-state.json（CI 没有这个文件，
    # _build_metadata_user_id 会退化成 "{}"）——固定住，测试不依赖环境。
    import unittest.mock as mock
    with mock.patch.object(zs, "_load_device_mid", lambda: "test-device-mid"):
        resp = asyncio.run(provider.forward(body, protocol, body))
    assert resp.status_code == 200
    assert fake.body is not None
    return fake.body


def test_forward_merges_client_system_into_upstream_body():
    """回归主犯：带 system 的 anthropic 请求，上游 body 里必须有 ZCode 前置块。"""
    upstream = _forward_capture({
        "model": "glm-5.3-flash", "max_tokens": 16,
        "system": "You are Claude Code, Anthropic's official CLI.",
        "messages": [{"role": "user", "content": "hi"}],
    })
    system = upstream["system"]
    assert system[:3] == _required_system_blocks()
    assert system[3]["text"].startswith("You are Claude Code")
    assert "cache_control" not in system[3]


def test_forward_injects_when_request_has_no_system():
    """没有 system 的老路径行为不变。"""
    upstream = _forward_capture({
        "model": "glm-5.3-flash", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert upstream["system"] == _required_system_blocks()


def test_forward_block_array_system_preserves_text_only():
    """Claude Code 实际会发块数组（带 cache_control）：只保留文本进合并块。"""
    upstream = _forward_capture({
        "model": "glm-5.3-flash", "max_tokens": 16,
        "system": [{"type": "text", "text": "block-a",
                    "cache_control": {"type": "ephemeral"}},
                   {"type": "text", "text": "block-b",
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "hi"}],
    })
    system = upstream["system"]
    assert system[3:] == [{"type": "text", "text": "block-a"},
                          {"type": "text", "text": "block-b"}]
    # 风控指纹：断点只在前置 3 块上
    assert "cache_control" not in system[3] and "cache_control" not in system[4]


def test_forward_openai_path_also_gets_session_metadata():
    """回归（2026-10-05 深夜实测）：/v1/chat/completions 走的 openai 路径
    也必须拿到已赋值的 session_id——当时工作树中间态把赋值挪到分支后面，
    openai 路径直接 UnboundLocalError。锁住「赋值在两个协议分支之前」的形状。
    """
    upstream = _forward_capture({
        "model": "glm-5.3-flash", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
    }, protocol="openai")
    assert "session_id" in upstream["metadata"]["user_id"]
