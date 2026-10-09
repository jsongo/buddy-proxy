"""Antigravity 运行时模型目录刷新（全离线）。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

import buddy_proxy.antigravity.provider as prov


def _account(account_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=account_id)


def _ids(provider: prov.AntigravityProvider) -> set[str]:
    return {str(model["id"]) for model in provider.models()}


def test_refresh_unions_accounts_preserves_verified_and_derives_new_models(monkeypatch):
    provider = prov.AntigravityProvider()
    static_snapshot = [dict(model) for model in prov.MODELS]
    accounts = [_account("a1"), _account("a2"), _account("broken")]
    monkeypatch.setattr(prov, "list_accounts", lambda: accounts)

    async def fake_fetch(account_id: str):
        if account_id == "broken":
            raise RuntimeError("offline")
        if account_id == "a1":
            return {"models": {
                "gemini-3.1-pro-low": {},
                "gemini-4-flash-extra-low": {},
                "gemini-4-flash-medium": {},
                "claude-sonnet-5-5": {},
                "chat_20706": {},
            }}
        return {"models": {
            "gemini-3.1-pro-high": {},
            "gemini-4-flash-high": {},
            "gpt-next-tiered": {},
            "tab_jump": {},
        }}

    monkeypatch.setattr(provider, "_fetch_available_models", fake_fetch)
    refreshed = asyncio.run(provider.refresh_models(force=True))

    refreshed_ids = {model["id"] for model in refreshed}
    assert {
        "gemini-3.1-pro",
        "gemini-4-flash",
        "claude-sonnet-5-5",
        "gpt-next",
    } <= refreshed_ids
    # broken 账号失败，本轮只能新增，不能把上一版 id 当成已下架。
    assert {model["id"] for model in static_snapshot} <= refreshed_ids
    assert "chat_20706" not in _ids(provider)
    assert "tab_jump" not in _ids(provider)

    by_id = provider._model_by_id
    # 本地实测条目的 metadata / upstream / effort 策略原样保留。
    local = next(model for model in static_snapshot if model["id"] == "gemini-3.1-pro")
    assert by_id["gemini-3.1-pro"] == local
    assert by_id["gemini-3.1-pro"] is not local

    dynamic = by_id["gemini-4-flash"]
    assert dynamic["upstream"] == "gemini-4-flash"
    assert dynamic["efforts"] == ["extra-low", "medium", "high"]
    assert dynamic["default_effort"] == "medium"
    assert dynamic["group"] == "gemini"

    # Claude 裸名里的版本/家族都属于固定模型名，不能被后缀归一化误伤。
    claude = by_id["claude-sonnet-5-5"]
    assert claude["id"] == claude["upstream"] == "claude-sonnet-5-5"
    assert "efforts" not in claude
    assert claude["group"] == "claude-gpt"

    tiered = by_id["gpt-next"]
    assert tiered["efforts"] == ["tiered"]
    assert tiered["default_effort"] == "tiered"
    assert tiered["group"] == "claude-gpt"

    # 刷新只改实例目录，不得改模块级冷启动表，也不得污染另一个实例。
    assert prov.MODELS == static_snapshot
    assert "gemini-4-flash" not in _ids(prov.AntigravityProvider())


def test_refresh_all_failures_or_empty_keep_previous_catalog(monkeypatch):
    provider = prov.AntigravityProvider()
    before = provider.models()
    monkeypatch.setattr(
        prov, "list_accounts", lambda: [_account("a1"), _account("a2")]
    )

    async def all_fail(account_id: str):
        raise RuntimeError(f"{account_id} down")

    monkeypatch.setattr(provider, "_fetch_available_models", all_fail)
    with pytest.raises(RuntimeError, match="全部失败"):
        asyncio.run(provider.refresh_models())
    assert provider.models() == before

    async def only_internal(_account_id: str):
        return {"models": {"chat_hidden": {}, "tab_hidden": {}}}

    monkeypatch.setattr(provider, "_fetch_available_models", only_internal)
    with pytest.raises(RuntimeError, match="均无公开模型"):
        asyncio.run(provider.refresh_models())
    assert provider.models() == before


def test_refresh_with_no_available_account_keeps_previous_catalog(monkeypatch):
    provider = prov.AntigravityProvider()
    before = provider.models()
    monkeypatch.setattr(prov, "list_accounts", lambda: [])

    with pytest.raises(RuntimeError, match="没有可用于刷新"):
        asyncio.run(provider.refresh_models())
    assert provider.models() == before


def test_partial_refresh_keeps_failed_account_models_and_queries_cooled_accounts(monkeypatch):
    provider = prov.AntigravityProvider()
    accounts = [_account("healthy"), _account("cooled")]
    monkeypatch.setattr(prov, "list_accounts", lambda: accounts)
    # 即使聊天 failover 会过滤 cooled，目录刷新也必须探它。
    monkeypatch.setattr(prov.failover, "available_accounts", lambda: [accounts[0]])
    rounds = 0
    calls: list[str] = []

    async def fake_fetch(account_id: str):
        nonlocal rounds
        calls.append(account_id)
        if rounds == 0:
            return {"models": {f"{account_id}-only-low": {}}}
        if account_id == "cooled":
            raise OSError("temporary failure")
        return {"models": {"healthy-only-high": {}, "brand-new-medium": {}}}

    monkeypatch.setattr(provider, "_fetch_available_models", fake_fetch)
    asyncio.run(provider.refresh_models())
    assert {"healthy-only", "cooled-only"} <= _ids(provider)
    rounds = 1
    asyncio.run(provider.refresh_models())

    assert calls == ["healthy", "cooled", "healthy", "cooled"]
    assert {"healthy-only", "cooled-only", "brand-new"} <= _ids(provider)


def test_unknown_model_is_rejected_instead_of_routing_to_default(monkeypatch):
    provider = prov.AntigravityProvider()
    monkeypatch.setattr(provider, "ensure_auth", lambda: None)

    with pytest.raises(prov.HTTPException) as exc_info:
        asyncio.run(provider.forward(
            {"model": "antigravity/not-a-real-model"},
            "openai",
        ))
    assert exc_info.value.status_code == 400
    assert "未知模型" in str(exc_info.value.detail)


def test_refreshed_model_routes_and_drives_quota_mapping(monkeypatch):
    provider = prov.AntigravityProvider()
    account = _account("a1")
    monkeypatch.setattr(prov, "list_accounts", lambda: [account])

    async def fake_fetch(_account_id: str):
        return {"models": {
            "gemini-4-pro-low": {},
            "gemini-4-pro-high": {},
        }}

    monkeypatch.setattr(provider, "_fetch_available_models", fake_fetch)
    asyncio.run(provider.refresh_models())

    quota = provider._quota_items_from(
        {"models": {
            "gemini-4-pro-high": {
                "quotaInfo": {"remainingFraction": 0.75}
            },
            # 已被新运行时目录移除的静态模型不得再参与映射。
            "claude-sonnet-4-6": {
                "quotaInfo": {"remainingFraction": 0.1}
            },
        }},
        "",
    )
    assert len(quota) == 1
    assert quota[0]["label"] == "Gemini 组"
    assert quota[0]["models_in_group"] == ["gemini-4-pro"]
    assert quota[0]["remaining"] == 750.0

    captured: dict[str, str] = {}

    def fake_convert(body, *, project_id, model):
        captured.update(project_id=project_id, model=model)
        return {}

    async def fake_client():
        return object()

    async def fake_send(_client, _method, _body, _headers, _stream):
        return httpx.Response(200, json={})

    monkeypatch.setattr(provider, "ensure_auth", lambda: None)
    monkeypatch.setattr(prov.failover, "available_accounts", lambda: [account])
    monkeypatch.setattr(prov, "ensure_account_token", lambda _account_id: ("token", {"project_id": "p1"}))
    monkeypatch.setattr(prov, "chat_to_antigravity_request", fake_convert)
    monkeypatch.setattr(prov, "gemini_response_to_chat", lambda _payload, *, model: {"model": model})
    monkeypatch.setattr(provider, "_get_client", fake_client)
    monkeypatch.setattr(provider, "_send_with_fallback", fake_send)

    response = asyncio.run(provider.forward(
        {"model": "antigravity/gemini-4-pro", "reasoning_effort": "high"},
        "openai",
    ))
    assert response.status_code == 200
    assert captured == {"project_id": "p1", "model": "gemini-4-pro-high"}
