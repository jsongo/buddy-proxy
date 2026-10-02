"""gemini ↔ 本机 CLI 凭证互通（cli_bridge + 登录入口）单元测试。

GEMINI_CLI_HOME 把 CLI 配置目录指到 tmp，GEMINI_OAUTH_JSON 把我们的
凭据文件也指到 tmp——两边都不碰真实配置。
"""

from __future__ import annotations

import json
import time

import pytest


def _our_cred(**over):
    cred = {
        "access_token": "at",
        "refresh_token": "rt",
        "token_type": "Bearer",
        "scope": "scope-a scope-b",
        "expiry": "2099-01-01T00:00:00+00:00",
        "email": "u@x.com",
    }
    cred.update(over)
    return cred


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    return tmp_path / ".gemini"


@pytest.fixture
def buddy_cred(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_OAUTH_JSON", str(tmp_path / "buddy.json"))
    return tmp_path / "buddy.json"


# ---------------------------------------------------------------------------
# sync_to_cli：写 CLI 三件套
# ---------------------------------------------------------------------------

def test_sync_writes_creds_settings_accounts(cli_home):
    from buddy_proxy.gemini import cli_bridge

    notes = cli_bridge.sync_to_cli(_our_cred())
    assert any("oauth_creds.json" in n for n in notes)

    raw = json.loads((cli_home / "oauth_creds.json").read_text())
    assert raw["access_token"] == "at"
    assert raw["refresh_token"] == "rt"
    assert raw["token_type"] == "Bearer"
    assert raw["scope"] == "scope-a scope-b"
    assert raw["expiry_date"] > 4_000_000_000_000  # 毫秒时间戳
    assert cli_home.joinpath("oauth_creds.json").stat().st_mode & 0o777 == 0o600

    settings = json.loads((cli_home / "settings.json").read_text())
    assert settings["security"]["auth"]["selectedType"] == "oauth-personal"

    accounts = json.loads((cli_home / "google_accounts.json").read_text())
    assert accounts["active"] == "u@x.com"


def test_sync_preserves_cli_existing_fields(cli_home):
    """CLI 已有字段（id_token）和用户 settings（hooks）都不能被覆盖丢掉。"""
    from buddy_proxy.gemini import cli_bridge

    cli_home.mkdir(parents=True)
    (cli_home / "oauth_creds.json").write_text(json.dumps(
        {"access_token": "old", "id_token": "idt", "refresh_token": "oldrt"}))
    (cli_home / "settings.json").write_text(json.dumps({"hooks": {"a": 1}}))

    cli_bridge.sync_to_cli(_our_cred())

    raw = json.loads((cli_home / "oauth_creds.json").read_text())
    assert raw["id_token"] == "idt"  # CLI 自己的额外字段保留
    assert raw["access_token"] == "at"  # 我们的更新生效
    settings = json.loads((cli_home / "settings.json").read_text())
    assert settings["hooks"] == {"a": 1}
    assert settings["security"]["auth"]["selectedType"] == "oauth-personal"


def test_sync_cache_account_rotates(cli_home):
    """换号：旧 active 进 old，新号从 old 挪出（cacheGoogleAccount 语义）。"""
    from buddy_proxy.gemini import cli_bridge

    cli_bridge.sync_to_cli(_our_cred(email="a@x.com"))
    cli_bridge.sync_to_cli(_our_cred(email="b@x.com"))
    accounts = json.loads((cli_home / "google_accounts.json").read_text())
    assert accounts["active"] == "b@x.com"
    assert accounts["old"] == ["a@x.com"]
    # 换回 a：a 从 old 移除，b 进 old
    cli_bridge.sync_to_cli(_our_cred(email="a@x.com"))
    accounts = json.loads((cli_home / "google_accounts.json").read_text())
    assert accounts["active"] == "a@x.com"
    assert accounts["old"] == ["b@x.com"]


def test_sync_no_email_skips_accounts(cli_home):
    from buddy_proxy.gemini import cli_bridge

    cli_bridge.sync_to_cli(_our_cred(email=""))
    assert not (cli_home / "google_accounts.json").exists()


def test_sync_settings_rejects_broken_structure(cli_home):
    """settings.json 的 security 不是对象时宁可不改，不能覆盖坏用户数据。"""
    from buddy_proxy.gemini import cli_bridge

    cli_home.mkdir(parents=True)
    (cli_home / "settings.json").write_text(json.dumps({"security": "oops"}))
    notes = cli_bridge.sync_to_cli(_our_cred())
    assert any("settings.json" in n and "失败" in n for n in notes)
    assert json.loads((cli_home / "settings.json").read_text()) == {"security": "oops"}


def test_sync_skips_oauth_creds_when_encrypted(cli_home, monkeypatch):
    """CLI 开了加密存储：明文 oauth_creds 写了也不被读，跳过并明说。"""
    from buddy_proxy.gemini import cli_bridge

    monkeypatch.setenv("GEMINI_FORCE_ENCRYPTED_FILE_STORAGE", "true")
    notes = cli_bridge.sync_to_cli(_our_cred())
    assert not (cli_home / "oauth_creds.json").exists()
    assert any("GEMINI_FORCE_ENCRYPTED_FILE_STORAGE" in n for n in notes)
    # settings / google_accounts 不受影响，照写
    settings = json.loads((cli_home / "settings.json").read_text())
    assert settings["security"]["auth"]["selectedType"] == "oauth-personal"
    accounts = json.loads((cli_home / "google_accounts.json").read_text())
    assert accounts["active"] == "u@x.com"


# ---------------------------------------------------------------------------
# 读回 + 双向转换
# ---------------------------------------------------------------------------

def test_load_and_convert_roundtrip(cli_home):
    from buddy_proxy.gemini import cli_bridge

    cli_bridge.sync_to_cli(_our_cred())
    loaded = cli_bridge.load_cli_creds()
    assert loaded is not None and loaded["access_token"] == "at"

    buddy = cli_bridge.to_buddy_format(loaded)
    assert buddy["refresh_token"] == "rt"
    assert buddy["expiry"].startswith("2099-01-01")
    assert buddy["token_type"] == "Bearer"

    again = cli_bridge.to_cli_format(buddy)
    assert again["expiry_date"] == loaded["expiry_date"]  # ms ↔ ISO 无损


def test_load_cli_creds_missing_or_broken(cli_home):
    from buddy_proxy.gemini import cli_bridge

    assert cli_bridge.load_cli_creds() is None  # 目录不存在
    cli_home.mkdir(parents=True)
    (cli_home / "oauth_creds.json").write_text("{not json")
    assert cli_bridge.load_cli_creds() is None
    (cli_home / "oauth_creds.json").write_text(json.dumps({"refresh_token": "r"}))
    assert cli_bridge.load_cli_creds() is None  # 没有 access_token 视为无效


def test_usable_states():
    from buddy_proxy.gemini import cli_bridge

    future = int((time.time() + 3600) * 1000)
    past = int((time.time() - 3600) * 1000)
    ok, note = cli_bridge.cli_creds_usable(
        {"access_token": "a", "refresh_token": "r", "expiry_date": future})
    assert ok and note == "有效"
    ok, note = cli_bridge.cli_creds_usable(
        {"access_token": "a", "refresh_token": "r", "expiry_date": past})
    assert ok and "刷新" in note  # 过期但有 refresh token → 仍可用
    ok, note = cli_bridge.cli_creds_usable(
        {"access_token": "a", "expiry_date": future})
    assert ok  # 裸 access token 未过期
    ok, _ = cli_bridge.cli_creds_usable({"access_token": "a", "expiry_date": past})
    assert not ok
    ok, _ = cli_bridge.cli_creds_usable({"access_token": "a"})  # 无 expiry 视为不可信
    assert not ok


# ---------------------------------------------------------------------------
# adopt_cli_login：免浏览器采用 CLI 登录态
# ---------------------------------------------------------------------------

def test_adopt_cli_login(cli_home, buddy_cred, monkeypatch):
    from buddy_proxy.gemini import login as gm_login

    cli_bridge = pytest.importorskip("buddy_proxy.gemini.cli_bridge")
    cli_bridge.sync_to_cli(_our_cred())  # 造出 CLI 登录态
    monkeypatch.setattr(gm_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "genai-x", "tier": "free-tier", "tier_name": "Free"})

    cred = gm_login.adopt_cli_login()
    assert cred["project_id"] == "genai-x"
    assert cred["email"] == "u@x.com"  # 来自 google_accounts.json
    assert json.loads(buddy_cred.read_text())["project_id"] == "genai-x"  # 已落盘


