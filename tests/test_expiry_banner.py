"""权益到期告警：数据采集 + 后端筛选 + 前端横幅。

背景（2026-10-03 用户需求）：每个账号的套餐 / 资源包 / 签到积分 / 加油包等
各种权益都有到期时间，界面上看不到；用户要「套餐剩余不足 7 天时，在 tab
顶部挂横幅」，后来又加了一条「展示在横幅上的还要再过滤下量，比如大于 300
积分再提醒」。

三块要一起测：
1. **采集**：各通道把到期时刻送进 ``expire_ts``（不是 ``reset_ts``）——
   改错字段横幅要么漏报要么把「5 小时后重置」误报成「快到期」。
2. **筛选**：``benefits._expiring`` 的天数 + 量两条过滤。
3. **渲染**：横幅只在有告警时出现，且「到期」与「重置」文案不串。
"""

from __future__ import annotations

import time

import pytest

from buddy_proxy.benefits import (
    EXPIRY_WARN_DAYS,
    EXPIRY_WARN_MIN_CREDITS,
    _expiring,
)

NOW = 1_800_000_000.0  # 固定「现在」，测试不跟真实时钟走
DAY = 86400.0


def _entry(pid: str, items: list[dict], name: str | None = None) -> dict:
    """造一个 provider 条目（形状同 snapshot() 的 provider_entries）。"""
    return {
        "id": pid,
        "name": name or pid,
        "checkin": {"supported": False},
        "quota": {"supported": True, "items": items},
    }


def _item(label: str, *, expire_days: float | None, remaining=1000.0,
          unit: str | None = "credit", reset_days: float | None = None) -> dict:
    return {
        "label": label, "used": 0, "total": 1000, "remaining": remaining,
        "percent": 0, "unit": unit,
        "expire_ts": int(NOW + expire_days * DAY) if expire_days is not None else None,
        "reset_ts": int(NOW + reset_days * DAY) if reset_days is not None else None,
    }


# ---------------------------------------------------------------------------
# _expiring：天数过滤
# ---------------------------------------------------------------------------

def test_expiring_picks_within_window_sorted_by_expiry():
    """7 天内到期的都该进来，按到期先后排序。"""
    out = _expiring([
        _entry("qoder", [
            _item("订阅额度", expire_days=3),
            _item("加油包", expire_days=1),
            _item("专属积分", expire_days=6.5),
        ]),
    ], now=NOW)
    assert [e["label"] for e in out] == ["加油包", "订阅额度", "专属积分"]
    assert out[0]["provider"] == "qoder"
    assert out[0]["days_left"] == 1.0


def test_expiring_ignores_beyond_window():
    """7 天及以后的不进横幅；没到期时间的条目直接跳过。"""
    out = _expiring([
        _entry("qoder", [
            _item("还早", expire_days=EXPIRY_WARN_DAYS),       # 恰好 7 天 → 不报
            _item("更早", expire_days=30),
            _item("无到期", expire_days=None),
        ]),
    ], now=NOW)
    assert out == []


def test_expiring_includes_already_expired():
    """已过期的也要报——「已经没了」比「快没了」更该提醒，由前端渲红色。"""
    out = _expiring([_entry("trae", [_item("签到奖励", expire_days=-2)])], now=NOW)
    assert len(out) == 1
    assert out[0]["days_left"] == -2.0


# ---------------------------------------------------------------------------
# _expiring：量过滤（用户要求「大于 300 积分再提醒」）
# ---------------------------------------------------------------------------

def test_expiring_skips_small_credit_remainders():
    """积分类剩余 ≤300 不提醒：每天签到送的 200 分小包会刷屏。"""
    out = _expiring([
        _entry("trae", [
            _item("签到奖励", expire_days=2, remaining=200, unit="credit"),
            _item("会员 Pro 包", expire_days=2, remaining=4000, unit="credit"),
        ]),
    ], now=NOW)
    assert [e["label"] for e in out] == ["会员 Pro 包"]


