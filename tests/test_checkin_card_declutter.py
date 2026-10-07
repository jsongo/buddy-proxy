"""签到卡「减负」契约（用户 2026-10-07 反馈，静态断言）。

三件事：① 通道名旁的聚合状态徽标在带逐账号明细行时不再重复；② 上游
campaign key（act-20260930-551 这类）不再当 chip 展示——对用户纯噪音；
③ 「每日 +X」保留。

渲染函数整体依赖 BENEFITS/DOM，离网 JS harness 的桩成本与这几行不成比例，
故对 benefits.js 做静态断言；改成结构性重构时这里要跟着挪。
"""

from __future__ import annotations

import pathlib

BENEFITS_JS = (pathlib.Path(__file__).resolve().parents[1]
               / "src/buddy_proxy/web/static/benefits.js")


def test_top_level_badge_hidden_when_account_rows_exist():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "const badge = (c.accounts && c.accounts.length && !c.error) ? '' : st;" in text, \
        "有明细行时第一行徽标要去掉（顶层查询失败除外）"
    assert "${badge}${meta}" in text, "br-meta 应渲染 badge 而非裸 st"


def test_activity_name_chip_is_gone():
    text = BENEFITS_JS.read_text(encoding="utf-8")
    assert "chips.push(c.activity_name)" not in text, \
        "上游 campaign key 不再上卡（用户反馈看不懂）"