def test_adopt_cli_login_onboard_fail_keeps_token(cli_home, buddy_cred, monkeypatch):
    """采用 CLI 态时 onboarding 失败：token 也要先落盘（授权不白费）。"""
    from buddy_proxy.gemini import login as gm_login

    cli_bridge = pytest.importorskip("buddy_proxy.gemini.cli_bridge")
    cli_bridge.sync_to_cli(_our_cred())

    def boom(token, project_id=""):
        raise gm_login.SetupError("UNSUPPORTED_LOCATION: nope")

    monkeypatch.setattr(gm_login, "setup_code_assist", boom)
    with pytest.raises(gm_login.LoginError, match="UNSUPPORTED_LOCATION"):
        gm_login.adopt_cli_login()
    from buddy_proxy.gemini.credentials import load_cred
    assert load_cred() is not None and load_cred()["access_token"] == "at"


def test_adopt_cli_login_refreshes_expired(cli_home, buddy_cred, monkeypatch):
    """access token 过期但有 refresh token：自动刷新后再走 onboarding。"""
    from buddy_proxy.gemini import cli_bridge, credentials as creds
    from buddy_proxy.gemini import login as gm_login

    cli_bridge.sync_to_cli(_our_cred(expiry="2000-01-01T00:00:00+00:00"))
    monkeypatch.setattr(creds, "_token_request", lambda data, timeout=30.0: {
        "access_token": "fresh", "expires_in": 3600})
    monkeypatch.setattr(gm_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "p", "tier": "free-tier", "tier_name": "Free"})

    cred = gm_login.adopt_cli_login()
    assert cred["access_token"] == "fresh"