def test_expiring_credit_threshold_is_exclusive():
    """恰好 300 不报（要求是「大于 300」）。边界值单拎出来盯住。"""
    assert _expiring([
        _entry("trae", [_item("刚好 300", expire_days=2,
                              remaining=EXPIRY_WARN_MIN_CREDITS, unit="credit")]),
    ], now=NOW) == []
    assert len(_expiring([
        _entry("trae", [_item("300 出头", expire_days=2,
                              remaining=EXPIRY_WARN_MIN_CREDITS + 0.01, unit="credit")]),
    ], now=NOW)) == 1


def test_expiring_non_credit_units_skip_the_amount_filter():
    """非积分类只看天数：mimo 的 remaining 是「还剩几天」（19.5），
    antigravity 是千分制（361.8）——拿它们跟 300 比大小毫无意义。"""
    out = _expiring([
        _entry("mimo", [_item("套餐有效期（天）", expire_days=3,
                              remaining=3.0, unit="day")]),
        _entry("antigravity", [_item("Gemini 组", expire_days=3,
                                     remaining=100.0, unit="permille")]),
        _entry("zcode", [_item("5 小时窗口", expire_days=3,
                               remaining=50, unit="count")]),
    ], now=NOW)
    assert {e["provider"] for e in out} == {"mimo", "antigravity", "zcode"}


def test_expiring_unknown_unit_fails_open():
    """``unit`` 缺失/None 时不做量过滤：新通道忘了填 unit 宁可多提醒一个，
    也别漏掉真到期。"""
    out = _expiring([
        _entry("newchan", [_item("新通道权益", expire_days=2,
                                 remaining=1.0, unit=None)]),
    ], now=NOW)
    assert len(out) == 1


def test_expiring_credit_without_remaining_is_skipped():
    """积分类但拿不到余量（None）→ 不报：无法判断值不值得提醒。"""
    out = _expiring([
        _entry("trae", [_item("权益包", expire_days=2,
                              remaining=None, unit="credit")]),
    ], now=NOW)
    assert out == []


# ---------------------------------------------------------------------------
# _expiring：结构性前提
# ---------------------------------------------------------------------------

def test_expiring_ignores_reset_only_items():
    """只有 ``reset_ts``（周期重置）的条目不算到期——ZCode 的 5 小时窗口、
    antigravity 的 weekly 池天天「快重置」，报成到期就是天天误报。"""
    out = _expiring([
        _entry("zcode", [_item("5 小时窗口", expire_days=None,
                               unit="count", reset_days=0.2)]),
    ], now=NOW)
    assert out == []


def test_expiring_skips_unsupported_and_notice_items():
    """不支持额度的通道、以及混在 items 里的失败说明条都不该进横幅。"""
    out = _expiring([
        {"id": "gemini-cli", "name": "gemini", "quota": {"supported": False}},
        _entry("traepat", [
            # 说明条：remaining 是文案不是数字
            {"label": "PAT 额度查询失败", "used": None, "total": None,
             "remaining": "2/9 个账号查询失败", "percent": None,
             "reset_ts": None},
        ]),
    ], now=NOW)
    assert out == []


def test_expiring_bad_expire_ts_is_skipped_not_crashed():
    """布尔 / 零 / 负数不吃；数字字符串照收（上游偶发给 ``"1793289600"``）。"""
    items = [
        _item("布尔", expire_days=1) | {"expire_ts": True},
        _item("零", expire_days=None) | {"expire_ts": 0},
        _item("负数", expire_days=None) | {"expire_ts": -1},
        _item("非数字串", expire_days=1) | {"expire_ts": "不是时间"},
        _item("空串", expire_days=1) | {"expire_ts": ""},
        # 数字字符串要收：上游给 zcode 的 unit/number 就发过字符串
        _item("数字串", expire_days=1) | {"expire_ts": str(int(NOW + DAY))},
    ]
    out = _expiring([_entry("weird", items)], now=NOW)
    assert [e["label"] for e in out] == ["数字串"]


