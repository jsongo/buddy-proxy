"""多账号本地别名（alias）测试：四通道 rename_account + UI rename 端点。

conftest autouse 把四通道 STATE_DIR 隔离进 tmp_path，这里直接写盘。
覆盖四条不变量：改名生效 / 未知账号拒绝 / 空串清除 / **重登 upsert 与索引
自愈都不丢 alias**——alias 是唯一会被 list_accounts 自愈重写吞掉的字段
（其余字段每次都从 entry 重建，alias 忘带就静默归零）。
"""

from __future__ import annotations

import asyncio
import json
import time
import types

import pytest


# ---------------------------------------------------------------------------
# credentials 层 ×4
# ---------------------------------------------------------------------------

def test_kimi_rename_roundtrip_and_survives_relogin_and_self_heal():
    from buddy_proxy.kimi import credentials as creds

    creds.save_account_cred(_kimi_cred("u1", nickname="登月者5782"))
    refs = creds.rename_account("u1", "主力号")
    assert [r.alias for r in refs] == ["主力号"]

    # 重登 upsert（同 RT、nickname 已换）：name 跟 cred 走，alias 不被覆盖
    creds.save_account_cred(_kimi_cred("u1", nickname="新昵称", refresh_token="rt-u1b"))
    refs = creds.list_accounts()
    assert refs[0].name == "新昵称"
    assert refs[0].alias == "主力号"

    # 自愈重写保全量 alias：删 u2 的 cred 触发整表重写，留下的 u1 alias 必须活着
    #（自愈的 kept.append(AccountRef(...)) 是唯一从 entry 重建字段的点，alias 忘带就归零）
    creds.save_account_cred(_kimi_cred("u2"))
    creds.account_cred_path("u2").unlink()
    refs = creds.list_accounts()
    assert [r.id for r in refs] == ["u1"]
    assert refs[0].alias == "主力号"

    # 空串清除 + 未知账号拒绝
    assert creds.rename_account("u1", "")[0].alias == ""
    with pytest.raises(ValueError, match="不存在"):
        creds.rename_account("ghost", "x")


def test_qoder_rename_roundtrip_and_survives_relogin_and_self_heal():
    from buddy_proxy.qoder import credentials as creds

    creds.save_account_cred(_qoder_cred("q1"))
    assert creds.rename_account("q1", "工作号")[0].alias == "工作号"

    # 重登（upsert 命中）：name/email 更新，alias 不动
    creds.save_account_cred(_qoder_cred("q1", name="Qoder官方名"))
    refs = creds.list_accounts()
    assert refs[0].name == "Qoder官方名"
    assert refs[0].alias == "工作号"

    # 自愈重写保全量 alias（同 kimi：删别的账号触发整表重写）
    creds.save_account_cred(_qoder_cred("q2"))
    creds.account_cred_path("q2").unlink()
    refs = creds.list_accounts()
    assert [r.id for r in refs] == ["q1"]
    assert refs[0].alias == "工作号"

    assert creds.rename_account("q1", "  ")[0].alias == ""  # 空白清除
    with pytest.raises(ValueError, match="不存在"):
        creds.rename_account("ghost", "x")


def test_antigravity_rename_roundtrip_and_survives_relogin_and_self_heal():
    from buddy_proxy.antigravity import credentials as creds

    creds.save_account_cred(_ag_cred("a@x.com"))
    assert creds.rename_account("a@x.com", "Google主号")[0].alias == "Google主号"

    # 重登（upsert 命中）：email 更新，alias 不动
    creds.save_account_cred(_ag_cred("a+new@x.com", refresh_token="rt-a@x.com"))
    refs = creds.list_accounts()
    assert refs[0].email == "a+new@x.com"
    assert refs[0].alias == "Google主号"

    # 自愈重写保全量 alias
    creds.save_account_cred(_ag_cred("b@y.com"))
    creds.account_cred_path("b@y.com").unlink()
    refs = creds.list_accounts()
    assert [r.id for r in refs] == ["a@x.com"]
    assert refs[0].alias == "Google主号"

    assert creds.rename_account("a@x.com", "")[0].alias == ""
    with pytest.raises(ValueError, match="不存在"):
        creds.rename_account("ghost", "x")


def test_trae_rename_roundtrip_and_survives_relogin_and_self_heal():
    from buddy_proxy.trae import credentials as creds

    creds.save_account_cred(_trae_cred("3001"))
    assert creds.rename_account("3001", "打包号")[0].alias == "打包号"

    # 重登（upsert 命中）：nickname 更新，alias 不动
    creds.save_account_cred(_trae_cred("3001", nickname="ethan"))
    refs = creds.list_accounts()
    assert refs[0].nickname == "ethan"
    assert refs[0].alias == "打包号"

    # 自愈重写保全量 alias
    creds.save_account_cred(_trae_cred("3002"))
    creds.account_cred_path("3002").unlink()
    refs = creds.list_accounts()
    assert [r.id for r in refs] == ["3001"]
    assert refs[0].alias == "打包号"

    assert creds.rename_account("3001", "")[0].alias == ""
    with pytest.raises(ValueError, match="不存在"):
        creds.rename_account("ghost", "x")


# ---------------------------------------------------------------------------
# accounts_status 透出（alias + 显示名 alias 优先）
# ---------------------------------------------------------------------------

