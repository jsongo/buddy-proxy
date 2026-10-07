"""qoder 账号身份回填（``/api/v1/userinfo``）。

deviceToken 各端点回包**没有** name/email（实测 2026-10-08），登录链路一直
拿不到账号身份——config 里早定义的 userinfo 端点从没人调用，海外区账号
在签到明细/额度面板一律显示裸 UUID（用户实报）。这里锁三件事：

1. ``fetch_userinfo`` 解析 name/email/username；
2. ``backfill_identity`` 只在无名时回填、写索引+cred、失败不抛；
3. 登录 CLI 与签到/额度路径的懒回填接线。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from buddy_proxy.qoder import credentials as qc
from buddy_proxy.qoder import provider as qoder_provider
from buddy_proxy.qoder.provider import QoderProvider


def _cred(aid: str, **over) -> dict:
    return {"account_id": aid, "token": f"dt-{aid}", "uid": aid,
            "refresh_token": f"rt-{aid}", "expires_at_ms": 4_102_444_800_000,
            "region": "cn", "name": "", "email": "", **over}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    qc.list_accounts.cache_clear() if hasattr(qc.list_accounts, "cache_clear") else None
    return None


def test_fetch_userinfo_parses_identity(tmp_path, monkeypatch):
    from buddy_proxy.qoder.credentials import fetch_userinfo

    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    qc.save_account_cred(_cred("a1"))
    monkeypatch.setattr(qc, "ensure_account_token", lambda aid: ("tok", {}))
    class _Resp:
        status_code = 200
        def json(self):
            return {"name": "ethan", "email": "e@x.com", "username": "u1"}
    monkeypatch.setattr(qc.httpx, "get", lambda *a, **k: _Resp())
    info = fetch_userinfo("a1")
    assert info == {"name": "ethan", "email": "e@x.com", "username": "u1"}


def test_fetch_userinfo_uses_account_region(tmp_path, monkeypatch):
    """端点必须按**账号自己的 region** 解析（#132 真机回归）。

    resolve_region() 不带参落默认区 CN，海外号 token 打 CN 域 401
    TOKEN_EXPIRE——token 有效、域不对，回填对所有 global 账号静默失效。
    """
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    qc.save_account_cred(_cred("g1", region="global"))
    seen = {}
    real_resolve = qc.resolve_region

    def spy_resolve(key=None):
        seen["key"] = key
        return real_resolve(key)

    monkeypatch.setattr(qc, "resolve_region", spy_resolve)
    monkeypatch.setattr(qc, "ensure_account_token", lambda aid: ("tok", {}))

    class _Resp:
        status_code = 200
        def json(self):
            return {"name": "n", "email": "e", "username": "u"}

    monkeypatch.setattr(qc.httpx, "get", lambda *a, **k: _Resp())
    qc.backfill_identity("g1")
    assert seen["key"] == "global", "userinfo 端点必须用账号的 region，不能落默认区"


def test_fetch_userinfo_raises_on_http_error(monkeypatch):
    from buddy_proxy.qoder.credentials import AuthError, fetch_userinfo

    monkeypatch.setattr(qc, "ensure_account_token", lambda aid: ("tok", {}))
    class _Resp:
        status_code = 500
        text = "boom"
    monkeypatch.setattr(qc.httpx, "get", lambda *a, **k: _Resp())
    with pytest.raises(AuthError):
        fetch_userinfo("a1")


def test_backfill_writes_index_and_cred(tmp_path, monkeypatch):
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    ref = qc.save_account_cred(_cred("a1"))
    # index 里 name/email 全空（deviceToken 登录后的真实形态）
    assert not ref.name and not ref.email
    monkeypatch.setattr(qc, "fetch_userinfo", lambda aid:
                        {"name": "ethan", "email": "e@x.com", "username": "u1"})
    info = qc.backfill_identity("a1")
    assert info and info["name"] == "ethan"
    a = next(a for a in qc.list_accounts() if a.id == "a1")
    assert a.name == "ethan" and a.email == "e@x.com"
    # cred 文件也带上了（下次 derive/rename 链路可见）
    assert qc.load_account_cred("a1")["email"] == "e@x.com"
    # alias 槽不动：用户改名不受回填影响
    assert a.alias == ""


def test_backfill_skips_when_named_or_already_filled(tmp_path, monkeypatch):
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    qc.save_account_cred(_cred("a1", name="已有名"))
    calls = []
    monkeypatch.setattr(qc, "fetch_userinfo",
                        lambda aid: calls.append(aid) or {"name": "x", "email": "", "username": ""})
    assert qc.backfill_identity("a1") is None
    assert calls == [], "已有名字的账号不打上游"
    # only_if_missing=False（登录路径）强制刷新
    assert qc.backfill_identity("a1", only_if_missing=False) is not None
    assert calls == ["a1"]


def test_backfill_swallows_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    qc.save_account_cred(_cred("a1"))

    def boom(aid):
        from buddy_proxy.qoder.credentials import AuthError
        raise AuthError("network down")

    monkeypatch.setattr(qc, "fetch_userinfo", boom)
    assert qc.backfill_identity("a1") is None, "身份回填失败不能拖垮调用方"


def test_checkin_status_backfills_nameless_accounts(tmp_path, monkeypatch):
    """签到枚举遇到无名账号时触发回填并重读账号列表。"""
    monkeypatch.setenv("QODER_STATE_DIR", str(tmp_path / "qoder"))
    from buddy_proxy.qoder.campaigns import Campaign

    first = SimpleNamespace(id="a1", priority=0, alias="", name="", email="", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="", name="", email="", region="cn")
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])

    filled = []

    def fake_backfill(aid, only_if_missing=True):
        filled.append(aid)
        # 模拟回填后 index 更新：账号获得名字
        if aid == "a1":
            first.name = "一号"
        else:
            second.name = "二号"
        return {"name": first.name if aid == "a1" else second.name,
                "email": "", "username": ""}

    monkeypatch.setattr(qoder_provider, "backfill_identity", fake_backfill)

    class _Client:
        async def list(self):
            return []
    async def client_for(self, account=None):
        return _Client()
    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)

    import asyncio
    st = asyncio.run(QoderProvider().checkin_status())
    assert filled == ["a1", "a2"]
    rows = st.get("accounts") or []
    assert [r["name"] for r in rows] == ["一号", "二号"], "本轮就该显示回填后的名字"


def test_quota_one_backfills_nameless(monkeypatch):
    """额度查询遇到无名账号触发回填（面板组标题从 UUID 变真名）。"""
    import asyncio

    acct = SimpleNamespace(id="a1", priority=0, alias="", name="", email="", region="cn")
    calls = []
    monkeypatch.setattr(qoder_provider, "backfill_identity",
                        lambda aid, **k: calls.append(aid) or None)
    monkeypatch.setattr(qoder_provider, "ensure_account_token",
                        lambda aid: ("tok", {}))
    # HTTP 层直接 stub：额度回包结构不必真实
    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"userQuota": {"total": 100, "used": 10, "remaining": 90},
                    "addOnQuota": {}, "resetAt": 0}
    monkeypatch.setattr(qoder_provider.httpx, "get", lambda *a, **k: _Resp())
    p = QoderProvider()
    items, ok = p._quota_one(acct, 1, multi=True)
    assert ok and calls == ["a1"]
    assert items and items[0]["label"].startswith("Qoder #1")


def test_login_flow_calls_backfill(tmp_path, monkeypatch):
    """登录 CLI 落盘后必须调一次回填（only_if_missing=False）。"""
    src = (pytest.importorskip("pathlib").Path(__file__).resolve().parents[1]
           / "src" / "buddy_proxy" / "auth" / "login.py").read_text(encoding="utf-8")
    assert "backfill_identity(ref.id, only_if_missing=False)" in src


def test_fetch_userinfo_raises_on_missing_region(tmp_path, monkeypatch):
    """region 为空显式抛错，不静默落默认区（否则又是一种查不到的 401）。

    save_account_cred 落盘时空 region 会被归一成 "cn"，真实存量到不了这个
    分支（只有 cred 文件被手工改坏才可能）——直接桩 load_account_cred。
    """
    monkeypatch.setattr(qc, "load_account_cred", lambda aid: {"account_id": "b1", "region": ""})
    from buddy_proxy.qoder.credentials import AuthError

    with pytest.raises(AuthError, match="没有 region 字段"):
        qc.fetch_userinfo("b1")