def test_adopt_cli_login_no_creds_raises(cli_home, buddy_cred):
    from buddy_proxy.gemini import login as gm_login

    with pytest.raises(gm_login.LoginError, match="未找到"):
        gm_login.adopt_cli_login()


# ---------------------------------------------------------------------------
# auth/login.py 入口：检测 CLI 态 → 问 → 采用
# ---------------------------------------------------------------------------

def test_login_gemini_uses_cli_creds_on_enter(cli_home, buddy_cred, monkeypatch, capsys):
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.gemini import cli_bridge, login as gm_login

    cli_bridge.sync_to_cli(_our_cred())
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: True)
    monkeypatch.setattr(gm_login, "setup_code_assist", lambda token, project_id="": {
        "project_id": "genai-x", "tier": "free-tier", "tier_name": "Free"})

    rc = auth_login._login_gemini(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "已有登录态" in out
    assert "u@x.com" in out
    assert "互通" in out


def test_login_gemini_no_declines_browser_flow(cli_home, buddy_cred, monkeypatch, capsys):
    """答 no：不采用 CLI 态，落回原有浏览器流程（这里 mock 掉登录本身）。"""
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.gemini import cli_bridge, login as gm_login

    cli_bridge.sync_to_cli(_our_cred())
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: False)
    monkeypatch.setattr(
        gm_login, "login_interactive",
        lambda open_browser=True: {"email": "n@x.com", "tier": "free-tier",
                                   "project_id": "p2"})

    rc = auth_login._login_gemini(open_browser=False)
    assert rc == 0
    assert "n@x.com" in capsys.readouterr().out


def test_login_gemini_shows_buddy_account_when_switching(cli_home, buddy_cred, monkeypatch, capsys):
    """buddy 已登录另一个账号：CLI 态提示要把当前账号亮出来（知情换号）。"""
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.gemini import cli_bridge, credentials as creds, login as gm_login

    cli_bridge.sync_to_cli(_our_cred())  # CLI 账号 u@x.com
    creds.save_cred({"access_token": "a", "refresh_token": "r",
                     "expiry": "2099-01-01T00:00:00+00:00",
                     "email": "a@x.com", "project_id": "p-old"})
    monkeypatch.setattr(auth_login, "_ask_default_yes", lambda q: False)
    monkeypatch.setattr(
        gm_login, "login_interactive",
        lambda open_browser=True: {"email": "u@x.com", "tier": "t", "project_id": "p"})

    rc = auth_login._login_gemini(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "当前 buddy 登录的是 a@x.com" in out
    assert "切换" in out


def test_login_gemini_unusable_cli_state_falls_through(cli_home, buddy_cred, monkeypatch, capsys):
    """CLI 态不可用：提示后直接走浏览器流程，不问、不采用。"""
    import buddy_proxy.auth.login as auth_login
    from buddy_proxy.gemini import login as gm_login

    cli_home.mkdir(parents=True)
    (cli_home / "oauth_creds.json").write_text(json.dumps(
        {"access_token": "dead", "expiry_date": int((time.time() - 9999) * 1000)}))
    monkeypatch.setattr(
        gm_login, "login_interactive",
        lambda open_browser=True: {"email": "f@x.com", "tier": "t", "project_id": "p"})

    rc = auth_login._login_gemini(open_browser=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "不可用" in out and "f@x.com" in out


def test_ask_default_yes(monkeypatch):
    import buddy_proxy.auth.login as auth_login

    import io
    assert auth_login._ask_default_yes("q") is True  # 非 tty → 默认 yes
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    monkeypatch.setattr(auth_login.sys.stdin, "isatty", lambda: True)
    assert auth_login._ask_default_yes("q") is True  # 回车 = yes
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    monkeypatch.setattr(auth_login.sys.stdin, "isatty", lambda: True)
    assert auth_login._ask_default_yes("q") is False
