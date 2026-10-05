"""TraeProvider：OpenAI/Anthropic 兼容入口，编排原生通道与文本协议兜底。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, AsyncIterator, Sequence

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..core.checkin import SOURCE_INFERRED, next_daily_reset
from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message
from ..providers.base import BaseProvider
from .benefits_api import claim_checkin_credits, fetch_checkin_status, fetch_ent_usage
from .config import (
    BASE_URL_CN,
    MODEL_CREDITS,
    MODEL_MAP,
    MODEL_SUPPORTS_IMAGES,
    MODEL_TIERS,
    TRAE_HEARTBEAT_INTERVAL,
    TRAE_SEMANTIC_TIMEOUT,
    _NATIVE_TOOLS_ENABLED,
    _debug_dump,
)
from . import failover
from .credentials import (
    _auth,
    ensure_account_token,
    list_accounts,
    load_account_cred,
    set_current_work_account,
)
from .leak_guard import _StreamLeakCleaner, _sanitize_agent_leak
from .native_tools import (
    _NativeToolAccumulator,
    _native_messages,
    _native_rejected,
    _send_native_chat,
)
from .sse import _parse_sse, _trae_error_text, _wrap_anthropic_stream
from .text_protocol import _extract_prompt, _looks_like_agent_request
from .text_toolcall import _StreamToolCallSplitter, _parse_tool_calls, _tool_names
from .transport import send_trae_chat

log = logging.getLogger(__name__)

#: 多账号额度并发查询：整轮 deadline + 常驻线程池（与 antigravity/qoder 同口径）。
#: 常驻（不是每轮新建）的理由见 trae/pat/quota.py：每轮新建 + shutdown(wait=False)
#: 会让慢轮线程留在后台累积；常驻池上限封顶，慢轮占名额、后续轮次自然排队。
_QUOTA_ROUND_DEADLINE_S = 8.0
_QUOTA_WORKERS = 4
_quota_pool: "concurrent.futures.ThreadPoolExecutor | None" = None
_quota_pool_lock = threading.Lock()


def _quota_executor() -> "concurrent.futures.ThreadPoolExecutor":
    global _quota_pool
    with _quota_pool_lock:
        if _quota_pool is None:
            _quota_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=_QUOTA_WORKERS, thread_name_prefix="trae-quota")
        return _quota_pool


class TraeProvider(BaseProvider):
    id = "trae"
    name = "Trae (本地解密直连)"
    # 打卡/积分 API 只有 Trae 上游提供（/ui 自动打卡据此识别）
    supports_checkin = True

    def __init__(self, base_url: str | None = None, edition: str = "cn"):
        self._base_url = base_url or BASE_URL_CN
        self._edition = edition

    def models(self) -> Sequence[dict[str, Any]]:
        result = []
        seen = set()
        for tier, models in MODEL_TIERS.items():
            for m in models:
                if m in seen:
                    continue
                seen.add(m)
                result.append({
                    "id": m,
                    "object": "model",
                    "created": 0,
                    "owned_by": self.id,
                    "tier": tier,
                    "credits": MODEL_CREDITS.get(m),
                    # 图片能力：与 CodeBuddy 通道同口径（供 /v1/models 的
                    # input_modalities 判定），漏报会让客户端误剥图片
                    "images": m in MODEL_SUPPORTS_IMAGES,
                    "description": f"Trae {tier} 模型",
                })
        # 加别名（外部名映射）——倍率与图片能力均跟随映射到的内部模型
        for external, internal in MODEL_MAP.items():
            if external not in seen:
                seen.add(external)
                result.append({
                    "id": external,
                    "object": "model",
                    "created": 0,
                    "owned_by": self.id,
                    "maps_to": internal,
                    "credits": MODEL_CREDITS.get(internal),
                    "images": internal in MODEL_SUPPORTS_IMAGES,
                    "description": f"Trae 别名 -> {internal}",
                })
        return result

    def ensure_auth(self) -> None:
        _auth()

    def _send_native_request(
        self,
        native_msgs: list[dict[str, Any]],
        model: str,
        stream: bool,
        tools: list[dict[str, Any]] | None,
    ) -> str:
        """Provider 级发送钩子；PAT 子类覆盖后不会因重叠模型串到个人账号。"""
        return _send_native_chat(native_msgs, model, stream, tools)

    def _keeps_native_error(self, model: str) -> bool:
        """PAT 子类覆写为 True，避免把其真实错误回落到个人文本通道。"""
        return False

    def _uses_native_mode(self) -> bool:
        """是否使用原生传输；PAT 子类始终启用以隔离账号体系。"""
        return _NATIVE_TOOLS_ENABLED

    def _stream_native_events(
        self,
        native_msgs: list[dict[str, Any]],
        model: str,
        tools: list[dict[str, Any]] | None,
        stop: threading.Event,
    ):
        """PAT 子类覆盖为增量事件源；个人通道继续沿用整段兼容路径。"""
        return None

    # ---- 打卡 / 额度（/ui 管理页消费，均经 asyncio.to_thread 调用） ----

    def _work_accounts(self) -> list[Any]:
        """遍历用的 work 账号列表（按 failover 顺位，剔除冷却中的）。

        PAT 子类（``_pat_variant``）不经过 work 多账号体系——签到/额度是
        个人账号专属，PAT 的 ``supports_checkin=False`` 本就挡住 UI 调度，
        这里再兜一层：PAT 返回空列表，调用方各自退化为单账号（拿首个可用
        work 账号）或返回 None（与改造前 PAT 行为一致）。
        """
        if self._is_pat_variant():
            return []
        return failover.available_accounts()

    def checkin_status(self) -> dict[str, Any] | None:
        """查今日签到状态。多账号**都查**，聚合：任一账号可领→可领；
        全部已签→已签；个别账号失败只在日志记、不阻塞整页（与 qoder 一致）。"""
        accounts = self._work_accounts()
        if not accounts:
            # 单账号（legacy 迁移前 / PAT 兜底）或全部在冷却：走首个可用账号
            try:
                data = fetch_checkin_status()
            except Exception as e:  # noqa: BLE001 — 状态查询失败不该让整页 500
                log.warning("trae 签到状态查询失败: %s", e)
                return {"checked_in": False, "claimable": False, "message": str(e)[:200]}
            return self._checkin_status_one(data, label="", multi=False)

        if len(accounts) == 1:
            return self._checkin_status_for_account(accounts[0], index=1, multi=False)

        # 多账号：并查，聚合任一可领 / 全部已签；per-account 明细放 ``accounts``
        # （管理页签到卡逐账号渲染「#N 已签 / 可领 / 查询失败」）。
        claimable_accts: list[str] = []
        signed_accts: list[str] = []
        failed_accts: list[str] = []
        acct_details: list[dict[str, Any]] = []
        enabled_any = False
        for i, acct in enumerate(accounts, 1):
            name = acct.nickname or acct.uid or acct.id
            st = self._checkin_status_for_account(acct, index=i, multi=True)
            if st is None:
                failed_accts.append(f"#{i}")
                acct_details.append({"index": i, "name": name, "error": "查询失败"})
                continue
            if not st.get("inactive"):
                enabled_any = True
            if st.get("claimable"):
                claimable_accts.append(f"#{i}")
            elif st.get("checked_in"):
                signed_accts.append(f"#{i}")
            acct_details.append({
                "index": i,
                "name": name,
                "checked_in": bool(st.get("checked_in")),
                "claimable": bool(st.get("claimable")),
                "inactive": bool(st.get("inactive")),
            })
        status: dict[str, Any] = {
            "checked_in": bool(signed_accts) and not claimable_accts,
            "claimable": bool(claimable_accts),
            "inactive": not enabled_any and not claimable_accts and not signed_accts,
            "message": "",
            "accounts": acct_details,
        }
        if claimable_accts:
            status["message"] = f"账号 {'、'.join(claimable_accts)} 可领"
        elif signed_accts:
            status["message"] = f"账号 {'、'.join(signed_accts)} 已签"
        if failed_accts:
            status["message"] = (status["message"] + " " if status["message"] else "") + \
                f"（{len(failed_accts)}/{len(accounts)} 个账号查询失败）"
        if enabled_any:
            status["next_ts"] = next_daily_reset()
            status["next_ts_source"] = SOURCE_INFERRED
        return status

    def _checkin_status_for_account(
            self, acct: Any, *, index: int, multi: bool) -> dict[str, Any] | None:
        """单账号签到状态查询。返回 None 表示该账号查询失败（网络/token）。"""
        try:
            token, _cred = ensure_account_token(acct.id)
            data = fetch_checkin_status(token=token, account_id=acct.id)
        except Exception as e:  # noqa: BLE001 — 单账号失败不该让整页 500
            log.warning("trae 签到状态查询失败（%s）: %s", acct.id, e)
            return None
        label = f"Trae #{index} · " if multi else ""
        return self._checkin_status_one(data, label=label, multi=multi)

    def _checkin_status_one(
            self, data: dict[str, Any], *, label: str, multi: bool) -> dict[str, Any]:
        """把上游 ``/ug/checkin_credits/status`` 原始响应收拢成统一签到状态。"""
        checked_in = bool(data.get("checked_in"))
        enabled = bool(data.get("enable", True))
        status: dict[str, Any] = {
            "checked_in": checked_in,
            "claimable": enabled and not checked_in,
            "inactive": not enabled,
            "message": data.get("message", ""),
        }
        # trae 的 ``/ug/checkin_credits/status`` 返回里**没有任何时间字段**
        # （实测只有 checked_in / enable / credits / message），连档期都不给。
        # 轮换时刻只能按 logs/checkin.jsonl 反推的本地零点算（09-27 01:56、
        # 02:20 这类凌晨领取也被记为新一天 → 零点轮换），故标 inferred。
        # 活动未开（enable=false）时不给：那会儿连有没有下一轮都不知道。
        if enabled:
            status["next_ts"] = next_daily_reset()
            status["next_ts_source"] = SOURCE_INFERRED
        return status

    def checkin_claim(self) -> dict[str, Any] | None:
        """领取今日签到积分。多账号**每个都领**（用户决策），逐个尝试、
        各自容错：单个账号失败（网络/token/已领过）不阻塞其它账号。汇总
        ``extra_credits`` 为各账号实领之和，``message`` 据实说明每个账号结果。"""
        accounts = self._work_accounts()
        if not accounts:
            # 单账号 legacy 兜底（PAT 由 supports_checkin=False 挡住 UI 调度，
            # 走不到这里；即便走到也退回原单账号语义）
            data = claim_checkin_credits()
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or json.dumps(data, ensure_ascii=False)[:200])
            return {
                "checked_in": True,
                "extra_credits": data.get("credits_granted", data.get("extra_credits")),
                "message": data.get("message", ""),
            }
        if len(accounts) == 1:
            # 单账号直通：不带 accounts 明细（与 checkin_status 的单账号分支对称，
            # 徽标本身就是它的状态，逐账号展开纯属噪音）
            acct = accounts[0]
            try:
                token, _cred = ensure_account_token(acct.id)
                data = claim_checkin_credits(token=token, account_id=acct.id)
            except Exception as e:  # noqa: BLE001 — 与多账号循环同款容错
                log.warning("trae 签到领取失败（%s）: %s", acct.id, e)
                raise RuntimeError(str(e)[:200]) from e
            if data.get("code") not in (0, None):
                raise RuntimeError(data.get("message") or json.dumps(data, ensure_ascii=False)[:200])
            granted = data.get("credits_granted", data.get("extra_credits"))
            return {
                "checked_in": True,
                "extra_credits": granted,
                "message": data.get("message", ""),
            }

        total_credits: float = 0.0
        any_claimed = False
        messages: list[str] = []
        acct_details: list[dict[str, Any]] = []
        for i, acct in enumerate(accounts, 1):
            name = acct.nickname or acct.uid or acct.id
            tag = f"#{i}"
            try:
                token, _cred = ensure_account_token(acct.id)
                data = claim_checkin_credits(token=token, account_id=acct.id)
                if data.get("code") in (0, None):
                    any_claimed = True
                    granted = data.get("credits_granted", data.get("extra_credits"))
                    if isinstance(granted, (int, float)):
                        total_credits += float(granted)
                    messages.append(f"{tag} 已领 {granted or ''}".strip())
                    acct_details.append({
                        "index": i, "name": name, "ok": True,
                        "credits": granted if isinstance(granted, (int, float)) else None,
                        "message": str(data.get("message") or "")[:80],
                    })
                else:
                    msg = str(data.get("message"))[:60]
                    messages.append(f"{tag} 失败：{msg}")
                    acct_details.append({"index": i, "name": name, "ok": False, "message": msg})
            except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞其它账号
                log.warning("trae 签到领取失败（%s）: %s", acct.id, e)
                msg = str(e)[:60]
                messages.append(f"{tag} 失败：{msg}")
                acct_details.append({"index": i, "name": name, "ok": False, "message": msg})
        return {
            "checked_in": any_claimed,
            "extra_credits": total_credits if any_claimed else None,
            "message": "；".join(messages) or "没有可领取的账号",
            "accounts": acct_details,
        }

    def quota(self) -> dict[str, Any] | None:
        """查额度（``/ug/usage``），多账号并发。多账号时各账号条目带
        ``Trae #N · `` 前缀供前端分组；个别账号失败插 ``query_failed``
        说明条（benefits 层认这个标记走短缓存）。"""
        accounts = self._work_accounts()
        if not accounts:
            # 单账号 legacy 兜底（迁移前 / PAT）：保持原行为
            try:
                data = fetch_ent_usage()
            except Exception as e:  # noqa: BLE001 — 额度查询失败不阻塞整页
                log.warning("trae 额度查询失败: %s", e)
                return None
            items = self._quota_items(data, label_prefix="")
            return {"items": items, "level": None}

        multi = len(accounts) > 1
        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, i + 1, multi=True)
                       for i, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failed: list[str] = []
            for acct, fut in zip(accounts, futures):  # 按 failover 顺位收集，UI 顺序稳定
                name = f"#{acct.priority + 1}"
                try:
                    its, ok = fut.result(timeout=max(deadline - time.monotonic(), 0.05))
                except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                    its, ok = [], False
                if not ok:
                    failed.append(name)
                else:
                    items.extend(its)
            if failed:
                items.insert(0, {
                    "label": "Trae 额度查询失败",
                    "used": None, "total": None,
                    "remaining": f"{len(failed)}/{len(accounts)} 个账号取不到额度"
                                 f"（{'、'.join(failed)}）",
                    "percent": None, "reset_ts": None,
                    "query_failed": True,
                })
        else:
            its, ok = self._quota_one(accounts[0], 1, multi=False)
            items = its if ok else []

        return {"items": items, "level": None}

    def quota_epoch(self) -> str:
        """quota 缓存代：账号列表一变（登录新号/删号/换顺位）旧快照就该作废。"""
        try:
            accts = list_accounts()
        except Exception:  # noqa: BLE001 - 拿不到就退回常量键
            return "unknown"
        return ",".join(f"{a.id}#{a.priority}" for a in accts) or "empty"

    def _quota_one(self, acct: Any, index: int, *, multi: bool
                   ) -> tuple[list[dict[str, Any]], bool]:
        """单账号额度查询：``(items, ok)``。同步跑在常驻线程池里。"""
        try:
            token, _cred = ensure_account_token(acct.id)
            data = fetch_ent_usage(token=token, account_id=acct.id)
        except Exception as e:  # noqa: BLE001 — 单账号失败不阻塞整页
            log.warning("trae 额度查询失败（%s）: %s", acct.id, e)
            return [], False
        prefix = f"Trae #{index} · " if multi else ""
        return self._quota_items(data, label_prefix=prefix), True

    def _quota_items(self, data: dict[str, Any], *, label_prefix: str) -> list[dict[str, Any]]:
        """把上游 ``/ug/usage`` 原始响应收拢成统一额度条目列表。"""
        us = data.get("usage_summary", {})
        items: list[dict[str, Any]] = []
        total, consumed = us.get("total_amount"), us.get("consumed_amount")
        if total is not None:
            ratio = us.get("consumption_ratio")
            percent = round(ratio * 100) if isinstance(ratio, (int, float)) else None
            remaining = None
            if isinstance(total, (int, float)) and isinstance(consumed, (int, float)):
                remaining = round(total - consumed, 2)
            items.append({"label": f"{label_prefix}总额度", "used": consumed, "total": total,
                          "remaining": remaining, "percent": percent,
                          # 总额度是所有包的合计，没有单一到期日——到期告警
                          # 由下面各权益包自己承担，合计行不参与
                          "reset_ts": None, "expire_ts": None, "unit": "credit",
                          # head_only：只在标题行「剩 X / Y」用它的数字，明细
                          # 列表不单列这一条（它是下面各权益包的合计，再铺一条
                          # 带进度条的明细行是重复——用户 2026-10-04 反馈）。
                          "head_only": True})
        # 权益包：每条都有额度与到期时间，全部展示（各条**不能相加**——它们
        # 是上面「总额度」的明细，加了就重复计算，故本通道不给 sum_items）。
        packs: list[dict[str, Any]] = []
        seen: set[tuple] = set()
        for p in data.get("user_entitlement_pack_list", []):
            eb = p.get("entitlement_base_info") or {}
            end_time = eb.get("end_time")
            if not end_time:
                continue
            desc = p.get("display_desc") or "权益包"
            limit = (eb.get("quota") or {}).get("credits_limit")
            # 去重按「名字 + 权益 id + 到期日」三元组，只防上游返回重复行。
            # **不能**只按名字去重：那样 25 条「签到奖励」会被合并成 1 条，
            # 12 条尚未消费的额度直接从界面上消失（2026-10-03 实测：升级前
            # 只显示前 3 条，其余 24 个包全被 `desc in seen` 加 `len>=3` 截掉）。
            key = (desc, str(eb.get("entitlement_id") or ""), int(end_time))
            if key in seen:
                continue
            seen.add(key)
            used: float | None = None
            limit_v: float | None = None
            if isinstance(limit, (int, float)) and limit > 0:
                limit_v = float(limit)
                amount = (p.get("usage") or {}).get("credits_amount")
                # ``credits_amount`` 是**已用**不是剩余——2026-10-03 实测交叉
                # 校验：Σlimit=9500.0、Σamount=6257.1772，Σlimit-Σamount 与
                # 接口自报 remaining（3242.82）差 0.00，而「amount=剩余」的
                # 假设差 3014.36。算反会把「剩 3242」显示成「剩 6257」。
                #
                # ``usage`` 缺失按 0 已用算（= 没花），**不是**「未知」：有
                # 记录的包已用合计 6257.1772 恰好等于 usage_summary 的
                # consumed_amount，且消耗严格按到期日 FIFO（先扣 4000 的会员
                # 包、再扣最早到期的签到包），未出现的包都是到期更晚、还没轮到
                # 的——真·满额。这与 ``trae/pat/quota.py`` 的相反先例不是一回
                # 事：那里是网关偶发只回半拉数据（包容量回来了、用量没回来），
                # 按 0 算会把未知说成满血；这里是接口语义，值为 0 就是没消费。
                used = round(float(amount), 2) if isinstance(amount, (int, float)) else 0.0
            packs.append({
                "label": f"{label_prefix}{desc}",
                "used": used,
                "total": round(limit_v, 2) if limit_v else None,
                "percent": round(used / limit_v * 100) if used is not None and limit_v else None,
                "remaining": round(limit_v - used, 2) if used is not None and limit_v else None,
                # 权益包只有到期、没有周期性重置，故 reset_ts 恒 None
                "reset_ts": None,
                "expire_ts": int(end_time),
                "unit": "credit",
            })
        packs.sort(key=lambda it: it["expire_ts"] or 0)  # 先到期的排前面
        items.extend(packs)
        return items

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        requested_model = body.get("model", "auto")
        messages = body.get("messages", [])
        stream = bool(body.get("stream", False))
        # coding agent 请求：不注入 guard、不清洗——下游自己解析工具调用语法
        agent_mode = _looks_like_agent_request(messages, body)
        _debug_dump(
            "debug_trae_route",
            model=requested_model,
            agent_mode=agent_mode,
            tools_count=len(body.get("tools") or []),
            stream=stream,
            message_count=len(messages),
        )

        tools = body.get("tools") or []
        # 原生通道：全部请求（含纯聊天）默认走 chat_v3 直通——该通道无 agent
        # 预设，纯聊天不再需要 guard 注入与泄漏清洗。上游 4001 拒绝时自动回落
        # 文本协议（prompt 始终照算，它就是兜底路径的输入：纯聊天带 guard、
        # 工具请求带教学）。
        native_mode = self._uses_native_mode()
        prompt = _extract_prompt(
            messages, guard=not agent_mode, tools=tools if agent_mode else None
        )
        if not prompt:
            raise HTTPException(status_code=400, detail="no text content")
        native = (
            {"messages": _native_messages(messages), "tools": tools}
            if native_mode else None
        )

        # ---- 多账号 failover：按顺位试可用 work 账号，账号级错误冷却换号 ----
        # PAT 子类（TraePatProvider）覆写了发送路径且有自己的账号体系，不该被卷
        # 进 work 多账号循环——直接单次转发（保持它原有的单账号语义）。
        if self._is_pat_variant():
            set_current_work_account(None)
            try:
                return await self._forward_once(
                    body, protocol, original, requested_model, prompt,
                    messages, tools, native, stream, agent_mode,
                )
            finally:
                set_current_work_account(None)

        accounts = failover.available_accounts()
        if not accounts:
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": (f"trae work 所有账号均在冷却中：{failover.cooldown_report()}"
                                "；额度冷却到点自动恢复"),
                    "type": "rate_limit_error"}})
        last_exc: BaseException | None = None
        try:
            for i, acct in enumerate(accounts):
                set_current_work_account(acct.id)
                try:
                    return await self._forward_once(
                        body, protocol, original, requested_model, prompt,
                        messages, tools, native, stream, agent_mode,
                    )
                except HTTPException as e:
                    last_exc = e
                    # 账号级错误（401 凭据失效 / 429 额度）且还有下一个账号：冷却换号；
                    # 其余错误（502 通道级/业务 4xx）换号无意义，直接透传。
                    if self._is_account_error(e) and i < len(accounts) - 1:
                        failover.mark_cooldown(
                            acct.id, quota=e.status_code == 429,
                            reason=f"HTTP {e.status_code}: {str(e.detail)[:120]}")
                        continue
                    raise
                finally:
                    set_current_work_account(None)
        except HTTPException as e:
            # 走到这是「最后一个账号也失败」或「非账号级错误透传」。anthropic 协议
            # 的错误体用标准形状（Claude Code 等 SDK 靠它渲染错误）；openai 协议
            # 维持「抛 HTTPException → 外层 exception handler 转 JSON」不变。
            if protocol == "anthropic":
                return JSONResponse(
                    status_code=e.status_code,
                    content={
                        "type": "error",
                        "error": {"type": "api_error", "message": str(e.detail)},
                    },
                )
            raise
        # 全部账号失败（循环正常跑完不该到这，兜底）
        raise last_exc or HTTPException(status_code=429, detail=failover.cooldown_report())

    def _is_pat_variant(self) -> bool:
        """PAT 子类标记：TraePatProvider 覆写 ``_pat_variant=True``（见
        pat_provider.py）。work 多账号 failover 只服务个人 work 通道。"""
        return bool(getattr(self, "_pat_variant", False))

    @staticmethod
    def _is_account_error(e: HTTPException) -> bool:
        """该 HTTPException 是否「换下一个账号可能好转」。

        401 = 凭据失效（换号有意义）；429 = 额度/限流（换号有意义）。502/504 是
        通道级（换号无意义，所有账号同网关）。其余业务 4xx 换号也无意义。
        """
        return e.status_code in (401, 429)

    async def _forward_once(
        self, body: dict[str, Any], protocol: str, original: dict[str, Any] | None,
        requested_model: str, prompt: Any, messages: list[dict[str, Any]],
        tools: list[dict[str, Any]], native: dict[str, Any] | None,
        stream: bool, agent_mode: bool,
    ) -> StreamingResponse | JSONResponse:
        """单账号实际转发（forward 的 failover 循环体）。"""
        if stream:
            include_usage = bool(
                (body.get("stream_options") or {}).get("include_usage"))
            # anthropic 协议没有 stream_options 字段，但 message_delta 需要
            # usage（Claude Code 靠它统计 token），这里强制向 _stream 索取
            if protocol == "anthropic":
                include_usage = True
            raw_gen = self._stream(prompt, requested_model,
                                   sanitize=not agent_mode, tools=tools or None,
                                   include_usage=include_usage, native=native)
            # 首事件闸门：预驱动**原始** _stream 到第一个非心跳事件再决定放行
            # 还是换号。trae 的 _stream 在等上游时会先吐「: heartbeat」保活——
            # 那是给下游超时看的，此刻 StreamingResponse 还没建、客户端一个字节
            # 都没收到，丢掉心跳无影响；第一个语义事件前的账号级错误（读线程透
            # 传）此刻抛出可安全换号（还没向客户端吐过内容，防重复计费）。见到
            # 语义事件即 committed，绝不重放。anthropic 包装在闸门之后（包住
            # 「缓冲 + 续跑」的拼接流）。
            gated = _gate_first_event(raw_gen)
            if isinstance(gated, BaseException):
                raise gated
            replay = _replay_prefixed(gated.buffered, gated.gen)
            gen = _wrap_anthropic_stream(replay, requested_model) \
                if protocol == "anthropic" else replay
            return StreamingResponse(
                _sync_to_async_iter(gen),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        # 非流式聚合内部是同步 urllib 调用（最长 180s），放线程池执行，
        # 避免阻塞事件循环拖垮所有并发请求。
        # 注意：这里的 HTTPException **必须 raise**（不能 return JSONResponse）——
        # forward 的 failover 循环靠捕获它来判断账号级错误（401/429）冷却换号；
        # 若在这里吞成返回值，撞错的账号不会被冷却、也不会换下一个账号。
        collected = await asyncio.to_thread(
            self._collect, prompt, requested_model, not agent_mode, tools or None,
            native,
        )
        if protocol == "anthropic":
            collected = chat_completion_to_anthropic_message(collected, original)
        return JSONResponse(content=collected)

    def _stream(
        self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
        tools: list[dict[str, Any]] | None = None,
        include_usage: bool = False,
        native: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """把 Trae SSE 转成 OpenAI chat.completion.chunk 流。"""
        request_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        usage: dict[str, Any] | None = None
        stop_signal: threading.Event | None = None
        last_semantic_at = time.monotonic()

        def chunk(delta: dict[str, Any], finish: str | None = None) -> str:
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def usage_chunk() -> str:
            # OpenAI 流式协议：stream_options.include_usage 时，[DONE] 前须有一个
            # choices 为空、只带 usage 的收尾 chunk。下游客户端（如 ethan）靠它
            # 判定 is_final——缺失会导致 final chunk 永远不到、tool_calls 整体丢失
            payload = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [],
                "usage": usage or {
                    "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                },
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def error_chunk(msg: str, code: Any = None) -> str:
            # 结构化错误 chunk（与 CodeBuddy 通道 stream_upstream 的错误格式一致）。
            # 不能塞进 content 文本——anthropic 客户端（Claude Code）会把
            # 伪正文当模型输出继续循环；包装层据此转成 event: error
            err: dict[str, Any] = {"message": msg, "type": "upstream_error"}
            if code is not None:
                err["code"] = code
            return f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"

        try:
            # 上游调用放独立读线程：send_trae_chat 是同步整段缓冲读（最长受
            # 上游 socket 180s 超时约束），原实现直接在生成器线程里阻塞读——
            # 生成期间客户端收不到任何字节，长生成（深度 review 大报告等）会
            # 触发下游单 chunk 超时（实测 Ethan _CHUNK_TIMEOUT=120s）→ 回合
            # 中止、落库空回复。拆成读线程 + 带 timeout 的队列等待后，等待
            # 间隙按 TRAE_HEARTBEAT_INTERVAL 发 reasoning 心跳：喂饱下游超时
            # 计时器（续命），也让下游 UI 知道中转还在等上游。
            _ev_q: queue.Queue = queue.Queue()
            _stop = threading.Event()
            stop_signal = _stop
            incremental = False

            def _read_upstream() -> None:
                try:
                    if native is not None:
                        event_source = self._stream_native_events(
                            native["messages"], model, native["tools"], _stop)
                        if event_source is not None:
                            for upstream_event in event_source:
                                if _stop.is_set():
                                    return
                                _ev_q.put(("event", upstream_event))
                            _ev_q.put(("eof", None))
                            return
                        raw_text = self._send_native_request(
                            native["messages"], model, stream=True,
                            tools=native["tools"])
                        used = True
                        if _native_rejected(raw_text) and not self._keeps_native_error(model):
                            log.warning(
                                "trae native tools rejected (4001), "
                                "fallback to text protocol: model=%s", model)
                            _debug_dump("trae_native_fallback", model=model,
                                        phase="stream")
                            raw_text = send_trae_chat(
                                messages, model, stream=True,
                                base_url=self._base_url)
                            used = False
                        _ev_q.put(("raw", (raw_text, used)))
                    else:
                        _ev_q.put(("raw", (send_trae_chat(
                            messages, model, stream=True,
                            base_url=self._base_url), False)))
                except BaseException as e:  # noqa: BLE001 — 原样转主线程抛出
                    _ev_q.put(("error", e))

            # 读线程以请求上下文的副本运行：ContextVar 赋值不跨线程，PAT 子类的
            # 发送钩子靠这份副本读到请求级账号 holder（ACCOUNT_META）并上报账号
            _request_ctx = contextvars.copy_context()
            threading.Thread(target=_request_ctx.run, args=(_read_upstream,),
                             daemon=True, name="trae-sse-reader").start()
            raw: str | None = None
            used_native = native is not None
            _waited = 0
            pending_event: tuple[str, dict[str, Any]] | None = None
            while raw is None and pending_event is None:
                semantic_left = TRAE_SEMANTIC_TIMEOUT - (time.monotonic() - last_semantic_at)
                timeout = (min(TRAE_HEARTBEAT_INTERVAL, semantic_left)
                           if TRAE_HEARTBEAT_INTERVAL > 0 else semantic_left)
                try:
                    kind, payload = _ev_q.get(timeout=max(0.001, timeout))
                except queue.Empty:
                    idle_for = time.monotonic() - last_semantic_at
                    _waited += max(0, timeout)
                    if idle_for >= TRAE_SEMANTIC_TIMEOUT:
                        _stop.set()
                        raise HTTPException(
                            status_code=504,
                            detail=f"trae stream produced no content for {round(idle_for)}s")
                    _debug_dump("trae_heartbeat", waited=round(_waited))
                    if TRAE_HEARTBEAT_INTERVAL > 0:
                        yield ": heartbeat\n\n"
                    continue
                if kind == "error":
                    raise payload
                if kind == "eof":
                    raise HTTPException(status_code=502, detail="trae stream returned no content")
                if kind == "event":
                    incremental = True
                    pending_event = payload
                else:
                    raw, used_native = payload
            acc = _NativeToolAccumulator() if used_native else None
            cleaner = (
                _StreamLeakCleaner() if sanitize and not used_native else None
            )
            splitter = (
                _StreamToolCallSplitter(_tool_names(tools))
                if tools and not used_native else None
            )
            dbg_parts: list[str] = []
            semantic_seen = False
            done_seen = False
            event_iter = iter((pending_event,)) if incremental else iter(_parse_sse(raw or ""))
            while True:
                try:
                    item = next(event_iter)
                except StopIteration:
                    if not incremental:
                        break
                    semantic_left = TRAE_SEMANTIC_TIMEOUT - (time.monotonic() - last_semantic_at)
                    timeout = (min(TRAE_HEARTBEAT_INTERVAL, semantic_left)
                               if TRAE_HEARTBEAT_INTERVAL > 0 else semantic_left)
                    try:
                        kind, payload = _ev_q.get(timeout=max(0.001, timeout))
                    except queue.Empty:
                        idle_for = time.monotonic() - last_semantic_at
                        _waited += max(0, timeout)
                        if idle_for >= TRAE_SEMANTIC_TIMEOUT:
                            _stop.set()
                            raise HTTPException(
                                status_code=504,
                                detail=f"trae stream produced no content for {round(idle_for)}s")
                        _debug_dump("trae_heartbeat", waited=round(_waited))
                        if TRAE_HEARTBEAT_INTERVAL > 0:
                            yield ": heartbeat\n\n"
                        continue
                    if kind == "error":
                        raise payload
                    if kind == "eof":
                        if not semantic_seen:
                            raise HTTPException(status_code=502, detail="trae stream returned no content")
                        if not done_seen:
                            raise HTTPException(status_code=502, detail="trae stream ended before completion")
                        break
                    if kind != "event":
                        raise RuntimeError("unexpected trae stream message")
                    item = payload
                event, data = item
                if event == "output" and (
                    data.get("reasoning_content") or data.get("response") or data.get("tool_calls")
                ):
                    semantic_seen = True
                    last_semantic_at = time.monotonic()
                    _waited = 0
                if event == "done":
                    done_seen = True
                if event == "error":
                    yield error_chunk(_trae_error_text(data), (data or {}).get("code"))
                    yield "data: [DONE]\n\n"
                    return
                if event == "output":
                    if data.get("reasoning_content"):
                        yield chunk({"reasoning_content": data["reasoning_content"]})
                    if acc is not None:
                        if data.get("tool_calls"):
                            acc.feed(data["tool_calls"])
                        text = data.get("response") or ""
                        if text:
                            dbg_parts.append(text)
                            yield chunk({"role": "assistant", "content": text})
                    elif data.get("response"):
                        text = data["response"]
                        if cleaner is not None:
                            text = cleaner.feed(text)
                        elif splitter is not None:
                            text = splitter.feed(text)
                        if text:
                            dbg_parts.append(text)
                            yield chunk({"role": "assistant", "content": text})
                elif event == "token_usage":
                    u = data or {}
                    try:
                        usage = {
                            "prompt_tokens": int(u.get("prompt_tokens") or 0),
                            "completion_tokens": int(u.get("completion_tokens") or 0),
                            "total_tokens": int(u.get("total_tokens") or 0),
                        }
                    except (TypeError, ValueError):
                        usage = None
                elif event == "done":
                    done_seen = True
                    break
            if not semantic_seen:
                raise HTTPException(status_code=502, detail="trae stream returned no content")
            if incremental and not done_seen:
                raise HTTPException(status_code=502, detail="trae stream ended before completion")
        except GeneratorExit:
            if stop_signal is not None:
                stop_signal.set()
            raise
        except HTTPException as e:
            if stop_signal is not None:
                stop_signal.set()
            yield error_chunk(f"trae error {e.status_code}: {e.detail}", e.status_code)
            yield "data: [DONE]\n\n"
            return
        except Exception as e:
            if stop_signal is not None:
                stop_signal.set()
            log.error("Trae stream error: %s", e)
            yield error_chunk(f"trae stream error: {e}")
            return

        if stop_signal is not None:
            stop_signal.set()
        finish = "stop"
        dbg_calls: list[dict[str, Any]] = []
        if used_native:
            calls = acc.finish()
            if calls:
                finish = "tool_calls"
                dbg_calls = calls
                # OpenAI 流式协议：tool_call 必须带 index（客户端靠它合并分片），
                # 首个 delta 带 role；不带 index 会被部分解析器直接丢弃
                yield chunk({"role": "assistant", "tool_calls": calls})
        elif cleaner is not None:
            tail = cleaner.flush()
            if tail:
                dbg_parts.append(tail)
                yield chunk({"role": "assistant", "content": tail})
        elif splitter is not None:
            tail, calls = splitter.flush()
            if tail:
                dbg_parts.append(tail)
                yield chunk({"role": "assistant", "content": tail})
            if calls:
                finish = "tool_calls"
                dbg_calls = calls
                # OpenAI 流式协议：tool_call 必须带 index（客户端靠它合并分片），
                # 首个 delta 带 role；不带 index 会被部分解析器直接丢弃
                yield chunk({"role": "assistant", "tool_calls": [
                    {"index": i, "id": c["id"], "type": "function",
                     "function": c["function"]}
                    for i, c in enumerate(calls)
                ]})
        _debug_dump("debug_trae_response", model=model, stream=True,
                    content="".join(dbg_parts),
                    tool_calls=[
                        {"name": c["function"]["name"],
                         "arguments": c["function"]["arguments"]}
                        for c in dbg_calls
                    ])
        yield chunk({}, finish)
        if include_usage:
            yield usage_chunk()
        yield "data: [DONE]\n\n"

    def _collect(
        self, messages: list[dict[str, Any]], model: str, sanitize: bool = True,
        tools: list[dict[str, Any]] | None = None,
        native: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """非流式：聚合 Trae SSE 成完整响应。"""
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        usage_real: dict[str, Any] | None = None

        if native is not None:
            raw = self._send_native_request(
                native["messages"], model, stream=False, tools=native["tools"])
            used_native = True
            if _native_rejected(raw) and not self._keeps_native_error(model):
                log.warning(
                    "trae native tools rejected (4001), "
                    "fallback to text protocol: model=%s", model)
                _debug_dump("trae_native_fallback", model=model, phase="collect")
                raw = send_trae_chat(messages, model, stream=False,
                                     base_url=self._base_url)
                used_native = False
        else:
            raw = send_trae_chat(messages, model, stream=False,
                                 base_url=self._base_url)
            used_native = False
        acc = _NativeToolAccumulator() if used_native else None
        for event, data in _parse_sse(raw):
            if event == "error":
                raise HTTPException(
                    status_code=502,
                    detail=_trae_error_text(data),
                )
            if event == "token_usage":
                u = data or {}
                try:
                    usage_real = {
                        "prompt_tokens": int(u.get("prompt_tokens") or 0),
                        "completion_tokens": int(u.get("completion_tokens") or 0),
                        "total_tokens": int(u.get("total_tokens") or 0),
                    }
                except (TypeError, ValueError):
                    usage_real = None
            if event == "output":
                if data.get("reasoning_content"):
                    reasoning_parts.append(data["reasoning_content"])
                if data.get("response"):
                    content_parts.append(data["response"])
                if acc is not None and data.get("tool_calls"):
                    acc.feed(data["tool_calls"])

        reasoning = "".join(reasoning_parts)
        content = "".join(content_parts)
        # 无 tools 请求也必须有初始值——下方 _debug_dump 无条件引用
        # （实测缺失时非流式无 tools 请求直接 UnboundLocalError -> internal error）
        tool_calls: list[dict[str, Any]] = []
        if used_native:
            # 原生通道：tool_calls 来自结构化事件；无 agent 预设，正文没有
            # 可泄漏的协议语法，不做解析/清洗
            tool_calls = acc.finish()
        else:
            # 顺序关键：带 tools 的请求必须先解析、后清洗——_sanitize_agent_leak
            # 会把 <tool_call>/</arg_value> 标签全剥掉，先清洗再解析会让解析器
            # 拿到被拆掉结构的残骸（实测 glm-5.3 函数调用表达式因此整段漏进正文）
            if tools:
                content, tool_calls = _parse_tool_calls(content, _tool_names(tools))
            if sanitize:
                content = _sanitize_agent_leak(content)
        _debug_dump("debug_trae_response", model=model, stream=False, content=content,
                    tool_calls=len(tool_calls))
        # 只有 tool_calls 没有正文也是合法响应（agent 直接发起调用），
        # 不注入空响应兜底文案——否则会混进 Anthropic tool_use 消息正文
        if not content and not reasoning and not tool_calls:
            content = "(trae upstream 返回了空响应，未产生任何内容)"
        if reasoning:
            if content:
                content = f"<think>\n{reasoning}\n</think>\n\n{content}"
            else:
                # 只有思维链没有正文时，别把 reasoning 整个丢掉
                content = f"<think>\n{reasoning}\n</think>"

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": tool_calls} if tool_calls else {}),
                },
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }],
            # 原生通道带真实 token_usage；文本协议上游不吐 usage，沿用旧启发式
            "usage": usage_real or {
                "prompt_tokens": 0,
                "completion_tokens": max(1, len(content.encode("utf-8")) // 4),
                "total_tokens": 0,
            },
        }



# ---------------------------------------------------------------------------
# 流式首事件闸门（多账号 failover 用）
# ---------------------------------------------------------------------------

class _GatedStream:
    """闸门结果：``buffered`` 是预驱动期间攒下的**语义**事件（心跳已丢），
    ``gen`` 是在途生成器（committed 后续跑它，不重开）。"""

    def __init__(self, buffered: list[str], gen: Any) -> None:
        self.buffered = buffered
        self.gen = gen


async def _sync_to_async_iter(it: Any) -> Any:
    """把同步 iterator 包装成 async iterator（喂 StreamingResponse）。

    starlette 的 StreamingResponse 只认 async iterator；本通道的 ``_stream``
    及 ``_wrap_anthropic_stream`` 都是同步生成器（读线程 + queue 轮询），
    改造前后都靠这层同步→异步适配喂给 StreamingResponse（由事件循环对
    ``next()`` 做线程池调度，不在主线程跑同步阻塞代码）。
    """
    it = iter(it)
    while True:
        try:
            yield next(it)
        except StopIteration:
            return


def _gate_first_event(gen: Any) -> "_GatedStream | BaseException":
    """预驱动 ``_stream``（**同步**生成器）到第一个语义事件。

    ``_stream`` 是读线程 + queue 轮询的同步生成器（非 async）——下游
    :func:`_wrap_anthropic_stream` 也按同步 ``Iterator[str]`` 消费，本闸门
    同步迭代即可；StreamingResponse 那边由 starlette 对同步 iterator 做
    线程池适配（与改造前一致）。

    返回 :class:`_GatedStream`（成功，可放行）或捕获到的异常（换号/透传）。
    心跳帧（``: heartbeat``）与空帧跳过不缓冲。第一个含 ``data:`` 的语义帧
    默认即 committed；**但若该帧是账号级错误**（``_stream`` 的 ``except
    HTTPException`` 把 401/429 yield 成 ``{"error": {...}}`` 错误 chunk 再
    return，是假成功）则**不缓冲、直接返回对应 HTTPException** 让 failover
    换号——此时客户端未收字节，换号安全。真正的异常在第一个语义帧**之前**
    抛出也都安全换号（客户端未收字节）。
    """
    buffered: list[str] = []
    try:
        for piece in gen:
            text = piece if isinstance(piece, str) else piece.decode("utf-8", "replace")
            stripped = text.strip()
            # 心跳/注释帧：保活用，不是内容，丢掉
            if not stripped or stripped.startswith(":"):
                continue
            # 账号级错误帧（_stream 把 HTTPException yield 成 error chunk）：
            # 不缓冲、直接当异常换号，别让它假成功混过闸门。
            err = _account_error_from_frame(stripped)
            if err is not None:
                return err
            buffered.append(text)
            return _GatedStream(buffered, gen)
        # 没有任何语义事件就结束：空流假成功，当错误交调用方换号
        return HTTPException(status_code=502, detail="trae stream returned no content")
    except HTTPException as e:
        return e
    except Exception as e:  # noqa: BLE001 — 读线程透传的原始异常
        return e


#: 错误 chunk 里判定「换下一个账号可能好转」的 HTTP 状态码（与
#: ``TraeProvider._is_account_error`` 一致）。
_ACCOUNT_ERROR_CODES = (401, 429)


def _account_error_from_frame(stripped_frame: str) -> "HTTPException | None":
    """从单个 SSE 帧里识别账号级错误（``{"error": {"code": 401|429, ...}}``）。

    ``_stream`` 的 ``except HTTPException`` 会把账号级错误 yield 成
    ``error_chunk``（payload 是 ``{"error": {"message", "type", "code"}}``）
    再 ``return``——那是假成功。本函数在闸门里把这类帧还原成 HTTPException，
    让 forward 的 failover 循环冷却换号。非账号级错误帧返回 None（放行，由
    下游/包装层按原样处理）。
    """
    if not stripped_frame.startswith("data:"):
        return None
    payload = stripped_frame[5:].strip()
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    try:
        code = int(err.get("code"))
    except (TypeError, ValueError):
        return None
    if code not in _ACCOUNT_ERROR_CODES:
        return None
    return HTTPException(status_code=code, detail=str(err.get("message") or "trae upstream error"))


def _replay_prefixed(buffered: list[str], gen: Any) -> Any:
    """先补放闸门缓冲的语义帧，再续跑在途生成器（不重开，防二次消费）。

    同步生成器（与 ``_stream`` 同型）；补放后直接 ``for`` 续跑在途迭代器，
    不重开（重开会二次消费上游 / 触发二次计费）。
    """
    for piece in buffered:
        yield piece
    for piece in gen:
        yield piece
