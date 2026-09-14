"""`--no-browser` 必须真的不弹浏览器、不喷登录 URL（完全离线）。

历史缺陷：后台链路（自动打卡轮询）在启动后一秒内就会调
``ProxyState.ensure_auth()`` → ``client.ensure_authenticated()``，后者用的是
``open_browser=True`` 默认值，完全绕过了 ``--no-browser``。于是用户传了
``--no-browser`` 仍然被拉起登录页，stdout 也会出现一个没人要的 authUrl。

本文件不读真实 session、~/.buddy-proxy、.env 或任何上游端点。
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from buddy_proxy.codebuddy_provider import client as client_module
from buddy_proxy.codebuddy_provider.client import CodeBuddyClient
from buddy_proxy.core.state import ProxyState


def _client(tmp_path) -> CodeBuddyClient:
    """凭证落 tmp；不需要 __init__ 里的会话加载行为干扰断言。"""
    return CodeBuddyClient("https://example.invalid",
                           session_file=tmp_path / "session.json")


def _state(client, *, interactive_login: bool) -> ProxyState:
    return ProxyState(
        client=client,
        mock_dir=None,
        log_file=None,
        interactive_login=interactive_login,
    )


# ---------------------------------------------------------------- 客户端层
@pytest.mark.parametrize("announce,open_browser,expect_url,expect_browser", [
    (True, True, True, True),      # 显式 --login：就是要弹窗 + 打 URL
    (True, False, True, False),    # 只要 URL 不弹窗
    (False, False, False, False),  # 后台补认证：都不做
])
def test_login_announce_and_open_browser_are_independent(
    monkeypatch, capsys, announce, open_browser, expect_url, expect_browser
):
    """登录 URL 的输出与浏览器拉起是两个独立开关。"""
    client = CodeBuddyClient("https://example.invalid", session_file=None)
    monkeypatch.setattr(
        client, "_request",
        lambda *a, **kw: {"data": {"authUrl": "https://up.example/login?state=xyz",
                                   "state": "xyz"}},
    )
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u) or True)
    # 轮询到超时即止：token 端点永远拿不到 token。只补丁 client 模块内的
    # 引用，不动全局 time.sleep（pytest 自己也用它）。
    monkeypatch.setattr(client_module.time, "sleep", lambda _s: None)

    client.login(open_browser=open_browser, announce=announce, timeout=0)
    out = capsys.readouterr().out
    assert ("https://up.example/login?state=xyz" in out) is expect_url
    assert bool(opened) is expect_browser


# ---------------------------------------------------------------- 状态层
def _capture_login(monkeypatch, client) -> list[dict]:
    """拦在 login 边界：只验「传了什么参数」，不驱动真实轮询循环。"""
    seen: list[dict] = []

    def fake_login(*, open_browser=True, timeout=300, announce=True):
        seen.append({"open_browser": open_browser, "announce": announce})

    monkeypatch.setattr(client, "login", fake_login)
    return seen


def test_no_browser_flag_suppresses_background_login(monkeypatch, tmp_path):
    """interactive_login=False（= 没传 --login）时不得弹窗、不得打登录 URL。"""
    client = _client(tmp_path)
    state = _state(client, interactive_login=False)
    monkeypatch.setattr(client, "session", {})      # 无 token
    monkeypatch.setattr(client, "refresh", lambda: False)  # refresh 也拿不到
    seen = _capture_login(monkeypatch, client)

    state.ensure_auth()  # login 被拦下，不会真去轮询

    assert seen == [{"open_browser": False, "announce": False}], (
        "后台补认证必须既不弹浏览器、也不打登录 URL"
    )


def test_login_flag_keeps_interactive_login(monkeypatch, tmp_path):
    """显式 --login（interactive_login=True）仍要打 URL 并弹窗。"""
    client = _client(tmp_path)
    state = _state(client, interactive_login=True)
    monkeypatch.setattr(client, "session", {})
    monkeypatch.setattr(client, "refresh", lambda: False)
    seen = _capture_login(monkeypatch, client)

    state.ensure_auth()

    assert seen == [{"open_browser": True, "announce": True}]


def test_valid_token_short_circuits_before_login(monkeypatch, tmp_path):
    """token 还有效时压根不该走 login（避免误报）。"""
    client = _client(tmp_path)
    state = _state(client, interactive_login=False)
    monkeypatch.setattr(client, "session", {
        "auth": {"accessToken": "t", "expiresAt": int(time.time() * 1000) + 3_600_000}
    })
    called: list[Any] = []
    monkeypatch.setattr(client, "login", lambda **kw: called.append(kw))

    state.ensure_auth()
    assert called == []


def test_refresh_success_short_circuits_before_login(monkeypatch, tmp_path):
    """refresh 成功时也不该进入 login。"""
    client = _client(tmp_path)
    state = _state(client, interactive_login=False)
    monkeypatch.setattr(client, "session", {"auth": {"accessToken": "t", "expiresAt": 1}})
    monkeypatch.setattr(client, "refresh", lambda: True)
    called: list[Any] = []
    monkeypatch.setattr(client, "login", lambda **kw: called.append(kw))

    state.ensure_auth()
    assert called == []


def test_ensure_auth_end_to_end_prints_no_url_when_non_interactive(
    monkeypatch, tmp_path, capsys
):
    """端到端（真跑 login，timeout=0 立即超时）：后台路径 stdout 无 URL、无弹窗。"""
    client = _client(tmp_path)
    state = _state(client, interactive_login=False)
    monkeypatch.setattr(client, "session", {})
    monkeypatch.setattr(client, "refresh", lambda: False)
    monkeypatch.setattr(
        client, "_request",
        lambda *a, **kw: {"data": {"authUrl": "https://up.example/login?state=xyz",
                                   "state": "xyz"}},
    )
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda u: opened.append(u) or True)
    # 让 login 的轮询窗口为 0：只跑一次「取 authUrl + 打提示」就结束。
    real_login = type(client).login

    def login_zero_timeout(*, open_browser=True, announce=True, timeout=300):
        return real_login(client, open_browser=open_browser, announce=announce,
                          timeout=0)

    monkeypatch.setattr(client, "login", login_zero_timeout)

    state.ensure_auth()  # timeout=0：无 token，直接返回

    out = capsys.readouterr().out
    assert opened == []
    assert "up.example/login" not in out, f"后台路径泄漏了登录 URL:\n{out}"
    assert "[Auth]" in out


def test_default_is_interactive_for_direct_users(monkeypatch, tmp_path):
    """直接构造 ProxyState（不走 CLI）保持原交互语义，行为不变。"""
    client = _client(tmp_path)
    state = ProxyState(client=client, mock_dir=None, log_file=None)
    assert state.interactive_login is True
    seen: list[dict] = []
    monkeypatch.setattr(client, "session", {})
    monkeypatch.setattr(client, "refresh", lambda: False)
    monkeypatch.setattr(client, "login", lambda **kw: seen.append(kw))

    state.ensure_auth()
    assert seen == [{"open_browser": True, "announce": True}]
