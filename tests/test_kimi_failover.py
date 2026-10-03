"""kimi 转发主链路 + 多账号 failover 测试（离线，httpx.MockTransport）。

模式照 test_antigravity_failover.py：
- 假上游按 ``Authorization: Bearer <token>`` 路由 canned 响应，token 由打桩的
  ``ensure_account_token`` 生成（``tok-<account_id>``），顺带断言账号归属；
- 流式响应用 ``_ChunkedBody`` 真实一次性流——httpx 判「消费过」就是消费过，
  不给「测试重开流掩盖双重消费 bug」留余地；
- 闸门类场景（流式）必须在**单个 event loop** 里跑完 forward + 消费：
  ``_ReplayStream`` 续跑的行迭代器跨 asyncio.run 边界会炸。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.core.metrics import ACCOUNT_META
from buddy_proxy.kimi import credentials, failover
from buddy_proxy.kimi import provider as kimi_provider
from buddy_proxy.kimi.credentials import save_account_cred
from buddy_proxy.kimi.provider import KimiProvider


# ---------------------------------------------------------------------------
# 基建
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _write_account(acct_id: str, *, nickname: str = "") -> None:
    save_account_cred({
        "account_id": acct_id,
        "type": "kimi",
        "access_token": f"stale-{acct_id}",  # 转发用打桩 token，盘上的值无谓
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "base_url": "https://api.kimi.test/coding",
        "oauth_host": "https://auth.kimi.com",
        "device_id": f"dev-{acct_id}",
        "nickname": nickname,
    })


class _ChunkedBody(httpx.AsyncByteStream):
    """真实一次性流：分块交付，闸门消费过就不许再开。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class _RaisingStream(httpx.AsyncByteStream):
    """迭代即抛读超时（首事件前挂死）。"""

    async def __aiter__(self):
        raise httpx.TimeoutException("read timed out")
        yield b""  # pragma: no cover - 让函数成为 async generator


class _Upstream:
    """按 Bearer token 路由 canned 响应；记录每次命中的 token（= 账号归属）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.routes: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        self.calls.append(token)
        route = self.routes.get(token)
        if route is None:
            return httpx.Response(500, json={"error": {"message": f"no route {token}"}})
        return route(request)


def _patch_token(monkeypatch) -> list[tuple[str, bool]]:
    """打桩 ensure_account_token（provider 命名空间）：token 与 cred 同源返回。"""
    calls: list[tuple[str, bool]] = []

    def fake(account_id, *, force_refresh=False):
        calls.append((account_id, force_refresh))
        return (f"tok-{account_id}", {
            "base_url": "https://api.kimi.test/coding",
            "device_id": f"dev-{account_id}",
        })

    monkeypatch.setattr("buddy_proxy.kimi.provider.ensure_account_token", fake)
    return calls


def _sse(chunks: list[dict], *, finish: str | None = "stop", done: bool = True) -> bytes:
    """OpenAI SSE 响应体：语义 chunk + （可选）finish/usage chunk + （可选）[DONE]。"""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps({"choices": [{"index": 0, "delta": c}]}) + "\n\n")
    if finish:
        lines.append("data: " + json.dumps({
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": {"total_tokens": 3},
        }) + "\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _stream_resp(*body_chunks: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          stream=_ChunkedBody(list(body_chunks)))


def _ok_stream(text: str = "pong") -> httpx.Response:
    return _stream_resp(_sse([{"role": "assistant", "content": text}]))


def _rate_limited(status: int, retry_after: str | None = None):
    headers = {"Retry-After": retry_after} if retry_after else None
    return lambda req: httpx.Response(status, json={"error": {"message": f"HTTP {status}"}},
                                      headers=headers)


@pytest.fixture
def two_accounts():
    _write_account("acct-a", nickname="A 号")   # priority 0（主号）
    _write_account("acct-b", nickname="B 号")   # priority 1（备号）
    upstream = _Upstream()
    prov = KimiProvider()
    prov._client = httpx.AsyncClient(
        transport=httpx.MockTransport(upstream),
        timeout=kimi_provider._TIMEOUT_NONSTREAM)
    return prov, upstream


def _body(**over) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "kimi/kimi-for-coding",  # 前缀在 forward 里剥掉
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    body.update(over)
    return body


def _run(prov: KimiProvider, body: dict, protocol: str = "openai"):
    return asyncio.run(prov.forward(body, protocol))


def _collect(prov: KimiProvider, body: dict, protocol: str = "openai") -> str:
    """流式：单 loop 跑完 forward + 消费（见模块 docstring）。"""

    async def one() -> str:
        resp = await prov.forward(body, protocol)
        parts = []
        async for chunk in resp.body_iterator:
            parts.append(chunk.decode() if isinstance(chunk, (bytes, bytearray)) else str(chunk))
        return "".join(parts)

    return asyncio.run(one())


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

def test_openai_stream_ok(two_accounts, monkeypatch):
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    assert up.calls == ["tok-acct-a"]  # 主号健康：备号不动


def test_openai_nonstream_ok(two_accounts, monkeypatch):
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: httpx.Response(200, json={
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}]})
    resp = _run(prov, _body())
    assert resp.status_code == 200
    assert json.loads(resp.body)["choices"][0]["message"]["content"] == "pong"


def test_anthropic_nonstream_thinking_block(two_accounts, monkeypatch):
    """非流式 anthropic：reasoning_content → thinking 块且排在 text 前。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: httpx.Response(200, json={
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "答案",
                                 "reasoning_content": "想一想"},
                     "finish_reason": "stop"}]})
    resp = _run(prov, _body(), "anthropic")
    data = json.loads(resp.body)
    types = [b["type"] for b in data["content"]]
    assert types[0] == "thinking"
    assert data["content"][0]["thinking"] == "想一想"
    assert "答案" in json.dumps(data, ensure_ascii=False)


