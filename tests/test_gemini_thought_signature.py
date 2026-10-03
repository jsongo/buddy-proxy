"""thoughtSignature 回环测试（2026-10-03 真机实证矩阵）。

上游行为（antigravity + gemini 通道实测）：
- gemini-3 系：functionCall 带 ``thoughtSignature``（~1KB），次轮必带（缺 → 400
  "Function call is missing a thought_signature"）；哨兵值 200。
- claude 系 / gpt-oss：fc.id 与 fr.id 必须成对回传（缺 → 400），无签名要求；
  上游不校验 id 等于原值（自造 id 配对也 200）。
- 三种都容忍哨兵值。

因此实现是：响应方向自造唯一 id + 签名进进程内 LRU（id 当回环通道）；
请求方向凭 id 还原 + fc.id/fr.id 配对 + gemini-3 缺签名注哨兵。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from buddy_proxy.gemini import thought_signature as ts
from buddy_proxy.gemini.convert import (
    chat_to_gemini_request,
    gemini_response_to_chat,
    new_tool_call,
)


@pytest.fixture(autouse=True)
def _clean_cache():
    ts._CACHE.clear()
    yield
    ts._CACHE.clear()


# ---------------------------------------------------------------------------
# 缓存模块
# ---------------------------------------------------------------------------

def test_remember_lookup_roundtrip():
    ts.remember("call_1", signature="sig", name="get_weather")
    assert ts.lookup("call_1") == ("sig", "get_weather")
    assert ts.lookup("call_missing") == ("", "")
    assert ts.lookup("") == ("", "")


def test_lru_eviction():
    for i in range(ts.MAX_ENTRIES + 10):
        ts.remember(f"call_{i}", signature=f"s{i}")
    assert ts.lookup("call_0") == ("", "")  # 最老的被弹出
    assert ts.lookup(f"call_{ts.MAX_ENTRIES + 9}")[0] == f"s{ts.MAX_ENTRIES + 9}"
    assert len(ts._CACHE) == ts.MAX_ENTRIES


def test_needs_signature_matrix():
    assert ts.needs_signature("gemini-3.1-pro-low")
    assert ts.needs_signature("gemini-3.8-flash-tiered")
    assert ts.needs_signature("gemini-2.5-flash")  # 2.5 起有签名机制，保守纳入
    assert not ts.needs_signature("claude-sonnet-4-6")
    assert not ts.needs_signature("gpt-oss-120b-medium")


def test_resolve_signature_priority():
    ts.remember("call_1", signature="cached")
    # 显式 > 缓存
    assert ts.resolve_signature("call_1", "explicit", model="gemini-3.1-pro-low") == "explicit"
    assert ts.resolve_signature("call_1", "", model="gemini-3.1-pro-low") == "cached"
    # gemini-3 缓存 miss → 哨兵
    assert ts.resolve_signature("call_x", "", model="gemini-3.1-pro-low") == ts.SENTINEL
    # 非 gemini-3 缓存 miss → 空（不注哨兵，上游不校验）
    assert ts.resolve_signature("call_x", "", model="claude-sonnet-4-6") == ""
    # 非 gemini-3 但显式带签名 → 保留（用户手动指定也不拦）
    assert ts.resolve_signature("call_x", "manual", model="claude-sonnet-4-6") == "manual"


# ---------------------------------------------------------------------------
# 响应方向：自造 id + 签名入缓存
# ---------------------------------------------------------------------------

def test_response_mints_id_and_caches_signature():
    payload = {"response": {"candidates": [{
        "content": {"parts": [{"functionCall": {
            "name": "get_weather", "args": {"city": "SF"}, "id": "call_850216"},
            "thoughtSignature": "real-sig"}]},
        "finishReason": "STOP",
    }]}}
    chat = gemini_response_to_chat(payload, model="gemini-3.1-pro-low")
    tc = chat["choices"][0]["message"]["tool_calls"][0]
    # 自造 id：不沿用上游短计数器 id（跨响应撞车会串缓存）
    assert tc["id"].startswith("call_") and tc["id"] != "call_850216"
    assert ts.lookup(tc["id"]) == ("real-sig", "get_weather")
    # OpenAI 扩展字段仍透出（直连 OpenAI 客户端原样回传即命中）
    assert tc["gemini_thought_signature"] == "real-sig"


def test_new_tool_call_mints_unique_ids():
    a = new_tool_call({"name": "f", "args": {}})
    b = new_tool_call({"name": "f", "args": {}})
    assert a["id"] != b["id"]


# ---------------------------------------------------------------------------
# 请求方向：id 还原签名 + fc/fr 配对 + name 还原
# ---------------------------------------------------------------------------

def _body_with_tool_round(call_id: str, tool_msg_extra: dict | None = None):
    tool_msg = {"role": "tool", "tool_call_id": call_id, "content": '{"ok": true}'}
    tool_msg.update(tool_msg_extra or {})
    return {
        "model": "m",
        "messages": [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city": "SF"}'}}]},
            tool_msg,
        ],
    }


def test_request_restores_signature_and_pairs_ids_for_gemini3():
    ts.remember("call_abc", signature="sig-xyz", name="get_weather")
    req = chat_to_gemini_request(
        _body_with_tool_round("call_abc"), project_id="p",
        model="gemini-3.1-pro-low", stream=False)
    contents = req["request"]["contents"]
    fc_part = contents[1]["parts"][0]
    assert fc_part["thoughtSignature"] == "sig-xyz"
    assert fc_part["functionCall"]["id"] == "call_abc"
    fr = contents[2]["parts"][0]["functionResponse"]
    assert fr["id"] == "call_abc"  # fr.id 与 fc.id 成对（claude/gpt-oss 硬要求）
    assert fr["name"] == "get_weather"  # tool 消息没带 name → 缓存还原真名


def test_request_sentinel_fallback_when_cache_miss_for_gemini3():
    """进程重启/缓存驱逐后的兜底：gemini-3 缺签名注哨兵（实证 200）。"""
    req = chat_to_gemini_request(
        _body_with_tool_round("call_unknown"), project_id="p",
        model="gemini-3.8-flash-tiered", stream=False)
    fc_part = req["request"]["contents"][1]["parts"][0]
    assert fc_part["thoughtSignature"] == ts.SENTINEL


def test_request_no_sentinel_for_non_gemini3():
    """claude/gpt-oss 不需要签名，缓存 miss 不注哨兵（干净请求体）。"""
    req = chat_to_gemini_request(
        _body_with_tool_round("call_unknown"), project_id="p",
        model="claude-sonnet-4-6", stream=False)
    fc_part = req["request"]["contents"][1]["parts"][0]
    assert "thoughtSignature" not in fc_part
    # 但 id 配对仍要带上
    assert fc_part["functionCall"]["id"] == "call_unknown"


def test_request_explicit_signature_wins_over_cache():
    """OpenAI 客户端原样回传扩展字段 → explicit 命中（即使缓存没这条）。"""
    body = _body_with_tool_round("call_openai_style")
    body["messages"][1]["tool_calls"][0]["gemini_thought_signature"] = "from-client"
    req = chat_to_gemini_request(body, project_id="p",
                                 model="gemini-3.1-pro-low", stream=False)
    fc_part = req["request"]["contents"][1]["parts"][0]
    assert fc_part["thoughtSignature"] == "from-client"


def test_request_name_resolution_prefers_tool_message_name():
    ts.remember("call_abc", signature="sig", name="cached_name")
    req = chat_to_gemini_request(
        _body_with_tool_round("call_abc", {"name": "explicit_name"}),
        project_id="p", model="gemini-3.1-pro-low", stream=False)
    fr = req["request"]["contents"][2]["parts"][0]["functionResponse"]
    assert fr["name"] == "explicit_name"


# ---------------------------------------------------------------------------
# Anthropic 端到端回环（Claude Code 真实形态：丢签名、只回 id）
# ---------------------------------------------------------------------------

def test_anthropic_round_trip_restores_signature():
    """模拟完整链路：第一轮响应自造 id+签名入缓存；第二轮 Claude Code
    请求（tool_use/tool_result 只带 id、无签名）→ 签名还原回上游 part。"""
    from buddy_proxy.protocols.anthropic_adapter import anthropic_to_chat
    from buddy_proxy.antigravity.convert import chat_to_antigravity_request

    # 第一轮：上游返回 functionCall + 签名，响应转换自造 id 并入缓存
    payload = {"response": {"candidates": [{
        "content": {"parts": [{"functionCall": {
            "name": "Bash", "args": {"command": "ls"}, "id": "call_847123"},
            "thoughtSignature": "REAL-SIG"}]},
        "finishReason": "STOP",
    }]}}
    chat = gemini_response_to_chat(payload, model="gemini-3.8-flash-tiered")
    minted = chat["choices"][0]["message"]["tool_calls"][0]["id"]

    # 第二轮：Claude Code 的请求（Anthropic 协议没有签名字段，只能回 id）
    anthropic_body = {
        "model": "antigravity/gemini-3.8-flash",
        "messages": [
            {"role": "user", "content": "run ls"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": minted, "name": "Bash",
                 "input": {"command": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": minted,
                 "content": "file1"}]},
        ],
    }
    chat_body = anthropic_to_chat(anthropic_body)
    upstream = chat_to_antigravity_request(
        chat_body, project_id="p", model="gemini-3.8-flash-tiered")
    contents = upstream["request"]["contents"]
    fc_part = next(p for c in contents for p in c["parts"] if "functionCall" in p)
    assert fc_part["thoughtSignature"] == "REAL-SIG"  # 经 id 回环还原
    assert fc_part["functionCall"]["id"] == minted
    # functionResponse 同 id 配对
    fr_part = next(p for c in contents for p in c["parts"] if "functionResponse" in p)
    assert fr_part["functionResponse"]["id"] == minted
    assert fr_part["functionResponse"]["name"] == "Bash"


def test_anthropic_round_trip_sentinel_after_restart():
    """进程重启（缓存空）：Claude Code 回 id 但缓存 miss → 哨兵兜底，不再 400。"""
    from buddy_proxy.protocols.anthropic_adapter import anthropic_to_chat
    from buddy_proxy.antigravity.convert import chat_to_antigravity_request

    anthropic_body = {
        "model": "antigravity/gemini-3.8-flash",
        "messages": [
            {"role": "user", "content": "run ls"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_deadbeef", "name": "Bash",
                 "input": {"command": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_deadbeef",
                 "content": "file1"}]},
        ],
    }
    chat_body = anthropic_to_chat(anthropic_body)
    upstream = chat_to_antigravity_request(
        chat_body, project_id="p", model="gemini-3.8-flash-tiered")
    fc_part = next(p for c in upstream["request"]["contents"]
                   for p in c["parts"] if "functionCall" in p)
    assert fc_part["thoughtSignature"] == ts.SENTINEL


# ---------------------------------------------------------------------------
# 流式路径
# ---------------------------------------------------------------------------

def test_stream_tool_call_minted_id_and_cached():
    import httpx

    from buddy_proxy.gemini.provider import _to_openai_stream

    inner = {"candidates": [{
        "content": {"parts": [{"functionCall": {
            "name": "get_weather", "args": {"city": "SF"}, "id": "call_999"},
            "thoughtSignature": "stream-sig"}]},
        "finishReason": "STOP",
    }]}
    line = "data: " + json.dumps({"response": inner}) + "\n\n"
    resp = httpx.Response(
        200, content=line.encode(),
        headers={"content-type": "text/event-stream"},
        request=httpx.Request("POST", "https://x/"),
    )

    async def _go():
        out = []
        async for piece in _to_openai_stream(resp, "gemini-3.1-pro-low"):
            out.append(piece)
        return "".join(out)

    text = asyncio.run(_go())
    tool_lines = [l for l in text.splitlines()
                  if l.startswith("data:") and '"tool_calls"' in l]
    assert tool_lines, text
    delta = json.loads(tool_lines[0][5:])["choices"][0]["delta"]["tool_calls"][0]
    assert delta["id"].startswith("call_") and delta["id"] != "call_999"
    assert ts.lookup(delta["id"]) == ("stream-sig", "get_weather")
