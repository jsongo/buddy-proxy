"""Xiaomi MiMo provider —— OpenAI 兼容 chat 直通转发。

上游是 OpenAI 形态的 ``/chat/completions``（SSE 流式 / 非流式 JSON），
所以结构照抄 ``providers/zcode.py`` 的直通模式：

- 请求体透传（只做 ``mimo-auto`` → ``mimo-pro`` 的模型名改写，与桌面端
  ``pN()``/``l2()`` 一致）
- 响应（含 SSE）原样回传
- **anthropic（``/v1/messages``）例外**：上游没有 Anthropic 原生端点，不能像
  zcode 那样直通，所以路由层传来的 body 已是 chat 格式，响应要**反向**转回
  Anthropic——流式用 ``AnthropicStreamConverter`` 包（与 trae 同一个），非流式用
  ``chat_completion_to_anthropic_message``。漏转的话 Claude Code 会报
  「0 stream events received」/「body is JSON but not a Message」（2026-09-23 实测）。

认证见 ``credentials.py``：API key 或小米 SSO（复用 MiMo 桌面登录态）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message
from .config import CHAT_UA, DEFAULT_MODELS, MODEL_NAME_CANONICAL, X_SOURCE_SSO
from .credentials import AuthError, ResolvedUpstream, resolve_upstream

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)


def _normalize_model(name: str) -> str:
    """模型名改写：``mimo-auto`` → ``mimo-pro``（与桌面端 pN/l2 一致）。"""
    key = (name or "").strip().lower()
    return MODEL_NAME_CANONICAL.get(key, name)


async def _pass_through_stream(
    response: httpx.Response,
) -> AsyncIterator[bytes]:
    """把上游 SSE/字节流原样泵给客户端；结束后确保连接释放。"""
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()


async def _iter_openai_chunks(response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """把上游 OpenAI SSE 解成一个个 chunk dict（供转 Anthropic 事件流）。

    只认 ``data:`` 行；``[DONE]`` 结束；解析不了的片段跳过（上游偶发半包/
    心跳注释行，跳过比整体报错好）。异常/结束时确保释放连接。
    """
    try:
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict):
                yield chunk
    finally:
        await response.aclose()


async def _to_anthropic_stream(
    response: httpx.Response, model: str
) -> AsyncIterator[str]:
    """上游 OpenAI SSE → Anthropic 事件流（``/v1/messages`` 客户端要的形状）。

    mimo 上游**没有** Anthropic 原生端点，所以不能像 zcode 那样直通；必须
    自己把 OpenAI chunk 转成 ``message_start`` / ``content_block_delta`` /
    ``message_stop``。转换器直接复用 ``anthropic_adapter.AnthropicStreamConverter``
    （与 trae 通道同一个），因此 reasoning_content → thinking 块、tool_calls →
    tool_use 块的行为与 trae 一致。

    不转的话：Claude Code 收 200 却拿不到任何 Anthropic 事件，报
    「Streaming response ended before any complete data was received」。
    """
    from ..protocols.anthropic_adapter import AnthropicStreamConverter

    converter = AnthropicStreamConverter(model)

    def _emit(event_name: str, payload: dict[str, Any]) -> str:
        return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    async for chunk in _iter_openai_chunks(response):
        if chunk.get("error"):
            err = chunk["error"]
            msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
            # 已开出内容块要先收尾，否则严格客户端会因块悬空而卡死
            for name, payload in converter.close_open_blocks():
                yield _emit(name, payload)
            yield _emit("error", {
                "type": "error",
                "error": {"type": "api_error", "message": msg},
            })
            return
        for name, payload in converter.feed_chunk(chunk):
            yield _emit(name, payload)
    for name, payload in converter.finish():
        yield _emit(name, payload)
    yield "data: [DONE]\n\n"


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    """把上游错误转成客户端错误响应（透传状态码与错误体，隐藏凭据痕迹）。"""
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


#: 业务侧（非鉴权）拒绝的特征——命中则**不要**重换 serviceToken
_BIZ_REJECT_RE = re.compile(
    r"membership_required|permission_error|未开通会员|会员已到期|30012",
    re.I,
)


def _run_sync(factory):
    """把协程工厂跑在独立事件循环里，返回结果。

    ``quota()`` 等同步接口可能被 ``/ui`` 从**已在运行的**事件循环里调到，
    直接 ``asyncio.run`` 会 ``RuntimeError``。这里先探有没有在跑的 loop：
    没有就地跑；有就丢到独立线程（自带 loop）里跑，避免嵌套。
    工厂（而非协程对象）传参，保证在目标 loop 里才创建协程、不漏 await。
    """
    import concurrent.futures

    def _runner():
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _runner()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result(timeout=30)


#: ``renewalMode`` → 中文（订阅对象里是英文枚举）
_RENEWAL_LABEL = {
    "ONE_TIME": "单次",
    "MONTHLY": "按月",
    "YEARLY": "按年",
    "AUTO_RENEW": "自动续订",
}

#: 计划周期的总时长兜底（拿不到 endTime 时按 30 天画进度条）
_PERIOD_FALLBACK_DAYS = 30.0


def _to_ts(value: Any) -> float | None:
    """ISO 时间串 / unix 秒（或毫秒）→ unix 秒；解析不了返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        # 上游两种都有可能：>1e12 当毫秒
        return v / 1000.0 if v > 1e12 else v
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _to_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except ValueError:
        return None


