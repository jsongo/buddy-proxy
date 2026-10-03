"""CodeBuddy 额度汇总（get-user-resource-summary）的离线测试。

盯的是一条**只在包多于 4 个时才会犯**的错：明细列表有 4 条上限，而那个
上限原先写在了「累加合计」的同一个循环里——

    used_sum += used; total_sum += total; remain_sum += remain
    packs.append({...})
    if len(packs) >= 4:
        break          # ← 顺手把合计也截断了

于是标题行的「积分余额合计」只加了前 4 个包，显示的剩余比账号实际少一截，
而界面上的明细也刚好在断点处结束，用户完全看不出还有包没算进去。这就是
「额度剩余不对」这一类问题的同一个病因（Qoder 那条是只取第一份），只是
触发条件是「包多到超过展示上限」。

本机账号当前只有 3 个包，所以线上没发作——正因为如此才更需要测试守住。

运行：
    .venv/bin/python -m pytest tests/test_workbuddy_quota_sum.py -v
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from buddy_proxy import codebuddy_provider as cbp  # noqa: E402


def _pack(code: str, total: float, used: float, remain: float | None = None) -> dict:
    p = {"PackageCode": code, "CycleTotalCapacity": total, "CycleUsedCapacity": used}
    if remain is not None:
        p["CycleRemainCapacity"] = remain
    return p


def _quota_with(monkeypatch, packages):
    client = mock.MagicMock()
    client.api_post.return_value = {
        "code": 0, "msg": "OK",
        "data": {"IsPaidUser": True, "SubscriptionPackageCode": "SUB",
                 "Packages": packages},
    }
    monkeypatch.setattr(cbp, "get_state", lambda: SimpleNamespace(
        client=client, ensure_auth=lambda: None))
    return cbp.CodeBuddyProvider().quota()


def test_headline_sums_every_pack_and_lists_them_all(monkeypatch):
    """合计必须覆盖**全部**包，明细也要**全给**——展示条数由前端折叠管。

    6 个包：订阅 4000 + 5 个 1000 的资源包，真实总额 9000。老实现把 break
    写在累加循环里，合计只到 7000（少 2000）。

    明细原先还有一条 ``len(packs) < 4`` 的截断。它比合计那个 bug 更隐蔽：
    合计错了至少能靠数字对不上发现，而被砍掉的包**后端不再提、前端无从得知**，
    界面上彻底消失且没有任何迹象。2026-10-03 起改由前端折叠（只铺没花完的 +
    超限收起 + 展开看全），后端如实给全部条目。
    """
    packages = [_pack("SUB", 4000, 0, 4000)]
    packages += [_pack(f"PK{i}", 1000, 100, 900) for i in range(1, 6)]

    out = _quota_with(monkeypatch, packages)
    head = out["items"][0]
    assert head["label"] == "积分余额合计"
    assert head["total"] == 9000.0, f"合计被截断了: {head}"
    assert head["used"] == 500.0, head
    assert head["remaining"] == 8500.0, head
    assert len(out["items"]) == 1 + 6, [i["label"] for i in out["items"]]


def test_headline_matches_the_sum_of_the_listed_details_when_under_the_cap(monkeypatch):
    """不超过上限时，合计与列出的明细必须对得上（正常路径别被改坏）。"""
    packages = [_pack("SUB", 4000, 4000, 0), _pack("PK1", 4600, 2800, 1800),
                _pack("PK2", 5000, 4938.12, 61.88)]

    out = _quota_with(monkeypatch, packages)
    head, rest = out["items"][0], out["items"][1:]
    assert len(rest) == 3
    assert head["total"] == round(sum(i["total"] for i in rest), 2)
    assert head["remaining"] == round(sum(i["remaining"] for i in rest), 2)
    assert head["used"] == round(sum(i["used"] for i in rest), 2)


def test_missing_remain_is_derived_and_still_counted(monkeypatch):
    """包不给 CycleRemainCapacity 时按 total-used 补，且照样计进合计。

    补 remain 与累计 sum 是同一步的两件事，改循环时容易漏掉一边。
    """
    packages = [_pack("SUB", 4000, 1000), _pack("PK1", 1000, 250, 750)]

    out = _quota_with(monkeypatch, packages)
    head = out["items"][0]
    assert head["total"] == 5000.0, head
    assert head["used"] == 1250.0, head
    assert head["remaining"] == 3750.0, head          # (4000-1000) + 750
    assert out["items"][1]["remaining"] == 3000.0     # 订阅那条补出来的


def test_packs_without_a_total_are_skipped_entirely(monkeypatch):
    """total 为空的包既不该列进明细，也不该混进合计。"""
    packages = [_pack("SUB", 4000, 1000, 3000), {"PackageCode": "JUNK"}]

    out = _quota_with(monkeypatch, packages)
    assert out["items"][0]["total"] == 4000.0, out["items"][0]
    assert len(out["items"]) == 2, [i["label"] for i in out["items"]]
