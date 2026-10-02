"""Qoder 额度 / 专属资源包的展示辅助。

2026-10 从 ``provider.py`` 拆出；旧路径 ``buddy_proxy.qoder.provider``
对以下名字保持 re-export 兼容。
"""

from __future__ import annotations

from typing import Any


#: 专属资源包 ``status`` 枚举里明确表示「不占额度」的片段。活跃态实测是
#: ``QUOTA_DETAIL_STATUS_ACTIVE``，失效态没有真样本（手上只有一个活跃包，
#: 过期/作废长什么样抓不到），所以按「含这些词就算失效」匹配。
_PKG_INACTIVE_HINTS = (
    "EXPIRED", "INVALID", "INACTIVE", "DISABLED", "USED_UP", "DEPLETED",
)


def _pkg_active(pkg: dict[str, Any]) -> bool:
    """专属资源包是否还占额度。

    ``available``（布尔）和 ``status``（``QUOTA_DETAIL_STATUS_*`` 枚举）两个
    信号一起看——失效时上游到底翻哪个字段，没有真样本能证。只信
    ``available`` 的话，万一它只改 ``status``，过期包就会被算进总额度，
    从「少显示」翻车成「多显示」。

    ``status`` 只排除明确不活跃的枚举，**未知值放行**：上游加新状态时宁可
    多显示一行，也不要把活跃包误杀（漏显额度正是这条链路修过的老 bug）。
    """
    if not pkg.get("available", True):
        return False
    status = str(pkg.get("status") or "").upper()
    return not any(hint in status for hint in _PKG_INACTIVE_HINTS)


def _pkg_label(pkg: dict[str, Any]) -> str:
    """专属资源包的展示名。

    上游把名字放在 ``displayLabels`` 里（``dimension == "title"`` 那条，带
    ``valueI18n`` 多语言），比 ``name`` 字段（``act-20260901-170`` 这种活动
    代号）更适合给人看。按 zh-CN → en-US → value → name 依次回退，都拿不到
    就用通用名。
    """
    for entry in pkg.get("displayLabels") or []:
        if not isinstance(entry, dict) or entry.get("dimension") != "title":
            continue
        i18n = entry.get("valueI18n") or {}
        if isinstance(i18n, dict):
            for key in ("zh-CN", "en-US"):
                text = str(i18n.get(key) or "").strip()
                if text:
                    return text
        text = str(entry.get("value") or "").strip()
        if text:
            return text
    return str(pkg.get("name") or "").strip() or "专属积分"


def _reset_ts(data: dict[str, Any]) -> int | None:
    """额度重置时间（上游给 ``expiresAt``，毫秒）。"""
    value = data.get("expiresAt")
    if isinstance(value, (int, float)) and value > 0:
        # 上游偶尔用 253402214400000（9999 年）表示「不重置」，过滤掉。
        if value < 4102444800000:
            return int(value / 1000)
    return None


def _used_percent(node: dict[str, Any], used: float, total: float) -> float:
    """额度节点 -> **已用**百分比（0~100）。

    上游 ``percentage`` 是**剩余**比例、且量纲是 0~1（实测：``total=200,
    used=101, remaining=99`` 时给 ``0.51``——``remaining/total=0.495`` 对得上，
    而 ``used/total=0.505`` 对不上）。管理页 ``quotaItemHtml`` 的 ``percent``
    要的是**已用**（填进度条 +「已用 x%」+ ≥85% 变红），直接透传会出现两个
    问题：进度条画反、且 0~1 的比例永远够不到 85 的阈值（红色告警成死代码）。
    Mimo 通道同样的坑见 ``mimo/provider.py`` 的 ``_usage_items`` 注释。

    但 ``percentage`` 并不总是可信：``userQuota`` 实测给过 ``percentage: 0.0``
    而 ``used: 0.0, remaining: 2000.0``（一分没用却说剩余 0%），三者互相矛盾。
    此时 ``used``/``total`` 是自洽的、也更直观，故**优先用 counted 值**：
    只有当 ``used``/``total`` 拿不到（``total`` 为 0）才退回 ``percentage``。
    """
    if total > 0:
        return max(0.0, min(100.0, used / total * 100.0))
    pct = node.get("percentage")
    if isinstance(pct, (int, float)) and not isinstance(pct, bool):
        # 0~1 当作剩余比例换算成已用；已经是 0~100 的（>1.5）按已用原样用。
        if pct <= 1.5:
            return max(0.0, min(100.0, (1.0 - float(pct)) * 100.0))
        return max(0.0, min(100.0, float(pct)))
    return 0.0

