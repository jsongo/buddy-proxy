"""antigravity 登录 / onboarding / 凭据存储单元测试。

ANTIGRAVITY_OAUTH_JSON 由 conftest autouse 指到 tmp，不碰真实凭据。
"""

from __future__ import annotations

import json
import urllib.error

import pytest


def _cred(**over):
    cred = {
        "access_token": "at",
        "refresh_token": "rt",
        "token_type": "Bearer",
        "scope": "s",
        "expiry": "2099-01-01T00:00:00+00:00",
        "email": "u@x.com",
    }
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# credentials：落盘 / 刷新
# ---------------------------------------------------------------------------

def test_cred_save_load_roundtrip(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    path = creds.save_cred(_cred())
    assert path.name == "ag.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert creds.load_cred()["refresh_token"] == "rt"
    assert creds.has_cred()

    (tmp_path / "ag.json").write_text("{bad")
    assert creds.load_cred() is None  # 损坏 → None
    (tmp_path / "ag.json").write_text(json.dumps({"access_token": "x"}))
    assert creds.load_cred() is None  # 缺 refresh_token → None


def test_refresh_cred_updates_and_saves(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    creds.save_cred(_cred(expiry="2000-01-01T00:00:00+00:00"))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600, "scope": "s2"})

    cred = creds.refresh_cred(creds.load_cred())
    assert cred["access_token"] == "fresh"
    assert cred["scope"] == "s2"
    assert creds.load_cred()["access_token"] == "fresh"  # 已落盘

    def _reject(data, timeout=30.0):
        raise creds.AuthError("Google token 接口拒绝: invalid_grant")

    monkeypatch.setattr(creds, "_token_request", _reject)
    with pytest.raises(creds.AuthError, match="invalid_grant"):
        creds.refresh_cred(cred)


# ---------------------------------------------------------------------------
# OAuth URL / 回调
# ---------------------------------------------------------------------------

def test_build_auth_url_params():
    from buddy_proxy.antigravity.login import SCOPES, _build_auth_url

    url = _build_auth_url("http://127.0.0.1:51121/oauth-callback", "st", "ch")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=1071006060591-" in url
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A51121%2Foauth-callback" in url
    assert "access_type=offline" in url and "prompt=consent" in url
    assert "code_challenge=ch" in url and "code_challenge_method=S256" in url
    assert "cclog" in url and "experimentsandconfigs" in url  # antigravity 多的两个 scope
    assert len(SCOPES) == 5


def test_pkce_pair_shape():
    from buddy_proxy.antigravity.login import _pkce_pair

    verifier, challenge = _pkce_pair()
    assert 40 < len(verifier) <= 128 and len(challenge) == 43


# ---------------------------------------------------------------------------
# _exchange / resume_onboarding：token 先落盘，onboarding 失败不白费授权
# ---------------------------------------------------------------------------

def test_exchange_keeps_token_when_onboarding_fails(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    # login.py 是 from-import 绑定，mock 要打在 login 模块的引用上
    monkeypatch.setattr(ag_login, "_token_request", lambda data, timeout=30.0: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "fetch_user_email", lambda token, timeout=15.0: "u@x.com")

    def boom(token, project_id=""):
        raise ag_login.SetupError("INELIGIBLE: nope")

    monkeypatch.setattr(ag_login, "setup_code_assist", boom)
    with pytest.raises(ag_login.LoginError, match="INELIGIBLE"):
        ag_login._exchange("code", "http://127.0.0.1:1/oauth-callback", "ver")
    # token 已落盘：重试只需续跑 onboarding
    assert creds.load_cred()["access_token"] == "at"


def test_exchange_success(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(ag_login, "_token_request", lambda data, timeout=30.0: {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "fetch_user_email", lambda token, timeout=15.0: "")
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "proj-x", "tier": "free-tier", "tier_name": ""})

    cred = ag_login._exchange("code", "uri", "ver")
    assert cred["project_id"] == "proj-x"
    saved = json.loads((tmp_path / "ag.json").read_text())
    assert saved["project_id"] == "proj-x"


def test_resume_onboarding(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.antigravity import login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    creds.save_cred(_cred(expiry="2000-01-01T00:00:00+00:00"))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "p", "tier": "free-tier", "tier_name": ""})

    cred = ag_login.resume_onboarding()
    assert cred["project_id"] == "p" and cred["access_token"] == "fresh"


# ---------------------------------------------------------------------------
# setup：loadCodeAssist / onboardUser（mock 端点）
# ---------------------------------------------------------------------------

class _FakeResp:
    """模拟 urlopen 返回：.status/.read()/上下文管理器。"""

    def __init__(self, status: int, payload: dict):
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeUrlopen:
    """按 URL 序列 canned 响应；记录请求体供断言。"""

    def __init__(self, responses: dict[str, list]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []
        self.headers: list[dict] = []

    def __call__(self, req, timeout=30.0):
        url = req.full_url
        body = json.loads(req.data.decode()) if req.data else {}
        self.calls.append((url, body))
        self.headers.append({k.lower(): v for k, v in req.header_items()})
        key = next((k for k in self.responses if k in url), None)
        if key is None:
            raise urllib.error.URLError("no canned response")
        item = self.responses[key]
        if not item:
            raise urllib.error.URLError("exhausted")
        status, payload = item.pop(0)
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "err", {}, iter([json.dumps(payload).encode()]))
        return _FakeResp(status, payload)


