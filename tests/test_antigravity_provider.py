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
    """fetchAvailableModels 有数据 → 按组聚合（upstream 前缀匹配变体名，组内取最小）。

    percent=已用（前端进度条语义），remaining/total 千分制，reset_ts 取最早刷新点。
    """
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "u@x.com",
                     "project_id": "p"})

    def _quota(frac, reset=None):
        info = {"remainingFraction": frac}
        if reset:
            info["resetTime"] = reset
        return {"quotaInfo": info}

    async def _fake_fetch():
        # 真实响应形态：models.<变体名>.quotaInfo.remainingFraction
        return {"models": {
            "gemini-3.1-pro-low": _quota(0.9, "2026-10-02T20:28:46Z"),
            "gemini-3.1-pro-high": _quota(0.8),
            "gemini-3.6-flash-medium": _quota(0.5, "2026-10-02T19:00:00Z"),
            "gemini-3.8-flash-tiered": _quota(0.7),
            "claude-sonnet-4-6": _quota(1.0, "2026-10-02T20:30:52Z"),
            "claude-opus-4-6-thinking": _quota(1.0),
            "gpt-oss-120b-medium": _quota(0.99),
            "chat_20706": _quota(0.1),  # 内部条目不该被算进来
        }}

    # _fetch 被 quota() 包进 _run_sync（asyncio.run），monkeypatch 掉外层拿原始数据
    import buddy_proxy.antigravity.provider as prov

    monkeypatch.setattr(provider, "_fetch_available_models", _fake_fetch)
    monkeypatch.setattr(prov, "_run_sync", lambda factory: prov.asyncio.run(factory()))

    quota = provider.quota()
    assert len(quota["items"]) == 2
    by_label = {i["label"].split("（")[0]: i for i in quota["items"]}
    gemini = by_label["Gemini 组"]
    cgpt = by_label["Claude/GPT 组"]
    # 组内跨模型取最小：gemini 组 min(0.9,0.8,0.5,0.7)=0.5；claude-gpt 组 min(1.0,1.0,0.99)=0.99
    assert gemini["remaining"] == 500.0 and gemini["total"] == 1000
    assert gemini["percent"] == 50.0  # 已用
    assert cgpt["remaining"] == 990.0 and cgpt["percent"] == 1.0
    # reset_ts = 组内最早的 resetTime epoch（gemini 组 19:00Z < 20:28Z）
    assert gemini["reset_ts"] == prov._iso_to_epoch("2026-10-02T19:00:00Z")
    assert cgpt["reset_ts"] == prov._iso_to_epoch("2026-10-02T20:30:52Z")


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


def test_iso_to_epoch():
    from buddy_proxy.antigravity.provider import _iso_to_epoch

    assert _iso_to_epoch("2026-10-02T20:28:46Z") > 0
    assert _iso_to_epoch("2026-10-02T20:28:46+08:00") > 0
    assert _iso_to_epoch("") == 0.0
    assert _iso_to_epoch(None) == 0.0
    assert _iso_to_epoch("garbage") == 0.0


def test_models_table_upstream_names():
    """模型表的 upstream 真名必须是 fetchAvailableModels 实测可用的形态。"""
    import buddy_proxy.antigravity.provider as prov

    by_id = {m["id"]: m for m in prov.MODELS}
    assert by_id["gemini-3.8-flash"].get("upstream") == "gemini-3.8-flash-tiered"
    assert by_id["gpt-oss-120b"].get("upstream") == "gpt-oss-120b-medium"
    assert by_id["gemini-3.1-pro"].get("efforts") == ["low", "high"]
    assert by_id["gemini-3.6-flash"].get("default_effort") == "medium"
    # claude 系裸名可用，无 efforts
    assert "efforts" not in by_id["claude-sonnet-4-6"]
