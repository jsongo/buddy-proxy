"""轮换周期 ≠ 日历日时，「今天已打过」必须以上游实况为准。

实测（``logs/checkin.jsonl`` + qoder 活动接口，2026-09-30）暴露的问题：

- qoder 的活动窗口是 ``09-29 10:00 → 09-30 09:59``，**10 点轮换**；
- 自动打卡按 ``_today()``（日历日）去重，``09-30 00:01`` 的巡检看到上一轮
  还是 ``checked_in``，就补记了一条 ``date=2026-09-30, ok=True``；
- 于是 ``09-30 10:00`` 新活动开出、上游报 ``claimable`` 时，本地记录已经说
  「今天打过了」→ ``_tick`` 整天跳过，界面也显示「已签到」+ 按钮禁用，
  而那 100 Credits 就此领不到，界面看起来却一切正常。

（注：``logs/checkin.jsonl`` 里 qoder 至今两条记录的 message 都是「今日已
领取」，即都走「上游已签到 → 补记」写入、代理自己没领过——所以这是「会漏」
而非「已漏」。测试断言的是行为，与历史是否真漏过无关。）

零点轮换的通道（codebuddy / trae）不受影响，故这条规则对它们是 no-op：
上游说不可领时，本地记录照旧作数。
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest import mock

from buddy_proxy.benefits import BenefitsManager, CheckinHistory
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core.metrics import MetricsCollector


class _RotatingCheckin:
    """上游按「非日历日」轮换的通道（qoder 的形态）。

    ``claimable`` 可由测试翻转，模拟 10:00 新活动开出。
    """

    id = "rotating"
    name = "Rotating"
    supports_checkin = True

    def __init__(self, claimable: bool = False):
        self.claimable = claimable
        self.checked_in = not claimable
        self.claim_calls = 0
        self.status_calls = 0

    def checkin_status(self):
        self.status_calls += 1
        return {"checked_in": self.checked_in, "claimable": self.claimable,
                "inactive": False, "streak_days": 1, "message": "ok",
                "next_ts": int(time.time()) + 3600, "next_ts_source": "upstream"}

    def checkin_claim(self):
        self.claim_calls += 1
        self.claimable = False
        self.checked_in = True
        return {"checked_in": True, "extra_credits": 100, "message": "ok"}

    def quota(self):
        return None


def _manager(tmp_path, monkeypatch, providers, **settings):
    """构造 BenefitsManager（顶掉默认 codebuddy 注入，避免碰真上游）。

    ``settings_mod.save_settings`` 会写 ``~/.buddy-proxy/settings.json``——
    不重定向就会把用户真实的 ``auto_checkin`` / ``model_order`` 覆盖掉，
    所以这里必须先沙箱化（``settings_path()`` 每次调用都读 env，无缓存）。
    """
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))
    providers = {"codebuddy": mock.MagicMock(supports_checkin=False,
                                             quota=lambda: None), **providers}
    client = mock.MagicMock()
    client.session = {}
    state = SimpleNamespace(
        client=client, providers=providers, mock_dir=None,
        started_at=time.time(), enable_desensitize=False,
        enable_optimize_context=False, verbose_llm=False,
        default_provider="codebuddy", default_model=None,
        metrics=MetricsCollector(None),
        write_log=mock.MagicMock(), ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(), json_logger=mock.MagicMock(),
        runtime_info={},
    )
    settings_mod.save_settings({"auto_checkin": True, "checkin_time": "00:00", **settings})
    mgr = BenefitsManager(tmp_path / "checkin.jsonl", state)
    state.benefits = mgr
    return state, mgr


def _predate_history(tmp_path, pid: str = "rotating") -> None:
    """写一条「今天已打过」的本地记录，复现 00:01 巡检留下的补记。"""
    CheckinHistory(tmp_path / "checkin.jsonl").append(pid, ok=True, message="今日已领取")


# --- 自动打卡循环 -----------------------------------------------------------


def test_tick_claims_when_upstream_says_claimable_despite_local_record(tmp_path, monkeypatch):
    """上游说可领 → 领，哪怕本地记录写着今天已打过。

    这是「会漏领」的直接复现：只有日历日记录、没有上游实况时，``_tick``
    会在 ``continue`` 里跳过一整天。
    """
    p = _RotatingCheckin(claimable=True)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    assert CheckinHistory(tmp_path / "checkin.jsonl").ok_dates_by_provider()["rotating"], \
        "前置条件：本地必须已有今天的成功记录"

    asyncio.run(mgr._tick())
    assert p.claim_calls == 1, "上游报 claimable 就必须领，不能被本地日历日记录挡住"


def test_tick_still_skips_when_local_record_and_upstream_quiet(tmp_path, monkeypatch):
    """上游没报可领 + 本地有今天记录 → 跳过（零点轮换通道的正常路径，不能回归）。"""
    p = _RotatingCheckin(claimable=False)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 0


def test_tick_does_not_double_claim_within_same_round(tmp_path, monkeypatch):
    """领完之后再巡检一次不该重复领（幂等仍要保住）。"""
    p = _RotatingCheckin(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 1
    # 领取后上游翻成 checked_in，且缓存已作废 → 重查也不会再领
    mgr._cache.clear()
    asyncio.run(mgr._tick())
    assert p.claim_calls == 1


def test_tick_reuses_snapshot_cache_instead_of_hitting_upstream_again(tmp_path, monkeypatch):
    """``_tick`` 走 ``_cached``：管理页开着时不该因为这次改动多打上游。"""
    p = _RotatingCheckin(claimable=False)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr.snapshot())
    calls_after_snapshot = p.status_calls
    assert calls_after_snapshot >= 1
    asyncio.run(mgr._tick())
    assert p.status_calls == calls_after_snapshot, "TTL 内应命中缓存，不再查上游"


def test_claim_invalidates_status_cache(tmp_path, monkeypatch):
    """领取后作废状态缓存：界面不必等满 TTL 才从「立即打卡」翻成「已签到」。"""
    p = _RotatingCheckin(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr.snapshot())
    assert mgr._cache.get("checkin:rotating") is not None
    asyncio.run(mgr.claim_now("rotating"))
    assert "checkin:rotating" not in mgr._cache, "领完必须清缓存，否则界面显示旧状态"


# --- 界面口径（snapshot）---------------------------------------------------


def test_snapshot_done_today_false_when_claimable(tmp_path, monkeypatch):
    """上游报可领 → ``done_today`` 必须为 False，按钮才不会被禁用。

    这条是「界面说谎」的直接断言：旧逻辑 ``today in done or checked_in`` 会把
    已补记的今天算成打过了，按钮 disabled，用户看不到自己还有 Credits 可领。
    """
    p = _RotatingCheckin(claimable=True)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is False
    assert ck["claimable"] is True


def test_snapshot_done_today_true_when_not_claimable(tmp_path, monkeypatch):
    """上游没报可领 + 今天有记录 → ``done_today`` 照旧为 True（不能回归）。"""
    p = _RotatingCheckin(claimable=False)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is True


def test_snapshot_done_today_survives_status_query_failure(tmp_path, monkeypatch):
    """上游查询失败（兜错结构）时退回本地记录，别把「打过了」显示成「没打」。

    失败结构里没有 ``claimable``，若被当成「不可领」无所谓；但也不能因为
    拿不到实况就把本地已完成的记录忽略掉。
    """
    class _Failing(_RotatingCheckin):
        def checkin_status(self):
            raise RuntimeError("gateway unreachable")

    p = _Failing(claimable=False)
    _predate_history(tmp_path)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is True
    assert "error" in ck


def test_tick_does_not_claim_when_upstream_contradicts_itself(tmp_path, monkeypatch):
    """``checked_in`` 与 ``claimable`` 同时为真 → 按「已打」处理，不重复领。

    三家真实 provider 都保证这俩互斥（qoder 的 ``checked_in`` 就是
    ``claimed and not claimable``），同时为真只能是上游自相矛盾。此时宁可
    不打：claim 虽幂等，但每轮巡检都会多打一次上游。
    """
    class _Contradictory(_RotatingCheckin):
        def checkin_status(self):
            self.status_calls += 1
            return {"checked_in": True, "claimable": True, "inactive": False,
                    "message": "矛盾态"}

    p = _Contradictory(claimable=True)
    state, mgr = _manager(tmp_path, monkeypatch, {"rotating": p})

    asyncio.run(mgr._tick())
    assert p.claim_calls == 0, "自相矛盾时不该领，免得每轮多打一次上游"

    snap = asyncio.run(mgr.snapshot())
    ck = next(x["checkin"] for x in snap["providers"] if x["id"] == "rotating")
    assert ck["done_today"] is True


def test_claimable_now_predicate_ignores_junk():
    """``claimable_now`` 对非 dict / 兜错结构 / 缺字段一律判否，不抛。"""
    from buddy_proxy.benefits import claimable_now

    assert claimable_now(None) is False
    assert claimable_now("nope") is False
    assert claimable_now({}) is False
    assert claimable_now({"error": "boom", "claimable": True}) is False
    assert claimable_now({"claimable": True, "checked_in": True}) is False
    assert claimable_now({"claimable": True}) is True
    assert claimable_now({"claimable": True, "checked_in": False}) is True
