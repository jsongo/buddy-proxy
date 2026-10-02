"""AntigravityProvider 纯逻辑测试（models/health/quota，不发起网络请求）。"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def provider():
    from buddy_proxy.antigravity.provider import AntigravityProvider

    return AntigravityProvider()


def test_models_table(provider):
    models = provider.models()
    assert models, "models 表为空"
    ids = {m["id"] for m in models}
    assert "gemini-3.1-pro" in ids and "claude-sonnet-4-6" in ids
    for m in models:
        assert m["owned_by"] == "antigravity"
        assert m["object"] == "model"


def test_health(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    assert provider.health()["configured"] is False

    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00",
                     "email": "u@x.com", "project_id": "p", "tier": "free-tier"})
    health = provider.health()
    assert health["configured"] is True
    assert health["email"] == "u@x.com"
    assert health["project_id"] == "p"


def test_quota_without_cred(provider, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    assert provider.quota() is None


def test_quota_falls_back_to_note_on_fetch_failure(provider, tmp_path, monkeypatch):
    """fetchAvailableModels 失败 → 退化为静态说明，不画假进度条。"""
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})
    monkeypatch.setattr(provider, "_fetch_available_models",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    quota = provider.quota()
    assert quota is not None and len(quota["items"]) == 1
    assert quota["items"][0]["percent"] is None  # 拿不到数据就不画进度条


def test_quota_aggregates_groups(provider, tmp_path, monkeypatch):
    """fetchAvailableModels 有数据 → 按组聚合剩余比例（组内取最小）。"""
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    async def _fake_fetch():
        return {"models": {
            "gemini-3.1-pro": {"remainingFraction": 0.9},
            "gemini-3.8-flash": {"remainingFraction": 0.5},
            "claude-sonnet-4-6": {"remainingFraction": 1.0},
        }}

    # _fetch 被 quota() 包进 _run_sync（asyncio.run），monkeypatch 掉外层拿原始数据
    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))

    quota = provider.quota()
    items = {i["label"]: i["percent"] for i in quota["items"]}
    assert items["Gemini 组（组内共享 weekly + 5h 双池）"] == 50.0  # 组内取最小
    assert items["Claude/GPT 组（组内共享 weekly + 5h 双池）"] == 100.0


def test_models_json_load_fallback(tmp_path, monkeypatch):
    """models.json 损坏时兜底单模型，不崩。"""
    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(prov, "_MODELS_JSON",
                        tmp_path / "broken.json")  # 不存在 → OSError 分支
    models = prov._load_models()
    assert models and models[0]["id"] == "gemini-3.8-flash"
    (tmp_path / "broken.json").write_text("{bad")
    assert prov._load_models()[0]["id"] == "gemini-3.8-flash"


def test_endpoint_order_matches_reference():
    """daily 优先、prod 兜底（参考实现同序），写死防手滑改反。"""
    from buddy_proxy.antigravity.provider import ENDPOINTS

    assert ENDPOINTS[0].startswith("https://daily-cloudcode-pa")
    assert ENDPOINTS[1] == "https://cloudcode-pa.googleapis.com"