def _usage_item(udata: dict[str, Any], subscribed: bool) -> dict[str, Any]:
    """周额用量条目（7 天一轮，锚点 = 订阅 ``startTime``）。

    上游 ``percent`` 是**剩余**百分比，不是已用——桌面端 i18n 直接写着
    ``remainingPercent:"剩余 {{percent}}%"``，进度条也是 ``width: min(percent,100)%``
    （条越满 = 剩得越多），模拟数据 ``percent: 100 - ++i`` 随调用递减。
    量级也对得上：未开会员时 0.0（一点不剩），刚买完 ~100（几乎没用）。

    而管理页 ``quotaItemHtml`` 的 ``percent`` 是**已用**（填进度条 +「已用 x%」
    文案 + ≥85% 变红），所以这里必须换算：``it["percent"] = 100 - 剩余``。
    也不是小数比例，别再乘 100（乘过会显示 9870%）。
    """
    raw = udata.get("percent")
    try:
        remaining = round(float(raw), 1) if raw is not None else None
    except (TypeError, ValueError):
        remaining = None
    if remaining is not None:
        remaining = min(max(remaining, 0.0), 100.0)
    used_pct = None if remaining is None else round(100.0 - remaining, 1)
    reset_ts = _to_ts(udata.get("resetAt")) or _to_ts(udata.get("resetDate"))
    label = "周额用量" if subscribed else "周额用量（未开通会员）"
    return {
        "label": label,
        "used": used_pct,
        "total": 100.0,
        "remaining": remaining,
        "percent": used_pct,
        "reset_ts": reset_ts,
    }


def _period_item(current: dict[str, Any]) -> list[dict[str, Any]]:
    """套餐有效期条目（0~100 的时间进度条）。拿不到起止时间就不画。"""
    start_ts = _to_ts(current.get("startTime"))
    end_ts = _to_ts(current.get("endTime"))
    if not end_ts:
        return []
    now = time.time()
    total_s = (end_ts - start_ts) if start_ts and end_ts > start_ts else (
        _PERIOD_FALLBACK_DAYS * 86400
    )
    used_s = min(max(now - (start_ts or (end_ts - total_s)), 0.0), total_s)
    used_days = round(used_s / 86400, 1)
    total_days = round(total_s / 86400, 1)
    left_days = round(max(total_days - used_days, 0.0), 1)
    end_label = datetime.fromtimestamp(end_ts).strftime("%m-%d") if end_ts else "—"
    return [
        {
            "label": f"套餐有效期至 {end_label}（天）",
            "used": used_days,
            "total": total_days,
            "remaining": left_days,
            "percent": round(used_s / total_s * 100, 1) if total_s else None,
            # 到期不是「重置」，故不给 reset_ts——前端就不会拼「… 重置」后缀
            "reset_ts": None,
        }
    ]


