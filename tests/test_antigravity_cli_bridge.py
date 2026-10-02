"""antigravity ↔ 本机 agy（Antigravity CLI）keyring 导入单元测试。

只读导入：security/secret-tool 用 monkeypatch mock，不碰真实 keyring。
"""

from __future__ import annotations

import base64
import json
import subprocess

import pytest


def _b64url(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _id_token(email: str = "agy@x.com") -> str:
    """最小 JWT（header.payload.sig），payload 带 email claim。"""
    return f"{_b64url({'alg': 'RS256'})}.{_b64url({'email': email, 'exp': 9999999999})}.sig"


def _keyring_payload(**over) -> dict:
    payload = {
        "token": {
            "access_token": "at",
            "token_type": "Bearer",
            "refresh_token": "rt",
            "expiry": "2099-01-01T00:00:00+00:00",
        },
        "auth_method": "consumer",
        "id_token": _id_token(),
    }
    token_over = over.pop("token_over", None)
    if token_over:
        payload["token"].update(token_over)
    payload.update(over)
    return payload


def _raw_value(payload: dict | str) -> str:
    """模拟 agy 写进 keyring 的原始字符串（go-keyring-base64 前缀）。"""
    if isinstance(payload, str):
        return payload
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()
    return f"go-keyring-base64:{b64}"


def _mock_security(monkeypatch, stdout: str = "", returncode: int = 0, calls=None):
    """mock subprocess.run：记录命令、返回 canned 输出。"""
    from buddy_proxy.antigravity import cli_bridge

    def fake_run(argv, capture_output=True, text=True, timeout=10):
        if calls is not None:
            calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(cli_bridge.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# 解析：_parse_payload / cli_token / cli_cached_email / to_buddy_format
# ---------------------------------------------------------------------------

def test_parse_payload_prefixed_and_bare():
    from buddy_proxy.antigravity import cli_bridge

    ok = cli_bridge._parse_payload(_raw_value(_keyring_payload()))
    assert ok is not None and ok["token"]["access_token"] == "at"
    # 无前缀裸 JSON 也认（容错 agy 未来改格式）
    ok = cli_bridge._parse_payload(json.dumps(_keyring_payload()))
    assert ok is not None
    assert cli_bridge._parse_payload("not json") is None
    assert cli_bridge._parse_payload(_raw_value({"token": {}})) is None  # 缺 token
    assert cli_bridge._parse_payload(_raw_value({"token": {"refresh_token": "r"}})) is None  # 缺 access_token
    assert cli_bridge._parse_payload("go-keyring-base64:!!!not-b64!!!") is None


def test_cli_cached_email_from_id_token():
    from buddy_proxy.antigravity import cli_bridge

    assert cli_bridge.cli_cached_email(_keyring_payload()) == "agy@x.com"
    assert cli_bridge.cli_cached_email(_keyring_payload(id_token="")) == ""
    assert cli_bridge.cli_cached_email(_keyring_payload(id_token="bad.token")) == ""
    assert cli_bridge.cli_cached_email(_keyring_payload(id_token="a.b.c")) == ""  # 解不开


def test_to_buddy_format_and_usable():
    from buddy_proxy.antigravity import cli_bridge

    payload = _keyring_payload()
    assert cli_bridge.cli_creds_usable(payload) == (True, "有效")

    expired = _keyring_payload(token_over={"expiry": "2000-01-01T00:00:00+00:00"})
    ok, note = cli_bridge.cli_creds_usable(expired)
    assert ok and "刷新" in note

    no_rt = _keyring_payload(token_over={"refresh_token": ""})
    ok, _ = cli_bridge.cli_creds_usable(no_rt)
    assert not ok

    buddy = cli_bridge.to_buddy_format(cli_bridge.cli_token(payload))
    assert buddy["access_token"] == "at"
    assert buddy["refresh_token"] == "rt"
    assert buddy["expiry"] == "2099-01-01T00:00:00+00:00"  # ISO 原样


# ---------------------------------------------------------------------------
# load_cli_creds：subprocess 编排
# ---------------------------------------------------------------------------

def test_load_cli_creds_reads_keyring(monkeypatch):
    from buddy_proxy.antigravity import cli_bridge

    calls: list[list[str]] = []
    _mock_security(monkeypatch, stdout=_raw_value(_keyring_payload()), calls=calls)
    payload = cli_bridge.load_cli_creds()
    assert payload is not None and payload["auth_method"] == "consumer"
    assert calls and calls[0][0] == "security"
    assert "gemini" in calls[0] and "antigravity" in calls[0]  # service/account 坐标


def test_load_cli_creds_missing_or_garbage(monkeypatch):
    from buddy_proxy.antigravity import cli_bridge

    _mock_security(monkeypatch, returncode=44)  # macOS item not found
    assert cli_bridge.load_cli_creds() is None
    _mock_security(monkeypatch, stdout="garbage-not-json")  # 返回 0 但内容坏
    assert cli_bridge.load_cli_creds() is None
    _mock_security(monkeypatch, stdout="")  # 空
    assert cli_bridge.load_cli_creds() is None


def test_keyring_service_account_names():
    """service=gemini / account=antigravity 是实测坐标，改了会读不到。"""
    from buddy_proxy.antigravity import cli_bridge

    assert cli_bridge.KEYRING_SERVICE == "gemini"
    assert cli_bridge.KEYRING_ACCOUNT == "antigravity"


# ---------------------------------------------------------------------------
# adopt_cli_login：导入 → 落盘 → onboarding
# ---------------------------------------------------------------------------

def test_adopt_cli_login(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import cli_bridge, credentials as creds
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: _keyring_payload())
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "proj-x", "tier": "free-tier", "tier_name": ""})

    cred = ag_login.adopt_cli_login()
    assert cred["project_id"] == "proj-x"
    assert cred["email"] == "agy@x.com"  # 来自 id_token
    assert creds.load_cred()["project_id"] == "proj-x"  # 已落盘


