"""qoder 多账号 failover 测试：冷却、可用账号筛选、账号状态面板。

转发编排（forward 的 failover 循环）与流式闸门在 test_qoder_provider.py；
这里聚焦 failover 模块自身的语义。
"""

from __future__ import annotations

import time

import pytest

from buddy_proxy.qoder import failover
from buddy_proxy.qoder.credentials import save_account_cred
from buddy_proxy.qoder.failover import (
    accounts_status,
    available_accounts,
    clear_cooldown,
    cooldown_left,
    cooldown_report,
    mark_cooldown,
)


def _cred(account_id: str, region: str = "cn", **over) -> dict:
    cred = {
        "account_id": account_id,
        "token": f"dt-{account_id}-real-token",
        "uid": f"uid-{account_id}",
        "machine_id": "m1",
        "refresh_token": f"rt-{account_id}",
        "expires_at_ms": int(time.time() * 1000) + 3600_000,
        "name": "",
        "email": f"{account_id}@qoder.example.com",
        "region": region,
        "plan": "",
        "source": "state",
    }
    cred.update(over)
    return cred


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


# ---------------------------------------------------------------------------
# 冷却基础
# ---------------------------------------------------------------------------

def test_mark_and_clear_cooldown():
    mark_cooldown("a", reason="测试")
    left, kind = cooldown_left("a")
    assert left > 0 and kind == "account"
    clear_cooldown("a")
    assert cooldown_left("a") == (0.0, "")


def test_quota_cooldown_longer_than_account():
    mark_cooldown("a")
    mark_cooldown("b", quota=True)
    a_left, _ = cooldown_left("a")
    b_left, b_kind = cooldown_left("b")
    assert b_kind == "quota"
    assert b_left > a_left


def test_retry_after_overrides_default():
    mark_cooldown("a", retry_after="10")
    left, _ = cooldown_left("a")
    assert 5 < left <= 12  # 钳到合理区间（10s + 调度抖动）


def test_expired_cooldown_reads_zero():
    mark_cooldown("a")
    failover._cooldowns["a"] = (time.time() - 1, "account")
    assert cooldown_left("a") == (0.0, "")


# ---------------------------------------------------------------------------
# available_accounts：顺位 + 区域过滤
# ---------------------------------------------------------------------------

def test_available_accounts_sorted_by_priority():
    save_account_cred(_cred("a"))
    save_account_cred(_cred("b"))
    save_account_cred(_cred("c"))
    mark_cooldown("b", reason="测试")
    got = [a.id for a in available_accounts()]
    assert got == ["a", "c"], "冷却账号被剔除，余者按 priority 排序"


def test_available_accounts_filters_by_region():
    """跨区账号不参与 failover（CN / 全球版账号不通用）。"""
    save_account_cred(_cred("cn-a", region="cn"))
    save_account_cred(_cred("gl-a", region="global"))
    save_account_cred(_cred("cn-b", region="cn"))
    got = [a.id for a in available_accounts("cn")]
    assert got == ["cn-a", "cn-b"]
    got_gl = [a.id for a in available_accounts("global")]
    assert got_gl == ["gl-a"]


def test_available_accounts_no_region_returns_all():
    save_account_cred(_cred("cn-a", region="cn"))
    save_account_cred(_cred("gl-a", region="global"))
    got = [a.id for a in available_accounts()]
    assert got == ["cn-a", "gl-a"]


def test_cooldown_report_mentions_only_cooling_accounts():
    save_account_cred(_cred("a"))
    save_account_cred(_cred("b"))
    mark_cooldown("b", quota=True, reason="测试")
    report = cooldown_report()
    assert "b" in report and "额度冷却" in report
    assert "a" not in report


# ---------------------------------------------------------------------------
# accounts_status（UI 面板数据）
# ---------------------------------------------------------------------------

def test_accounts_status_shape():
    save_account_cred(_cred("a", email="me@qoder.com"))
    save_account_cred(_cred("b", expires_at_ms=0))  # 无 expires_at → hours_left None
    out = accounts_status()
    assert out["enabled"] is True
    assert [a["id"] for a in out["accounts"]] == ["a", "b"]
    a0 = out["accounts"][0]
    assert a0["index"] == 1
    assert a0["email"] == "me@qoder.com"
    assert a0["name"] == "me@qoder.com"  # name 空时回落 email
    assert a0["token"] == "ok"
    assert a0["hours_left"] is not None and a0["hours_left"] > 0
    assert a0["cooling"] == []
    a1 = out["accounts"][1]
    assert a1["hours_left"] is None
    # 冷却标记要出现在状态里
    mark_cooldown("a", quota=True, reason="测试")
    out = accounts_status()
    assert out["accounts"][0]["cooling"][0]["kind"] == "quota"


def test_accounts_status_region_field():
    save_account_cred(_cred("gl", region="global"))
    out = accounts_status()
    assert out["accounts"][0]["region"] == "global"


def test_accounts_status_empty():
    out = accounts_status()
    assert out["enabled"] is False and out["accounts"] == []
