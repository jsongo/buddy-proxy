"""CodeBuddy 默认上游 provider：认证、签到/额度/流水查询与转发分派。

复杂转发逻辑（SSE 解析、DSML、协议转换）在 ``pipeline``；本模块的
``CodeBuddyProvider.forward`` 只做「认证 + 构造上游请求 + 分派流式/非流式」。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse

from buddy_proxy.providers import BaseProvider
from buddy_proxy.state import diagnostic

from .observability import body_summary
from .pipeline import desensitize_body


def get_state():
    """经包命名空间转发 ``state.get_state``。

    本模块各方法体内的 ``get_state()`` 全局查找都经由这里：测试以
    ``monkeypatch.setattr(codebuddy_provider, "get_state", ...)`` 打补丁时
    （见 test_workbuddy_usage_records），方法体取到的是补丁对象而非导入期
    绑定的原函数。未打补丁时经包 ``__getattr__`` 解析到 observability 里
    的原函数，行为与直接导入一致。
    """
    import buddy_proxy.codebuddy_provider as _pkg

    return _pkg.get_state()


def _normalize_tool_choice(tool_choice: Any) -> Any:
    """把 OpenAI 的 object 形式 tool_choice 转成上游接受的 string 形式。

    上游 CodeBuddy 后端（Go）的 Request.tool_choice 字段是 string 类型，
    OpenAI 标准里 ``{"type":"function","function":{"name":"X"}}`` 这种 object
    形式（强制调用函数 X）会触发 400：cannot unmarshal object into ...
    of type string。此处转换为等价的函数名字符串 "X"（实测上游接受且语义一致）。
    """
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return name
        # {"type": "function"} 但缺 name：退化为 required（强制调用工具）
        if tool_choice.get("type") == "function":
            return "required"
    return tool_choice


class CodeBuddyProvider(BaseProvider):
    """默认的 CodeBuddy 上游，实现 BaseProvider 接口。

    与豆包（DoubaoProvider）对称统一。CodeBuddy 的复杂转发逻辑
    （SSE 解析、DSML、工具调用、协议转换）仍由本模块的
    ``stream_upstream`` / ``collect_upstream`` / ``convert_nonstream``
    承担，本类只做「认证 + 构造上游请求 + 分派流式/非流式」。
    """

    id = "codebuddy"
    name = "CodeBuddy"
    # WorkBuddy/CodeBuddy IDE 提供每日签到（billing/meter，2026-09 从 IDE asar 反查）
    supports_checkin = True

    def models(self) -> list[dict[str, Any]]:
        # CodeBuddy 的模型列表由 /v1/models 统一从本地配置加载，
        # 此处返回空（不参与 provider 路由的模型合并，避免重复）。
        return []

    def ensure_auth(self) -> None:
        state = get_state()
        state.ensure_auth()

    # ---- 打卡 / 额度（/ui 管理页消费，均经 asyncio.to_thread 调用） ----
    # 端点来自 WorkBuddy IDE asar 反查（2026-09）：Desktop 走 /v2 前缀的 IDE 网关。
    # ⚠️ 签到状态必须用 checkin-activity-status（活动版）——不带 /v2 的
    # checkin-status 是另一个（web/cookie）变体，Bearer 调用只会返回全空数据。
    # 签到活动有档期（如「开学季」9/1-9/15），active=false 表示当前档期未开。

    def checkin_status(self) -> dict[str, Any] | None:
        state = get_state()
        state.ensure_auth()
        payload = state.client.api_post("/v2/billing/meter/checkin-activity-status")
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        active = bool(data.get("active"))
        checked_in = bool(data.get("today_checked_in"))
        return {
            "checked_in": checked_in,
            "claimable": active and not checked_in,
            "inactive": not active,
            "streak_days": data.get("streak_days") or 0,
            "daily_credit": data.get("today_credit") or data.get("daily_credit") or 0,
            "checkin_dates": data.get("checkin_dates") or [],
            "activity_name": data.get("activity_name") or "",
            "message": payload.get("msg", ""),
        }

    def checkin_claim(self) -> dict[str, Any] | None:
        state = get_state()
        state.ensure_auth()
        payload = state.client.api_post("/v2/billing/meter/daily-checkin")
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        return {
            "checked_in": True,
            "extra_credits": data.get("credit") or data.get("today_credit") or data.get("daily_credit"),
            "streak_days": data.get("streak_days") or 0,
            "message": payload.get("msg", ""),
        }

    def quota(self) -> dict[str, Any] | None:
        """查询积分资源包汇总（get-user-resource-summary）：每个包给周期额度，
        单位 credits；返回「剩余合计 + 各包明细」。

        ⚠️ 资源汇总走无前缀路径（/v2 下反而 404），与签到接口的前缀规则相反。
        """
        state = get_state()
        state.ensure_auth()
        payload = state.client.api_post("/billing/meter/get-user-resource-summary")
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        sub_code = data.get("SubscriptionPackageCode") or ""

        def to_float(v) -> float | None:
            try:
                return round(float(v), 2) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        packs = []
        used_sum = total_sum = remain_sum = 0.0
        for p in data.get("Packages") or []:
            used, total, remain = (to_float(p.get("CycleUsedCapacity")),
                                   to_float(p.get("CycleTotalCapacity")),
                                   to_float(p.get("CycleRemainCapacity")))
            if total is None:
                continue
            if used is None:
                used = 0.0
            if remain is None:
                remain = round(total - used, 2)
            used_sum += used; total_sum += total; remain_sum += remain
            packs.append({
                "label": "订阅套餐" if p.get("PackageCode") == sub_code else "资源包",
                "used": used, "total": total, "remaining": remain,
                "percent": round(used / total * 100) if total else 0,
                "reset_ts": None,
            })
            if len(packs) >= 4:
                break
        items = [{
            "label": "积分余额合计",
            "used": round(used_sum, 2), "total": round(total_sum, 2),
            "remaining": round(remain_sum, 2),
            "percent": round(used_sum / total_sum * 100) if total_sum else 0,
            "reset_ts": None,
        }]
        items.extend(packs)
        return {"items": items, "level": "pro" if data.get("IsPaidUser") else "free"}

    # ---- 计费流水（WorkBuddy web「使用记录」同源接口，2026-09-06 实测） ----

    def usage_records(
        self,
        start: str = "",
        end: str = "",
        page_num: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """按请求粒度的积分消耗流水（/billing/meter/get-user-request-usage）。

        端点来自 WorkBuddy web（/profile/plans-usage 页 XHR 反查）。与签到/
        资源汇总同族：走 IDE 插件 Bearer 认证即可，**无需网页 cookie**；
        copilot.tencent.com 与 www.workbuddy.cn 同路径均实测 200。

        记录字段对应网页「使用记录」列：credit=实扣积分、model、client、
        requestTime、input（prompt 原文/截断）、requestId（crb-…）。
        start/end 格式 "YYYY-MM-DD HH:MM:SS"，缺省为最近 7 天。

        用途预留：管理页消费流水视图；或按 requestTime/model 把流水 credit
        回填 metrics，对上游 usage 缺失实扣的请求做对账。
        """
        state = get_state()
        state.ensure_auth()
        if not end:
            end = time.strftime("%Y-%m-%d %H:%M:%S")
        if not start:
            start = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(time.time() - 7 * 86400))
        body = {
            "startTime": start,
            "endTime": end,
            "pageNum": max(1, int(page_num)),
            "pageSize": min(100, max(1, int(page_size))),
        }
        payload = state.client.api_post("/billing/meter/get-user-request-usage", body)
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("msg") or f"code={payload.get('code')}")
        data = payload.get("data") or {}
        records = []
        for r in data.get("data") or []:
            if not isinstance(r, dict):
                continue
            records.append({
                "request_id": r.get("requestId"),
                "credit": r.get("credit"),
                "model": r.get("model"),
                "client": r.get("client"),
                "request_time": r.get("requestTime"),
                "input": (r.get("inputTrunc") or r.get("input") or "")[:200],
            })
        return {"total": data.get("total") or len(records), "records": records}

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        state = get_state()
        state.ensure_auth()

        diagnostic("upstream_request", protocol=protocol, **body_summary(body))

        stream = bool(body.get("stream"))
        upstream_body = dict(body)

        # 归一化 tool_choice：object 形式 → 函数名字符串（上游只接受 string）
        if "tool_choice" in upstream_body:
            upstream_body["tool_choice"] = _normalize_tool_choice(upstream_body["tool_choice"])

        # 应用脱敏处理
        if state.enable_desensitize:
            upstream_body = desensitize_body(upstream_body, compact_harness=True)

        # 始终以流式方式请求上游（聚合或转发）
        upstream_body["stream"] = True
        upstream_body.setdefault("stream_options", {"include_usage": True})

        url = state.client.endpoint + "/v2/chat/completions"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; Genie-IDE/1.0)",
            **state.client.auth_headers(),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        # 🔍 调试：输出实际发送的IDE识别headers
        if state.logger:
            ide_headers = {k: v for k, v in headers.items()
                           if k.startswith("X-IDE-") or k == "X-Product-Version" or k == "X-Machine-Id"}
            diagnostic("upstream_ide_headers", **ide_headers)

        if stream:
            # 流式：直接转发。经包命名空间延迟解析而非模块顶层导入：测试以
            # monkeypatch.setattr(codebuddy_provider, "stream_upstream", ...) 打
            # 补丁时（见 test_endpoints_smoke），这里必须取到补丁后的对象。
            from buddy_proxy.codebuddy_provider import stream_upstream

            return StreamingResponse(
                stream_upstream(url, headers, upstream_body, protocol, original),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        else:
            # 非流式：聚合后返回（延迟解析理由同上）
            from buddy_proxy.codebuddy_provider import collect_upstream, convert_nonstream

            collected = await collect_upstream(url, headers, upstream_body, protocol)
            return JSONResponse(content=convert_nonstream(collected, protocol, original))


# 默认 CodeBuddy provider 单例（供 forward_chat 默认路径调用）。
# 定义在 CodeBuddyProvider 类之后，实例化安全。
_default_codebuddy = CodeBuddyProvider()
