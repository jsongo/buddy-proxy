"""CodeBuddy 额度汇总（get-user-resource-summary）的离线测试。

盯的是一条**只在包多于 4 个时才会犯**的错：明细列表有 4 条上限，而那个
上限原先写在了「累加合计」的同一个循环里——

    used_sum += used; total_sum += total; remain_sum += remain
    packs.append({...})
    if len(packs) >= 4:
        break          # ← 顺手把明细也截断了

于是界面上的明细刚好在断点处结束，用户完全看不出还有包没算进去。这就是
「额度剩余不对」这一类问题的同一个病因（Qoder 那条是只取第一份），只是
触发条件是「包多到超过展示上限」。

2026-10-03 起改法：后端不再造一条「积分余额合计」明细假条目（用户指正
「其它的都没有」——别的通道都没有这种汇总行），而是给 ``quota`` 打
``sum_items: True`` 标记，合计交给前端标题行（``benefits.js`` 的
``quotaHeadSum``）去加。所以这里守两件事：

1. **明细全给、不许截断**（后端砍掉的条目前端无从得知，会被永久藏起来）；
2. **声明 sum_items**（否则前端标题行退回「只取第一条」，合计就没了）。

本机账号当前只有 3 个包，所以线上没发作——正因为如此才更需要测试守住。

运行：
    .venv/bin/python -m pytest tests/test_workbuddy_quota_sum.py -v
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from buddy_proxy import codebuddy_provider as cbp  # noqa: E402


def _pack(code: str, total: float, used: float, remain: float | None = None) -> dict:
    p = {"PackageCode": code, "CycleTotalCapacity": total, "CycleUsedCapacity": used}
    if remain is not None:
        p["CycleRemainCapacity"] = remain
    return p


def _acct(idx: int = 0):
    return cbp.creds.AccountRef(id=f"uid-{idx}", uid=f"uid-{idx}", nickname=f"tester-{idx}",
                                priority=idx, added_at=1000 + idx)


def _quota_with(monkeypatch, packages, accounts=None):
    """quota() 现走多账号 store（failover.available_accounts + creds.api_post_as）。

    单账号（无前缀）是最基本形态，原断言不变；``accounts`` 给出时测多账号
    前缀分组形态。
    """
    envelope = {
        "code": 0, "msg": "OK",
        "data": {"IsPaidUser": True, "SubscriptionPackageCode": "SUB",
                 "Packages": packages},
    }
    accts = accounts if accounts is not None else [_acct()]
    monkeypatch.setattr(cbp.failover, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "list_accounts", lambda: accts)
    monkeypatch.setattr(cbp.creds, "api_post_as",
                        lambda aid, path, body=None, **kw: dict(envelope))
    return cbp.CodeBuddyProvider().quota()


def test_multi_account_labels_are_prefixed_per_account(monkeypatch):
    """多账号逐个查：各账号条目带「CodeBuddy #N · 」前缀供前端分组（qoder 同款）。"""
    packages = [_pack("SUB", 4000, 0, 4000)]
    out = _quota_with(monkeypatch, packages,
                      accounts=[_acct(0), _acct(1)])
    labels = [i["label"] for i in out["items"]]
    assert labels == ["CodeBuddy #1 · 订阅套餐", "CodeBuddy #2 · 订阅套餐"], labels


def test_lists_every_pack_and_declares_sum_items(monkeypatch):
    """明细必须**全给**（6 个包就是 6 条），且声明 ``sum_items`` 让前端合计。

    明细原先有一条 ``len(packs) < 4`` 的截断。它比数字错更隐蔽：被砍掉的包
    **后端不再提、前端无从得知**，界面上彻底消失且没有任何迹象。2026-10-03
    起改由前端折叠（只铺没花完的 + 超限收起 + 展开看全），后端如实给全部条目。

    合计由标题行负责：去掉「积分余额合计」假条目后，唯一能让 ``quotaHeadSum``
    把各包加起来的开关就是 ``sum_items``——漏了它就退回「只取第一条」，
    账号总量被显示成第一个包的量。
    """
    packages = [_pack("SUB", 4000, 0, 4000)]
    packages += [_pack(f"PK{i}", 1000, 100, 900) for i in range(1, 6)]

    out = _quota_with(monkeypatch, packages)
    assert out.get("sum_items") is True, "没声明 sum_items，前端标题行不会合计"
    assert len(out["items"]) == 6, [i["label"] for i in out["items"]]
    assert all(i["label"] != "积分余额合计" for i in out["items"]), \
        "不该再有汇总假条目（用户指正别的通道都没有）"


def test_listed_details_sum_to_the_real_total(monkeypatch):
    """不超过上限时，明细自己的加总就是账号真实总量（前端标题行据此显示）。

    这等价于原先那条「合计与明细对得上」的断言——只是现在合计是前端算的，
    后端要保证的是**明细本身完整且逐条准确**。
    """
    packages = [_pack("SUB", 4000, 4000, 0), _pack("PK1", 4600, 2800, 1800),
                _pack("PK2", 5000, 4938.12, 61.88)]

    out = _quota_with(monkeypatch, packages)
    items = out["items"]
    assert len(items) == 3
    assert sum(i["total"] for i in items) == 13600.0
    assert round(sum(i["remaining"] for i in items), 2) == 1861.88
    assert round(sum(i["used"] for i in items), 2) == 11738.12


def test_missing_remain_is_derived(monkeypatch):
    """包不给 CycleRemainCapacity 时按 total-used 补，明细里也要是补出来的值。"""
    packages = [_pack("SUB", 4000, 1000), _pack("PK1", 1000, 250, 750)]

    out = _quota_with(monkeypatch, packages)
    by_total = {i["total"]: i for i in out["items"]}
    assert by_total[4000.0]["remaining"] == 3000.0     # 补出来的
    assert by_total[1000.0]["remaining"] == 750.0


def test_packs_without_a_total_are_skipped_entirely(monkeypatch):
    """total 为空的包不该列进明细（用不了，也不参与合计）。"""
    packages = [_pack("SUB", 4000, 1000, 3000), {"PackageCode": "JUNK"}]

    out = _quota_with(monkeypatch, packages)
    assert len(out["items"]) == 1, [i["label"] for i in out["items"]]
    assert out["items"][0]["total"] == 4000.0
