"""签到枚举不剔除冷却账号：冷却挡的是模型转发，不挡签到 billing 面。

2026-10-07 用户实报：codebuddy 双号、1 号在 quota 冷却里，签到卡只剩
一个账号、「每日 +100」只算一半——``checkin_status``/``checkin_claim``
沿用 ``available_accounts()``（剔除冷却）把在冷却的账号整条藏掉了。
trae/qoder 同病同修：三家签到枚举改为全量（同区过滤保留）。额度遍历
（quota/forward）的冷却过滤**不动**。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from buddy_proxy.codebuddy_provider import credentials as cb_creds
from buddy_proxy.codebuddy_provider import failover as cb_failover
from buddy_proxy.codebuddy_provider.provider import CodeBuddyProvider
from buddy_proxy.trae import failover as trae_failover
from buddy_proxy.trae.credentials import save_account_cred as trae_save
from buddy_proxy.trae.provider import TraeProvider

# --- codebuddy ---------------------------------------------------------------

from test_codebuddy_checkin_daily_credit import _cred as _cb_cred
from test_codebuddy_checkin_daily_credit import _payload as _cb_payload


def _cb_post(account_ids: list[str]):
    """记录被查询的账号，逐一回「未签到、今日 100」。"""
    calls = []

    def fake_post(account_id, path, *args, **kwargs):
        calls.append(account_id)
        return _cb_payload(False)

    return fake_post, calls


def test_codebuddy_status_includes_cooling_account(monkeypatch):
    a = cb_creds.save_account_cred(_cb_cred("u1"))
    b = cb_creds.save_account_cred(_cb_cred("u2"))
    post, calls = _cb_post([a.id, b.id])
    monkeypatch.setattr(cb_creds, "api_post_as", post)
    # 1 号打上 quota 冷却（_cooldowns 是 _tracker 内部 dict 的活引用）
    cb_failover._cooldowns.clear()
    cb_failover._cooldowns[a.id] = (time.time() + 600, "quota")
    try:
        st = CodeBuddyProvider().checkin_status()
        # 对照：额度/转发用的 available_accounts 仍剔除冷却——两套口径分开
        assert [x.id for x in cb_failover.available_accounts()] == [b.id]
    finally:
        cb_failover._cooldowns.clear()
    # 冷却中的账号仍被查询/展示，「每日」是两号总和
    assert calls == [a.id, b.id]
    assert len(st["accounts"]) == 2
    assert st["daily_credit"] == 200


# --- trae --------------------------------------------------------------------


def _trae_cred(uid: str, nickname: str) -> dict:
    return {
        "uid": uid, "nickname": nickname,
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


def test_trae_status_includes_cooling_account(monkeypatch):
    trae_save(_trae_cred("u1", "U1"))
    trae_save(_trae_cred("u2", "U2"))
    trae_failover._cooldowns.clear()
    trae_failover._cooldowns["u1"] = (time.time() + 600, "quota")
    try:
        monkeypatch.setattr(
            "buddy_proxy.trae.provider.fetch_checkin_status",
            lambda token="", account_id="", region="":
                {"checked_in": True, "enable": True, "message": "", "credits": 100})
        st = TraeProvider().checkin_status()
    finally:
        trae_failover._cooldowns.clear()
    assert len(st["accounts"]) == 2
    assert st["daily_credit"] == 200


# --- qoder -------------------------------------------------------------------


def test_qoder_status_includes_cooling_account(monkeypatch):
    from test_qoder_campaigns import LIVE_LISTING, _FakeClient
    from buddy_proxy.qoder import provider as qoder_provider
    from buddy_proxy.qoder.provider import QoderProvider

    first = SimpleNamespace(id="a1", priority=0, alias="", name="First",
                            email="first@test", region="cn")
    second = SimpleNamespace(id="a2", priority=1, alias="Second", name="Second",
                             email="second@test", region="cn")
    monkeypatch.setattr(qoder_provider, "list_accounts", lambda: [first, second])
    # 模拟「全部账号都在冷却」：available_accounts 返回空——签到不该跟着空
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [])
    clients = {"a1": _FakeClient(LIVE_LISTING), "a2": _FakeClient(LIVE_LISTING)}

    async def client_for(self, account=None):
        return clients[account.id]

    monkeypatch.setattr(QoderProvider, "_campaigns", client_for)
    st = asyncio.run(QoderProvider().checkin_status())
    assert len(st["accounts"]) == 2
    assert st["daily_credit"] == 200
