"""kimi provider 纯函数 + 面板数据（health/quota/quota_epoch）测试。"""

from __future__ import annotations

import pytest

from buddy_proxy.kimi import provider as kimi_provider
from buddy_proxy.kimi.credentials import reorder_accounts, save_account_cred
from buddy_proxy.kimi.provider import KimiProvider
from buddy_proxy.kimi.upstream import (
    build_upstream_body,
    cred_expired_epoch,
    device_headers,
    iso_to_epoch,
    map_thinking,
    normalize_base_url,
    usages_to_items,
)

_ALL_MODEL_IDS = {"kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"}


# ---------------------------------------------------------------------------
# 模型表 / upstream 纯函数
# ---------------------------------------------------------------------------

def test_models_table():
    models = KimiProvider().models()
    ids = [m["id"] for m in models]
    assert set(ids) == _ALL_MODEL_IDS
    assert ids[0] == "kimi-for-coding"  # 表里第一条即默认模型
    assert all(m["owned_by"] == "kimi" for m in models)


def test_normalize_base_url_idempotent():
    for raw in ("https://api.kimi.com/coding",
                "https://api.kimi.com/coding/",
                "https://api.kimi.com/coding/v1",
                "https://api.kimi.com/coding/v1/"):
        assert normalize_base_url(raw) == "https://api.kimi.com/coding/v1"
    assert normalize_base_url("") == ""


def test_map_thinking_levels():
    assert map_thinking("low") == {"thinking": {"type": "enabled", "effort": "low"}}
    assert map_thinking("minimal")["thinking"]["effort"] == "low"
    assert map_thinking("medium")["thinking"]["effort"] == "high"
    assert map_thinking("high")["thinking"]["effort"] == "high"
    assert map_thinking("xhigh")["thinking"]["effort"] == "max"
    assert map_thinking("max")["thinking"]["effort"] == "max"
    assert map_thinking("HIGH")["thinking"]["effort"] == "high"  # 大小写不敏感
    assert map_thinking(None) is None  # 不注入 → 上游默认 max
    assert map_thinking("bogus") is None


def test_build_upstream_body_whitelist_and_thinking():
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.5,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "reasoning_effort": "low",  # 不透传，被 map_thinking 消费
        "top_k": 5,                 # 白名单外：丢弃
    }
    up = build_upstream_body(body, model="k3", stream=True)
    assert up["model"] == "k3"
    assert up["messages"] == body["messages"]
    assert up["temperature"] == 0.5
    assert up["tools"] == body["tools"]
    assert up["thinking"] == {"type": "enabled", "effort": "low"}
    for absent in ("reasoning_effort", "top_k"):
        assert absent not in up
    # stream 必须显式带给上游：OpenAI 兼容上游按**请求体**这个字段决定返 SSE
    # 还是整块 JSON。漏了它上游回非流式 JSON，本地按 SSE 逐行解析不出 data:
    # 事件 → 判成「首事件前空流」换号 → 客户端拿 502（review 实证）。
    assert up["stream"] is True
    assert build_upstream_body(body, model="k3", stream=False)["stream"] is False


def test_device_headers_shape():
    headers = device_headers({"device_id": "dev-1", "access_token": "tok"})
    assert headers["X-Msh-Device-Id"] == "dev-1"
    assert headers["X-Msh-Platform"] == "kimi_code_cli"
    assert headers["Authorization"] == "Bearer tok"
    assert headers["User-Agent"].startswith("kimi-code-cli/")


def test_usages_to_items_new_shape():
    data = {"usages": {
        "limit_5h": {"used_ratio": 0.25, "reset_time": "2026-10-03T18:00:00Z"},
        "limit_7d": {"used_ratio": 0.5, "reset_time": "2026-10-08T00:00:00Z"},
    }}
    items = usages_to_items(data)
    assert [i["label"] for i in items] == ["5 小时窗口", "7 天池"]  # 周期由小到大
    assert [i["percent"] for i in items] == [25.0, 50.0]  # percent 是已用
    assert all(i["unit"] == "percent" and i["expire_ts"] is None for i in items)
    assert items[0]["reset_ts"] == iso_to_epoch("2026-10-03T18:00:00Z")
    prefixed = usages_to_items(data, prefix="Kimi #2 · ")
    assert prefixed[0]["label"].startswith("Kimi #2 · ")


def test_usages_to_items_legacy_fallback():
    items = usages_to_items({"usage": {"limit": "1000", "used": "250",
                                       "remaining": "750",
                                       "resetTime": "2026-10-08T00:00:00Z"}})
    assert len(items) == 1 and items[0]["label"] == "7 天池"
    assert items[0]["percent"] == 25.0
    assert usages_to_items({}) == []
    assert usages_to_items({"usage": {"limit": "0", "used": "0"}}) == []


def test_iso_and_cred_expiry():
    assert iso_to_epoch("2026-10-03T00:00:00Z") > 0
    assert iso_to_epoch("garbage") == 0.0
    assert iso_to_epoch(None) == 0.0
    assert cred_expired_epoch({"expired": "2099-01-01T00:00:00Z"}) > 0
    assert cred_expired_epoch({"expiry": "2099-01-01T00:00:00Z"}) > 0
    assert cred_expired_epoch({}) == 0.0


