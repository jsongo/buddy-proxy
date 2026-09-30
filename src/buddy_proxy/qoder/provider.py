"""Qoder provider：COSY 面聊天转发 + 额度查询。

Qoder 的聊天面（``/algo/.../agent_chat_generation``）与 OpenAI 线缆的差别
只在**外壳**：

- 出站：COSY 签名头 + 自定义编码 body；body 明文里除标准 OpenAI 字段外，
  还要带官方客户端的**用量归因信封**（``request_id`` / ``session_id`` /
  ``chat_context`` / ``model_config`` / ``business``）。实测裸 body 会被
  业务路由拒（``[FAIL]node:agent_router msg:None flow nodes found``），
  带上信封即正常出字。
- 入站：SSE 信封 ``data:{"headers":…,"body":"<内层 JSON 字符串>"}``，
  内层才是标准 OpenAI ``chat.completion.chunk``。另有 ``event:finish``
  尾帧（无 body）与带内错误帧（HTTP 200 但 body 里是 ``{code,message}``）。

本 provider 把信封拆掉，把内层 chunk 原样下游——增量/tool_calls/finish/usage
全是标准 OpenAI 形态，无需二次翻译。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.core.checkin import SOURCE_UPSTREAM, next_from_window
from buddy_proxy.providers.base import BaseProvider

from .campaigns import CLAIM_ACTION, CampaignClient
from .catalog import Catalog, is_hidden, public_model_id, to_openai_model
from .config import COSY_VERSION, Region, resolve_region, with_cached_endpoints
from .cosy import sign
from .credentials import AuthError, Credential, ensure_credential

log = logging.getLogger(__name__)


#: 归 CodeBuddy 的档位式名字：Qoder 目录里也挂着同名档位（``auto`` 是
#: ``TIER_MODELS`` 合成的档位模型），但这个名字在 CodeBuddy 那边是**默认模型**，
#: 语义上归 CodeBuddy。这些名字两个轮次都让开，只在显式 ``qoder/`` 前缀下
#: 才由本通道服务。
#:
#: 为什么不能全靠 ``_is_codebuddy_model`` 推出来：静态表里还有
#: ``qwen3.8-max`` / ``kimi-k3`` / ``deepseek-v4.1-flash`` / ``glm-5.3``
#: —— 它们同时也是本通道真实发布的模型（同一批权重，两边都有资格），
#: 让开会让这些模型在目录里"消失"。能自动区分的只有「**档位**模型」这一条：
#: ``TIER_MODELS`` 是本地合成的、不是上游目录里的真实模型。
CODEBUDDY_OWNED_IDS: frozenset[str] = frozenset({"auto"})


def _is_codebuddy_owned(name: str) -> bool:
    """该名字是否归 CodeBuddy（本通道让开）。

    只对档位模型（:data:`CODEBUDDY_OWNED_IDS`）成立。大小写不敏感——``auto``
    是档位名、没有大小写语义，``Auto`` / ``AUTO`` 也该让开；而别名轮那道
    ``_is_codebuddy_model`` 守卫刻意做精确比对（否则 ``Qwen3.8-Max`` 这类
    官方显示名会被误挡），漏得过去。
    """
    return name.strip().lower() in CODEBUDDY_OWNED_IDS

#: 出站透传给上游的 OpenAI 字段白名单（其余私有扩展不透传）。
_PASSTHROUGH_FIELDS = (
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "reasoning_effort",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "seed",
    "user",
    "parallel_tool_calls",
    # 代理自身会写入 messages，但没有谁规定 system 只能在 messages 里。
    # 上游有多个取 system 文本的位置，这里一并透传，避免出现「客户端设了
    # 系统提示词、上游却看不到」的那种「根本没生效」。
    "system",
)

#: 上游首字节超时（边缘「收下不回应」时快速失败）。
FIRST_BYTE_TIMEOUT_S = 60.0

#: 整轮流式上限（防止挂死连接长期占用）。
STREAM_TIMEOUT_S = 600.0


class QoderProvider(BaseProvider):
    """Qoder（Qwen3.8 / DeepSeek / GLM / Kimi …）通道。"""

    id = "qoder"
    name = "Qoder"
    # 每日活动权益（「每天领 100 Credits」）走 /sash 面，见 qoder/campaigns.py。
    supports_checkin = True

    def __init__(self, region: Region | None = None) -> None:
        self._region = with_cached_endpoints(region or resolve_region())
        self._catalog = Catalog(self._region)
        self._cred: Credential | None = None

    # -- 认证 ---------------------------------------------------------------

    def region(self) -> Region:
        """当前区域（含从桌面端缓存覆盖的端点）。"""
        return self._region

    def ensure_auth(self) -> None:
        """启动时校验凭证；缺失/不可用抛 401。"""
        try:
            self._cred = ensure_credential_sync(self._region)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    async def _credential(self) -> Credential:
        """取（必要时刷新的）凭据。"""
        try:
            self._cred = await ensure_credential(self._region)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return self._cred

    # -- 模型 ---------------------------------------------------------------

    def models(self) -> Sequence[dict[str, Any]]:
        """同步返回模型列表（用兜底目录；异步刷新见 ``refresh_models``）。

        旧模型（:data:`catalog.HIDDEN_KEYS`）只列表不展示——列表太长反而找不到
        要用的那几个。隐藏 ≠ 停用：直接点名仍可调用，只是不列出来。
        """
        entries = self._catalog._models or Catalog.fallback()
        return [to_openai_model(e, self.id) for e in entries if not is_hidden(e)]

    async def refresh_models(self, force: bool = False) -> list[dict[str, Any]]:
        """从上游刷新目录（供管理页「刷新模型」与转发前预热用）。"""
        try:
            cred = await self._credential()
        except HTTPException as exc:
            # 未登录时不该让管理页整体 500：回落到本地目录，把原因带回去。
            log.warning("qoder 目录刷新跳过（认证未就绪）: %s", exc.detail)
            entries = Catalog.fallback()
        else:
            entries = await self._catalog.fetch(cred, force=force)
        return [to_openai_model(e, self.id) for e in entries if not is_hidden(e)]

    def resolve_model(self, model: str) -> str:
        """把显示名/大小写变体归一成上游 key。"""
        return self._catalog.resolve_key(model)

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        """目录别名（显示名/大小写变体）也要能被自动路由命中。

        Qoder 的目录 key 是内部代号（``qfmodel``），客户端常直接发显示名
        （``Qwen3.8-Flash``）——只比 id 会漏配，请求掉进兜底通道后被上游拒成
        「模型不存在」。

        ``aliases=False``（路由第一轮）**只认本通道自己发布的 id**（裸名形态，
        即 :data:`catalog.MODEL_IDS` 的值）：``qwen3.8-flash`` / ``glm-5.3`` 等。

        为什么第一轮不能直接调基类 ``accepts_model``：基类把「剥前缀裸名与请求
        名相等」也算精确命中，而 Qoder 的裸名里有 ``auto``、``glm-5.3``、
        ``glm-5.3-flash`` —— 前一个是 CodeBuddy 静态表的默认模型，后两个是 zcode
        按 id 发布的模型名。它们对 Qoder 而言**只是别名**（上游叫 ``gmodel`` /
        ``gfmodel``），让它们在精确轮命中就会把别人的模型抢走：实测 ``auto`` 被
        路由到 qoder 并真的走通了（CodeBuddy 的默认模型静默改道）。所以精确轮
        只比对**本通道 id 集合**，其余一律留给别名轮兜底。
        """
        want = (model or "").strip()
        if not want:
            return False
        # 名字若同时属于 CodeBuddy 静态表（如 ``auto``），**两个轮次都让开**。
        # 别名轮那道 ``_is_codebuddy_model`` 守卫管不到精确轮，而基类会把
        # ``qoder/auto`` 剥前缀后当成精确命中；Qoder 的档位 id 恰好就是 ``auto``，
        # 于是 ``auto``（CodeBuddy 的默认模型）被静默改道到本通道（实测确实发生
        # 了：provider_route 显示 qoder 服务了 auto）。文件名式判断统一走
        # ``_is_codebuddy_model``，静态表变了不用两边同步。
        # 大小写变体（``Auto`` / ``AUTO``）也一并让开：``auto`` 这个档位名本来
        # 就没有大小写语义，而 ``_is_codebuddy_model`` 是精确比对的（它必须如此，
        # 免得 ``Qwen3.8-Max`` 这类官方显示名被误挡）。显式 ``qoder/auto`` 走
        # 前缀路由，不经过本方法。
        if _is_codebuddy_owned(want):
            return False
        entries = self._catalog._models or Catalog.fallback()
        # 隐藏模型（旧模型）也放进精确集合：它们只是不出现在列表里，点名仍可调。
        exact_ids = {(public_model_id(e) or "").strip() for e in entries}
        exact_ids.discard("")
        if want in exact_ids:
            return True
        if not aliases:
            return False
        # ``resolve_key`` 对「已经是上游 key」与「认不出」两种输入都原样返回，
        # 单看 ``key == want`` 无法区分——只能直接查目录：归一结果确实存在，
        # 才认领；否则（真正的未知模型）放行给别人。
        key = self._catalog.resolve_key(want)
        return any(str(m.get("key") or "") == key for m in entries)

    # -- 每日活动权益（打卡） ------------------------------------------------

    async def _campaigns(self) -> CampaignClient:
        """构造活动面客户端（复用当前凭据）。"""
        cred = await self._credential()
        return CampaignClient(self._region, cred)

    async def checkin_status(self) -> dict[str, Any] | None:
        """查今日「活动权益」领取状态（``/sash/api/v1/me/campaigns``）。

        判据是**逐条**看 ``CLAIM_BENEFIT`` + ``CLAIMABLE``，不看顶层
        ``claimable``（它把「仅查看详情」的活动也算进来了，会把"无奖励"报成
        "可领"，导致自动打卡对着一条领不出东西的活动反复打）。

        每天是可领活动的**新 campaignId**（10:00 UTC+8 轮换），故这里永远
        重新拉列表、不跨天缓存。返回 ``None`` 以外的结构见
        ``BaseProvider.checkin_status``。
        """
        try:
            client = await self._campaigns()
            campaigns = await client.list()
        except Exception as exc:  # noqa: BLE001 - 状态查询失败不该让整页 500
            log.warning("qoder 活动状态查询失败: %s", exc)
            return {"checked_in": False, "claimable": False, "message": str(exc)[:200]}

        claimable = [c for c in campaigns if c.is_claimable]
        claimed = [c for c in campaigns if c.action_type == CLAIM_ACTION and c.is_claimed]
        today = claimable[0] if claimable else (claimed[0] if claimed else None)

        status: dict[str, Any] = {
            "checked_in": bool(claimed) and not claimable,
            "claimable": bool(claimable),
            "inactive": not claimable and not claimed,
            "streak_days": 0,
            "message": "",
        }
        if today is not None:
            status.update(
                {
                    "daily_credit": today.amount,
                    "benefit_kind": today.kind,
                    "activity_key": today.key,
                    "activity_name": today.key,
                    "campaign_id": today.id,
                    "ends_at": today.end_at or None,
                }
            )
            # 「下次」= 当前状态翻转的时刻，两种状态翻转点不同：
            # - 已领取：下一轮开始。窗口实测是 ``10:00:00 → 次日 09:59:00``，
            #   故 ``endAt + 60`` 正是下一轮的 10:00（上游不给未来那条，只能推）。
            # - 还没领：本轮**截止**。此刻用户该去点「立即打卡」而不是等，显示
            #   截止时间才有意义（错过就没了）；显示下一轮开始反而误导。
            if claimable:
                if today.end_at > 0:
                    status["next_ts"] = int(today.end_at)
                    status["next_ts_source"] = SOURCE_UPSTREAM
            else:
                window_next = next_from_window(today.start_at, today.end_at)
                if window_next is not None:
                    status["next_ts"] = window_next
                    status["next_ts_source"] = SOURCE_UPSTREAM
        if claimable:
            status["message"] = f"今日可领 {claimable[0].amount or ''} Credits".strip()
        elif claimed:
            status["message"] = "今日已领取"
        return status

    async def checkin_claim(self) -> dict[str, Any] | None:
        """领取今日活动 Credits（``POST .../{campaignId}/claim``）。

        **领取是幂等的**：对已领过的活动再 POST 返回 ``replayed: true``（且
        ``claimedAt`` 是过去那次的时间）——那是补记，不是新领取。返回值里用
        ``replayed`` 区分，``message`` 据实说明，避免把重放报成「刚领到 100」。
        """
        client = await self._campaigns()
        campaigns = await client.list()
        target = next((c for c in campaigns if c.is_claimable), None)
        if target is None:
            # 没有可领的：区分「今天已领过」与「今天本来就没有活动」。
            claimed = [c for c in campaigns if c.action_type == CLAIM_ACTION and c.is_claimed]
            if claimed:
                return {
                    "checked_in": True,
                    "claimable": False,
                    "message": "今日已领取（无需重复领取）",
                    "activity_key": claimed[0].key,
                }
            return {
                "checked_in": False,
                "claimable": False,
                "inactive": True,
                "message": "当前没有可领取的活动",
            }

        result = await client.claim(target.id)
        if not result.ok:
            raise RuntimeError(
                result.message or result.error_code or f"领取失败（status={result.status}）"
            )
        return {
            "checked_in": True,
            "claimable": False,
            "replayed": result.replayed,
            # 重放时并没有新发奖，不给积分数字，免得上游打卡历史记成「今天领了」。
            "extra_credits": None if result.replayed else target.amount,
            "activity_key": target.key,
            "grant_id": result.grant_id,
            "claimed_at": result.claimed_at,
            "message": (
                "该活动此前已领取（重放），本次未新发 Credits"
                if result.replayed
                else f"已领取 {target.amount or ''} Credits".strip()
            ),
        }

    # -- 额度 ---------------------------------------------------------------

    async def quota(self) -> dict[str, Any] | None:
        """查额度（``/api/v2/quota/usage``）。

        Qoder 的 ``userQuota`` 在部分账号（个人版）恒为 0，真实余额在
        ``addOnQuota``——因此取「total 更大的一侧」作为展示口径。
        """
        cred = await self._credential()
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    self._region.quota_url(),
                    headers={
                        "Authorization": f"Bearer {cred.token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            log.warning("qoder 额度查询失败: %s", exc)
            return None
        if resp.status_code != 200:
            log.warning("qoder 额度查询 HTTP %s: %s", resp.status_code, resp.text[:160])
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        out = self._format_quota(data, cred)
        # 账号套餐名来自额度接口（``userType``），顺手回填到内存凭据与状态文件，
        # 让 /health、鉴权面板不必再单独查一次。
        tier = str(data.get("userType") or "")
        if tier and cred.plan != tier:
            cred.plan = tier
            try:
                from .credentials import _persist

                _persist(cred)
            except Exception as exc:  # noqa: BLE001 - 回填失败不影响额度展示
                log.debug("qoder 套餐名回填失败: %s", exc)
        return out

    def _account_info(self) -> dict[str, Any]:
        cred = self._cred
        if cred is None:
            return {}
        return {
            "uid": cred.uid,
            "name": cred.name,
            "email": cred.email,
            "region": self._region.key,
            "region_label": self._region.label,
            "plan": cred.plan,
            "source": cred.source,
            "expires_at_ms": cred.expires_at_ms,
        }

    def _format_quota(self, data: dict[str, Any], cred: Credential) -> dict[str, Any]:
        """上游额度 -> 管理页统一结构。"""
        user_q = data.get("userQuota") or {}
        addon_q = data.get("addOnQuota") or {}

        def _total(node: dict) -> float:
            try:
                return float(node.get("total") or 0)
            except (TypeError, ValueError):
                return 0.0

        # 个人版 userQuota 常为 0，真实额度在 addOnQuota；取有额度的那一侧。
        primary, label = (addon_q, "加油包") if _total(addon_q) >= _total(user_q) else (user_q, "订阅额度")
        total = _total(primary)
        try:
            used = float(primary.get("used") or 0)
        except (TypeError, ValueError):
            used = 0.0
        try:
            remaining = float(primary.get("remaining") or 0)
        except (TypeError, ValueError):
            remaining = max(total - used, 0.0)
        percent = _used_percent(primary, used, total)

        def _item(node: dict, name: str, reset_ts: int | None = None) -> dict[str, Any]:
            total_v = _total(node)
            try:
                used_v = float(node.get("used") or 0)
            except (TypeError, ValueError):
                used_v = 0.0
            try:
                remain_v = float(node.get("remaining") or 0)
            except (TypeError, ValueError):
                remain_v = max(total_v - used_v, 0.0)
            return {
                "label": name,
                "used": round(used_v, 4),
                "total": round(total_v, 4),
                "remaining": round(remain_v, 4),
                "percent": round(_used_percent(node, used_v, total_v), 4),
                # 专属包自带过期时间（比账号级的更早），不传则用账号级 expiresAt
                "reset_ts": reset_ts if reset_ts is not None else _reset_ts(data),
            }

        # 有额度的一侧排前面（个人版 userQuota 常为 0，主力额度在 addOnQuota），
        # 但另一侧只要非零就也展示——订阅额度与加油包可以并存。
        items = [_item(primary, label)]
        other_node, other_label = (user_q, "订阅额度") if label == "加油包" else (addon_q, "加油包")
        if _total(other_node) > 0:
            items.append(_item(other_node, other_label))

        # 专属资源包（活动赠送，如「Qwen 专属积分」）：与订阅额度/加油包**并存**，
        # 是账号总额度的一部分。漏掉它会让管理页显示的积分比实际少一截
        # （实测 personal_professional 账号：userQuota 2000 + 专属包 2000，
        # 只读前两个节点就只显示 2000，用户以为额度对不上）。
        for pkg in data.get("dedicatedResourcePackages") or []:
            if not isinstance(pkg, dict):
                continue
            if not _pkg_active(pkg):
                continue  # 已失效/过期的包不占额度，不展示
            items.append(_item(
                pkg, _pkg_label(pkg), reset_ts=_reset_ts(pkg),
            ))

        return {
            "level": data.get("userType") or cred.plan or None,
            "usage_type": data.get("usageType") or "credits",
            "quota_exceeded": bool(data.get("isQuotaExceeded")),
            "total_percent": data.get("totalUsagePercentage"),
            "upgrade_url": data.get("upgradeUrl"),
            "region": self._region.key,
            "items": items,
            # 上面三项（订阅额度 / 加油包 / 专属积分，各自又是列表里的一条）是
            # **并存的份额**，加起来才是账号剩余总量——管理页标题行据此求和，
            # 而不是只显示第一条（否则「剩 1621/2000」会漏掉加油包与专属积分，
            # 用户看到的就是比实际少的数）。上游自己也这么算：实测三项合计
            # 已用 37.5%，上游 totalUsagePercentage 正好是 0.38。
            "sum_items": True,
            "account": self._account_info(),
        }

    # -- 健康 ---------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        cred = self._cred
        info: dict[str, Any] = {
            "provider": self.id,
            "region": self._region.key,
            "region_label": self._region.label,
            "base_url": self._region.infer_base,
            "endpoint_type": "cosy",
            "protocol": "openai-envelope",
            "cosy_version": COSY_VERSION,
            "models": len(self._catalog._models or Catalog.fallback()),
        }
        if cred is not None:
            info.update(
                {
                    "authenticated": True,
                    "account": cred.uid,
                    "plan": cred.plan or None,
                    "auth_source": cred.source,
                    "expires_at_ms": cred.expires_at_ms or None,
                }
            )
        else:
            info["authenticated"] = False
        return info

    # -- 转发 ---------------------------------------------------------------

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        """把 OpenAI chat 请求转发到 Qoder COSY 面。"""
        cred = await self._credential()
        want_stream = bool(body.get("stream", True))
        model = self.resolve_model(str(body.get("model") or "auto"))

        upstream = self._build_upstream(body, model)
        body_json = json.dumps(upstream, ensure_ascii=False, separators=(",", ":"))
        url = self._region.chat_url()
        enc_body, headers = sign(
            url,
            body_json,
            cred.uid,
            cred.token,
            cred.machine_id,
            name=cred.name,
            email=cred.email,
            model_key=model,
        )
        headers["Accept"] = "text/event-stream"

        client = httpx.AsyncClient(timeout=httpx.Timeout(STREAM_TIMEOUT_S, connect=20))
        try:
            req = client.build_request("POST", url, headers=headers, content=enc_body.encode())
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise _upstream_error(f"上游连接失败: {exc}") from exc

        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "replace")[:300]
            await resp.aclose()
            await client.aclose()
            message = f"HTTP {resp.status_code}: {text}"
            if resp.status_code in (401, 429):
                # 鉴权/限流是明确的、客户端可自行判断的错误，保持原样与状态码
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"qoder 上游 HTTP {resp.status_code}: {text}",
                )
            raise _upstream_error(message)

        if want_stream:
            # Anthropic 客户端（Claude Code 的 /v1/messages）不能收 OpenAI chunk：
            # 上游没有 Anthropic 原生端点，必须在这里把 OpenAI SSE 转成
            # message_start / content_block_delta / message_stop 事件流。
            # 不转的话 Claude Code 收到 200 却拿不到事件，报
            # 「Streaming response ended before any complete data was received」。
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import AnthropicStreamConverter
                stream: AsyncIterator[bytes] = _to_anthropic_stream(
                    self._stream(resp, client, model), model, AnthropicStreamConverter,
                )
            else:
                stream = self._stream(resp, client, model)
            return StreamingResponse(
                stream,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        payload = await self._collect(resp, client, model)
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message
            return JSONResponse(chat_completion_to_anthropic_message(payload, original))
        return JSONResponse(payload)

    # -- 出站体构造 ---------------------------------------------------------

    def _build_upstream(self, body: dict[str, Any], model: str) -> dict[str, Any]:
        """组装带归因信封的上游 body（明文）。"""
        upstream: dict[str, Any] = {
            "model": model,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        for field in _PASSTHROUGH_FIELDS:
            value = body.get(field)
            if value is not None:
                upstream[field] = value

        # developer -> system：上游在反序列化阶段整请求拒绝该 role。
        messages = upstream.get("messages")
        if isinstance(messages, list):
            upstream["messages"] = [_normalize_message(m) for m in messages]

        now_ms = int(time.time() * 1000)
        request_id = str(uuid.uuid4())
        request_set_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        prompt_text = _last_user_text(upstream.get("messages") or [])
        entry = self._catalog.entry(model)

        upstream.update(
            {
                "request_id": request_id,
                "request_set_id": request_set_id,
                "chat_record_id": request_id,
                "session_id": session_id,
                "chat_task": "FREE_INPUT",
                "chat_context": {
                    "text": prompt_text,
                    "features": [],
                    "extra": {
                        "context": [],
                        "modelConfig": {
                            "key": model,
                            "is_reasoning": bool(entry.get("is_reasoning")),
                        },
                        "originalContent": prompt_text,
                    },
                    "chatPrompt": "",
                    "imageUrls": None,
                },
                "is_reply": True,
                "is_retry": False,
                "source": 1,
                "version": "3",
                "agent_id": "agent_common",
                "task_id": "common",
                "session_type": "qoderclicn",
                "aliyun_user_type": "",
                "model_config": {
                    "key": model,
                    "display_name": str(entry.get("display_name") or model),
                    "model": "",
                    "format": "openai",
                    "is_vl": bool(entry.get("is_vl")),
                    "is_reasoning": bool(entry.get("is_reasoning")),
                    "api_key": "",
                    "url": "",
                    "source": "system",
                    "max_input_tokens": entry.get("max_input_tokens") or 128000,
                },
                "business": {
                    "product": "cli",
                    "version": COSY_VERSION,
                    "type": "agent",
                    "id": request_set_id,
                    "name": prompt_text[:10],
                    "begin_at": now_ms,
                    "stage": "processing",
                },
            }
        )
        return upstream

    # -- 入站解析 -----------------------------------------------------------

    async def _stream(
        self,
        resp: httpx.Response,
        client: httpx.AsyncClient,
        model: str,
    ) -> AsyncIterator[bytes]:
        """拆 SSE 信封，把内层 OpenAI chunk 原样下游。"""
        started = time.time()
        try:
            async for line in resp.aiter_lines():
                if time.time() - started > STREAM_TIMEOUT_S:
                    log.warning("qoder 流超时（%.0fs），中止", STREAM_TIMEOUT_S)
                    break
                raw = line.strip()
                if not raw or raw.startswith("event:"):
                    continue
                if not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                inner, error, done = _unwrap(payload)
                if error is not None:
                    log.warning("qoder 上游带内错误 (%s): %s", model, error)
                    yield _sse_error(error)
                    yield b"data: [DONE]\n\n"
                    return
                if done:
                    yield b"data: [DONE]\n\n"
                    return
                if inner is not None:
                    yield f"data: {inner}\n\n".encode()
        except httpx.HTTPError as exc:
            log.warning("qoder 流中断: %s", exc)
            yield _sse_error(f"上游流中断: {exc}")
            yield b"data: [DONE]\n\n"
        finally:
            await resp.aclose()
            await client.aclose()

    async def _collect(
        self,
        resp: httpx.Response,
        client: httpx.AsyncClient,
        model: str,
    ) -> dict[str, Any]:
        """非流式：聚合内层 chunk 成一个 ``chat.completion``。"""
        created = int(time.time())
        content: list[str] = []
        reasoning: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        tool_calls: dict[int, dict[str, Any]] = {}
        error: str | None = None
        try:
            async for line in resp.aiter_lines():
                raw = line.strip()
                if not raw.startswith("data:"):
                    continue
                inner, err, done = _unwrap(raw[5:].strip())
                if err is not None:
                    error = err
                    break
                if done:
                    break
                if inner is None:
                    continue
                try:
                    chunk = json.loads(inner)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    choice = choices[0] or {}
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        content.append(str(delta["content"]))
                    if delta.get("reasoning_content"):
                        reasoning.append(str(delta["reasoning_content"]))
                    for call in delta.get("tool_calls") or []:
                        idx = int(call.get("index") or 0)
                        slot = tool_calls.setdefault(
                            idx, {"id": "", "type": "function",
                                  "function": {"name": "", "arguments": ""}}
                        )
                        if call.get("id"):
                            slot["id"] = call["id"]
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += str(fn["arguments"])
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                if chunk.get("usage"):
                    usage = chunk["usage"]
        except httpx.HTTPError as exc:
            error = f"上游流中断: {exc}"
        finally:
            await resp.aclose()
            await client.aclose()

        if error is not None:
            raise _upstream_error(error)

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason or "stop",
                }
            ],
            "usage": usage
            or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


# ---------------------------------------------------------------------------
# 模块级辅助
# ---------------------------------------------------------------------------


async def _to_anthropic_stream(
    openai_stream: AsyncIterator[bytes],
    model: str,
    converter_cls: Any,
) -> AsyncIterator[bytes]:
    """OpenAI chat chunk SSE 流 -> Anthropic Messages 事件流。

    ``openai_stream`` 是本 provider 已拆掉 COSY 外壳的 OpenAI SSE 字节流。
    转换器复用 ``anthropic_adapter.AnthropicStreamConverter``（与 trae/mimo
    同一个），因此 reasoning_content -> thinking 块、tool_calls -> tool_use
    块的行为与其它通道一致。

    转换途中任何异常都**先收尾再抛**：``feed_chunk`` 对畸形 chunk 会抛，
    不兜的话客户端拿到「内容块悬空、没有 message_stop」的残流，比直接报错
    更难排查。
    """
    converter = converter_cls(model)

    def _emit(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def _close_open() -> list[str]:
        try:
            return [_emit(n, p) for n, p in converter.close_open_blocks()]
        except Exception:  # noqa: BLE001 - 收尾失败不能盖掉原错误
            return []

    def _abort(msg: str) -> list[str]:
        out = _close_open()
        out.append(_emit("error", {"type": "error",
                                   "error": {"type": "api_error", "message": msg}}))
        return out

    buffer = ""
    try:
        async for raw in openai_stream:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            buffer += text
            # 按 SSE 帧切：只处理完整的 ``data: ...\n\n``，最后一段留在 buffer。
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for line in frame.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    if chunk.get("error"):
                        err = chunk["error"]
                        msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
                        for event in _abort(msg):
                            yield event
                        return
                    for name, payload in converter.feed_chunk(chunk):
                        yield _emit(name, payload)
        for name, payload in converter.finish():
            yield _emit(name, payload)
    except Exception as exc:  # noqa: BLE001 - 上游畸形数据不该让客户端只收到半截流
        log.warning("qoder anthropic 流中断: %s: %s", type(exc).__name__, exc)
        for event in _abort(f"{type(exc).__name__}: {exc}"):
            yield event
        return
    yield "data: [DONE]\n\n"


def _unwrap(payload: str) -> tuple[str | None, str | None, bool]:
    """拆一层 SSE 信封。

    返回 ``(inner_json | None, error | None, done)``：

    - ``event:finish`` 之类的尾帧（无 ``body``）-> ``(None, None, False)``
    - ``body == "[DONE]"`` -> ``(None, None, True)``
    - 带内业务错误（``code``/``message`` 且无 ``choices``/``usage``）-> 错误
    """
    if payload == "[DONE]":
        return None, None, True
    try:
        frame = json.loads(payload)
    except ValueError:
        return None, None, False
    if not isinstance(frame, dict):
        return None, None, False
    body = frame.get("body")
    if not isinstance(body, str):
        # 尾帧（计时统计）等：无 body，直接忽略
        return None, None, False
    if body == "[DONE]":
        return None, None, True
    try:
        chunk = json.loads(body)
    except ValueError:
        return None, f"上游返回非法 JSON: {body[:200]}", False
    if not isinstance(chunk, dict):
        return None, None, False
    if "choices" not in chunk and "usage" not in chunk and (
        chunk.get("code") is not None or isinstance(chunk.get("message"), str)
    ):
        return None, _describe_upstream_error(chunk), False
    return body, None, False


def _normalize_message(message: Any) -> Any:
    """单条消息的上游适配（Qoder 专属，不改公共转换器）。

    三条上游硬性要求，实测（2026-09）：

    1. **``developer`` role 整请求被拒**（反序列化阶段就挂），转 ``system``。
    2. **带 ``tool_calls`` 的消息，``content`` 不能是 ``null``**。
       Anthropic 的 ``tool_use`` only 回合转出来正是 ``content: null``，
       上游会拒单——而且**报错文案误导**：它说「role 'tool' 必须回应带
       tool_calls 的消息」，害得往 tool 配对方向排查。实际把它改成 ``""``
       即可通过（``content=""`` 实测 200）。这条**不绑 role**：绑了
       ``assistant`` 的话，``developer`` 那条先被改成 ``system`` 就永远命中
       不了（而且必须在摘 ``tool_calls`` 之前做，否则条件同样不成立）。
    3. **``tool_calls`` 只能挂在 ``assistant`` 上**。``system`` 带 ``tool_calls``
       一样被那句误导文案拒掉（实测：``system`` + ``content:""`` 仍 ❌，
       ``assistant`` + ``content:""`` ✅）——因为其后的 ``tool`` 没有
       ``assistant`` 可配对。所以 ``developer`` 转 ``system`` 时要把
       ``tool_calls`` 摘掉（系统消息本就不该发起工具调用，摘掉不丢信息）。

    这三条只影响本通道：其它 provider 共用同一个转换器，不能在那里改。
    """
    if not isinstance(message, dict):
        return message
    out = message
    # ⚠️ content 的修正必须排在摘 ``tool_calls`` **之前**（见 ``developer`` 分支）：
    # 一旦先摘掉 tool_calls，下面「有没有 tool_calls」就再也不成立，
    # ``content: null`` 会原样出站。
    if out.get("tool_calls") and out.get("content") is None:
        out = {**out, "content": ""}
    if out.get("role") == "developer":
        # role 改成 system，同时摘掉不可能属于系统消息的 tool_calls
        out = {k: v for k, v in out.items() if k != "tool_calls"}
        out["role"] = "system"
    return out


def _describe_upstream_error(chunk: dict[str, Any]) -> str:
    """带内错误帧 -> 可读原因。

    上游把真正的失败原因放在 ``details`` 里（JSON 字符串），顶层 ``message``
    只有一句没用的 ``Error in upstream response``。只取 ``message`` 会让
    「模型不存在」「参数非法」「渠道校验拦截」全都退化成同一句话，线上只能
    靠猜——所以这里把 ``details.error.message`` 一并挖出来。
    """
    parts = [str(chunk.get("message") or "")] if chunk.get("message") else []
    code = chunk.get("code")
    if code:
        parts.append(f"code={code}")
    details = chunk.get("details")
    detail_msg = ""
    if isinstance(details, str) and details.strip():
        try:
            parsed = json.loads(details)
        except ValueError:
            detail_msg = details.strip()
        else:
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict):
                detail_msg = str(err.get("message") or "")
            elif isinstance(parsed, dict):
                detail_msg = str(parsed.get("message") or "")
    elif isinstance(details, dict):
        err = details.get("error")
        detail_msg = str((err or {}).get("message") or "") if isinstance(err, dict) else ""
    if detail_msg:
        parts.append(detail_msg)
    return " | ".join(p for p in parts if p)[:500] or "上游返回未知错误"


def _sse_error(message: str) -> bytes:
    """构造 OpenAI 风格的 SSE 错误帧。"""
    payload = json.dumps({"error": {"message": message, "type": "upstream_error"}},
                         ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


def _upstream_error(error: str) -> HTTPException:
    """上游带内错误 -> HTTPException（502）。

    Anthropic 客户端（Claude Code）只认 ``{"type":"error","error":{...}}``，
    收到我们原来的 ``{"detail": ...}`` 会把它当**未知可重试错误**，于是对着
    同一个请求重试到上限（线上表现为 ``Retrying in 15s · attempt 7/10``）。
    这里补上 Anthropic 形状，让它能正确识别并停止无谓重试。
    """
    return HTTPException(
        status_code=502,
        detail={
            "type": "error",
            "error": {"type": "api_error", "message": f"qoder 上游错误: {error}"},
        },
    )


def _last_user_text(messages: list[Any]) -> str:
    """取最后一条 user 消息的纯文本（多模态时拼接 text part）。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        return ""
    return ""


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


def ensure_credential_sync(region: Region) -> Credential:
    """同步取凭据（只读，不刷新）——供 ``ensure_auth`` 启动校验用。"""
    from .credentials import resolve_credential

    return resolve_credential(region)
