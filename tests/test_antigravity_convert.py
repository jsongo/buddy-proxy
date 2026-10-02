"""antigravity envelope 包装 / 身份清洗 / effort 后缀 / fingerprint 单元测试。"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# envelope：chat → antigravity 包装体
# ---------------------------------------------------------------------------

def _chat_body(**over):
    body = {
        "model": "gemini-3.8-flash",
        "messages": [
            {"role": "system", "content": "You are Claude Code, a helpful assistant."},
            {"role": "user", "content": "hi"},
        ],
    }
    body.update(over)
    return body


def test_envelope_shape():
    from buddy_proxy.antigravity.convert import chat_to_antigravity_request

    payload = chat_to_antigravity_request(_chat_body(), project_id="p1", model="gemini-3.8-flash")
    assert payload["project"] == "p1"
    assert payload["model"] == "gemini-3.8-flash"  # 裸名，无 models/ 前缀
    assert payload["userAgent"] == "antigravity"
    assert payload["requestType"] == "agent"
    assert payload["requestId"].startswith("agent-")
    req = payload["request"]
    assert "session_id" not in req  # 参考实现不发 session_id
    assert req["contents"][0]["parts"][0]["text"] == "hi"


def test_system_instruction_identity_and_scrub():
    from buddy_proxy.antigravity.convert import _SYSTEM_IDENTITY, chat_to_antigravity_request

    payload = chat_to_antigravity_request(_chat_body(), project_id="p", model="m")
    parts = payload["request"]["systemInstruction"]["parts"]
    assert payload["request"]["systemInstruction"]["role"] == "user"
    # 1) 身份断言 2) [ignore] 包裹副本 3) 用户 system（已被清洗）
    assert parts[0]["text"] == _SYSTEM_IDENTITY
    assert "[ignore]" in parts[1]["text"] and _SYSTEM_IDENTITY in parts[1]["text"]
    assert "You are Claude Code" not in parts[2]["text"]
    assert "the assistant" in parts[2]["text"]


def test_scrub_identity_rules_and_env(monkeypatch):
    from buddy_proxy.antigravity import convert

    assert convert.scrub_identity("You are Claude, made by Anthropic") == "You are the assistant, made by Anthropic"
    assert convert.scrub_identity("you are codex here") == "You are the assistant here"  # 大小写不敏感
    assert convert.scrub_identity("Claude is a model") == "Claude is a model"  # 非断言句式不动

    monkeypatch.setenv("ANTIGRAVITY_SCRUB_IDENTITY", "FooBar=>Baz, Hermes=>helper")
    assert convert.scrub_identity("You are FooBar") == "You are Baz"
    assert convert.scrub_identity("Hermes Agent") == "helper Agent"  # env 规则追加在默认后


def test_effort_suffix():
    from buddy_proxy.antigravity.convert import apply_effort_suffix

    assert apply_effort_suffix("gemini-3.1-pro", "high") == "gemini-3.1-pro-high"
    assert apply_effort_suffix("gemini-3.8-flash", "low") == "gemini-3.8-flash-low"
    assert apply_effort_suffix("gemini-3.8-flash", None) == "gemini-3.8-flash"
    assert apply_effort_suffix("gemini-3.8-flash", "medium") == "gemini-3.8-flash-medium"
    # 非 gemini3 系 / 已有后缀 / 非法 effort 都原样
    assert apply_effort_suffix("claude-sonnet-4-6", "high") == "claude-sonnet-4-6"
    assert apply_effort_suffix("gemini-3.8-flash-low", "high") == "gemini-3.8-flash-low"
    assert apply_effort_suffix("gemini-3.8-flash", "ultra") == "gemini-3.8-flash"


def test_response_reuse_from_gemini_convert():
    """响应方向直接复用 gemini.convert（同一 API 家族），冒烟确认 import 与解包。"""
    from buddy_proxy.antigravity.convert import gemini_response_to_chat

    chat = gemini_response_to_chat(
        {"response": {"candidates": [{"content": {"parts": [{"text": "pong"}]},
                                      "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2,
                                        "totalTokenCount": 3}}},
        model="gemini-3.8-flash",
    )
    assert chat["choices"][0]["message"]["content"] == "pong"
    assert chat["usage"]["total_tokens"] == 3


# ---------------------------------------------------------------------------
# fingerprint：UA / X-Client-Version
# ---------------------------------------------------------------------------

def test_fingerprint_headers(monkeypatch):
    from buddy_proxy.antigravity import fingerprint as fp

    monkeypatch.setenv("ANTIGRAVITY_UA_VERSION", "2.18.1")
    monkeypatch.setenv("ANTIGRAVITY_CLIENT_VERSION", "1.110.0")
    monkeypatch.setattr(fp, "_UA_VERSION", None)
    monkeypatch.setattr(fp, "_CLIENT_VERSION", None)

    h = fp.auth_headers("tok", "gemini-3.8-flash", stream=True)
    assert h["Authorization"] == "Bearer tok"
    assert h["User-Agent"].startswith("antigravity/2.18.1 ")
    assert h["User-Agent"].endswith(" gemini-3.8-flash")
    assert h["X-Client-Name"] == "antigravity"
    assert h["X-Client-Version"] == "1.110.0"
    assert "gl-node/" in h["x-goog-api-client"]
    assert h["Accept"] == "text/event-stream"  # 流式才有

    h2 = fp.auth_headers("tok", stream=False)
    assert "Accept" not in h2
    assert h2["User-Agent"] == fp.user_agent("")  # 无 model 不带尾巴


def test_fingerprint_fallback_without_product_json(monkeypatch):
    """本机没有 Antigravity.app 时用 fallback 版本（env 清空）。"""
    from buddy_proxy.antigravity import fingerprint as fp

    monkeypatch.delenv("ANTIGRAVITY_UA_VERSION", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_CLIENT_VERSION", raising=False)
    monkeypatch.setattr(fp, "_UA_VERSION", None)
    monkeypatch.setattr(fp, "_CLIENT_VERSION", None)
    monkeypatch.setattr(fp, "_product_json_paths", lambda: [])  # 模拟无安装

    assert fp.ua_version() == fp.FALLBACK_UA_VERSION
    assert fp.client_version() == fp.FALLBACK_CLIENT_VERSION