# ---------------------------------------------------------------------------
# health / quota / quota_epoch
# ---------------------------------------------------------------------------

def _write(acct_id: str, **over):
    cred = {
        "account_id": acct_id,
        "type": "kimi",
        "access_token": f"at-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "base_url": "https://api.kimi.com/coding",
        "oauth_host": "https://auth.kimi.com",
        "device_id": f"dev-{acct_id}",
        "user_id": acct_id,
        "nickname": f"名{acct_id}",
    }
    cred.update(over)
    return save_account_cred(cred)


def test_health_offline():
    prov = KimiProvider()
    assert prov.health()["configured"] is False  # 未登录也不触网
    _write("u1")
    h = prov.health()
    assert h["id"] == "kimi" and h["configured"] is True
    assert h["nickname"] == "名u1"
    assert h["accounts"] == [{"id": "u1", "name": "名u1", "user_id": "u1"}]
    assert set(h["models"]) == _ALL_MODEL_IDS


def test_quota_login_notice_when_no_accounts():
    """未登录也要返回说明条——返回 None 面板会隐藏整个通道块，导入入口就没了。"""
    data = KimiProvider().quota()
    assert data["level"] is None
    assert len(data["items"]) == 1
    assert "buddy login kimi" in data["items"][0]["remaining"]


def _patch_quota_upstream(monkeypatch, usages_by_token):
    monkeypatch.setattr(
        kimi_provider, "ensure_account_token",
        lambda aid, **k: (f"tok-{aid}", {"base_url": "https://api.kimi.com/coding"}))

    def fake_usages(base, tok, timeout=15.0):
        handler = usages_by_token.get(tok)
        if handler is None:
            return {"usages": {"limit_5h": {"used_ratio": 0.1, "reset_time": ""},
                               "limit_7d": {"used_ratio": 0.2, "reset_time": ""}}}
        return handler(base, tok)

    monkeypatch.setattr(kimi_provider, "fetch_usages", fake_usages)


def test_quota_single_account(monkeypatch):
    _write("u1")
    _patch_quota_upstream(monkeypatch, {})
    data = KimiProvider().quota()
    assert [i["label"] for i in data["items"]] == ["5 小时窗口", "7 天池"]  # 单账号无前缀
    assert data["level"] is None  # cred 没有 user_level_name


def test_quota_multi_account_labels_and_failure_notice(monkeypatch):
    _write("u1")
    _write("u2")
    monkeypatch.setattr(
        kimi_provider, "ensure_account_token",
        lambda aid, **k: (f"tok-{aid}", {"base_url": "https://api.kimi.com/coding"}))

    def boom(base, tok, timeout=15.0):
        raise OSError("boom")

    monkeypatch.setattr(kimi_provider, "fetch_usages",
                        lambda base, tok, timeout=15.0: (
                            boom(base, tok) if tok == "tok-u2" else
                            {"usages": {"limit_5h": {"used_ratio": 0.1, "reset_time": ""},
                                        "limit_7d": {"used_ratio": 0.2, "reset_time": ""}}}))
    data = KimiProvider().quota()
    labels = [i["label"] for i in data["items"]]
    assert labels[0] == "Kimi 额度查询失败"  # 失败说明条置顶（UI 警告色）
    assert data["items"][0]["query_failed"] is True
    assert "1/2" in data["items"][0]["remaining"]
    assert any(l.startswith("Kimi #1 · ") for l in labels)  # 多账号带顺位前缀
    assert not any(l.startswith("Kimi #2") for l in labels)  # u2 失败：无条目


def test_quota_empty_usages_not_failure(monkeypatch):
    """/usages 查通但返回空对象（实测 Free 层如此）≠ 查询失败。

    真机踩过：健康账号被标失败，和真死号一起显示「2/2 个账号查询失败」。
    空数据只是没分桶可展示，不该挂 query_failed。"""
    _write("u1")
    _write("u2")
    monkeypatch.setattr(
        kimi_provider, "ensure_account_token",
        lambda aid, **k: (f"tok-{aid}", {"base_url": "https://api.kimi.com/coding"}))
    monkeypatch.setattr(
        kimi_provider, "fetch_usages",
        lambda base, tok, timeout=15.0: (
            {} if tok == "tok-u2" else
            {"usages": {"limit_5h": {"used_ratio": 0.1, "reset_time": ""},
                        "limit_7d": {"used_ratio": 0.2, "reset_time": ""}}}))
    data = KimiProvider().quota()
    labels = [i["label"] for i in data["items"]]
    assert not any(i.get("query_failed") for i in data["items"]), "空数据不算失败"
    assert any(l.startswith("Kimi #1 · ") for l in labels)  # u1 正常展示
    assert not any(l.startswith("Kimi #2") for l in labels)  # u2 无数据：无条目但也无告警


def test_quota_epoch_tracks_account_list():
    prov = KimiProvider()
    assert prov.quota_epoch() == "empty"
    _write("u1")
    _write("u2")
    epoch1 = prov.quota_epoch()
    assert epoch1 == "u1#0,u2#1"
    reorder_accounts(["u2", "u1"])
    assert prov.quota_epoch() != epoch1  # 换顺位也作废旧快照（#71 同坑）