def test_anthropic_stream_thinking_events(two_accounts, monkeypatch):
    """流式 anthropic：reasoning_content 走 AnthropicStreamConverter → thinking 块。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: _stream_resp(_sse([
        {"role": "assistant", "reasoning_content": "思考中"},
        {"content": "答案"},
    ]))
    out = _collect(prov, _body(stream=True), "anthropic")
    assert "message_start" in out
    assert "思考中" in out and "答案" in out
    assert "message_stop" in out


def test_account_meta_tagged(two_accounts, monkeypatch):
    """metrics 账号归属：ACCOUNT_META 记录实际服务的账号。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: httpx.Response(200, json={
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                     "finish_reason": "stop"}]})
    meta: dict = {}
    ACCOUNT_META.set(meta)
    _run(prov, _body())
    assert meta.get("account") == "acct-a"


# ---------------------------------------------------------------------------
# 换号矩阵（HTTP 状态码分诊）
# ---------------------------------------------------------------------------

def test_429_switches_and_honors_retry_after(two_accounts, monkeypatch):
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = _rate_limited(429, retry_after="120")
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    assert up.calls == ["tok-acct-a", "tok-acct-b"]
    left, kind = failover.cooldown_left("acct-a")
    assert kind == "quota"
    assert 110 <= left <= 120  # Retry-After 覆盖默认 300s


def test_403_switches_with_account_cooldown(two_accounts, monkeypatch):
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = _rate_limited(403)
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    left, kind = failover.cooldown_left("acct-a")
    assert kind == "account"  # 403 是账号级问题，短冷却
    assert 50 <= left <= 60


def test_401_force_refresh_then_switch(two_accounts, monkeypatch):
    """401：强刷一次重试同账号，仍拒才冷却换号（refresh_token 可能已轮换）。"""
    prov, up = two_accounts
    token_calls = _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = _rate_limited(401)
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    assert ("acct-a", True) in token_calls  # force_refresh 过
    assert up.calls.count("tok-acct-a") == 2  # 同账号重试过一次
    _, kind = failover.cooldown_left("acct-a")
    assert kind == "account"


def test_business_400_passthrough_no_switch(two_accounts, monkeypatch):
    """其余业务 4xx（模型名不合法等）：换号没意义，原样透传且不冷却。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: httpx.Response(
        400, json={"error": {"message": "bad model"}})
    resp = _run(prov, _body())
    assert resp.status_code == 400
    assert json.loads(resp.body)["error"]["message"] == "bad model"
    assert up.calls == ["tok-acct-a"]
    assert failover.cooldown_left("acct-a") == (0.0, "")


def test_missing_cred_cooldowns_and_switches(two_accounts, monkeypatch):
    """凭据没了（AuthError）：冷却该账号跳到下一个。"""
    prov, up = two_accounts

    def fake(account_id, *, force_refresh=False):
        if account_id == "acct-a":
            raise credentials.AuthError("凭据不存在或已损坏")
        return (f"tok-{account_id}", {"base_url": "https://api.kimi.test/coding",
                                       "device_id": f"dev-{account_id}"})

    monkeypatch.setattr("buddy_proxy.kimi.provider.ensure_account_token", fake)
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    _, kind = failover.cooldown_left("acct-a")
    assert kind == "account"


# ---------------------------------------------------------------------------
# 流式闸门
# ---------------------------------------------------------------------------

def test_in_band_429_error_switches(two_accounts, monkeypatch):
    """200 但带内 error 429：一个字节都没出网，冷却换号。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: _stream_resp(
        b'data: {"error":{"code":429,"message":"quota exhausted"}}\n\n', b"data: [DONE]\n\n")
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    _, kind = failover.cooldown_left("acct-a")
    assert kind == "quota"


