"""Trae 海外版独立 provider（``traeintl``）——与个人 CN 通道分开展示/计额/路由。

照 ``TraePatProvider`` 的先例：同上游家族的另一个变体用**独立 provider id 的
子类**，而不是给 ``TraeProvider`` 加运行期开关。这样：

- 客户端用 ``traeintl/<模型>`` 前缀显式路由到海外网关（``forward_chat`` 的
  前缀逻辑已支持），与 CN 的 ``trae/`` 互不干扰；
- ``/ui`` 管理页 CN 与海外各一张额度卡，各自计费口径（CN 积分 / 海外美元
  Usage）——由继承的 ``_quota_items`` 按 ``self._region.billing`` 自动分叉；
- 海外**无每日签到**（上游 ``checkin_credits/*`` 实测应用级 404），故
  ``supports_checkin=False``，自动签到调度直接跳过它，不会天天打一串 404
  进 ``checkin.jsonl`` 污染日历。

与 ``TraePatProvider`` 的关键差异：PAT 有**独立账号体系**（bearer 服务账号，
``_pat_variant=True`` 走单账号直通）；海外版**共用** work 多账号池，只是按
``region="global"`` 过滤——所以它不设 ``_pat_variant``，照常走父类的同区
failover（``forward`` / ``_work_accounts`` 已按 ``self._region.key`` 过滤）。

海外模型目录（``config.MODEL_*_INTL``）目前**留空**：只能由拿海外账号真发
请求 probe 出来（见 config.py 注释），空表时 ``models()`` 返回空、``forward``
把模型名原样透传，上游不认就诚实地 4001 冒出来。
"""

from __future__ import annotations

from typing import Any

from .provider import TraeProvider


def intl_enabled() -> bool:
    """有没有海外区账号（``--trae`` 分支据此决定注册 ``traeintl``）。

    与 ``traepat`` 的 ``pat_enabled()`` 同款条件注册：没登过海外账号时不挂
    这个通道，``/v1/models`` 不会多出一个空目录、``traeintl/`` 前缀也会走
    「通道未启用」的明确报错（而不是注册了却每个请求都 401）。

    只看索引（``list_accounts``，纯本地），**不看**冷却状态——账号全在冷却
    时通道仍该注册，转发侧自然 429 快速失败；否则「所有海外号都冷却」会被
    误报成「通道没开」，方向完全错。
    """
    try:
        from .credentials import list_accounts

        return any(a.region == "global" for a in list_accounts())
    except Exception:  # noqa: BLE001 - 索引读不动就当没登过
        return False


class TraeIntlProvider(TraeProvider):
    id = "traeintl"
    name = "Trae 海外版"
    # 海外上游没有签到端点（实测 404）——不进 /ui 打卡列表、不被自动签到调度。
    supports_checkin = False

    def __init__(self, base_url: str | None = None):
        # 区域钉死 global：chat 网关、模型目录、failover 选账号、额度口径全按它。
        super().__init__(base_url=base_url, region="global")

    def ensure_auth(self) -> None:
        # 父类 _auth() 取的是**首个可用 work 账号**（可能是 CN 的），对海外通道
        # 没有意义——这里改成校验本区确实有账号，没有就明确报「未登录海外版」，
        # 免得健康检查拿 CN 账号蒙混过关、真发请求时才 401。
        from . import failover

        if not failover.available_accounts(self._region.key):
            from fastapi import HTTPException

            raise HTTPException(
                status_code=401,
                detail=(f"traeintl 无海外版可用账号：请先 "
                        f"`buddy login trae --region global`"))

    # —— 签到为个人 CN 账号专属；海外无端点，显式覆盖为 None ——
    # （supports_checkin=False 已挡住 UI 调度，这里再兜一层，防止继承的
    #  TraeProvider 实现对海外账号发 checkin 请求。）

    def checkin_status(self) -> dict[str, Any] | None:
        return None

    def checkin_claim(self) -> dict[str, Any] | None:
        return None
