"""Qoder 海外版独立 provider（``qoderintl``）——与 CN 通道分开路由/计额。

Qoder 的双区域机制本就完整（``config.Region`` + 账号级 region + 同区
failover），``QoderProvider`` 全部按 ``self._region`` 取址；本类只把区域
钉死 ``global`` 并换 id，照 ``trae/pat_provider.py`` / ``traeintl`` 的先例：
同上游家族的另一个变体用独立 provider id 的子类，客户端以
``qoderintl/<模型>`` 前缀显式路由，``/ui`` 管理页 CN 与海外各一张额度卡。

模型目录无需静态表：``Catalog.fetch`` 按 region 动态拉取（海外目录与 CN
不同，上游说了算）。

签到（每日活动权益）**保留** ``supports_checkin=True``：海外活动由上游
``/sash/.../campaigns`` 决定。没有任何活动时返回 ``inactive``；若有
``VIEW_DETAILS`` 等活动但无可领签到奖励，则状态标为 ``unavailable``，UI 显示
「有活动，暂无可领签到奖励」，自动打卡不会对非奖励活动发送 claim。注意
签到活动按**机器指纹**定向发放（CN/全球一致），活动请求会带桌面端
``runtime-info`` 同款 ``Cosy-Machine*`` 指纹头（见 ``umid.py``），指纹不可用
时回退静态头——此时上游可能不发签到条目，状态会误报「无可领签到奖励」。

``forward()`` 继承父类的同区严格语义：海外通道只轮转 ``region=global`` 的
账号，一个都没有时报 401「无该区域可用账号」，**不**兜底捞 CN 账号。
"""

from __future__ import annotations

from fastapi import HTTPException

from .config import REGIONS
from .provider import QoderProvider


def intl_enabled() -> bool:
    """有没有海外区账号（``--qoder`` 分支据此决定注册 ``qoderintl``）。

    条件注册的理由与 ``traeintl`` 同款：没登过海外账号时不挂通道，
    ``qoderintl/`` 前缀走「通道未启用」的明确报错。只看索引不看冷却——
    全冷却时通道仍该注册，让转发侧 429 快速失败，而不是误报成「没开」。
    """
    try:
        from .credentials import list_accounts

        return any(a.region == "global" for a in list_accounts())
    except Exception:  # noqa: BLE001 - 索引读不动就当没登过
        return False


class QoderIntlProvider(QoderProvider):
    id = "qoderintl"
    name = "Qoder 海外版"

    def __init__(self) -> None:
        super().__init__(region=REGIONS["global"])

    def ensure_auth(self) -> None:
        """启动校验：必须有海外区账号（父类只看「有没有账号」，会把只有 CN
        账号也放行——海外通道拿 CN 账号发请求必 401）。"""
        from . import failover

        if not failover.available_accounts(self._region.key):
            raise HTTPException(
                status_code=401,
                detail=("qoderintl 无海外版账号：请先 "
                        "`buddy login qoder --region global`"))
