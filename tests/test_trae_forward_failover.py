"""trae work forward 多账号 failover 循环单测。

核心新行为：`_forward_once` 抛 401/429 时冷却当前账号、换下一个；非账号级错误
（502）直接透传；PAT 子类（_pat_variant=True）单账号直通不卷入 work 循环。
用 mock `_forward_once` 按调用序返回/抛错，断言冷却与换号顺序。conftest 已隔离
TRAE_WORK_STATE_DIR。异步 forward 经 asyncio.run 驱动（项目未配 pytest-asyncio）。
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from buddy_proxy.trae import failover
from buddy_proxy.trae.credentials import _CURRENT_WORK_ACCOUNT, save_account_cred
from buddy_proxy.trae.provider import TraeProvider


def _cred(uid: str, nickname: str = "T") -> dict:
    return {
        "uid": uid, "nickname": nickname,
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def _seed_accounts(*uids: str):
    return [save_account_cred(_cred(u, u.title())) for u in uids]


def _ok_response() -> JSONResponse:
    return JSONResponse(content={"choices": [], "object": "chat.completion"})


def _forward(p: TraeProvider, protocol: str = "openai"):
    return p.forward({"model": "m", "messages": [{"role": "user", "content": "hi"}]}, protocol)


# -- 换号逻辑 -----------------------------------------------------------------


def test_401_fails_over_to_next_account():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth expired")
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())

    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id]
    assert failover.cooldown_left(accounts[0].id)[0] > 0   # 401 → 冷却
    assert failover.cooldown_left(accounts[1].id)[0] == 0  # 成功的账号不冷却


def test_429_marks_quota_cooldown():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        raise HTTPException(status_code=429, detail="quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 429
    # u1 是 429（账号级）→ 冷却 quota 档；u2 是最后一个账号，直接 raise 不再冷却
    _l1, k1 = failover.cooldown_left(accounts[0].id)
    _l2, k2 = failover.cooldown_left(accounts[1].id)
    assert k1 == "quota"
    assert k2 == "" and _l2 == 0.0


def test_last_account_error_reraised():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth")
        raise HTTPException(status_code=429, detail="quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 429  # 最后一个账号的错误
    assert calls == [accounts[0].id, accounts[1].id]


def test_non_account_error_passthrough_no_failover():
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        calls.append(_CURRENT_WORK_ACCOUNT.get())
        raise HTTPException(status_code=502, detail="channel down")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 502
    assert calls == [accounts[0].id]              # 没换号
    assert failover.cooldown_left(accounts[0].id)[0] == 0  # 没冷却


def test_anthropic_nonstream_error_fails_over():
    """关键回归：anthropic 非流式遇 401 必须换号，不能吞成 JSONResponse 返回。

    修复前 _forward_once 对 anthropic 非流式把 HTTPException return 成 JSONResponse，
    循环收不到异常 → 不换号不冷却。现在应冷却 #1 并试 #2。
    """
    accounts = _seed_accounts("u1", "u2")
    p = TraeProvider()
    calls: list[str] = []

    async def fake_forward_once(body, protocol, original, requested_model,
                                prompt, messages, tools, native, stream, agent_mode):
        acct = _CURRENT_WORK_ACCOUNT.get()
        calls.append(acct)
        if acct == accounts[0].id:
            raise HTTPException(status_code=401, detail="auth")
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, protocol="anthropic")
    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls == [accounts[0].id, accounts[1].id]
    assert failover.cooldown_left(accounts[0].id)[0] > 0


def test_anthropic_nonstream_final_error_has_anthropic_shape():
    _seed_accounts("u1")
    p = TraeProvider()

    async def fake_forward_once(*a, **k):
        raise HTTPException(status_code=429, detail="quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p, protocol="anthropic")
    resp = asyncio.run(run())
    assert resp.status_code == 429
    body = json.loads(resp.body.decode())
    assert body.get("type") == "error"
    assert "error" in body


# -- PAT 直通 -----------------------------------------------------------------


class _PatProvider(TraeProvider):
    _pat_variant = True


def test_pat_variant_single_shot_no_failover():
    """PAT 子类（_pat_variant=True）单次转发，不循环、不冷却任何 work 账号。"""
    _seed_accounts("u1", "u2")
    p = _PatProvider()
    calls = 0

    async def fake_forward_once(*a, **k):
        nonlocal calls
        calls += 1
        return _ok_response()

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            return await _forward(p)
    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert calls == 1
    assert failover.cooldown_left("u1")[0] == 0
    assert failover.cooldown_left("u2")[0] == 0


def test_pat_variant_error_reraised_no_cooldown():
    _seed_accounts("u1", "u2")
    p = _PatProvider()

    async def fake_forward_once(*a, **k):
        raise HTTPException(status_code=429, detail="pat quota")

    async def run():
        with mock.patch.object(TraeProvider, "_forward_once", side_effect=fake_forward_once):
            with pytest.raises(HTTPException) as ei:
                await _forward(p)
            return ei.value
    exc = asyncio.run(run())
    assert exc.status_code == 429
    assert failover.cooldown_left("u1")[0] == 0
    assert failover.cooldown_left("u2")[0] == 0
