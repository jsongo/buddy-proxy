"""antigravity 多账号 failover 单元测试。

httpx.MockTransport 离线打桩（按 Bearer token 路由到「账号」），不触网。
冷却状态是模块级内存 dict，autouse fixture 每用例清零防串扰。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest


# ---------------------------------------------------------------------------
# 打桩基建
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    from buddy_proxy.antigravity import failover

    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _gemini_ok(text: str = "pong") -> dict:
    return {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}],
        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3},
    }


def _sse(lines: list[str]) -> bytes:
    return "".join(f"data: {line}\n\n" for line in lines).encode()


_SSE_OK = _sse([
    json.dumps({"candidates": [{"content": {"parts": [{"text": "pong"}]}}]}),
    json.dumps({"usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 2}}),
])
_SSE_429 = _sse([json.dumps({"error": {"code": 429, "message": "RESOURCE_EXHAUSTED: quota"}})])


class _Upstream:
    """按 Bearer token（=账号）路由 canned 响应，记录调用顺序。"""

    def __init__(self):
        self.calls: list[str] = []  # 命中的 token 顺序
        self.plan: dict[str, tuple] = {}  # token -> (status, payload, headers)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        tok = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.calls.append(tok)
        status, payload, headers = self.plan[tok]
        if isinstance(payload, (bytes, str)):
            return httpx.Response(status, content=payload, headers=headers)
        return httpx.Response(status, json=payload, headers=headers)


@pytest.fixture
def two_accounts():
    """真实写盘两个账号（u@x.com 主、v@y.com 备），返回 (provider, upstream)。"""
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity.provider import AntigravityProvider

    creds.save_account_cred({"access_token": "a1", "refresh_token": "r1",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    creds.save_account_cred({"access_token": "a2", "refresh_token": "r2",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "v@y.com", "project_id": "p2"})

    upstream = _Upstream()
    provider = AntigravityProvider()
    import buddy_proxy.antigravity.provider as prov

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream),
                                         timeout=prov._TIMEOUT)
    return provider, upstream


def _patch_token(monkeypatch, refreshes: list[str] | None = None):
    import buddy_proxy.antigravity.provider as prov

    def fake(account_id, *, force_refresh=False):
        if refreshes is not None and force_refresh:
            refreshes.append(account_id)
        return f"tok-{account_id}", {"project_id": f"proj-{account_id}"}

    monkeypatch.setattr(prov, "ensure_account_token", fake)


_BODY = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}


def _run(provider, stream=False):
    from buddy_proxy.antigravity.provider import MODELS

    body = {**_BODY, "model": f"antigravity/{MODELS[0]['id']}", "stream": stream}
    return asyncio.run(provider.forward(body, "openai"))


def _stream_text(sr) -> str:
    async def _collect():
        chunks = []
        async for chunk in sr.body_iterator:
            chunks.append(chunk.encode() if isinstance(chunk, str) else chunk)
        return b"".join(chunks).decode()

    return asyncio.run(_collect())


# ---------------------------------------------------------------------------
# 转发 failover
# ---------------------------------------------------------------------------

def test_429_switches_account_and_respects_retry_after(two_accounts, monkeypatch):
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (429, {"error": {"code": 429, "message": "quota"}}, {"Retry-After": "90"})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200 and b"pong" in resp.body
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]  # 主账号失败才动备用
    left, kind = failover.cooldown_left("u@x.com")
    assert 0 < left <= 90 and kind == "quota"  # Retry-After 覆盖默认 300s
    assert failover.cooldown_left("v@y.com")[0] == 0


def test_403_switches_account_short_cooldown(two_accounts, monkeypatch):
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (403, {"error": {"code": 403, "message": "PERMISSION_DENIED"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 200
    left, kind = failover.cooldown_left("u@x.com")
    assert 0 < left <= 60 and kind == "account"


def test_401_forces_refresh_then_switches(two_accounts, monkeypatch):
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (401, {"error": {"code": 401, "message": "Unauthorized"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    refreshes: list[str] = []
    _patch_token(monkeypatch, refreshes)

    resp = _run(provider)
    assert resp.status_code == 200
    assert refreshes == ["u@x.com"]  # 401 先强刷同账号一次
    assert up.calls == ["tok-u@x.com", "tok-u@x.com", "tok-v@y.com"]  # 强刷重发后仍拒才换号
    assert failover.cooldown_left("u@x.com")[0] > 0


def test_all_accounts_failed_maps_last_status(two_accounts, monkeypatch):
    from fastapi import HTTPException

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (429, {"error": {"code": 429, "message": "quota a"}}, {})
    up.plan["tok-v@y.com"] = (429, {"error": {"code": 429, "message": "quota b"}}, {})
    _patch_token(monkeypatch)

    with pytest.raises(HTTPException) as ei:
        _run(provider)
    assert ei.value.status_code == 429
    msg = ei.value.detail["error"]["message"]
    assert "所有账号均不可用" in msg and "quota b" in msg  # 带上最后错误详情


def test_business_4xx_passthrough_without_switching(two_accounts, monkeypatch):
    """业务 4xx 换号没意义：原样透传，不冷却也不打下一个账号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (400, {"error": {"code": 400, "message": "bad request"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    resp = _run(provider)
    assert resp.status_code == 400
    assert up.calls == ["tok-u@x.com"]
    assert failover.cooldown_left("u@x.com") == (0.0, "")


def test_no_available_accounts_fast_429(two_accounts):
    from fastapi import HTTPException

    from buddy_proxy.antigravity import failover

    provider, _up = two_accounts
    failover.mark_cooldown("u@x.com", quota=True)
    failover.mark_cooldown("v@y.com", quota=True)

    with pytest.raises(HTTPException) as ei:
        _run(provider)
    assert ei.value.status_code == 429
    assert "冷却中" in ei.value.detail["error"]["message"]


def test_account_meta_records_selected_account(two_accounts, monkeypatch):
    from buddy_proxy.core.metrics import ACCOUNT_META

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (429, {"error": {"code": 429, "message": "quota"}}, {})
    up.plan["tok-v@y.com"] = (200, _gemini_ok(), {})
    _patch_token(monkeypatch)

    meta: dict = {}
    token = ACCOUNT_META.set(meta)
    try:
        resp = _run(provider)
    finally:
        ACCOUNT_META.reset(token)
    assert resp.status_code == 200
    assert meta["account"] == "v@y.com"  # metrics 落库的是实际服务的账号


# ---------------------------------------------------------------------------
# 流式首事件闸门
# ---------------------------------------------------------------------------

def test_stream_gate_switches_on_in_band_429(two_accounts, monkeypatch):
    """HTTP 200 但首条 SSE 事件是 429 error：未出字节，冷却换号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _SSE_429, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    text = _stream_text(sr)
    assert "RESOURCE_EXHAUSTED" not in text  # 客户端没吃到失败账号的错误
    assert failover.cooldown_left("u@x.com")[0] > 0
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]


def test_stream_gate_committed_after_candidates(two_accounts, monkeypatch):
    """首条事件已是 candidates：语义已至，缓冲行补放、绝不换号。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, _SSE_OK, {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    text = _stream_text(sr)
    assert "pong" in text and "data:" in text
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com"]


def test_stream_gate_eof_switches_without_cooldown(two_accounts, monkeypatch):
    """语义事件前 EOF（假成功）：换号但不冷却（可能只是网络抖动）。"""
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    up.plan["tok-u@x.com"] = (200, b"", {})
    up.plan["tok-v@y.com"] = (200, _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert "pong" in _stream_text(sr)
    assert failover.cooldown_left("u@x.com") == (0.0, "")
    assert up.calls == ["tok-u@x.com", "tok-v@y.com"]


def test_stream_gate_buffer_cap_committed(two_accounts, monkeypatch):
    """闸门缓冲超过上限：按 committed 透传放行，不无界攒内存也不冷却。"""
    import buddy_proxy.antigravity.provider as prov
    from buddy_proxy.antigravity import failover

    provider, up = two_accounts
    keepalive = b": keepalive\n\n" * (prov._GATE_BUFFER_MAX_LINES + 10)
    up.plan["tok-u@x.com"] = (200, keepalive + _SSE_OK, {})
    _patch_token(monkeypatch)

    sr = _run(provider, stream=True)
    assert sr.status_code == 200
    assert failover.cooldown_left("u@x.com") == (0.0, "")  # 放行不冷却
    assert up.calls == ["tok-u@x.com"]  # 不换号


# ---------------------------------------------------------------------------
# failover 模块本身
# ---------------------------------------------------------------------------

def test_mark_cooldown_retry_after_clamp():
    from buddy_proxy.antigravity import failover

    failover.mark_cooldown("a", retry_after="2", quota=True)
    assert 1 < failover.cooldown_left("a")[0] <= 2
    failover.mark_cooldown("b", retry_after="99999999", quota=True)  # 离谱值 → 钳 7d
    assert failover.cooldown_left("b")[0] <= 7 * 86400
    failover.mark_cooldown("c", retry_after="not-a-number", quota=True)  # 解析失败 → 默认
    assert failover.cooldown_left("c")[0] <= failover._QUOTA_COOLDOWN_S
    failover.mark_cooldown("d")  # 非 quota → 60s 档
    left, kind = failover.cooldown_left("d")
    assert kind == "account" and left <= failover._ACCOUNT_COOLDOWN_S


def test_available_accounts_filters_and_orders():
    from buddy_proxy.antigravity import credentials as creds, failover

    creds.save_account_cred({"access_token": "a", "refresh_token": "r", "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com"})
    creds.save_account_cred({"access_token": "b", "refresh_token": "r2", "expiry": "2099-01-01T00:00:00+00:00", "email": "v@y.com"})
    creds.save_account_cred({"access_token": "c", "refresh_token": "r3", "expiry": "2099-01-01T00:00:00+00:00", "email": "w@z.com"})

    failover.mark_cooldown("v@y.com", quota=True)
    ids = [a.id for a in failover.available_accounts()]
    assert ids == ["u@x.com", "w@z.com"]  # 冷却的中间账号被剔除，顺位不变


def test_accounts_status_shape():
    from buddy_proxy.antigravity import credentials as creds, failover

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    failover.mark_cooldown("u@x.com", quota=True)

    status = failover.accounts_status()
    assert status["enabled"] is True
    acct = status["accounts"][0]
    assert acct["id"] == "u@x.com" and acct["index"] == 1
    assert acct["project_id"] == "p1" and acct["token"] == "ok"
    assert acct["hours_left"] is not None  # token 有效期还早
    assert acct["cooling"] and acct["cooling"][0]["kind"] == "quota"

    creds.account_cred_path("u@x.com").unlink()
    assert failover.accounts_status()["enabled"] is False  # 自愈后空列表


def test_ui_endpoint_antigravity_accounts():
    """/ui/api/antigravity/accounts 冒烟：端点直连 failover.accounts_status。"""
    import asyncio
    import types

    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.web.ui import channels as web_ui  # 2026-10-03 拆包后端点在 channels

    creds.save_account_cred({"access_token": "a", "refresh_token": "r",
                             "expiry": "2099-01-01T00:00:00+00:00",
                             "email": "u@x.com", "project_id": "p1"})
    request = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    out = asyncio.run(web_ui.ui_antigravity_accounts(request))
    assert out["enabled"] is True
    assert out["accounts"][0]["email"] == "u@x.com"