def test_expiring_garbage_input_does_not_crash():
    """形状完全不对的输入不能炸（函数是纯的、又长在聚合出口上）。

    正常路径由 snapshot() 保证 entry 是 dict，但这里宽松取数是几行代码的事，
    而崩在聚合出口会连带整页 500——代价不对等。
    """
    out = _expiring([
        None, "x", 42,
        {"id": "a"},                       # 没有 quota
        {"id": "b", "quota": None},
        {"id": "c", "quota": "怪值"},
        {"id": "d", "quota": {"supported": True, "items": None}},
        {"id": "e", "quota": {"supported": True, "items": [None, "x", 7]}},
    ], now=NOW)
    assert out == []


def test_expiring_rejects_bool_remaining():
    """``remaining`` 是布尔时不算数：``True`` 是 1 会变成「只剩 1 分」的假数据。"""
    out = _expiring([
        _entry("x", [_item("真", expire_days=1, remaining=True, unit="credit")]),
    ], now=NOW)
    assert out == []


def test_expiring_accepts_numeric_string_remaining():
    """数字字符串的余量照收——上游偶发把数额发成字符串。"""
    assert len(_expiring([
        _entry("x", [_item("够多", expire_days=1, remaining="500", unit="credit")]),
    ], now=NOW)) == 1
    assert _expiring([
        _entry("x", [_item("太少", expire_days=1, remaining="200", unit="credit")]),
    ], now=NOW) == []


# ---------------------------------------------------------------------------
# 采集端：到期时刻必须进 expire_ts，不能留在 reset_ts
# ---------------------------------------------------------------------------

def test_trae_pack_remaining_uses_credits_amount_as_used(monkeypatch):
    """Trae 权益包补全 remaining——``credits_amount`` 是**已用**不是剩余。

    2026-10-03 实测交叉校验（账号总额度当标尺）：Σlimit=9500.0、
    Σamount=6257.1772，``Σlimit - Σamount`` 与接口自报 remaining（3242.82）
    差 0.00；而「amount=剩余」的假设差 3014.36。这条断言把方向钉死：
    算反的话下面 remaining 会变成 6257.18，横幅就会把「已花光」的包
    报成「还剩一大笔」，正好反了。
    """
    from buddy_proxy.trae import provider as trae_provider

    monkeypatch.setattr(trae_provider, "fetch_ent_usage", lambda: {
        "usage_summary": {"total_amount": 9500.0, "consumed_amount": 6257.18,
                          "consumption_ratio": 0.66},
        "user_entitlement_pack_list": [
            {   # 有 usage：已用 4000 → 剩 0（会员包已花光）
                "display_desc": "会员 Pro 连续包月",
                "entitlement_base_info": {
                    "entitlement_id": "pro", "end_time": int(NOW + 1 * DAY),
                    "quota": {"credits_limit": 4000},
                },
                "usage": {"credits_amount": 4000.0},
            },
            {   # usage 缺失 = 真·未消费（不是「未知」），按 0 已用算 → 剩 500
                "display_desc": "每月登录赠送",
                "entitlement_base_info": {
                    "entitlement_id": "monthly_bonus", "end_time": int(NOW + 3 * DAY),
                    "quota": {"credits_limit": 500},
                },
                "usage": {},
            },
        ],
    })
    monkeypatch.setattr(trae_provider.TraeProvider, "_client", None, raising=False)

    out = trae_provider.TraeProvider().quota()
    packs = [i for i in out["items"] if i["label"] != "总额度"]
    by_label = {i["label"]: i for i in packs}

    pro = by_label["会员 Pro 连续包月"]
    assert pro["used"] == 4000.0
    assert pro["remaining"] == 0.0, "已花光的包不能报成还剩 4000"
    bonus = by_label["每月登录赠送"]
    assert bonus["used"] == 0.0
    assert bonus["remaining"] == 500.0
    for it in packs:
        assert it["expire_ts"] is not None, "权益包必须给结构化到期时间"
        assert it["reset_ts"] is None, "到期不是重置"
        assert it["unit"] == "credit"


