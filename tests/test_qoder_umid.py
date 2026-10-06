"""Qoder 活动面机器指纹（umid）单元测试。

背景（2026-10-07 实测）：全球区 ``/sash/**`` 活动面按机器指纹**定向发放**
签到活动——只带静态 ``Cosy-MachineId`` 时，返回的活动列表里没有当天的
CLAIM_BENEFIT 条目（桌面端同一账号却能看到）。带上桌面端 ``runtime-info``
生成的 ``Cosy-MachineToken/Code/Type`` 后条目立刻出现。

这里锁的是我们复刻的指纹链路：头构造、二进制调用、缓存与降级。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from buddy_proxy.qoder import umid
from buddy_proxy.qoder.campaigns import CampaignClient, campaign_headers
from buddy_proxy.qoder.credentials import Credential
from buddy_proxy.qoder.umid import MachineIdentity, clear_cache, machine_identity

IDENT = MachineIdentity(token="tok", code="code", type="typ")


@pytest.fixture(autouse=True)
def _clean_umid_cache():
    clear_cache()
    yield
    clear_cache()


def _cred() -> Credential:
    return Credential(token="dt-abc", uid="u1", machine_id="m1", region="global")


# --- 请求头 ----------------------------------------------------------------


def test_campaign_headers_without_identity_keep_static_shape():
    """无指纹时保持旧行为：MachineToken=静态 machine_id，无 MachineCode。"""
    h = campaign_headers(_cred())
    assert h["Authorization"] == "Bearer dt-abc"
    assert h["Cosy-MachineId"] == h["Cosy-MachineToken"] == "m1"
    assert h["Cosy-MachineType"] == "10"
    assert "Cosy-MachineCode" not in h
    assert "Cosy-MachineOS" not in h


def test_campaign_headers_with_identity_use_attestation():
    """有指纹时换上真实三件套 + 客户端标识；静态 machine_id 只留 MachineId。"""
    h = campaign_headers(_cred(), IDENT)
    assert h["Cosy-MachineId"] == "m1"
    assert h["Cosy-MachineToken"] == "tok"
    assert h["Cosy-MachineCode"] == "code"
    assert h["Cosy-MachineType"] == "typ"
    assert h["Cosy-MachineOS"]
    assert h["Cosy-MachineHostname"]


def test_campaign_client_carries_identity():
    from buddy_proxy.qoder.config import REGIONS

    client = CampaignClient(REGIONS["global"], _cred(), IDENT)
    assert client.identity is IDENT
    assert CampaignClient(REGIONS["cn"], _cred()).identity is None


# --- 指纹生成 --------------------------------------------------------------


def _patch_runtime(monkeypatch, binary="/fake/runtime-info", payload=None, exc=None):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))
        if exc is not None:
            raise exc
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload or {
                "machineToken": "tok", "machineCode": "code", "machineType": "typ",
                "vmInfo": {"isVm": False},
            }) + "\n",
            stderr="",
        )

    monkeypatch.setattr(umid, "_find_binary", lambda region: binary)
    monkeypatch.setattr(umid.subprocess, "run", fake_run)
    return calls


def test_machine_identity_parses_runtime_info(monkeypatch):
    calls = _patch_runtime(monkeypatch)
    ident = machine_identity("global", "u1")
    assert ident == IDENT
    assert len(calls) == 1
    assert "--account-stdin" in calls[0][0]
    assert json.loads(calls[0][1]) == {"account": "u1"}


def test_machine_identity_caches_per_account(monkeypatch):
    calls = _patch_runtime(monkeypatch)
    assert machine_identity("global", "u1") is machine_identity("global", "u1")
    assert len(calls) == 1, "同账号 1h 内应命中缓存"
    machine_identity("global", "u2")
    assert len(calls) == 2
    machine_identity("cn", "u1")
    assert len(calls) == 3


def test_machine_identity_returns_none_without_binary(monkeypatch):
    monkeypatch.setattr(umid, "_find_binary", lambda region: None)

    def _boom(*a, **k):
        raise AssertionError("不该起子进程")

    monkeypatch.setattr(umid.subprocess, "run", _boom)
    assert machine_identity("global", "u1") is None


def test_machine_identity_degrades_on_failure(monkeypatch):
    _patch_runtime(monkeypatch, exc=RuntimeError("exit=1 boom"))
    assert machine_identity("global", "u1") is None
    # 失败走短负缓存：立刻再查不再起子进程
    assert umid._cache[("global", "u1")][1] is None


def test_machine_identity_rejects_incomplete_output(monkeypatch):
    _patch_runtime(monkeypatch, payload={"machineToken": "tok"})
    assert machine_identity("global", "u1") is None


def test_binary_candidates_env_override(monkeypatch):
    monkeypatch.setenv("QODER_UMID_BIN", "/custom/bin")
    assert umid._binary_candidates("global")[0] == "/custom/bin"
    monkeypatch.delenv("QODER_UMID_BIN")
    assert all("Qoder" in p for p in umid._binary_candidates("global"))


def test_binary_candidates_cover_user_level_applications():
    """用户级安装（~/Applications）也要覆盖：前缀替换，不能拼出 Applications/Applications。"""
    cands = umid._binary_candidates("global")
    assert "/Applications/Qoder.app/Contents/Resources/umid/runtime-info" in cands
    home_app = str(
        umid.Path.home() / "Applications/Qoder.app/Contents/Resources/umid/runtime-info"
    )
    assert home_app in cands
    assert not any("Applications/Applications" in p for p in cands)


def test_machine_identity_logs_when_binary_missing(monkeypatch, caplog):
    """二进制缺失要有 debug 留痕（回退静态指纹后海外区新号会误报无可领，排查靠它）。"""
    monkeypatch.setattr(umid, "_find_binary", lambda region: None)
    import logging

    # 全量跑时别的测试会调 setup_logging（把 buddy_proxy 的 propagate 关掉），
    # 记录走到 buddy_proxy 那层就停了，caplog 的 handler 在 root 上收不到——
    # 单跑不复现。恢复的是 buddy_proxy 那层的传播，改 umid 自己没用。
    monkeypatch.setattr(logging.getLogger("buddy_proxy"), "propagate", True)
    with caplog.at_level(logging.DEBUG, logger="buddy_proxy.qoder.umid"):
        assert machine_identity("global", "u1") is None
    assert any("回退静态指纹" in r.message for r in caplog.records)


# --- provider 集成 ---------------------------------------------------------


def test_provider_campaigns_client_gets_identity(monkeypatch):
    from buddy_proxy.qoder import provider as qoder_provider
    from buddy_proxy.qoder.provider import QoderProvider

    acct = SimpleNamespace(id="a1", region="cn", priority=0)
    cred_dict = {"token": "dt-x", "uid": "uid-x", "machine_id": "m1", "region": "cn"}
    sentinel = MachineIdentity(token="t", code="c", type="y")
    monkeypatch.setattr(qoder_provider.failover, "available_accounts", lambda *_: [acct])
    monkeypatch.setattr(qoder_provider, "ensure_account_token", lambda _aid: (None, cred_dict))
    monkeypatch.setattr(qoder_provider, "machine_identity", lambda region, account: sentinel)

    client = __import__("asyncio").run(QoderProvider()._campaigns(acct))
    assert isinstance(client, CampaignClient)
    assert client.identity is sentinel


def test_machine_identity_never_raises(monkeypatch):
    """指纹链路自身必须吞掉一切异常——provider 不该因指纹挂掉而丢签到。"""
    _patch_runtime(monkeypatch, exc=KeyboardInterrupt("simulated"))
    assert machine_identity("global", "u1") is None
