"""Kimi 动态模型目录刷新测试（全离线）。"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from buddy_proxy.kimi import provider as kimi_provider
from buddy_proxy.kimi import upstream
from buddy_proxy.kimi.provider import KimiProvider


def _accounts(*ids: str) -> list[SimpleNamespace]:
    return [SimpleNamespace(id=account_id) for account_id in ids]


def test_fetch_models_parses_openai_shape_and_uses_auth_device_headers(monkeypatch):
    """官方 /v1/models 形状可解析，请求同时带 Bearer 与账号设备头。"""
    seen: dict[str, Any] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps({"data": [
                {"id": "kimi-for-coding", "name": "Coding"},
                {"id": "kimi-new", "display_name": "Kimi New"},
                {"name": "name-only"},
                {"object": "model"},
                "string-model",
            ]}).encode()

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(upstream.urllib.request, "urlopen", fake_urlopen)
    models = upstream.fetch_models(
        "https://api.kimi.test/coding/", "token-1", {"device_id": "device-1"}, timeout=7)

    assert seen["url"] == "https://api.kimi.test/coding/v1/models"
    headers = {key.lower(): value for key, value in seen["headers"].items()}
    assert headers["authorization"] == "Bearer token-1"
    assert headers["x-msh-device-id"] == "device-1"
    assert headers["x-msh-platform"] == "kimi_code_cli"
    assert seen["timeout"] == 7
    assert models == [
        {"id": "kimi-for-coding", "name": "Coding", "description": "Coding"},
        {"id": "kimi-new", "display_name": "Kimi New", "description": "Kimi New"},
        {"id": "name-only", "name": "name-only", "description": "name-only"},
        {"id": "string-model", "description": "string-model"},
    ]


def test_refresh_models_unions_accounts_preserves_local_metadata_and_instance_scope(monkeypatch):
    """多账号取并集、失败账号容忍；已知条目 metadata 保留且不污染别的实例。"""
    monkeypatch.setattr(kimi_provider.failover, "available_accounts",
                        lambda: _accounts("bad", "limited", "full"))
    token_calls: list[str] = []

    def fake_token(account_id: str):
        token_calls.append(account_id)
        return f"tok-{account_id}", {
            "base_url": "https://api.kimi.test/coding",
            "device_id": f"dev-{account_id}",
        }

    def fake_fetch(base_url, token, cred, timeout=15.0):
        assert base_url == "https://api.kimi.test/coding"
        assert cred["device_id"] == token.replace("tok-", "dev-")
        if token == "tok-bad":
            raise OSError("offline")
        if token == "tok-limited":
            return [
                {"id": "kimi-for-coding", "display_name": "Remote renamed",
                 "description": "Remote renamed"},
                {"id": "account-a-only", "name": "A Model", "description": "A Model"},
            ]
        return [
            {"id": "account-a-only", "name": "duplicate", "description": "duplicate"},
            {"id": "brand-new", "display_name": "Brand New", "description": "Brand New"},
        ]

    monkeypatch.setattr(kimi_provider, "ensure_account_token", fake_token)
    monkeypatch.setattr(kimi_provider, "fetch_models", fake_fetch)
    provider = KimiProvider()
    untouched = KimiProvider()

    models = asyncio.run(provider.refresh_models(force=True))
    by_id = {model["id"]: model for model in models}

    assert token_calls == ["bad", "limited", "full"]
    assert set(by_id) == {"kimi-for-coding", "account-a-only", "brand-new"}
    # models.json 的人工描述不可被上游简名覆盖。
    assert by_id["kimi-for-coding"]["description"] == "Kimi K2.8 Preview (1M)"
    assert by_id["brand-new"]["description"] == "Brand New"
    assert provider.accepts_model("brand-new")
    assert not untouched.accepts_model("brand-new")
    assert "brand-new" not in {m["id"] for m in kimi_provider.MODELS}


def test_refresh_models_all_fail_raises_and_keeps_previous_catalog(monkeypatch):
    monkeypatch.setattr(kimi_provider.failover, "available_accounts",
                        lambda: _accounts("a", "b"))
    monkeypatch.setattr(
        kimi_provider, "ensure_account_token",
        lambda account_id: (f"tok-{account_id}", {"base_url": "https://api.test", "device_id": account_id}),
    )
    monkeypatch.setattr(
        kimi_provider, "fetch_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )
    provider = KimiProvider()
    before = list(provider.models())

    with pytest.raises(RuntimeError, match="全部失败"):
        asyncio.run(provider.refresh_models())

    assert list(provider.models()) == before


def test_refreshed_model_routes_and_unknown_model_is_rejected(monkeypatch):
    """刷新发现的新 id 可直接转发；未知 id 不再静默落到默认模型。"""
    monkeypatch.setattr(kimi_provider, "has_cred", lambda: True)
    monkeypatch.setattr(kimi_provider.failover, "available_accounts",
                        lambda: _accounts("acct"))
    monkeypatch.setattr(
        kimi_provider, "ensure_account_token",
        lambda account_id: ("tok-acct", {
            "base_url": "https://api.kimi.test/coding",
            "device_id": "dev-acct",
        }),
    )
    monkeypatch.setattr(
        kimi_provider, "fetch_models",
        lambda *args, **kwargs: [
            {"id": "runtime-new", "display_name": "Runtime New", "description": "Runtime New"}
        ],
    )
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        })

    provider = KimiProvider()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def scenario():
        await provider.refresh_models()
        response = await provider.forward({
            "model": "kimi/runtime-new",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
        }, "openai")
        assert response.status_code == 200
        with pytest.raises(HTTPException) as exc_info:
            await provider.forward({
                "model": "does-not-exist",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }, "openai")
        assert exc_info.value.status_code == 400
        await provider._client.aclose()

    asyncio.run(scenario())
    assert sent == [{
        "model": "runtime-new",
        "stream": False,
        "messages": [{"role": "user", "content": "hi"}],
    }]
