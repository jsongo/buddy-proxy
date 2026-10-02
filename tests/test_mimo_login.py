"""小米账号登录（``mimo/login.py``）测试——完全离线，不访问小米上游。

覆盖：
- 登录链接生成（**callback 必须为空**——非空会被上游 10025 拒掉）
- 长轮询结果解析（``data`` 子节点与顶层两种形态）
- 未登录时挂住重连、超时/失效的报错路径
- 凭据落盘 0600 + 读取回退链（文件优先于桌面 cookie 库）

运行：
    .venv/bin/python -m pytest tests/test_mimo_login.py -v
"""
from __future__ import annotations

import json
import stat

import httpx
import pytest

from buddy_proxy.mimo import login
from buddy_proxy.mimo.sso import AccountCookies


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "buddy-state"
    d.mkdir()
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(d))
    monkeypatch.delenv("MIMO_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("MIMO_COOKIE_DB", raising=False)
    return d


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# 登录链接生成
# ---------------------------------------------------------------------------


def test_start_login_keeps_callback_empty():
    """``callback`` 必须是空串——非空会被上游判「Callback连接不合法」。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            text="&&&START&&&" + json.dumps({
                "code": 0, "loginUrl": "https://ak.account.xiaomi.com/lp?t=1",
                "lp": "https://ak.lp.account.xiaomi.com/lp/s?k=1", "timeout": 300,
            }),
        )

    with _client(handler) as c:
        session = login.start_login(client=c)

    assert seen["callback"] == "", f"callback 必须为空，实际={seen['callback']!r}"
    assert seen["sid"] == "mimopc"
    assert session.login_url.startswith("https://")
    assert session.poll_url.startswith("https://")
    assert session.expires_in == 300


def test_start_login_rejects_10025():
    """上游用 10025 表达「回调不合法」，要转成可读错误而不是静默。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="&&&START&&&" + json.dumps({
                "code": 10025, "result": "error", "desc": "Callback连接不合法",
            }),
        )

    with _client(handler) as c:
        with pytest.raises(login.LoginError, match="Callback连接不合法"):
            login.start_login(client=c)


def test_start_login_opens_login_page_not_api_endpoint():
    """给浏览器打开的必须是 ``location`` 里的登录页，**不是** ``loginUrl``。

    这是实测踩出来的坑：``loginUrl`` 是给程序调的 API 端点，浏览器直接开只会
    看到一段 ``code:70016 登录验证失败`` 的 JSON。真正的登录页在同一个响应的
    ``location`` 字段里。
    """
    api = "https://ak.account.xiaomi.com/longPolling/login?ticket=lp_x&sid=mimopc"
    page = "https://account.xiaomi.com/fe/service/login?_group=DEFAULT&sid=passport"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:  # loginUrl
            return httpx.Response(200, text="&&&START&&&" + json.dumps({
                "code": 0, "loginUrl": api,
                "lp": "https://ak.lp.account.xiaomi.com/lp/s?k=1", "timeout": 300,
            }))
        # 跟一次 loginUrl，拿到带 location 的结果
        return httpx.Response(200, text="&&&START&&&" + json.dumps({
            "code": 70016, "desc": "登录验证失败", "location": page,
        }))

    with _client(handler) as c:
        session = login.start_login(client=c)

    assert session.login_url == page, "浏览器要开 location 里的登录页"
    assert session.poll_url.startswith("https://")


def test_browser_url_falls_back_when_no_location():
    """取不到 location 时退回原地址，不能变成空链接让用户干瞪眼。"""
    api = "https://ak.account.xiaomi.com/longPolling/login?ticket=lp_y"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='&&&START&&&{"code":70016,"desc":"x"}')

    with _client(handler) as c:
        assert login._browser_url(api, c) == api


def test_start_login_reports_missing_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='&&&START&&&{"code":0}')

    with _client(handler) as c:
        with pytest.raises(login.LoginError, match="缺字段"):
            login.start_login(client=c)


# ---------------------------------------------------------------------------
# 结果解析：passToken/userId 可能挂在 data 下，也可能在顶层
# ---------------------------------------------------------------------------


def test_extract_account_from_data_node():
    node = {"code": 0, "data": {"passToken": "pt-1", "userId": "u1", "cUserId": "c1"}}
    acct = login._extract_account(node)
    assert acct is not None
    assert (acct.pass_token, acct.user_id, acct.c_user_id) == ("pt-1", "u1", "c1")


def test_extract_account_from_top_level():
    node = {"code": 0, "passToken": "pt-2", "userId": "u2"}
    acct = login._extract_account(node)
    assert acct is not None
    assert (acct.pass_token, acct.user_id) == ("pt-2", "u2")
    assert acct.c_user_id == ""


