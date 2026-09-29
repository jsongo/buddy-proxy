"""打卡「下次时间」的轮换推算（core/checkin.py）。

三个通道的轮换语义各不相同，且只有 qoder 的上游明确给时间窗——其余两家是
从 ``logs/checkin.jsonl`` 的真实领取记录反推的本地零点。断言锁住这套区分：
推断值必须标 ``inferred``，算不出下次时必须**不给字段**（界面不显示），
而不是显示一个已过期的时刻骗用户。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from buddy_proxy.core.checkin import (
    SOURCE_INFERRED,
    SOURCE_UPSTREAM,
    daily_reset_within_season,
    next_daily_reset,
    next_from_window,
    parse_upstream_datetime,
)


def _at(year, month, day, hour=0, minute=0):
    return int(datetime(year, month, day, hour, minute).timestamp())


# --- 零点轮换 ---------------------------------------------------------------


def test_next_daily_reset_is_tomorrow_midnight():
    """本地零点轮换：不管现在几点，下次都是明天 00:00。

    用本地时区而非 UTC——打卡记录的 ``date`` 走 ``time.strftime``（本地），
    两边口径必须一致，否则跨时区部署时界面显示的「下次」会与日历差一天。
    """
    now = _at(2026, 9, 30, 23, 59)
    assert next_daily_reset(now) == _at(2026, 10, 1)

    # 凌晨刚过零点也算「明天」，不会返回一个已经过去或此刻的时刻
    dawn = _at(2026, 9, 30, 0, 1)
    assert next_daily_reset(dawn) == _at(2026, 10, 1)

    # 跨年
    assert next_daily_reset(_at(2026, 12, 31, 12, 0)) == _at(2027, 1, 1)


def test_next_daily_reset_is_strictly_future():
    """返回值必须严格晚于 now——否则界面会显示一个已经过期的时刻。"""
    for hour in range(0, 24):
        now = _at(2026, 9, 30, hour, 30)
        assert next_daily_reset(now) > now


def test_parse_upstream_datetime_accepts_codebuddy_shapes():
    """CodeBuddy 实测给的是无时区标注的本地时间字符串。"""
    assert parse_upstream_datetime("2026-10-15 23:59:59") == _at(2026, 10, 15, 23, 59) + 59
    assert parse_upstream_datetime("2026-09-30 00:00:00") == _at(2026, 9, 30)
    assert parse_upstream_datetime("2026-09-30") == _at(2026, 9, 30)


def test_parse_upstream_datetime_rejects_junk_instead_of_raising():
    """解析不出返回 None，绝不抛——这是展示用的辅助信息，不该有让 500 的能力。"""
    for junk in (None, "", "   ", "not-a-date", 12345, ["2026-09-30"], {"a": 1}):
        assert parse_upstream_datetime(junk) is None, repr(junk)


def test_daily_reset_respects_season_end():
    """档期最后一天之后就没有「下次」了。

    继续显示明天零点会骗用户：到点确实翻篇，但活动已结束、领不出东西。
    """
    now = _at(2026, 10, 14, 9, 0)          # 档期倒数第二天
    end = "2026-10-15 23:59:59"
    assert daily_reset_within_season(end, now) == _at(2026, 10, 15)

    # 档期就在明天结束：下一次零点已超出档期
    now_late = _at(2026, 10, 15, 9, 0)
    assert daily_reset_within_season(end, now_late) is None


def test_daily_reset_without_season_still_works():
    """上游没给档期（trae 就是这样）时退回纯零点轮换，别因为缺字段就不显示。"""
    now = _at(2026, 9, 30, 8, 0)
    assert daily_reset_within_season(None, now) == _at(2026, 10, 1)
    assert daily_reset_within_season("", now) == _at(2026, 10, 1)


# --- 上游时间窗（qoder） ----------------------------------------------------


def test_next_from_window_is_end_at_plus_60():
    """实测窗口 ``10:00:00 → 次日 09:59:00``，故 endAt+60 正是下一轮 10:00。

    上游不返回未来那条活动，只能从当前窗口推——这正是为什么这条路径仍标
    ``upstream``：绝对时刻来自上游给的 endAt，不是我们猜的。
    """
    start = _at(2026, 9, 29, 10, 0)
    end = _at(2026, 9, 30, 9, 59)
    now = _at(2026, 9, 30, 0, 21)          # 抓包当时的真实时刻
    assert next_from_window(start, end, now) == _at(2026, 9, 30, 10, 0)


def test_next_from_window_falls_back_to_start_plus_day():
    """endAt 已过（列表是旧的/时钟漂移）退回 startAt+24h，同样是 10:00。"""
    start = _at(2026, 9, 29, 10, 0)
    end = _at(2026, 9, 30, 9, 59)
    now = _at(2026, 9, 30, 9, 59, ) + 30   # 刚过 endAt，还没到 endAt+60
    assert next_from_window(start, end, now) == _at(2026, 9, 30, 10, 0)


def test_next_from_window_returns_none_when_all_past():
    """两个候选都已过期就别给——显示一个过去的时刻比不显示更糟。"""
    start = _at(2026, 9, 28, 10, 0)
    end = _at(2026, 9, 29, 9, 59)
    assert next_from_window(start, end, _at(2026, 9, 30, 12, 0)) is None


def test_next_from_window_tolerates_missing_fields():
    """上游字段缺失（0）时不炸，退到另一个；都没有就 None。"""
    now = _at(2026, 9, 30, 0, 0)
    assert next_from_window(0, 0, now) is None
    assert next_from_window(_at(2026, 9, 30, 10, 0), 0, now) == _at(2026, 10, 1, 10, 0)


# --- 两个来源标记 -----------------------------------------------------------


def test_source_constants_are_distinct():
    """界面靠这两个值决定要不要标「≈推断」，撞成一个就没法区分了。"""
    assert SOURCE_UPSTREAM != SOURCE_INFERRED
    assert SOURCE_UPSTREAM == "upstream"
    assert SOURCE_INFERRED == "inferred"


def test_inferred_and_upstream_agree_on_qoder_boundary():
    """qoder 的 10:00 轮换与「推断零点」必须**不同**——这是分来源标注的意义。

    如果两者算出来一样，就说明某一家被错误地套用了另一家的规则。
    """
    now = _at(2026, 9, 30, 0, 21)
    inferred = next_daily_reset(now)
    upstream = next_from_window(_at(2026, 9, 29, 10, 0), _at(2026, 9, 30, 9, 59), now)
    assert inferred != upstream
    assert inferred == _at(2026, 10, 1)
    assert upstream == _at(2026, 9, 30, 10, 0)


def test_reset_is_a_full_day_away_at_most():
    """任何时刻算出来的「下次」都不该超过一天——超过就是时区算错了。"""
    base = datetime(2026, 3, 8, 0, 0)       # 跨 DST 的日子
    for i in range(48):
        now = int((base + timedelta(hours=i / 2)).timestamp())
        delta = next_daily_reset(now) - now
        assert 0 < delta <= 86400, f"i={i} delta={delta}"
