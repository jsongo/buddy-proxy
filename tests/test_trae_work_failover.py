"""trae work failover 薄壳单测（CooldownTracker 公共模块的 trae 封装）。

覆盖：mark_cooldown / clear_cooldown / cooldown_left / available_accounts /
cooldown_report / accounts_status 字段口径（uid/nickname）。conftest 已隔离
TRAE_WORK_STATE_DIR / TRAE_WORK_CRED_PATH。
"""
from __future__ import annotations

import time

import pytest

from buddy_proxy.trae import failover
from buddy_proxy.trae.credentials import save_account_cred


def _cred(uid: str, nickname: str = "T") -> dict:
    return {
        "uid": uid, "nickname": nickname,
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    failover._cooldowns.clear()
    yield
    failover._cooldowns.clear()


def test_mark_and_left_account_kind():
    failover.mark_cooldown("a1")
    left, kind = failover.cooldown_left("a1")
    assert kind == "account"
    assert 0 < left <= failover._ACCOUNT_COOLDOWN_S


def test_mark_quota_kind():
    failover.mark_cooldown("a1", quota=True)
    _left, kind = failover.cooldown_left("a1")
    assert kind == "quota"


def test_retry_after_override():
    got = failover.mark_cooldown("a1", quota=True, retry_after="120")
    assert got == 120.0


def test_clear_cooldown():
    failover.mark_cooldown("a1")
    assert failover.cooldown_left("a1")[0] > 0
    failover.clear_cooldown("a1")
    assert failover.cooldown_left("a1") == (0.0, "")


def test_available_accounts_excludes_cooling():
    r1 = save_account_cred(_cred("u1", "Alice"))
    r2 = save_account_cred(_cred("u2", "Bob"))
    failover.mark_cooldown(r1.id)
    avail = failover.available_accounts()
    assert [a.id for a in avail] == [r2.id]


def test_available_accounts_all():
    r1 = save_account_cred(_cred("u1", "Alice"))
    r2 = save_account_cred(_cred("u2", "Bob"))
    avail = failover.available_accounts()
    assert [a.id for a in avail] == [r1.id, r2.id]


def test_cooldown_report_lists_only_cooling():
    save_account_cred(_cred("u1", "Alice"))
    save_account_cred(_cred("u2", "Bob"))
    failover.mark_cooldown("u1", quota=True)
    got = failover.cooldown_report()
    assert "u1" in got and "额度" in got
    assert "u2" not in got


def test_accounts_status_fields():
    save_account_cred(_cred("u1", "Alice"))
    st = failover.accounts_status()
    assert st["enabled"] is True
    accts = st["accounts"]
    assert len(accts) == 1
    a = accts[0]
    assert a["id"] == "u1"
    assert a["uid"] == "u1"
    assert a["nickname"] == "Alice"
    assert a["index"] == 1
    assert a["token"] == "ok"
    assert a["cooling"] == []
    assert a["hours_left"] is not None


def test_accounts_status_reflects_cooling():
    save_account_cred(_cred("u1", "Alice"))
    failover.mark_cooldown("u1", quota=True)
    a = failover.accounts_status()["accounts"][0]
    assert len(a["cooling"]) == 1
    assert a["cooling"][0]["kind"] == "quota"


def test_accounts_status_empty():
    st = failover.accounts_status()
    assert st["enabled"] is False
    assert st["accounts"] == []


def test_cooldowns_is_live_reference():
    # 薄壳：_cooldowns 是 tracker 内部 dict 的活引用，测试/外部可直接清
    assert failover._cooldowns is failover._tracker._cooldowns
    failover.mark_cooldown("a1")
    assert "a1" in failover._cooldowns
    failover._cooldowns.clear()
    assert failover.cooldown_left("a1") == (0.0, "")
