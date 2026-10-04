"""DuMate（百度搭子）provider 单测。

锁定 2026-10-05 接入时的几个关键行为：

1. 模型目录：id 小写、含 ``/`` 的自动路由档（``dm-auto-model/text.L0``），
   同档只留最新款（glm-5 / qwen3.5 旧款被 kimi-k3 / qwen3.8-max 替换）。
2. 转发：openai 协议直通本地代理（URL 带 inapp key header），stream/非流式
   都原样回传；anthropic 协议 400 拒绝（DuMate 网关只认 OpenAI chat）。
3. 额度：本地 ``/api/dumate/points/remaining`` 的布尔态翻译成 1/1 条目。
4. 端点发现失败（App 未运行）时 ensure_auth 抛 401、forward 抛 503。

全部用 mock 隔离：不真发网络请求、不读真实进程环境。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.dumate import discovery, provider as dumate_provider
from buddy_proxy.dumate.provider import DumateProvider


def _endpoint(port: int = 52414, key: str = "a" * 64) -> discovery.DumateEndpoint:
    return discovery.DumateEndpoint(pid=9007, port=port, inapp_key=key,
                                    app_version="1.0.82.317")


# --- 模型目录 --------------------------------------------------------------


def test_models_catalog_latest_only():
    """同档只留最新款：旧 glm-5 / qwen3.5 不上目录（2026-10-05 用户要求）。"""
    p = DumateProvider()
    ids = [m["id"] for m in p.models()]
    assert "dm-auto-model/text.L0" in ids
    assert "kimi-k3" in ids
    assert "qwen3.8-max" in ids
    assert "glm-5" not in ids
    assert "qwen3.5-35b-a3b" not in ids


def test_models_id_keeps_slash_for_auto_router():
    """自动路由档 id 含 ``/``，路由剥 ``dumate/`` 前缀后原样透传上游。"""
    p = DumateProvider()
    auto = next(m for m in p.models() if m["id"] == "dm-auto-model/text.L0")
    assert auto["reasoning"] is True
    assert auto["tool_call"] is True


# --- 端点发现失败时的行为 ----------------------------------------------------


def test_ensure_auth_401_when_app_not_running():
    p = DumateProvider()
    with mock.patch.object(discovery, "discover", return_value=None), \
         mock.patch.object(discovery, "is_app_installed", return_value=True):
        with pytest.raises(HTTPException) as ei:
            p.ensure_auth()
    assert ei.value.status_code == 401


def test_forward_503_when_app_not_running():
    import asyncio

    async def _run():
        p = DumateProvider()
        body = {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
        with mock.patch.object(discovery, "discover", return_value=None), \
             mock.patch.object(discovery, "is_app_installed", return_value=False):
            with pytest.raises(HTTPException) as ei:
                await p.forward(body, "openai")
        return ei.value.status_code

    assert asyncio.run(_run()) == 503


# --- anthropic 协议拒绝 ----------------------------------------------------


def test_forward_rejects_anthropic():
    import asyncio

    async def _run():
        p = DumateProvider()
        with mock.patch.object(discovery, "discover", return_value=_endpoint()):
            with pytest.raises(HTTPException) as ei:
                await p.forward({"model": "kimi-k3", "messages": []}, "anthropic")
        return ei.value.status_code

    assert asyncio.run(_run()) == 400


# --- 转发直通（mock httpx） -------------------------------------------------


def test_forward_openai_nonstream_pass_through():
    """openai 非流式：POST 到本地 chat 接口、带 inapp key，返回上游 JSON。"""
    import asyncio

    async def _run():
        p = DumateProvider()
        ep = _endpoint()
        upstream_json = {"id": "gd-1", "choices": [{"message": {"content": "ok"}}]}

        class _FakeResp:
            status_code = 200
            def json(self): return upstream_json

        sent = {}
        async def _fake_post(url, json=None, headers=None):
            sent["url"] = url
            sent["headers"] = headers
            sent["json"] = json
            return _FakeResp()

        with mock.patch.object(discovery, "discover", return_value=ep), \
             mock.patch.object(p, "_get_client") as gc:
            gc.return_value.post = _fake_post
            resp = await p.forward(
                {"model": "kimi-k3", "messages": [{"role": "user", "content": "hi"}]},
                "openai")
        return sent, resp

    sent, resp = asyncio.run(_run())
    assert sent["url"] == _endpoint().chat_url()
    assert sent["headers"]["X-Dumate-Inapp-Key"] == _endpoint().inapp_key
    assert resp.status_code == 200


# --- 额度：布尔态 → 1/1 条目 ------------------------------------------------


def test_quota_true_becomes_full():
    p = DumateProvider()
    ep = _endpoint()

    class _FakeResp:
        status_code = 200
        def json(self): return {"hasRemainingPoints": True}

    with mock.patch.object(discovery, "discover", return_value=ep), \
         mock.patch.object(httpx, "Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        q = p.quota()

    assert q["items"][0]["remaining"] == 1
    assert q["items"][0]["used"] == 0
    assert q["items"][0]["percent"] == 0.0


def test_quota_false_becomes_empty():
    p = DumateProvider()
    ep = _endpoint()

    class _FakeResp:
        status_code = 200
        def json(self): return {"hasRemainingPoints": False}

    with mock.patch.object(discovery, "discover", return_value=ep), \
         mock.patch.object(httpx, "Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        q = p.quota()

    assert q["items"][0]["remaining"] == 0
    assert q["items"][0]["used"] == 1
    assert q["items"][0]["percent"] == 100.0


# --- 签到（bceConsole 通道） ------------------------------------------------


def test_checkin_status_parses_login_bonus_info():
    from buddy_proxy.dumate import checkin as dumate_checkin

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"success": True, "result": {
                "hasIssued": True, "totalPoints": 1000, "totalTimes": 2,
                "signInDays": ["2026-10-02", "2026-10-05"],
            }}

    auth = SimpleNamespace(headers=lambda: {"Cookie": "x", "csrftoken": "y"})
    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=auth), \
         mock.patch.object(httpx, "Client") as client_cls:
        client_cls.return_value.__enter__.return_value.get.return_value = _FakeResp()
        st = dumate_checkin.fetch_checkin_status()

    assert st["checked_in"] is True
    assert st["claimable"] is False
    assert st["total_points"] == 1000
    assert st["daily_credit"] == 500
    assert st["sign_in_days"] == ["2026-10-02", "2026-10-05"]


def test_checkin_status_none_when_not_logged_in():
    from buddy_proxy.dumate import checkin as dumate_checkin

    with mock.patch.object(dumate_checkin, "resolve_bceconsole_auth", return_value=None):
        assert dumate_checkin.fetch_checkin_status() is None
