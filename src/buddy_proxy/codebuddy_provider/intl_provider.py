"""CodeBuddy 海外版独立 provider（``codebuddyintl``）——与 CN 通道分开路由/计额。

CodeBuddy 桌面端本就有 intl/CN 两份产品配置（product-ide.json vs
product-ide-cn.json），唯一差异是上游 host：海外版
``https://www.codebuddy.ai``，CN ``https://copilot.tencent.com``。2026-10
实测两边 ``/v2/plugin`` 协议逐字节同构（auth/state、token 轮询、chat 网关
同一套），海外账号只是**换域名登录/转发**，凭据形状完全一致——因此 CN 与
海外共用同一个账号 store（index.json + per-account cred 文件），账号落
``region`` 标（cn/global）区分归属。

照 ``traeintl`` / ``qoderintl`` 的先例：同上游家族的另一个变体用独立
provider id 的子类，客户端以 ``codebuddyintl/<模型>`` 前缀显式路由，
``/ui`` 管理页 CN 与海外各一张额度卡。``forward()``/``checkin``/``quota``
全部继承父类，父类按 ``self._region`` 只取本区账号。

模型目录返回空（不参与裸名自动匹配）：海外站的模型表要等登过海外账号后
实测再录，v1 靠显式 ``codebuddyintl/<模型>`` 前缀路由。
"""

from __future__ import annotations

from fastapi import HTTPException

from .provider import CodeBuddyProvider


def intl_enabled() -> bool:
    """有没有海外版账号（``__main__`` 据此决定注册 ``codebuddyintl``）。

    条件注册的理由与 ``traeintl``/``qoderintl`` 同款：没登过海外账号时不挂
    通道，``codebuddyintl/`` 前缀走「通道未启用」的明确报错。只看索引不看
    冷却——全冷却时通道仍该注册，让转发侧 429 快速失败，而不是误报成「没开」。
    """
    try:
        from .credentials import list_accounts

        return any(a.region == "global" for a in list_accounts())
    except Exception:  # noqa: BLE001 - 索引读不动就当没登过
        return False


class CodeBuddyIntlProvider(CodeBuddyProvider):
    id = "codebuddyintl"
    name = "CodeBuddy 海外版"

    # 额度/签到分组标签：``CodeBuddy 海外版 #N · ``——与 CN 卡的
    # ``CodeBuddy #N`` 区分开，前端按前缀各渲染各的卡。
    _quota_tag = "CodeBuddy 海外版"

    def __init__(self) -> None:
        super().__init__(region="global")

    def ensure_auth(self) -> None:
        """启动校验：必须有海外版账号。

        父类走 state.ensure_auth（只看「有没有账号」）——只有 CN 账号时也会
        放行，海外通道拿 CN 账号打 www.codebuddy.ai 必 401，故自己查。
        口径与 ``intl_enabled()`` 对齐：只看索引不看冷却——账号全冷却时通道
        照样注册，让转发侧 429 快速失败，而不是误报「没登过海外账号」。
        """
        from .credentials import list_accounts

        if not any(a.region == "global" for a in list_accounts()):
            raise HTTPException(
                status_code=401,
                detail=("codebuddyintl 无海外版账号：请先 "
                        "`buddy login codebuddy --region global`"))