def test_eof_before_semantic_switches_without_cooldown(two_accounts, monkeypatch):
    """空流假成功（只有 [DONE]）：换号，但不冷却（可能只是网络抖动）。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: _stream_resp(b"data: [DONE]\n\n")
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    assert failover.cooldown_left("acct-a") == (0.0, "")


def test_first_event_timeout_switches_with_cooldown(two_accounts, monkeypatch):
    """首事件前读超时：换号 + 短冷却（挂死账号别每轮都被首选）。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=_RaisingStream())
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out
    _, kind = failover.cooldown_left("acct-a")
    assert kind == "account"


def test_gate_buffered_lines_not_lost(two_accounts, monkeypatch):
    """闸门 committed 后：缓冲行补放 + 尾流续跑，字节级无损（#72 同坑回归）。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    head = _sse([{"role": "assistant", "content": "pon"}],
                finish=None, done=False)  # 闸门在此行 committed
    tail = _sse([{"content": "g"}])       # 之后才到达
    up.routes["tok-acct-a"] = lambda req: _stream_resp(head, tail)
    out = _collect(prov, _body(stream=True))
    assert out == (head + tail).decode()  # 缓冲行不重不漏，剩余流不丢
    assert '"content": "pon"' in out and '"content": "g"' in out  # 两段都到了客户端


# ---------------------------------------------------------------------------
# 通道级耗尽
# ---------------------------------------------------------------------------

def test_all_accounts_exhausted_reports_cooldowns(two_accounts, monkeypatch):
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = _rate_limited(429)
    up.routes["tok-acct-b"] = _rate_limited(429)
    with pytest.raises(HTTPException) as ei:
        _run(prov, _body(stream=True))
    assert ei.value.status_code == 429
    msg = ei.value.detail["error"]["message"]
    assert "acct-a" in msg and "acct-b" in msg  # cooldown_report 逐账号说明


def test_all_accounts_cooling_fast_fail(two_accounts, monkeypatch):
    """全部账号冷却中：不开新尝试，直接 429 快速失败。"""
    prov, up = two_accounts
    _patch_token(monkeypatch)
    failover.mark_cooldown("acct-a", quota=True)
    failover.mark_cooldown("acct-b", quota=True)
    with pytest.raises(HTTPException) as ei:
        _run(prov, _body())
    assert ei.value.status_code == 429
    assert up.calls == []


# ---------------------------------------------------------------------------
# UI 数据形状（failover.accounts_status）
# ---------------------------------------------------------------------------

def test_accounts_status_shape():
    _write_account("acct-a", nickname="A 号")
    failover.mark_cooldown("acct-a", quota=True, reason="测试")
    status = failover.accounts_status()
    assert status["enabled"] is True
    item = status["accounts"][0]
    assert item["id"] == "acct-a" and item["name"] == "A 号" and item["index"] == 1
    assert item["token"] == "ok"
    assert item["cooling"] == [{"kind": "quota", "minutes_left": pytest.approx(5.0, abs=0.1)}]


def test_in_band_access_terminated_switches_and_cools(two_accounts, monkeypatch):
    """带内 error 没有 code、只有 type=access_terminated_error（真机形状）。

    上游订阅失效时 403 是 {"error":{"message":...,"type":"access_terminated_error"}}
    且**不带 code**（code 恒 0）。只看 code 的话会判成「已 committed」直接透传，
    既不冷却也不换号——每轮 failover 照样先选中这个死账号白付一次往返。
    """
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: _stream_resp(
        b'data: {"error":{"message":"Your current subscription does not have access '
        b'to Kimi Code right now.","type":"access_terminated_error"}}\n\n',
        b"data: [DONE]\n\n")
    up.routes["tok-acct-b"] = lambda req: _ok_stream()
    out = _collect(prov, _body(stream=True))
    assert "pong" in out, "应换到第二个账号"
    assert failover.cooldown_left("acct-a")[1], "带内账号级错误要冷却，别每轮首选"


def test_clean_eof_reports_upstream_closed_not_na(two_accounts, monkeypatch):
    """干净 EOF（上游空响应）要给能看懂的原因，不是「最后错误 HTTP n/a」。

    不设 last_status/last_detail 时会落到 502 + n/a：用户分不清是上游断流
    还是网关坏了，token 过期这类可行动信息全被吞掉。
    """
    prov, up = two_accounts
    _patch_token(monkeypatch)
    up.routes["tok-acct-a"] = lambda req: _stream_resp(b"data: [DONE]\n\n")
    up.routes["tok-acct-b"] = lambda req: _stream_resp(b"data: [DONE]\n\n")
    with pytest.raises(HTTPException) as ei:
        _collect(prov, _body(stream=True))
    msg = str(ei.value.detail)
    assert "n/a" not in msg, msg
    assert "断流" in msg or "空响应" in msg, msg