def test_adopt_cli_login_refreshes_expired(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import cli_bridge, credentials as creds
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds",
                        lambda: _keyring_payload(token_over={"expiry": "2000-01-01T00:00:00+00:00"}))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600})
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "p", "tier": "free-tier", "tier_name": ""})

    cred = ag_login.adopt_cli_login()
    assert cred["access_token"] == "fresh"


def test_adopt_cli_login_onboard_fail_keeps_token(tmp_path, monkeypatch):
    """导入时 onboarding 失败：token 也要先落盘（agy 的授权不白费）。"""
    from buddy_proxy.antigravity import cli_bridge, credentials as creds
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: _keyring_payload())

    def boom(token, project_id=""):
        raise ag_login.SetupError("INELIGIBLE: nope")

    monkeypatch.setattr(ag_login, "setup_code_assist", boom)
    with pytest.raises(ag_login.LoginError, match="INELIGIBLE"):
        ag_login.adopt_cli_login()
    assert creds.load_cred() is not None and creds.load_cred()["access_token"] == "at"


def test_adopt_cli_login_no_creds(tmp_path, monkeypatch):
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: None)
    with pytest.raises(ag_login.LoginError, match="未找到"):
        ag_login.adopt_cli_login()


# ---------------------------------------------------------------------------
# auth/login.py 入口：检测 agy 态 → 问 → 采用
# ---------------------------------------------------------------------------

def test_login_entry_uses_agy_creds_on_enter(tmp_path, monkeypatch, capsys):
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.antigravity import cli_bridge, credentials as creds
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: _keyring_payload())
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: True)
    monkeypatch.setattr(ag_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "proj-x", "tier": "free-tier", "tier_name": ""})

    rc = auth_login._login_antigravity(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "已有登录态" in out and "agy@x.com" in out
    assert creds.load_cred()["project_id"] == "proj-x"


def test_login_entry_no_declines_browser_flow(tmp_path, monkeypatch, capsys):
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: _keyring_payload())
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: False)
    monkeypatch.setattr(
        ag_login, "login_interactive",
        lambda open_browser=True: {"email": "n@x.com", "tier": "t", "project_id": "p2"})

    rc = auth_login._login_antigravity(open_browser=False)
    assert rc == 0
    assert "n@x.com" in capsys.readouterr().out


def test_login_entry_shows_buddy_account_when_switching(tmp_path, monkeypatch, capsys):
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.antigravity import cli_bridge, credentials as creds
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: _keyring_payload())  # agy@x.com
    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00",
                     "email": "old@x.com", "project_id": "p-old"})
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: False)
    monkeypatch.setattr(
        ag_login, "login_interactive",
        lambda open_browser=True: {"email": "agy@x.com", "tier": "t", "project_id": "p"})

    rc = auth_login._login_antigravity(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "当前 buddy 登录的是 old@x.com" in out


def test_login_entry_unusable_agy_state_falls_through(tmp_path, monkeypatch, capsys):
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds",
                        lambda: _keyring_payload(token_over={"refresh_token": ""}))
    monkeypatch.setattr(
        ag_login, "login_interactive",
        lambda open_browser=True: {"email": "f@x.com", "tier": "t", "project_id": "p"})

    rc = auth_login._login_antigravity(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "不可用" in out and "f@x.com" in out


def test_login_entry_adopt_failure_falls_through_to_resume(tmp_path, monkeypatch, capsys):
    """采用 agy 失败（onboarding 被拒等）→ 落回续跑/浏览器，不直接退出。

    adopt 在 onboarding 阶段失败时 token 已落盘，fallthrough 后应走 resume
    分支免浏览器续跑（与 gemini 通道「失败可改走浏览器授权」同款兜底）。
    """
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.antigravity import cli_bridge, login as ag_login

    monkeypatch.setenv("ANTIGRAVITY_OAUTH_JSON", str(tmp_path / "ag.json"))
    monkeypatch.setattr(cli_bridge, "load_cli_creds", lambda: _keyring_payload())
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: True)

    def boom():
        raise ag_login.LoginError("Antigravity onboarding 失败: INELIGIBLE")

    monkeypatch.setattr(ag_login, "adopt_cli_login", boom)
    # token 已落盘（boom 模拟 onboarding 失败的中间态）
    from buddy_proxy.antigravity import credentials as creds

    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00", "email": "agy@x.com"})
    monkeypatch.setattr(
        ag_login, "resume_onboarding",
        lambda: {"email": "agy@x.com", "tier": "t", "project_id": "p"})

    rc = auth_login._login_antigravity(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "落回浏览器授权流程" in out and "agy@x.com" in out
