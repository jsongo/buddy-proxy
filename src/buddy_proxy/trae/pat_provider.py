"""PAT 独立 provider：服务账号的扩展模型目录（与个人 trae 通道分开展示/计额）。

复用 TraeProvider 的 forward 机制（SSE 解析、流式转换、native 拦截路由），
仅替换身份与目录：
- ``id = "traepat"``：客户端以 ``traepat/<模型>`` 指定，与个人 ``trae/`` 区分；
- ``models()`` 只含 PAT 扩展模型（未配置 TRAE_PAT_BEARER 时空列表）；
- ``quota()`` 只含 PAT 周包/日包，不再混入个人额度；
- 打卡/积分/用量记录均为个人账号专属功能，这里一律不支持。
"""

from __future__ import annotations

from typing import Any, Sequence

from .pat import pat_enabled, pat_model_names, fetch_pat_ent_usage, get_pat_credentials
from .provider import TraeProvider


class TraePatProvider(TraeProvider):
    id = "traepat"
    name = "Trae PAT (服务账号直连)"
    # 打卡/积分是个人账号专属，服务账号不支持（避免出现在 /ui 打卡列表里）
    supports_checkin = False

    def models(self) -> Sequence[dict[str, Any]]:
        if not pat_enabled():
            return []
        return [{
            "id": m,
            "object": "model",
            "created": 0,
            "owned_by": self.id,
            "tier": "PAT",
            "description": "Trae PAT 扩展模型",
        } for m in pat_model_names()]

    def ensure_auth(self) -> None:
        get_pat_credentials()

    def quota(self) -> dict[str, Any] | None:
        try:
            return {"items": fetch_pat_ent_usage(), "level": "PAT"}
        except Exception as e:
            # 额度查询失败不抛——UI 展示为查询错误，不影响其它 provider
            return {"items": [{"label": "PAT 额度查询失败", "used": None, "total": None,
                               "remaining": str(e)[:120], "percent": None, "reset_ts": None}],
                    "level": "PAT"}

    # —— 打卡/积分/用量记录均为个人账号专属：显式覆盖为 None，防止继承的
    # TraeProvider 实现拿个人凭证调用（supports_checkin=False 已挡住 UI 调度）——

    def checkin_status(self) -> dict[str, Any] | None:
        return None

    def checkin_claim(self) -> dict[str, Any] | None:
        return None

    def usage_records(self, start: str, end: str, page_num: int = 1,
                      page_size: int = 20) -> dict[str, Any] | None:
        return None