def test_trae_pack_dedup_keeps_same_named_packs(monkeypatch):
    """同名「签到奖励」不能只留一条：25 条里有 12 条是独立额度。

    早先按名字去重 + 只留前 3 条，界面上剩余积分比实际少一大截。
    """
    from buddy_proxy.trae import provider as trae_provider

    packs = []
    for i, used in enumerate((200.0, 200.0, None, None)):
        packs.append({
            "display_desc": "签到奖励",
            "entitlement_base_info": {
                "entitlement_id": f"checkin_{i}", "end_time": int(NOW + (i + 1) * DAY),
                "quota": {"credits_limit": 200},
            },
            "usage": ({"credits_amount": used} if used is not None else {}),
        })
    monkeypatch.setattr(trae_provider, "fetch_ent_usage", lambda: {
        "usage_summary": {"total_amount": 800.0, "consumed_amount": 400.0},
        "user_entitlement_pack_list": packs,
    })

    out = trae_provider.TraeProvider().quota()
    named = [i for i in out["items"] if i["label"] == "签到奖励"]
    assert len(named) == 4, "四条签到奖励是四份额度，不能合并"
    assert [i["remaining"] for i in named] == [0.0, 0.0, 200.0, 200.0]
    # 到期早的排前面
    assert [i["expire_ts"] for i in named] == sorted(i["expire_ts"] for i in named)


def test_trae_pack_duplicate_rows_still_deduped(monkeypatch):
    """上游重复返回同一行（同名字 + 同 id + 同到期日）仍要合并。"""
    from buddy_proxy.trae import provider as trae_provider

    row = {
        "display_desc": "会员 Pro 连续包月",
        "entitlement_base_info": {
            "entitlement_id": "pro", "end_time": int(NOW + 1 * DAY),
            "quota": {"credits_limit": 4000},
        },
        "usage": {"credits_amount": 100.0},
    }
    monkeypatch.setattr(trae_provider, "fetch_ent_usage", lambda: {
        "usage_summary": {"total_amount": 4000.0, "consumed_amount": 100.0},
        "user_entitlement_pack_list": [row, dict(row)],
    })
    out = trae_provider.TraeProvider().quota()
    assert len([i for i in out["items"] if i["label"] == "会员 Pro 连续包月"]) == 1


def test_mimo_period_item_uses_expire_ts_not_label():
    """MiMo 的套餐到期走结构化字段，不再拼进 label。

    拼在 label 里（「套餐有效期至 10-23（天）」）前端拿不到值，做不了告警；
    现在横幅列明细时还会和 expire_ts 渲染的日期重复。
    """
    from buddy_proxy.mimo.provider import _period_item

    # _period_item 用真实时钟算已用/剩余，这里也按真实时钟造时间窗：
    # 30 天套餐走到第 10.5 天 → 剩 19.5 天
    end = time.time() + 19.5 * DAY
    items = _period_item({"startTime": end - 30 * DAY, "endTime": end})
    assert len(items) == 1
    it = items[0]
    assert it["expire_ts"] == int(end)
    assert it["unit"] == "day"
    assert it["reset_ts"] is None
    assert "有效期至" not in it["label"], "日期不该再拼进 label（会与横幅重复）"
    assert it["remaining"] == pytest.approx(19.5, abs=0.1)


def test_qoder_quota_items_carry_expire_ts_and_credit_unit():
    """Qoder 的额度只到期不重置：expire_ts 有值、reset_ts 恒 None。"""
    from buddy_proxy.qoder.provider import QoderProvider
    from buddy_proxy.qoder.credentials import Credential
    from buddy_proxy.qoder.config import Region

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._format_quota({
        "userType": "personal_professional",
        "expiresAt": int((NOW + 4 * DAY) * 1000),
        "userQuota": {"total": 2000.0, "used": 100.0, "remaining": 1900.0},
        "addOnQuota": {"total": 0.0, "used": 0.0, "remaining": 0.0},
        "dedicatedResourcePackages": [],
    }, Credential(token="t", uid="u"))
    assert out["items"], "至少要有一条额度"
    for it in out["items"]:
        assert it["expire_ts"] == int(NOW + 4 * DAY)
        assert it["reset_ts"] is None
        assert it["unit"] == "credit"