def test_extract_account_requires_both_fields():
    """只有 passToken 没有 userId 不算成功（两者缺一换不了票）。"""
    assert login._extract_account({"passToken": "pt"}) is None
    assert login._extract_account({"userId": "u1"}) is None
    assert login._extract_account({"data": "not-a-dict"}) is None
    assert login._extract_account(None) is None


# ---------------------------------------------------------------------------
# 长轮询
# ---------------------------------------------------------------------------


def _session():
    return login.LoginSession(
        login_url="https://ak.account.xiaomi.com/lp?t=1",
        poll_url="https://ak.lp.account.xiaomi.com/lp/s?k=1",
        expires_in=300,
    )


def test_poll_retries_through_timeouts_then_succeeds():
    """未登录时连接会挂住（ReadTimeout）——那不算失败，要重连继续等。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadTimeout("still waiting", request=request)
        return httpx.Response(
            200,
            text="&&&START&&&" + json.dumps({
                "code": 0, "data": {"passToken": "pt-9", "userId": "u9"},
            }),
        )

    with _client(handler) as c:
        acct = login.poll_login(_session(), client=c)

    assert calls["n"] == 3, "前两次超时后应继续轮询"
    assert (acct.pass_token, acct.user_id) == ("pt-9", "u9")


def test_poll_skips_non_json_bodies():
    """HTML 错误页之类的非 JSON 响应不能被当成结果。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="<html>502 Bad Gateway</html>")
        return httpx.Response(
            200, text='&&&START&&&{"passToken":"pt-a","userId":"ua"}',
        )

    with _client(handler) as c:
        acct = login.poll_login(_session(), client=c)

    assert acct.user_id == "ua"


