"""Qoder 活动权益（每日领取 Credits）——``/sash/**`` 面。

桌面端启动时自动弹出的「专属活动权益 · 每天领 100 Credits」走的是这一面，
与聊天/目录的 ``/algo/**``（COSY 签名）**完全无关**：

.. code-block:: text

    GET  {openapi}/sash/api/v1/me/campaigns                      活动列表
    GET  {openapi}/sash/api/v1/me/campaigns/{campaignId}/reward  领取结果
    POST {openapi}/sash/api/v1/me/campaigns/{campaignId}/claim   领取

**不需要 COSY 签名**——桌面端由 Electron 主进程直调（日志里
``requestSource: "native_main"``），普通 ``Authorization: Bearer <dt-token>``
即可，实测从纯 Python 直接调通。

## 每日语义（实测，2026-09）

活动窗口就是「当日 10:00 → 次日 09:59」（UTC+8），与界面文案「每日 10:00
（UTC+8）刷新」一致；``campaignKey`` 逐日递增（``act-20260923-556`` →
次日 ``-557``），``campaignId`` 是每天**全新的 UUID**。因此打卡必须每天重新
拉列表取当天的 id，不能缓存跨天。

## 领取幂等

对已领过的活动再 POST 不报错，返回 ``{"status":"CLAIMED","replayed":true}``
（``claimedAt`` 是**过去**那次的时间）。判定「这次真的领到」必须同时满足
``status == CLAIMED`` 且 ``replayed`` 非真、``claimedAt`` 是当前时刻——
只看 HTTP 200 会把重放当成新领取（这个坑已踩过）。

## 判定「可领」不能看顶层 claimable

顶层 ``claimable`` 同时涵盖 ``VIEW_DETAILS`` 类活动与窗口未开的情况。真正
可领的判据是**逐条**看：``actionType == "CLAIM_BENEFIT"`` 且
``claimStatus == "CLAIMABLE"``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Region, with_cached_endpoints
from .credentials import Credential
from .umid import MachineIdentity, identity_headers

log = logging.getLogger(__name__)

#: 活动面的客户端标识（与桌面端主进程一致）。
CAMPAIGN_CLIENT_TYPE = "10"
CAMPAIGN_COSY_VERSION = "0.4.3"

#: 单次请求超时。
TIMEOUT_S = 20.0

#: 可领的活动类型：只有这个类型带 benefit（其余是「查看详情」）。
CLAIM_ACTION = "CLAIM_BENEFIT"
#: 等待领取的状态。
CLAIMABLE = "CLAIMABLE"
#: 已领取的状态。
CLAIMED = "CLAIMED"


@dataclass
class Campaign:
    """一条活动。"""

    id: str
    key: str
    action_type: str
    claim_status: str
    start_at: int = 0
    end_at: int = 0
    amount: float | None = None
    kind: str = ""
    validity: dict[str, Any] | None = None

    @property
    def is_claimable(self) -> bool:
        """是否「可领取的 Credits 活动」。"""
        return self.action_type == CLAIM_ACTION and self.claim_status == CLAIMABLE

    @property
    def is_claimed(self) -> bool:
        return self.claim_status == CLAIMED

    @classmethod
    def parse(cls, raw: object) -> "Campaign | None":
        """上游条目 -> ``Campaign``；结构不符时返回 ``None``。"""
        if not isinstance(raw, dict):
            return None
        cid = str(raw.get("campaignId") or "").strip()
        if not cid:
            return None
        benefit = raw.get("benefit") if isinstance(raw.get("benefit"), dict) else {}
        amount = benefit.get("amount")
        return cls(
            id=cid,
            key=str(raw.get("campaignKey") or cid),
            action_type=str(raw.get("actionType") or ""),
            claim_status=str(raw.get("claimStatus") or ""),
            start_at=_to_int(raw.get("startAt")),
            end_at=_to_int(raw.get("endAt")),
            amount=float(amount) if isinstance(amount, (int, float)) else None,
            kind=str(benefit.get("kind") or ""),
            validity=benefit.get("validity") if isinstance(benefit.get("validity"), dict) else None,
        )


@dataclass
class ClaimResult:
    """一次 claim 的结果。"""

    ok: bool
    status: str = ""
    replayed: bool = False
    grant_id: str = ""
    claimed_at: str = ""
    message: str = ""
    error_code: str = ""

    @classmethod
    def parse(cls, body: object) -> "ClaimResult":
        data = body if isinstance(body, dict) else {}
        status = str(data.get("status") or "")
        replayed = bool(data.get("replayed"))
        # 只有 status=CLAIMED 才算领到；``replayed`` 说明是**过去**领过的那次，
        # 这一次并没有新发奖——但仍算「已完成打卡」，调用方据此区分展示。
        return cls(
            ok=status == CLAIMED,
            status=status,
            replayed=replayed,
            grant_id=str(data.get("grantId") or ""),
            claimed_at=str(data.get("claimedAt") or ""),
            message=str(data.get("errorMessage") or ""),
            error_code=str(data.get("errorCode") or ""),
        )


def _to_int(value: object) -> int:
    """时间戳取整（上游给秒级整数；异常值归 0）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return 0


