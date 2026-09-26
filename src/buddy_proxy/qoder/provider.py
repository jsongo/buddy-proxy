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

from buddy_proxy.providers.base import BaseProvider

from .catalog import Catalog, to_openai_model
from .config import COSY_VERSION, Region, resolve_region, with_cached_endpoints
from .cosy import sign
from .credentials import AuthError, Credential, ensure_credential

log = logging.getLogger(__name__)

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
)

#: 上游首字节超时（边缘「收下不回应」时快速失败）。
FIRST_BYTE_TIMEOUT_S = 60.0

#: 整轮流式上限（防止挂死连接长期占用）。
STREAM_TIMEOUT_S = 600.0


class QoderProvider(BaseProvider):
    """Qoder（Qwen3.8 / DeepSeek / GLM / Kimi …）通道。"""

    id = "qoder"
    name = "Qoder"

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
        """同步返回模型列表（用兜底目录；异步刷新见 ``refresh_models``）。"""
        entries = self._catalog._models or Catalog.fallback()
        return [to_openai_model(e, self.id) for e in entries]

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
        return [to_openai_model(e, self.id) for e in entries]

    def resolve_model(self, model: str) -> str:
        """把显示名/大小写变体归一成上游 key。"""
        return self._catalog.resolve_key(model)

    def accepts_model(self, model: str) -> bool:
        """目录别名（显示名/大小写变体）也要能被自动路由命中。

        Qoder 的目录 key 是内部代号（``qfmodel``），而客户端常直接发显示名
        （``Qwen3.8-Flash``）——只比 id 会漏配，请求掉进兜底通道后被上游拒成
        「模型不存在」。这里用 catalog 的别名表兜住：归一后仍等于原值（说明
        catalog 不认识它）才判否，保留前向兼容。
        """
        want = (model or "").strip()
        if not want:
            return False
        if super().accepts_model(want):
            return True
        return self._catalog.resolve_key(want) != want

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
        percent = primary.get("percentage")
        if not isinstance(percent, (int, float)):
            percent = (used / total * 100) if total else 0.0

        def _item(node: dict, name: str) -> dict[str, Any]:
            total_v = _total(node)
            try:
                used_v = float(node.get("used") or 0)
            except (TypeError, ValueError):
                used_v = 0.0
            try:
                remain_v = float(node.get("remaining") or 0)
            except (TypeError, ValueError):
                remain_v = max(total_v - used_v, 0.0)
            pct = node.get("percentage")
            if not isinstance(pct, (int, float)):
                pct = (used_v / total_v * 100) if total_v else 0.0
            return {
                "label": name,
                "used": round(used_v, 4),
                "total": round(total_v, 4),
                "remaining": round(remain_v, 4),
                "percent": round(float(pct), 4),
                "reset_ts": _reset_ts(data),
            }

        # 有额度的一侧排前面（个人版 userQuota 常为 0，主力额度在 addOnQuota），
        # 但另一侧只要非零就也展示——订阅额度与加油包可以并存。
        items = [_item(primary, label)]
        other_node, other_label = (user_q, "订阅额度") if label == "加油包" else (addon_q, "加油包")
        if _total(other_node) > 0:
            items.append(_item(other_node, other_label))

        return {
            "level": data.get("userType") or cred.plan or None,
            "usage_type": data.get("usageType") or "credits",
            "quota_exceeded": bool(data.get("isQuotaExceeded")),
            "total_percent": data.get("totalUsagePercentage"),
            "upgrade_url": data.get("upgradeUrl"),
            "region": self._region.key,
            "items": items,
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
            raise HTTPException(status_code=502, detail=f"qoder 上游连接失败: {exc}") from exc

        if resp.status_code != 200:
            text = (await resp.aread()).decode("utf-8", "replace")[:300]
            await resp.aclose()
            await client.aclose()
            status = resp.status_code if resp.status_code in (401, 429) else 502
            raise HTTPException(
                status_code=status,
                detail=f"qoder 上游 HTTP {resp.status_code}: {text}",
            )

        if want_stream:
            return StreamingResponse(
                self._stream(resp, client, model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(await self._collect(resp, client, model))

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
            upstream["messages"] = [
                {**m, "role": "system"} if isinstance(m, dict) and m.get("role") == "developer" else m
                for m in messages
            ]

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
            raise HTTPException(status_code=502, detail=f"qoder 上游错误: {error}")

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
        return None, str(chunk.get("message") or chunk)[:300], False
    return body, None, False


def _sse_error(message: str) -> bytes:
    """构造 OpenAI 风格的 SSE 错误帧。"""
    payload = json.dumps({"error": {"message": message, "type": "upstream_error"}},
                         ensure_ascii=False)
    return f"data: {payload}\n\n".encode()


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


def _reset_ts(data: dict[str, Any]) -> int | None:
    """额度重置时间（上游给 ``expiresAt``，毫秒）。"""
    value = data.get("expiresAt")
    if isinstance(value, (int, float)) and value > 0:
        # 上游偶尔用 253402214400000（9999 年）表示「不重置」，过滤掉。
        if value < 4102444800000:
            return int(value / 1000)
    return None


def ensure_credential_sync(region: Region) -> Credential:
    """同步取凭据（只读，不刷新）——供 ``ensure_auth`` 启动校验用。"""
    from .credentials import resolve_credential

    return resolve_credential(region)