def test_poll_reports_expired_ticket():
    """ticket 过期要立刻报错，不能让用户干等到总预算耗尽。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="&&&START&&&" + json.dumps({
                "code": 70001, "result": "error", "desc": "ticket已过期",
            }),
        )

    with _client(handler) as c:
        with pytest.raises(login.LoginError, match="已失效"):
            login.poll_login(_session(), client=c)


def test_poll_stops_at_ticket_expiry():
    """截止时间用链接自带的 ``expires_in``，超时就报错——不能无限等。

    这条是实测逼出来的：ticket 过期后上游**依然挂住连接**、不返回任何错误，
    所以只能自己掐表，否则用户会对着一个死链接白等。
    """
    expired = login.LoginSession(
        login_url="https://ak.account.xiaomi.com/lp?t=1",
        poll_url="https://ak.lp.account.xiaomi.com/lp/s?k=1",
        expires_in=0.05,  # 减去 grace 后立即到期
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang forever", request=request)

    with _client(handler) as c:
        with pytest.raises(login.LoginError, match="已失效"):
            login.poll_login(expired, client=c)


def test_poll_without_expires_in_uses_default_budget(monkeypatch):
    """上游没给 ``timeout`` 字段时用兜底预算，而不是立刻放弃。"""
    monkeypatch.setattr(login, "_DEFAULT_BUDGET_S", 0.05)
    session = login.LoginSession(
        login_url="https://ak.account.xiaomi.com/lp?t=1",
        poll_url="https://ak.lp.account.xiaomi.com/lp/s?k=1",
        expires_in=0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("hang forever", request=request)

    with _client(handler) as c:
        with pytest.raises(login.LoginError, match="已失效"):
            login.poll_login(session, client=c)


def test_poll_calls_on_tick():
    ticks = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='&&&START&&&{"passToken":"p","userId":"u"}')

    with _client(handler) as c:
        login.poll_login(_session(), on_tick=lambda: ticks.__setitem__("n", ticks["n"] + 1), client=c)

    assert ticks["n"] == 1


# ---------------------------------------------------------------------------
# 凭据落盘 / 读取
# ---------------------------------------------------------------------------


def test_save_account_file_mode_is_0600(state_dir):
    """凭据含 passToken，必须 0600 落盘。"""
    path = login.save_account(
        AccountCookies(pass_token="pt-secret", user_id="u1", c_user_id="c1")
    )
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"凭据文件权限应为 0600，实际 {oct(mode)}"
    assert path.name == "mimo_account.json"


def test_save_then_load_round_trip(state_dir):
    login.save_account(AccountCookies(pass_token="pt-1", user_id="u1", c_user_id="c1"))
    loaded = login.load_saved_account()
    assert loaded is not None
    assert (loaded.pass_token, loaded.user_id, loaded.c_user_id) == ("pt-1", "u1", "c1")


def test_load_saved_account_missing_file(state_dir):
    assert login.load_saved_account() is None


def test_load_saved_account_rejects_incomplete(state_dir):
    """字段不全的文件当没登录，别把半个凭据喂给换票流程。"""
    for payload in ({}, {"pass_token": "pt"}, {"user_id": "u1"}, {"pass_token": "", "user_id": "u1"}):
        login.account_path().write_text(json.dumps(payload), encoding="utf-8")
        assert login.load_saved_account() is None, f"不该接受 {payload}"


def test_load_saved_account_tolerates_bad_json(state_dir):
    login.account_path().write_text("{not json", encoding="utf-8")
    assert login.load_saved_account() is None


def test_saved_file_wins_over_desktop_cookie_db(state_dir, tmp_path, monkeypatch):
    """**登录落盘的文件优先于桌面 cookie 库**——新机器不装桌面也能用。"""
    import sqlite3

    from buddy_proxy.mimo import sso

    db = tmp_path / "Cookies"
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE cookies (name TEXT, value TEXT, encrypted_value BLOB)")
        con.executemany(
            "INSERT INTO cookies (name, value, encrypted_value) VALUES (?, ?, ?)",
            [("passToken", "from-desktop", b""), ("userId", "desktop-user", b"")],
        )
        con.commit()
    finally:
        con.close()
    monkeypatch.setenv("MIMO_COOKIE_DB", str(db))

    # 只有桌面库时读桌面
    acct = sso.load_account_cookies()
    assert acct is not None and acct.pass_token == "from-desktop"

    # 登录落盘后，文件说了算
    login.save_account(AccountCookies(pass_token="from-login", user_id="login-user"))
    acct2 = sso.load_account_cookies()
    assert acct2 is not None
    assert acct2.pass_token == "from-login", "文件应优先于桌面 cookie 库"
    assert acct2.user_id == "login-user"


# ---------------------------------------------------------------------------
# serviceToken 缓存必须绑定账号
# ---------------------------------------------------------------------------


def test_token_cache_rejects_other_account(state_dir):
    """换号后旧 serviceToken 不得命中——它只对签发它的那个账号有效。

    缓存文件是全局一份，早期实现只校验 sid。于是 ``buddy login mimo`` 换个
    账号之后，网关仍会把上一个账号的票发出去（额度记到别人头上，或者直接
    401——而那时已经白跑了一轮才发现）。这里钉住 user_id 校验。
    """
    from buddy_proxy.mimo import sso

    st = sso.ServiceToken(sid=sso.SSO_SID, token="tok-A", obtained_at=sso.time.time())
    sso.save_cached_token(st, user_id="user-A")

    # 同一账号：命中
    got = sso.load_cached_token(sso.SSO_SID, user_id="user-A")
    assert got is not None and got.token == "tok-A"

    # 换了账号：必须判失效
    assert sso.load_cached_token(sso.SSO_SID, user_id="user-B") is None

    # 不传 user_id（旧调用形态）：维持原行为，仍可命中
    assert sso.load_cached_token(sso.SSO_SID) is not None


def test_token_cache_without_user_id_field_is_stale(state_dir):
    """老缓存文件没有 user_id 字段 → 判失效，宁可多换一次票。

    字段缺失意味着无法确认它属于谁。放行就等于把「未知归属的票」当成当前
    账号的票用——正是要修的 bug，所以缺字段时一律不认。
    """
    from buddy_proxy.mimo import sso

    path = state_dir / "mimo_sso_token.json"
    path.write_text(json.dumps({
        "sid": sso.SSO_SID, "token": "tok-legacy",
        "extra_cookies": {}, "obtained_at": sso.time.time(),
    }))
    assert sso.load_cached_token(sso.SSO_SID, user_id="user-A") is None


def test_login_invalidates_stale_token_cache(state_dir, monkeypatch):
    """登录完成后清掉旧的 serviceToken 缓存（换号场景的兜底）。"""
    from buddy_proxy.mimo import sso

    sso.save_cached_token(
        sso.ServiceToken(sid=sso.SSO_SID, token="tok-old", obtained_at=sso.time.time()),
        user_id="user-old",
    )
    monkeypatch.setattr(
        login, "start_login",
        lambda **kw: login.LoginSession(
            login_url="http://x/login", poll_url="http://x/poll", expires_in=300),
    )
    monkeypatch.setattr(
        login, "poll_login",
        lambda session, on_tick=None: AccountCookies(
            pass_token="p", user_id="user-new", c_user_id="c"),
    )
    login.login_interactive(open_browser=False)

    assert sso.load_cached_token(sso.SSO_SID, user_id="user-old") is None, \
        "换号后旧票必须作废"