def campaign_headers(cred: Credential, identity: MachineIdentity | None = None) -> dict[str, str]:
    """活动面的请求头。

    与桌面端主进程一致；``Cosy-Machine*`` 一族只作标识用——**签名不参与**
    （这一面不吃 COSY 签名，实测裸 Bearer 即可）。

    签到活动按机器指纹定向发放（CN/全球一致）：有 ``identity``（桌面端
    runtime-info 同款指纹）时带上真实 ``Cosy-Machine*`` 三件套 + 客户端标识，
    否则回退静态 machine_id——上游不认识的指纹拿不到签到条目（见 ``umid.py``）。
    """
    machine = cred.machine_id or ""
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cred.token}",
        "Cosy-ClientType": CAMPAIGN_CLIENT_TYPE,
        "Cosy-Version": CAMPAIGN_COSY_VERSION,
        "Cosy-MachineId": machine,
        "Cosy-MachineToken": machine,
        "Cosy-MachineType": CAMPAIGN_CLIENT_TYPE,
        "User-Agent": "Qoder",
    }
    if identity is not None:
        headers.update(identity_headers(identity))
    return headers


class CampaignClient:
    """活动面客户端（异步）。"""

    def __init__(
        self, region: Region, cred: Credential, identity: MachineIdentity | None = None
    ) -> None:
        self.region = with_cached_endpoints(region)
        self.cred = cred
        self.identity = identity

    def _url(self, suffix: str = "") -> str:
        return f"{self.region.openapi_base}/sash/api/v1/me/campaigns{suffix}"

    async def list(self) -> list[Campaign]:
        """拉取活动列表（逐条解析，坏条目跳过）。"""
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(self._url(), headers=campaign_headers(self.cred, self.identity))
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        body = resp.json()
        raw = (body or {}).get("campaigns") if isinstance(body, dict) else None
        out: list[Campaign] = []
        for item in raw or []:
            parsed = Campaign.parse(item)
            if parsed is not None:
                out.append(parsed)
        return out

    async def claim(self, campaign_id: str) -> ClaimResult:
        """领取指定活动。网络/HTTP 错误抛异常，业务失败在返回值里。

        ``campaign_id`` 为 UUID，直接拼路径（无特殊字符）。
        """
        url = self._url(f"/{campaign_id}/claim")
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.post(url, headers=campaign_headers(self.cred, self.identity), json={})
        try:
            body = resp.json()
        except ValueError:
            body = {}
        result = ClaimResult.parse(body)
        if resp.status_code != 200 and not result.error_code:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        if resp.status_code != 200:
            result.ok = False
        return result

    async def reward(self, campaign_id: str) -> ClaimResult:
        """查领取结果（发奖详情；``replayed`` 为真表示是重放）。"""
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            resp = await client.get(
                self._url(f"/{campaign_id}/reward"), headers=campaign_headers(self.cred, self.identity)
            )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        result = ClaimResult.parse(body)
        result.ok = result.ok and resp.status_code == 200
        return result
