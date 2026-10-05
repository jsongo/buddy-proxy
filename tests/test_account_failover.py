"""公共冷却状态机 CooldownTracker 单测。

antigravity / kimi / qoder / trae 的 failover.py 都收敛到这一个模块。这里直接
测公共行为（档位、Retry-After 钳制、聚合视图），不依赖任何通道的 AccountRef。
"""
from __future__ import annotations

import time

import pytest

from buddy_proxy.core.account_failover import (
    CooldownTracker,
    DEFAULT_ACCOUNT_COOLDOWN_S,
    DEFAULT_QUOTA_COOLDOWN_S,
    RETRY_AFTER_MAX_S,
    RETRY_AFTER_MIN_S,
)


@pytest.fixture()
def tracker():
    return CooldownTracker()


# -- mark / left 基本行为 ----------------------------------------------------


def test_account_default_cooldown(tracker):
    got = tracker.mark("a")
    assert got == DEFAULT_ACCOUNT_COOLDOWN_S
    left, kind = tracker.left("a")
    assert kind == "account"
    assert DEFAULT_ACCOUNT_COOLDOWN_S - 1 < left <= DEFAULT_ACCOUNT_COOLDOWN_S


def test_quota_kind_uses_quota_duration(tracker):
    got = tracker.mark("a", kind="quota")
    assert got == DEFAULT_QUOTA_COOLDOWN_S
    _left, kind = tracker.left("a")
    assert kind == "quota"


def test_extra_kinds_duration():
    tr = CooldownTracker(extra_kinds={"blacklist": 6 * 3600.0})
    got = tr.mark("a", kind="blacklist")
    assert got == 6 * 3600.0
    _left, kind = tr.left("a")
    assert kind == "blacklist"


def test_unknown_kind_falls_back_to_account(tracker):
    # kind 未注册档位时按 account 档算，不应 KeyError
    got = tracker.mark("a", kind="whatever")
    assert got == DEFAULT_ACCOUNT_COOLDOWN_S


def test_no_cooling_returns_zero(tracker):
    assert tracker.left("nope") == (0.0, "")
    assert tracker.is_cooling("nope") is False


def test_clear_removes(tracker):
    tracker.mark("a")
    assert tracker.is_cooling("a")
    tracker.clear("a")
    assert tracker.left("a") == (0.0, "")


def test_clear_all(tracker):
    tracker.mark("a")
    tracker.mark("b", kind="quota")
    tracker.clear_all()
    assert tracker.snapshot() == {}


def test_expired_entry_treated_as_clear(tracker):
    tracker.mark("a")
    # 直接改内部 until 到过去，模拟时间流逝（不真 sleep）
    until, kind = tracker._cooldowns["a"]
    tracker._cooldowns["a"] = (until - 10_000, kind)
    assert tracker.left("a") == (0.0, "")
    assert tracker.is_cooling("a") is False
    assert "a" not in tracker.snapshot()


# -- Retry-After 钳制 ---------------------------------------------------------


def test_retry_after_overrides_default(tracker):
    got = tracker.mark("a", kind="quota", retry_after="120")
    assert got == 120.0


def test_retry_after_clamped_to_min(tracker):
    got = tracker.mark("a", retry_after="0.0001")
    assert got == RETRY_AFTER_MIN_S


def test_retry_after_clamped_to_max(tracker):
    got = tracker.mark("a", retry_after=str(10 * 86400))
    assert got == RETRY_AFTER_MAX_S


def test_retry_after_invalid_falls_back_to_default(tracker):
    got = tracker.mark("a", kind="quota", retry_after="not-a-number")
    assert got == DEFAULT_QUOTA_COOLDOWN_S


# -- report / snapshot 聚合视图 ----------------------------------------------


def test_report_only_cooling_accounts(tracker):
    tracker.mark("a", kind="quota")
    tracker.mark("b")  # account 档
    # c 不冷却：不应出现在 report
    got = tracker.report(["a", "b", "c"])
    assert "a" in got and "额度冷却" in got
    assert "b" in got and "账号冷却" in got
    assert "c" not in got


def test_report_custom_labels(tracker):
    tr = CooldownTracker(extra_kinds={"blacklist": 3600.0})
    tr.mark("a", kind="blacklist")
    got = tr.report(["a"], labels={"blacklist": "疑似拉黑"})
    assert "疑似拉黑" in got


def test_snapshot_only_active(tracker):
    tracker.mark("a", kind="quota")
    tracker.mark("b")
    snap = tracker.snapshot()
    assert set(snap) == {"a", "b"}
    for left, kind in snap.values():
        assert left > 0
        assert kind in {"quota", "account"}


# -- fmt_left 静态方法 --------------------------------------------------------


def test_fmt_left_minutes():
    assert CooldownTracker.fmt_left(300) == "5.0min"
    assert CooldownTracker.fmt_left(89 * 60) == "89.0min"


def test_fmt_left_hours():
    assert CooldownTracker.fmt_left(6 * 3600) == "6.0h"
    assert CooldownTracker.fmt_left(90 * 60) == "1.5h"


# -- 线程安全冒烟：并发 mark 不崩、left 自洽 -----------------------------------


def test_concurrent_marks(tracker):
    import threading

    def _worker(i: int) -> None:
        tracker.mark(f"acct-{i}", kind="quota" if i % 2 else "account")

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tracker.snapshot()) == 50
