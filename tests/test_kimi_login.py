"""kimi 登录/导入测试（SAMPLE 用真实 kimi cli 导出 JSON 的形状，token 换假）。"""

from __future__ import annotations

import json

import pytest

from buddy_proxy.auth import login as auth_login
from buddy_proxy.kimi import login as kimi_login
from buddy_proxy.kimi.credentials import (
    AuthError,
    account_cred_path,
    list_accounts,
    load_account_cred,
)
from buddy_proxy.kimi.login import LoginError, cred_from_payload, import_cred_payload

SAMPLE = {
    "access_token": "eyJ-example.at",
    "refresh_token": "r-example-refresh",
    "base_url": "https://api.kimi.com/coding",
    "device_id": "8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1",
    "expired": "2099-01-01T06:06:17Z",
    "last_refresh": 1759445177,
    "scope": "kimi-code",
    "token_type": "Bearer",
    "type": "kimi",
    "domain": "kimi.com",
    "disabled": False,
    "timestamp": 1759444572000,
}


@pytest.fixture(autouse=True)
def _no_enrich_network(monkeypatch):
    """/v1/me 不触网：按 access_token 合成 user_id（enrich 失败不影响导入，这里走成功路径）。"""
    monkeypatch.setattr(
        kimi_login, "fetch_me",
        lambda base_url, token, timeout=15.0: {"user_id": f"u-{token}",
                                               "nickname": f"名{token[-3:]}"})


def test_cred_from_payload_sample_shape():
    cred = cred_from_payload(dict(SAMPLE))
    assert cred["type"] == "kimi"
    assert cred["base_url"] == "https://api.kimi.com/coding"  # 原样保留（不带 /v1）
    assert cred["oauth_host"] == "https://auth.kimi.com"
    assert cred["device_id"] == SAMPLE["device_id"]  # 保留原 device_id（设备指纹不换）
    assert cred["expired"] == "2099-01-01T06:06:17Z"


def test_cred_from_payload_requires_both_tokens():
    with pytest.raises(LoginError, match="access_token"):
        cred_from_payload({"refresh_token": "rt"})
    with pytest.raises(LoginError, match="refresh_token"):
        cred_from_payload({"access_token": "at"})


def test_cred_from_payload_rejects_disabled():
    with pytest.raises(LoginError, match="disabled"):
        cred_from_payload(dict(SAMPLE, disabled=True))


def test_cred_from_payload_expired_key_compat():
    for key in ("expiry", "expires_at"):
        payload = dict(SAMPLE)
        del payload["expired"]
        payload[key] = "2030-05-05T00:00:00Z"
        assert cred_from_payload(payload)["expired"] == "2030-05-05T00:00:00Z"
    # 三键全缺：留空，首次使用自动刷新（refresh_token 才是长期凭据）
    payload = {k: v for k, v in SAMPLE.items() if k != "expired"}
    assert cred_from_payload(payload)["expired"] == ""


def test_cred_from_payload_base_url_and_host_derivation():
    # 缺 base_url：按 oauth_host 推导（global 区）
    cred = cred_from_payload({"access_token": "a", "refresh_token": "r",
                              "oauth_host": "https://auth.kimi.ai"})
    assert cred["base_url"] == "https://api.kimi.ai/coding"
    # 缺 oauth_host：按 base_url 域推导（按账号存，混用不串）
    cred = cred_from_payload({"access_token": "a", "refresh_token": "r",
                              "base_url": "https://api.kimi.ai/coding"})
    assert cred["oauth_host"] == "https://auth.kimi.ai"
    # 全缺：mainland 默认
    cred = cred_from_payload({"access_token": "a", "refresh_token": "r"})
    assert cred["base_url"] == "https://api.kimi.com/coding"
    assert cred["oauth_host"] == "https://auth.kimi.com"


def test_cred_from_payload_generates_device_id():
    cred = cred_from_payload({"access_token": "a", "refresh_token": "r"})
    assert len(cred["device_id"]) == 36  # uuid4


def test_type_mismatch_only_warns():
    """type 不是 kimi 只 warning 不拦（防御上游改字段，别把能用的号挡外面）。"""
    cred = cred_from_payload(dict(SAMPLE, type="something-else"))
    assert cred["type"] == "kimi"


# ---------------------------------------------------------------------------
# import_cred_payload（面板「导入账号」同款入口）
# ---------------------------------------------------------------------------

def test_import_roundtrip_and_enrich():
    cred = import_cred_payload(json.dumps(SAMPLE))
    ref = list_accounts()[0]
    # 先落盘（当时无 user_id）→ device_id 派生 id；enrich 回填的 user_id 不改已落盘 id
    assert ref.id == "kimi-8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1"
    assert ref.name == f"名{SAMPLE['access_token'][-3:]}"  # nickname 照常更新
    stored = load_account_cred(ref.id)
    assert stored["refresh_token"] == SAMPLE["refresh_token"]
    assert stored["user_id"] == f"u-{SAMPLE['access_token']}"  # enrich 回填进了 cred
    assert account_cred_path(ref.id).stat().st_mode & 0o777 == 0o600
    assert cred["account_id"] == ref.id


def test_import_accepts_dict_and_keeps_priority():
    import_cred_payload(dict(SAMPLE))
    import_cred_payload({"access_token": "second", "refresh_token": "r-second",
                         "user_id": "u-second"})  # payload 自带 user_id（enrich 合成值恰好一致）
    refs = list_accounts()
    assert [r.id for r in refs] == ["kimi-8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1", "u-second"]
    # 同 refresh_token 重导：命中原账号，顺位不变
    import_cred_payload(dict(SAMPLE))
    assert [r.id for r in list_accounts()] == [
        "kimi-8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1", "u-second"]


def test_import_rejects_garbage():
    with pytest.raises(LoginError):
        import_cred_payload("not json")
    with pytest.raises(LoginError, match="为空"):
        import_cred_payload("   ")
    with pytest.raises(LoginError, match="不是 JSON 对象"):
        import_cred_payload([1, 2])


def test_import_survives_enrich_failure(monkeypatch):
    """access 失效且 refresh 也失败（旧导出 JSON 的常态）：导入照样成功落盘。

    /v1/me 401 → 尝试刷新 → 刷新也被拒（refresh_token 已轮换作废）→ 只记
    debug 日志。导入的价值在 refresh_token 本身落盘，展示字段拿得到就赚。
    """
    def _boom(base_url, token, timeout=15.0):
        raise OSError("HTTP Error 401")

    def _refresh_denied(cred):
        cred["access_token"] = "new-at"  # 真函数会回写；这里模拟失败前不动
        raise AuthError("Kimi OAuth 请求失败: HTTP Error 400: Bad Request")

    monkeypatch.setattr(kimi_login, "fetch_me", _boom)
    monkeypatch.setattr(kimi_login, "refresh_account_cred", _refresh_denied)
    import_cred_payload(json.dumps(SAMPLE))  # 不抛
    ref = list_accounts()[0]
    assert ref.id == "kimi-8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1"
    stored = load_account_cred(ref.id)
    assert stored["refresh_token"] == SAMPLE["refresh_token"]
    assert "user_id" not in stored  # enrich 没成功，但落盘不受影响


# ---------------------------------------------------------------------------
# CLI 注册（buddy login kimi）
# ---------------------------------------------------------------------------

def test_cli_dispatch_registered():
    assert "kimi" in auth_login.KNOWN_PROVIDERS
    assert "kimi" in auth_login._DISPATCH