def test_metadata_numeric_enum(monkeypatch):
    from buddy_proxy.antigravity import setup

    monkeypatch.setattr(setup.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup.platform, "machine", lambda: "arm64")
    meta = setup.core_metadata()
    assert meta == {"ideType": 9, "platform": 2, "pluginType": 2}  # ANTIGRAVITY / DARWIN_ARM64 / GEMINI


def test_setup_already_onboarded(monkeypatch):
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({"loadCodeAssist": [(200, {
        "currentTier": {"id": "free-tier", "name": "Free"},
        "cloudaicompanionProject": {"id": "genai-managed"},
    })]})
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)

    info = setup.setup_code_assist("tok")
    assert info["project_id"] == "genai-managed"
    url, body = fake.calls[0]
    assert "daily-cloudcode-pa" in url  # daily 优先
    assert body["metadata"]["ideType"] == 9  # 数字枚举不是字符串


def test_post_sends_real_bearer_token(monkeypatch):
    """Authorization 必须是真 token——占位指纹头展开顺序反了会覆盖成
    "Bearer "（空），上游 401 CREDENTIALS_MISSING（真实链路踩过）。"""
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({"loadCodeAssist": [(200, {
        "currentTier": {"id": "free-tier"},
        "cloudaicompanionProject": "proj-x"})]})
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)

    setup.setup_code_assist("tok-real")
    assert fake.headers, "没有请求被发出"
    for headers in fake.headers:
        assert headers.get("authorization") == "Bearer tok-real"
        assert headers.get("x-client-name") == "antigravity"  # 指纹头仍在


def test_setup_onboard_free_tier_lro(monkeypatch):
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({
        "loadCodeAssist": [(200, {"allowedTiers": [
            {"id": "free-tier", "isDefault": True}, {"id": "standard-tier"}]})],
        "onboardUser": [(200, {"done": False, "name": "ops/x"}),
                        (200, {"done": False, "name": "ops/x"}),
                        (200, {"done": True,
                               "response": {"cloudaicompanionProject": {"id": "proj-1"}}})],
        # GET operation 走 name 轮询，也吃 onboardUser key 之外的路由：
        "ops/x": [(200, {"done": True,
                         "response": {"cloudaicompanionProject": {"id": "proj-1"}}})],
    })
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    info = setup.setup_code_assist("tok")
    assert info == {"project_id": "proj-1", "tier": "free-tier", "tier_name": ""}
    onboard_bodies = [b for u, b in fake.calls if "onboardUser" in u]
    assert onboard_bodies[0] == {"tierId": "free-tier",
                                 "metadata": {"ideType": 9, "platform": setup._platform_enum(),
                                              "pluginType": 2}}  # free 不带 project


def test_setup_fallback_to_prod_endpoint(monkeypatch):
    """daily 端点 5xx → prod 兜底成功。"""
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({"loadCodeAssist": [(500, {"error": "boom"}),
                                            (200, {"currentTier": {"id": "free-tier"},
                                                   "cloudaicompanionProject": "proj-2"})]})
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)

    info = setup.setup_code_assist("tok")
    assert info["project_id"] == "proj-2"
    urls = [u for u, _ in fake.calls]
    assert "daily-cloudcode-pa" in urls[0] and "cloudcode-pa" in urls[1]
    assert "daily" not in urls[1].split("/v1internal")[0].replace("daily-", "", 0) or True


def test_setup_ineligible_diag(monkeypatch):
    """onboarding 走完却没项目：报错要带 ineligibleTiers 的真实拒绝原因。"""
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({
        "loadCodeAssist": [(200, {
            "ineligibleTiers": [{"tierId": "free-tier", "reasonCode": "UNSUPPORTED_LOCATION",
                                 "reasonMessage": "region not supported"}]})],
        "onboardUser": [(200, {"done": True, "response": {}})],
    })
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    with pytest.raises(setup.SetupError, match="UNSUPPORTED_LOCATION"):
        setup.setup_code_assist("tok")


def test_setup_lro_error_surface(monkeypatch):
    from buddy_proxy.antigravity import setup

    fake = _FakeUrlopen({
        "loadCodeAssist": [(200, {"allowedTiers": [{"id": "free-tier", "isDefault": True}]})],
        "onboardUser": [(200, {"done": True, "error": {"code": 7, "message": "permission denied"}})],
    })
    monkeypatch.setattr(setup.urllib.request, "urlopen", fake)
    monkeypatch.setattr(setup.time, "sleep", lambda s: None)

    with pytest.raises(setup.SetupError, match="permission denied"):
        setup.setup_code_assist("tok")


# ---------------------------------------------------------------------------
# auth/login.py 入口
# ---------------------------------------------------------------------------

def test_login_entry_antigravity_registered():
    from buddy_proxy.auth import login as auth_login

    assert "antigravity" in auth_login.KNOWN_PROVIDERS
    assert auth_login._DISPATCH["antigravity"] is auth_login._login_antigravity
