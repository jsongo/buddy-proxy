"""kimi OAuth Device Flow 单元测试（离线，stub urllib）。

覆盖：device_authorization 请求形状与必填校验、poll_token 状态机
（pending / slow_down / expired_token / access_denied / 成功）、refresh_token
的 form 形状与错误路径。轮询 sleep 打桩为零，测纯状态机。
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from buddy_proxy.kimi import oauth


class _FakeResp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def oauth_stub(monkeypatch):
    """stub urllib.request.urlopen：按序弹出 canned 响应，记录每个请求。"""
    calls: list = []
    queue: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        item = queue.pop(0) if queue else {}
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(oauth.time, "sleep", lambda s: None)  # 轮询不等真实时间
    return calls, queue


def _form(req) -> dict[str, str]:
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(req.data.decode()).items()}


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    """带可读 body 的 HTTPError（HTTPError 自身就是 response 对象，read() 走 fp）。"""
    return urllib.error.HTTPError(
        "https://auth.kimi.test/api/oauth/token", code, "err",
        hdrs=None, fp=io.BytesIO(body))


def test_device_authorization_request_shape(oauth_stub):
    calls, queue = oauth_stub
    queue.append({
        "user_code": "ABCD-1234",
        "device_code": "dev-xyz",
        "verification_uri": "https://auth.kimi.com/device",
        "verification_uri_complete": "https://auth.kimi.com/device?code=ABCD-1234",
        "expires_in": 600,
        "interval": 5,
    })

    flow = oauth.start_device_authorization("https://auth.kimi.com", device_id="dev-1")

    assert flow.user_code == "ABCD-1234" and flow.device_code == "dev-xyz"
    assert flow.verification_uri_complete.endswith("code=ABCD-1234")
    assert flow.interval == 5.0
    req = calls[0]
    assert req.full_url == "https://auth.kimi.com/api/oauth/device_authorization"
    form = _form(req)
    assert form["client_id"] == oauth.CLIENT_ID
    # urllib Request.headers 会把 key 规范化成 capitalize 形态（X-Msh-Device-Id → X-msh-device-id）
    sent = {k.lower(): v for k, v in req.headers.items()}
    assert sent["x-msh-device-id"] == "dev-1"
    assert "kimi-code-cli/" in sent["user-agent"]


def test_device_authorization_missing_fields_raise(oauth_stub):
    _calls, queue = oauth_stub
    queue.append({"user_code": "x"})  # 缺 device_code / verification_uri_complete

    with pytest.raises(oauth.OAuthError, match="缺字段"):
        oauth.start_device_authorization()


def test_poll_pending_then_success(oauth_stub):
    calls, queue = oauth_stub
    queue.extend([
        {"error": "authorization_pending"},
        {"error": "authorization_pending"},
        {"access_token": "at", "refresh_token": "rt", "expires_in": 900},
    ])
    waits: list[float] = []

    payload = oauth.poll_token(
        "https://auth.kimi.com", "dev-xyz", interval=5.0, on_wait=waits.append)

    assert payload["access_token"] == "at"
    assert len(calls) == 3
    form = _form(calls[0])
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert form["device_code"] == "dev-xyz" and form["client_id"] == oauth.CLIENT_ID
    assert waits == [5.0, 5.0]  # 每次等待前回调（CLI 打进度 tick）


def test_poll_slow_down_increases_interval(oauth_stub):
    calls, queue = oauth_stub
    queue.extend([
        {"error": "slow_down"},
        {"access_token": "at", "refresh_token": "rt", "expires_in": 900},
    ])
    waits: list[float] = []

    oauth.poll_token("https://auth.kimi.com", "d", interval=5.0, on_wait=waits.append)

    assert waits == [10.0]  # slow_down 后间隔 +5（RFC 8628）


def test_poll_expired_and_denied_terminal(oauth_stub):
    _calls, queue = oauth_stub

    queue.append({"error": "expired_token"})
    with pytest.raises(oauth.OAuthError, match="过期"):
        oauth.poll_token("https://auth.kimi.com", "d")

    queue.append({"error": "access_denied"})
    with pytest.raises(oauth.OAuthError, match="拒绝"):
        oauth.poll_token("https://auth.kimi.com", "d")


def test_poll_unknown_error_raises(oauth_stub):
    _calls, queue = oauth_stub
    queue.append({"error": "boom", "error_description": "服务器炸了"})

    with pytest.raises(oauth.OAuthError, match="服务器炸了"):
        oauth.poll_token("https://auth.kimi.com", "d")


def test_refresh_token_shape_and_error(oauth_stub):
    calls, queue = oauth_stub

    queue.append({"access_token": "at2", "refresh_token": "rt2", "expires_in": 900,
                  "token_type": "Bearer", "scope": "kimi-code"})
    payload = oauth.refresh_token("https://auth.kimi.com", "rt-old", device_id="dev-1")
    assert payload["access_token"] == "at2"
    form = _form(calls[0])
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-old"
    assert form["client_id"] == oauth.CLIENT_ID

    queue.append({"error": "invalid_grant"})
    with pytest.raises(oauth.OAuthError, match="invalid_grant"):
        oauth.refresh_token("https://auth.kimi.com", "rt-bad")


def test_poll_reads_semantic_400_body(oauth_stub):
    """真机 pending/slow_down 以 HTTP 400 + {"error":...} 表达——要读 body 分诊，
    不能当传输错误抛（否则 device flow 在第一个 pending 上就崩）。"""
    calls, queue = oauth_stub
    queue.extend([
        _http_error(400, json.dumps({"error": "authorization_pending"}).encode()),
        _http_error(400, json.dumps({"error": "slow_down"}).encode()),
        {"access_token": "at", "refresh_token": "rt", "expires_in": 900},
    ])

    payload = oauth.poll_token("https://auth.kimi.com", "d", interval=5.0)

    assert payload["access_token"] == "at"
    assert len(calls) == 3


def test_refresh_rejected_via_400_body(oauth_stub):
    """refresh_token 被拒（invalid_grant 也是 400）：报语义错误而非传输错误。"""
    _calls, queue = oauth_stub
    queue.append(_http_error(400, json.dumps({"error": "invalid_grant"}).encode()))

    with pytest.raises(oauth.OAuthError, match="invalid_grant"):
        oauth.refresh_token("https://auth.kimi.com", "rt-revoked")


def test_400_non_json_and_non_400_still_transport_error(oauth_stub):
    """400 但 body 不是 JSON、以及其它状态码：照旧按传输错误抛。"""
    _calls, queue = oauth_stub
    queue.append(_http_error(400, b"<html>gateway</html>"))
    with pytest.raises(oauth.OAuthError, match="请求失败"):
        oauth.refresh_token("https://auth.kimi.com", "rt")

    queue.append(_http_error(500, b"boom"))
    with pytest.raises(oauth.OAuthError, match="请求失败"):
        oauth.refresh_token("https://auth.kimi.com", "rt")