def test_accounts_status_prefers_alias():
    from buddy_proxy.antigravity import failover as ag_failover
    from buddy_proxy.kimi import failover as kimi_failover
    from buddy_proxy.qoder import failover as qoder_failover
    from buddy_proxy.trae import failover as trae_failover

    from buddy_proxy.antigravity import credentials as ag_creds
    from buddy_proxy.kimi import credentials as kimi_creds
    from buddy_proxy.qoder import credentials as qoder_creds
    from buddy_proxy.trae import credentials as trae_creds

    ag_creds.save_account_cred(_ag_cred("a@x.com"))
    kimi_creds.save_account_cred(_kimi_cred("u1"))
    qoder_creds.save_account_cred(_qoder_cred("q1"))
    trae_creds.save_account_cred(_trae_cred("3001"))
    ag_creds.rename_account("a@x.com", "AG别名")
    kimi_creds.rename_account("u1", "Kimi别名")
    qoder_creds.rename_account("q1", "Qoder别名")
    trae_creds.rename_account("3001", "Trae别名")

    ag = ag_failover.accounts_status()["accounts"][0]
    assert ag["alias"] == "AG别名" and ag["email"] == "AG别名"
    kimi = kimi_failover.accounts_status()["accounts"][0]
    assert kimi["alias"] == "Kimi别名" and kimi["name"] == "Kimi别名"
    qoder = qoder_failover.accounts_status()["accounts"][0]
    assert qoder["alias"] == "Qoder别名" and qoder["name"] == "Qoder别名"
    trae = trae_failover.accounts_status()["accounts"][0]
    assert trae["alias"] == "Trae别名" and trae["nickname"] == "Trae别名"


# ---------------------------------------------------------------------------
# UI 端点 ×4（stub request 照 order/delete 端点测试同款）
# ---------------------------------------------------------------------------

def _rename_request(aid, alias):
    class _Req:
        client = types.SimpleNamespace(host="127.0.0.1")

        async def json(self):
            return {"id": aid, "alias": alias}

    return _Req()


def test_qoder_rename_endpoint(tmp_path):
    from fastapi import HTTPException

    from buddy_proxy.qoder import credentials as creds
    from buddy_proxy.web.ui import channels as web_ui

    creds.save_account_cred(_qoder_cred("q1"))
    out = asyncio.run(web_ui.ui_qoder_accounts_rename(_rename_request("q1", " renamed ")))
    assert out["accounts"][0]["alias"] == "renamed"  # 端点层不 strip，落库前 strip

    with pytest.raises(HTTPException) as ei:  # 未知账号 → 404
        asyncio.run(web_ui.ui_qoder_accounts_rename(_rename_request("ghost", "x")))
    assert ei.value.status_code == 404
    for bad in ({"alias": "x"}, {"id": "q1", "alias": 1}):  # 缺 id / alias 非串 → 400
        with pytest.raises(HTTPException) as ei:
            class _Req2:
                client = types.SimpleNamespace(host="127.0.0.1")

                async def json(self):
                    return bad

            asyncio.run(web_ui.ui_qoder_accounts_rename(_Req2()))
        assert ei.value.status_code == 400


def test_antigravity_rename_endpoint():
    from fastapi import HTTPException

    from buddy_proxy.antigravity import credentials as creds
    from buddy_proxy.web.ui import channels as web_ui

    creds.save_account_cred(_ag_cred("a@x.com"))
    out = asyncio.run(web_ui.ui_antigravity_accounts_rename(_rename_request("a@x.com", "主号")))
    assert out["accounts"][0]["alias"] == "主号"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(web_ui.ui_antigravity_accounts_rename(_rename_request("ghost", "x")))
    assert ei.value.status_code == 404


def test_trae_rename_endpoint():
    from fastapi import HTTPException

    from buddy_proxy.trae import credentials as creds
    from buddy_proxy.web.ui import channels as web_ui

    creds.save_account_cred(_trae_cred("3001"))
    out = asyncio.run(web_ui.ui_trae_accounts_rename(_rename_request("3001", "签到号")))
    assert out["accounts"][0]["alias"] == "签到号"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(web_ui.ui_trae_accounts_rename(_rename_request("ghost", "x")))
    assert ei.value.status_code == 404


def test_kimi_rename_endpoint():
    from fastapi import HTTPException

    from buddy_proxy.kimi import credentials as creds
    from buddy_proxy.web.ui import channels as web_ui

    creds.save_account_cred(_kimi_cred("u1"))
    out = asyncio.run(web_ui.ui_kimi_accounts_rename(_rename_request("u1", "导入号")))
    assert out["accounts"][0]["alias"] == "导入号"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(web_ui.ui_kimi_accounts_rename(_rename_request("ghost", "x")))
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------------
# fixture cred 构造（每账号唯一 RT——upsert 按 RT 撞号的坑见 test_trae_checkin_per_account）
# ---------------------------------------------------------------------------

def _kimi_cred(acct_id: str, **over) -> dict:
    cred = {
        "account_id": acct_id,
        "type": "kimi",
        "access_token": f"at-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "base_url": "https://api.kimi.com/coding",
        "oauth_host": "https://auth.kimi.com",
        "device_id": f"dev-{acct_id}",
    }
    cred.update(over)
    return cred


def _qoder_cred(acct_id: str, **over) -> dict:
    cred = {
        "account_id": acct_id,
        "token": f"dt-{acct_id}",
        "uid": f"uid-{acct_id}",
        "machine_id": "m1",
        "refresh_token": f"rt-{acct_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "name": "",
        "email": f"{acct_id}@qoder.example.com",
        "region": "cn",
        "plan": "",
        "source": "state",
    }
    cred.update(over)
    return cred


def _ag_cred(email: str, **over) -> dict:
    cred = {
        "access_token": f"tok-{email}",
        "refresh_token": f"rt-{email}",
        "expiry": "2099-01-01T00:00:00+00:00",
        "email": email,
        "project_id": "p1",
    }
    cred.update(over)
    return cred


def _trae_cred(uid: str, nickname: str = "Test", **over) -> dict:
    cred = {
        "uid": uid,
        "nickname": nickname,
        "access_token": f"at-{uid}",
        "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }
    cred.update(over)
    return cred