def _is_auth_rejection(resp: httpx.Response) -> bool:
    """判断 401/403 是不是「凭据失效」（值得重换票）。

    上游把「未开通会员」也放在 403 里，但 ``code=membership_required`` /
    ``biz_code=30012``；这类重试没意义。
    """
    try:
        payload = resp.json()
    except Exception:
        return True  # 看不懂就当鉴权失败，重试一次无妨
    err = payload.get("error") if isinstance(payload, dict) else None
    blob = json.dumps(err or payload, ensure_ascii=False)
    return not _BIZ_REJECT_RE.search(blob)


class MimoProvider(BaseProvider):
    id = "mimo"
    name = "Xiaomi MiMo (桌面端登录态 / API key)"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": mid,
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": desc,
            }
            for mid, desc in DEFAULT_MODELS.items()
        ]

    def ensure_auth(self) -> None:
        # 同步探活：只检查「有没有可用凭据源」，不做网络换票（换票在 forward 里）。
        from .credentials import resolve_api_key
        from .sso import load_account_cookies

        key, _ = resolve_api_key()
        if key:
            return
        if load_account_cookies() is not None:
            return
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": (
                        "mimo 未配置认证：请设置 MIMO_API_KEY，或在 MiMo Desktop "
                        "登录小米账号（本 provider 会自动读取其 cookie）"
                    ),
                    "type": "authentication_error",
                }
            },
        )

    async def forward(
        self,
        body: dict[str, Any],
        protocol: str,
        original: dict[str, Any] | None = None,
    ) -> StreamingResponse | JSONResponse:
        self.ensure_auth()
        stream = bool(body.get("stream", False))

        upstream_body = {k: v for k, v in body.items() if not k.startswith("_")}
        upstream_body["model"] = _normalize_model(str(upstream_body.get("model") or ""))

        up = await self._resolve(force_refresh=False)
        client = await self._get_client()
        resp = await self._send(client, up, upstream_body, stream)

        # 401/403：可能是 SSO serviceToken 失效，也可能是业务权限错误
        # （membership_required=30012「未开通会员」）。只有前者才值得重换票；
        # 后者重试只会白跑一次 SSO。API key 模式不重试。
        if resp.status_code in (401, 403) and up.mode == "sso":
            if stream:
                await resp.aread()
            if _is_auth_rejection(resp):
                log.warning(
                    "mimo sso token rejected (%s), refreshing", resp.status_code
                )
                await resp.aclose()
                up = await self._resolve(force_refresh=True)
                resp = await self._send(client, up, upstream_body, stream)

        if resp.status_code >= 400:
            if stream:
                # 先把错误体读完再关流：aclose() 会丢弃未读的 body，
                # 之后拿不到上游真实错误（429 配额 / 401 凭据都会变空）。
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            # anthropic 客户端（Claude Code 的 /v1/messages）要的是 Anthropic
            # 事件流；上游只给 OpenAI chunk，必须转换，否则客户端收 200 却
            # 「0 stream events received」。
            if protocol == "anthropic":
                return StreamingResponse(
                    _to_anthropic_stream(resp, str(upstream_body.get("model") or "")),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )
            return StreamingResponse(
                _pass_through_stream(resp),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )
        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "message": "mimo upstream returned non-JSON",
                        "type": "bad_gateway",
                    }
                },
            ) from exc
        # 非流式同理：OpenAI chat.completion → Anthropic message。
        # 不转的话 Claude Code 报「body is JSON but not a Message」。
        if protocol == "anthropic":
            payload = chat_completion_to_anthropic_message(payload, original)
        return JSONResponse(content=payload)

    # ---- 内部 ----

    async def _resolve(self, force_refresh: bool) -> ResolvedUpstream:
        try:
            return await resolve_upstream(force_refresh=force_refresh)
        except AuthError as exc:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": str(exc),
                        "type": "authentication_error",
                    }
                },
            ) from exc

    async def _send(
        self,
        client: httpx.AsyncClient,
        up: ResolvedUpstream,
        body: dict[str, Any],
        stream: bool,
    ) -> httpx.Response:
        headers = dict(up.headers)
        headers["Accept"] = "text/event-stream" if stream else "application/json"
        try:
            req = client.build_request("POST", up.chat_url, json=body, headers=headers)
            return await client.send(req, stream=stream)
        except httpx.TimeoutException as exc:
            log.warning("mimo upstream timeout: %s", exc)
            raise HTTPException(
                status_code=504,
                detail={"error": {"message": "mimo upstream timeout", "type": "timeout"}},
            ) from exc
        except httpx.HTTPError as exc:
            log.warning("mimo upstream error: %s", exc)
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {"message": "mimo upstream error", "type": "bad_gateway"}
                },
            ) from exc

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        from .credentials import resolve_api_key
        from .sso import load_account_cookies

        key, base = resolve_api_key()
        has_sso = load_account_cookies() is not None
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(key) or has_sso,
            "auth_mode": "key" if key else ("sso" if has_sso else "none"),
            "base_url": base if key else "",
            "models": list(DEFAULT_MODELS),
        }

    def quota(self) -> dict[str, Any] | None:
        """套餐额度：周期用量 + 套餐有效期（asar: ``Mse`` 的 gC 调用表）。

        两个上游接口（SSO 通道才有；API key 通道没有对应 user/* 路由，返回 None）：

        - ``GET /user/usage``                     → ``{percent, resetDate, resetAt}``
        - ``GET /user/xiaomi/subscription/self``  → ``{current, subscriptions}``
          订阅对象含 ``title`` / ``planCode`` / ``renewalMode`` / ``startTime`` /
          ``endTime`` / ``percent`` / ``nextResetTime``。

        归一化成管理页 ``quotaItemHtml`` 的条目形态（``reset_ts`` 是**秒**，
        前端 ``new Date(reset_ts * 1000)``）。接口是同步的（``BaseProvider`` 约定，
        调用方用 ``asyncio.to_thread`` 包装），但 SSO 换票是协程，所以经
        :func:`_run_sync` 桥接——被 ``/ui`` 从事件循环里调到也不会炸。
        """
        from .config import subscription_url, usage_url
        from .credentials import resolve_api_key
        from .sso import ensure_service_token, load_account_cookies

        key, _base = resolve_api_key()
        if key:
            return None
        account = load_account_cookies()
        if account is None:
            return None

        async def _fetch() -> tuple[dict, dict]:
            token = await ensure_service_token()
            headers = {
                "Cookie": token.cookie_header(account),
                "User-Agent": CHAT_UA,
                "X-Mimo-Source": X_SOURCE_SSO,
            }
            with httpx.Client(timeout=15.0) as http:
                usage = http.get(usage_url(), headers=headers).json()
                sub = http.get(subscription_url(), headers=headers).json()
            return usage, sub

        try:
            usage, sub = _run_sync(_fetch)
        except Exception as exc:  # noqa: BLE001 — /ui 展示用，失败不抛
            log.warning("mimo quota query failed: %s", exc)
            return None

        udata = (usage or {}).get("data") or {}
        sdata = (sub or {}).get("data") or {}
        udata = udata if isinstance(udata, dict) else {}
        sdata = sdata if isinstance(sdata, dict) else {}
        current = sdata.get("current") if isinstance(sdata.get("current"), dict) else {}
        subscriptions = sdata.get("subscriptions") or []

        items: list[dict[str, Any]] = []
        items.append(_usage_item(udata, bool(current or subscriptions)))
        items.extend(_period_item(current))

        if not current and not subscriptions:
            level = "未开通会员"
        else:
            level = current.get("title") or current.get("planCode") or "已订阅"
            renew = current.get("renewalMode")
            if renew:
                level = f"{level}·{_RENEWAL_LABEL.get(renew, renew)}"
        return {"items": items, "level": level}


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.mimo.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import asyncio

    async def _main() -> None:
        p = MimoProvider()
        print("health:", p.health())
        up = await p._resolve(force_refresh=False)
        print("resolved:", up.describe(), "url:", up.chat_url)
        resp = await p.forward(
            {
                "model": "mimo-auto",
                "messages": [{"role": "user", "content": "只回复两个字：pong"}],
                "stream": False,
                "max_tokens": 32,
            },
            "openai",
        )
        print("status:", resp.status_code)
        print(str(resp.body)[:400])

    asyncio.run(_main())
