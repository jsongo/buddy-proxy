"""Antigravity provider —— cloudcode-pa 免费通道转发。

上游是 Gemini 原生协议的 antigravity 包装（``/v1internal:streamGenerateContent
?alt=sse``，envelope 见 convert.py），请求/响应双向转换的骨架与 gemini 通道
同款；差异：

- 多账号 failover：按登录顺序主备降级（见 failover.py），429/403/凭据失效
  冷却当前账号换下一个；首个语义事件到达后绝不重放（防重复计费）。
- 端点 fallback：daily-cloudcode-pa 优先、cloudcode-pa 兜底，请求级网络错误
  /5xx/404 才换端点（4xx 业务错误换端点没意义）。
- reasoning_effort 自动映射成 gemini 3 系模型名的 effort 后缀。
- quota：上游有 ``fetchAvailableModels``（带各模型剩余比例与重置时间），
  可画真进度条；拿不到时退化为静态说明（与 gemini 同策略）。

认证：OAuth（buddy login antigravity，支持多账号），access_token 过期自动刷新。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..core.metrics import ACCOUNT_META
from ..gemini.convert import gemini_response_to_chat
from . import failover
from .convert import apply_effort_suffix, chat_to_antigravity_request
from .credentials import (
    AuthError,
    ensure_account_token,
    has_cred,
    list_accounts,
    load_account_cred,
)

log = logging.getLogger(__name__)

ENDPOINTS = (
    "https://daily-cloudcode-pa.googleapis.com",
    "https://cloudcode-pa.googleapis.com",
)
_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)

#: 模型表放 JSON（models.json，随包分发）：实测要增删模型改文件就行。
_MODELS_JSON = Path(__file__).with_name("models.json")


def _load_models() -> list[dict[str, Any]]:
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("antigravity models.json 读取失败（%s），使用内置兜底表", exc)
        return [{"id": "gemini-3.8-flash", "group": "gemini", "description": "Gemini 3.8 Flash (fallback)"}]
    return [m for m in data.get("models") or [] if isinstance(m, dict) and m.get("id")] or [
        {"id": "gemini-3.8-flash", "group": "gemini", "description": "Gemini 3.8 Flash (fallback)"}
    ]


MODELS: list[dict[str, Any]] = _load_models()
DEFAULT_MODELS: dict[str, str] = {m["id"]: str(m.get("description") or m["id"]) for m in MODELS}
_MODEL_BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in MODELS}

_QUOTA_NOTE = "两组模型各自共享 weekly + 5h 双池（Gemini 组 / Claude+GPT 组），按 token 成本比例消耗"

#: 多账号额度并发查询：整轮 deadline + 常驻线程池。
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
                max_workers=_QUOTA_WORKERS, thread_name_prefix="ag-quota")
        return _quota_pool


def _quota_failure_notice(failed: int, total: int) -> dict[str, Any]:
    """部分账号额度查询失败时的说明条（UI 警告色展示；benefits 认 query_failed 走短缓存）。"""
    return {
        "label": "Antigravity 额度查询失败",
        "used": None,
        "total": None,
        "remaining": f"{failed}/{total} 个账号查询失败（网络或凭据问题；其余账号数据照常展示）",
        "percent": None,
        "reset_ts": None,
        "query_failed": True,
    }


def _run_sync(factory):
    """协程工厂 → 独立事件循环执行（/ui 线程安全，见 mimo/provider._run_sync）。"""
    def _runner():
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _runner()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result(timeout=30)


def _upstream_error_response(resp: httpx.Response) -> JSONResponse:
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": {"message": resp.text[:500], "type": "upstream_error"}}
    return JSONResponse(status_code=resp.status_code, content=payload)


def _iso_to_epoch(value: Any) -> float:
    """上游 ISO 时间（``2026-10-02T20:28:46Z``）→ epoch 秒；解析失败返回 0。"""
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class AntigravityProvider(BaseProvider):
    #: /v1/models 里模型前缀即 ``antigravity/gemini-3.1-pro``。
    id = "antigravity"
    name = "Antigravity (Gemini/Claude/GPT free tier)"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    # ---- BaseProvider 接口 ----

    def models(self) -> Sequence[dict[str, Any]]:
        return [
            {
                "id": m["id"],
                "object": "model",
                "created": 0,
                "owned_by": self.id,
                "description": str(m.get("description") or m["id"]),
            }
            for m in MODELS
        ]

    def ensure_auth(self) -> None:
        if not has_cred():
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "antigravity 未登录：请先运行 `buddy login antigravity`",
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
        model = str(body.get("model") or MODELS[0]["id"]).removeprefix(f"{self.id}/")
        # 模型表条目驱动：upstream 真名 + effort 后缀（上游只认列表变体名，
        # gemini 3 系裸名会被 429 RESOURCE_EXHAUSTED 伪装拒绝）
        entry = _MODEL_BY_ID.get(model) or MODELS[0]
        upstream_model = apply_effort_suffix(entry, body.get("reasoning_effort"))

        # failover 循环：按登录顺位尝试可用账号（429/403/凭据失效 → 冷却换号）
        accounts = failover.available_accounts()
        if not accounts:
            raise HTTPException(
                status_code=429,
                detail={"error": {
                    "message": "antigravity 所有账号均在冷却中（额度耗尽或凭据问题），稍后自动恢复",
                    "type": "rate_limit_error",
                }},
            )

        from .fingerprint import auth_headers

        meta = ACCOUNT_META.get()  # metrics 账号归属（_instrument 放入的 dict）
        client = await self._get_client()
        method = "streamGenerateContent" if stream else "generateContent"
        last_status = 0
        last_detail = ""

        for acct in accounts:
            # token 刷新是同步 urllib；丢线程池避免卡事件循环。
            # token 与 cred 同源返回：project_id 从同一份 cred 取，多账号不串号。
            try:
                access_token, cred = await asyncio.to_thread(ensure_account_token, acct.id)
            except AuthError as exc:
                failover.mark_cooldown(acct.id, reason=f"凭据不可用: {exc}")
                continue
            project_id = str(cred.get("project_id") or "")
            if not project_id:
                failover.mark_cooldown(acct.id, reason="缺 project_id（onboarding 未完成）")
                continue
            if meta is not None:
                meta["account"] = acct.id

            upstream_body = chat_to_antigravity_request(
                body, project_id=project_id, model=upstream_model
            )
            resp = await self._send_with_fallback(
                client, method, upstream_body,
                auth_headers(access_token, upstream_model, stream), stream,
            )

            # 401：token 被上游拒（比如文件被手工改过）——强刷一次重试同账号；
            # 仍拒说明凭据层面失效，冷却换号
            if resp.status_code == 401:
                await _drain_and_close(resp, stream)
                try:
                    access_token, _ = await asyncio.to_thread(
                        ensure_account_token, acct.id, force_refresh=True)
                except AuthError as exc:
                    failover.mark_cooldown(acct.id, reason=f"强刷失败: {exc}")
                    continue
                resp = await self._send_with_fallback(
                    client, method, upstream_body,
                    auth_headers(access_token, upstream_model, stream), stream,
                )
                if resp.status_code == 401:
                    last_status, last_detail = 401, await _error_detail(resp, stream)
                    await _drain_and_close(resp, stream)
                    failover.mark_cooldown(acct.id, reason="401 强刷后仍被拒")
                    continue

            # 403/429：账号级拒绝（额度/风控）——冷却该账号换下一个
            if resp.status_code in (403, 429):
                last_status = resp.status_code
                last_detail = await _error_detail(resp, stream)
                failover.mark_cooldown(
                    acct.id,
                    retry_after=resp.headers.get("Retry-After"),
                    quota=resp.status_code == 429,
                    reason=f"HTTP {resp.status_code}{(': ' + last_detail) if last_detail else ''}",
                )
                await _drain_and_close(resp, stream)
                continue

            if resp.status_code >= 400:
                # 其余业务 4xx（模型名不合法等）：换号没意义，原样透传
                if stream:
                    await resp.aread()
                try:
                    return _upstream_error_response(resp)
                finally:
                    await resp.aclose()

            # 200：过首事件闸门——首个上游事件若是 429/403 error，还没向客户端
            # 吐过任何字节，可以安全冷却换号；见到语义事件后绝不重放（防重复计费）
            gate = await self._gate_first_event(resp, stream)
            if gate.account_error:
                last_status, last_detail = gate.code, gate.message
                failover.mark_cooldown(
                    acct.id, quota=gate.code == 429,
                    reason=f"带内 error {gate.code}: {gate.message}")
                await _drain_and_close(gate.resp, stream)
                continue
            if gate.eof:
                # 语义事件之前断流/空流：没向客户端吐过字节，换下一个账号
                await _drain_and_close(gate.resp, stream)
                continue

            if stream:
                from ..gemini.provider import _to_anthropic_stream, _to_openai_stream

                replay = _ReplayStream(gate.resp, gate.buffered)
                if protocol == "anthropic":
                    return StreamingResponse(
                        _to_anthropic_stream(replay, upstream_model),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "close"},
                    )
                return StreamingResponse(
                    _to_openai_stream(replay, upstream_model),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )

            payload = gate.payload
            if payload is None:
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "antigravity upstream returned non-JSON",
                                      "type": "bad_gateway"}},
                )
            await gate.resp.aclose()
            chat = gemini_response_to_chat(payload, model=upstream_model)
            if protocol == "anthropic":
                from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

                chat = chat_completion_to_anthropic_message(chat, original)
            return JSONResponse(content=chat)

        raise HTTPException(
            status_code=last_status if last_status in (401, 403, 429) else 502,
            detail={"error": {
                "message": (f"antigravity 所有账号均不可用（最后错误 HTTP {last_status or 'n/a'}"
                            f"{(': ' + last_detail) if last_detail else ''}）"),
                "type": "rate_limit_error" if last_status in (403, 429) else "bad_gateway",
            }},
        )

    async def _gate_first_event(self, resp: httpx.Response, stream: bool) -> "_Gate":
        """压住第一个上游事件再决定透传还是换号。

        非流式：全量解析 JSON，``error.code ∈ {429,403}`` 视为账号级错误
        （此时一个字节都没出网，换号绝对安全）。流式：缓冲原始 SSE 行直到
        第一条能定性的事件——见 candidates 即语义已至（committed，缓冲行随
        透传补放，客户端无损）；error 节点按 code 分类；只有 usageMetadata
        则继续等；语义事件前断流/EOF 按 eof 处理（换号）。
        """
        if not stream:
            try:
                payload = resp.json()
            except Exception:
                return _Gate(resp=resp, committed=True, payload=None)
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                code = int(err.get("code") or 0)
                if code in (429, 403):
                    return _Gate(resp=resp, account_error=True, code=code,
                                 message=str(err.get("message") or ""))
            return _Gate(resp=resp, committed=True,
                         payload=payload if isinstance(payload, dict) else None)

        buffered: list[str] = []
        try:
            async for line in resp.aiter_lines():
                buffered.append(line)
                stripped = line.strip()
                if not stripped.startswith("data:"):
                    continue
                data = stripped[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                inner = payload.get("response") if isinstance(payload.get("response"), dict) else payload
                if not isinstance(inner, dict):
                    continue
                err = inner.get("error")
                if isinstance(err, dict):
                    code = int(err.get("code") or 0)
                    if code in (429, 403):
                        return _Gate(resp=resp, account_error=True, code=code,
                                     message=str(err.get("message") or ""))
                    # 其它带内错误（400 等）：换号没意义，缓冲行随透传交给转换器
                    return _Gate(resp=resp, committed=True, buffered=buffered)
                if inner.get("candidates"):
                    return _Gate(resp=resp, committed=True, buffered=buffered)
                # 仅 usageMetadata 等非语义事件：继续等下一条
        except httpx.HTTPError:
            return _Gate(resp=resp, eof=True)
        return _Gate(resp=resp, eof=True)  # 语义事件前 EOF：假成功，换号

    # ---- 内部 ----

    async def _send_with_fallback(
        self,
        client: httpx.AsyncClient,
        method: str,
        body: dict[str, Any],
        headers: dict[str, str],
        stream: bool,
    ) -> httpx.Response:
        """请求级 fallback：daily 失败（网络/5xx/404）换 prod；业务 4xx 不换。"""
        last: httpx.Response | None = None
        for i, base in enumerate(ENDPOINTS):
            url = f"{base}/v1internal:{method}"
            if stream:
                url += "?alt=sse"
            req = client.build_request("POST", url, json=body, headers=headers)
            try:
                resp = await client.send(req, stream=stream)
            except httpx.TimeoutException as exc:
                log.warning("antigravity upstream timeout (%s): %s", base, exc)
                raise HTTPException(
                    status_code=504,
                    detail={"error": {"message": "antigravity upstream timeout", "type": "timeout"}},
                ) from exc
            except httpx.HTTPError as exc:
                log.warning("antigravity upstream error (%s): %s", base, exc)
                if i + 1 < len(ENDPOINTS):
                    continue
                raise HTTPException(
                    status_code=502,
                    detail={"error": {"message": "antigravity upstream error", "type": "bad_gateway"}},
                ) from exc
            # 5xx/404：端点侧问题，试下一个；其余（401/429/4xx）直接返回
            if resp.status_code in (404, 500, 502, 503, 504) and i + 1 < len(ENDPOINTS):
                if stream:
                    await resp.aread()
                await resp.aclose()
                last = resp
                continue
            return resp
        return last  # pragma: no cover - ENDPOINTS 非空，上面必然 return/raise

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=_TIMEOUT)
        return self._client

    def health(self) -> dict[str, Any]:
        """健康概览（纯本地不触网）：顶层字段保持主账号（#1）语义兼容旧前端，
        多账号概览放 ``accounts``（不含 token 等秘密）。"""
        accounts = list_accounts()
        cred = (load_account_cred(accounts[0].id) if accounts else None) or {}
        return {
            "id": self.id,
            "name": self.name,
            "configured": bool(accounts),
            "email": str(cred.get("email") or ""),
            "project_id": str(cred.get("project_id") or ""),
            "tier": str(cred.get("tier") or ""),
            "models": list(DEFAULT_MODELS),
            "accounts": [
                {
                    "id": a.id,
                    "email": a.email or a.id,
                    "project_id": str((load_account_cred(a.id) or {}).get("project_id") or ""),
                }
                for a in accounts
            ],
        }

    def quota(self) -> dict[str, Any] | None:
        """免费额度：fetchAvailableModels 带各模型剩余比例（可画真进度条）。

        同一组（gemini / claude-gpt）内模型共享 weekly + 5h 双池；上游把
        ``quotaInfo.remainingFraction``（0~1，两池取当前的合成水位）记在
        每个模型变体上（gemini 系按 effort 后缀分条）。这里按 upstream 名
        前缀归模型、按组聚合，组内取最小值代表该组水位。展示约定与前端
        ``quotaItemHtml`` 对齐：``percent`` 是**已用**百分比（进度条语义）、
        ``remaining/total`` 用千分制（0.9987 → 998.7/1000，小数看着直观）。

        多账号：并发查（常驻线程池 min(4,n) + 整轮 deadline 兜底），每账号
        两条（Gemini 组 / Claude+GPT 组），label 带 ``AG #N · `` 前缀供前端
        分组；个别账号失败不拖垮整页，插 ``query_failed`` 说明条（benefits
        层认这个标记走短缓存，网络恢复后尽快自愈）。拿不到退化为静态说明，
        不画假进度条。
        """
        accounts = list_accounts()
        if not accounts:
            return None
        multi = len(accounts) > 1

        if multi:
            pool = _quota_executor()
            futures = [pool.submit(self._quota_one, a, i + 1, multi=True)
                       for i, a in enumerate(accounts)]
            deadline = time.monotonic() + _QUOTA_ROUND_DEADLINE_S
            items: list[dict[str, Any]] = []
            failures = 0
            for fut in futures:  # 按 failover 顺位收集，UI 顺序稳定
                try:
                    its, ok = fut.result(timeout=max(deadline - time.monotonic(), 0.05))
                except Exception:  # noqa: BLE001 - 超时/异常账号都算失败
                    its, ok = [], False
                if ok:
                    items.extend(its)
                else:
                    failures += 1
            if failures:
                items.insert(0, _quota_failure_notice(failures, len(accounts)))
        else:
            its, ok = self._quota_one(accounts[0], 1, multi=False)
            items = its if (ok and its) else [{
                "label": _QUOTA_NOTE,
                "used": None,
                "total": None,
                "remaining": None,
                "percent": None,
                "reset_ts": None,
            }]
        cred = (load_account_cred(accounts[0].id) if accounts else None) or {}
        return {
            "items": items,
            "level": str(cred.get("tier_name") or cred.get("tier") or "free-tier"),
        }

    def _quota_one(
        self, acct: Any, index: int, *, multi: bool
    ) -> tuple[list[dict[str, Any]], bool]:
        """单账号额度查询：成功返回 (items, True)，失败 ([], False)。"""
        try:
            data = _run_sync(lambda: self._fetch_available_models(acct.id))
        except Exception:  # noqa: BLE001 - quota 展示失败不影响主链路
            return [], False
        if not (isinstance(data, dict) and isinstance(data.get("models"), dict)):
            return [], False
        prefix = f"AG #{index} · " if multi else ""
        return self._quota_items_from(data, prefix), bool(data["models"])

    @staticmethod
    def _quota_items_from(data: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
        """fetchAvailableModels 响应 → 按组聚合的额度条目（prefix 拼在 label 前）。"""
        upstream = data["models"]
        by_group: dict[str, list[tuple[float, float]]] = {}  # group -> [(frac, reset_epoch)]
        for m in MODELS:
            base = str(m.get("upstream") or m["id"])
            fracs: list[tuple[float, float]] = []
            for key, q in upstream.items():
                if key != base and not key.startswith(f"{base}-"):
                    continue  # 变体名（-low/-medium/-high/-tiered）也算这个模型的
                if not isinstance(q, dict):
                    continue
                info = q.get("quotaInfo") or {}
                frac = info.get("remainingFraction")
                if not isinstance(frac, (int, float)):
                    continue
                fracs.append((float(frac), _iso_to_epoch(info.get("resetTime"))))
            if fracs:
                by_group.setdefault(str(m.get("group") or "gemini"), []).extend(fracs)
        items: list[dict[str, Any]] = []
        for group, fracs in by_group.items():
            label = "Gemini 组" if group == "gemini" else "Claude/GPT 组"
            worst = min(f for f, _ in fracs)
            reset = min((t for _, t in fracs if t > 0), default=0)
            items.append({
                "label": f"{prefix}{label}（组内共享 weekly + 5h 双池，取组内最紧水位）",
                "used": None,
                "total": 1000,
                "remaining": round(worst * 1000, 1),
                "percent": round((1 - worst) * 100, 2),  # 前端 percent=已用
                "reset_ts": reset or None,
            })
        return items

    async def _fetch_available_models(self, account_id: str) -> dict[str, Any]:
        """POST /v1internal:fetchAvailableModels（带各模型配额剩余）。

        用一次性 client：本方法经 ``_run_sync`` 在临时事件循环里跑（/ui 线程），
        复用缓存的 ``self._client`` 会把连接池绑到这个短命 loop 上——loop 关闭
        后主循环的转发请求复用它就 ``RuntimeError: Event loop is closed``
        （未捕获 → internal error）。quota 是低频管理操作，新建开销可忽略。
        """
        from .fingerprint import auth_headers

        token, _cred = await asyncio.to_thread(ensure_account_token, account_id)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await self._send_with_fallback(
                client, "fetchAvailableModels", {}, auth_headers(token), stream=False
            )
            try:
                if resp.status_code >= 400:
                    return {}
                return resp.json()
            finally:
                await resp.aclose()


# ---------------------------------------------------------------------------
# failover 支撑：token 错误转 HTTP / 闸门类型 / 流适配
# ---------------------------------------------------------------------------

def _primary_token() -> str:
    """主账号 token（quota/health 这类管理面用，不走 failover）。"""
    accounts = list_accounts()
    if not accounts:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "antigravity 未登录", "type": "authentication_error"}},
        )
    try:
        return ensure_account_token(accounts[0].id)[0]
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": str(exc), "type": "authentication_error"}},
        ) from exc


@dataclass
class _Gate:
    """首事件闸门的判定结果。"""
    resp: httpx.Response
    committed: bool = False  # 已见语义事件：缓冲行必须透传，绝不重放
    buffered: list[str] = field(default_factory=list)  # 闸门期间缓冲的原始 SSE 行
    payload: dict[str, Any] | None = None  # 非流式：解析好的 JSON
    account_error: bool = False  # 429/403 账号级错误：冷却换号
    code: int = 0
    message: str = ""
    eof: bool = False  # 语义事件前断流/EOF：假成功，换号


class _ReplayStream:
    """闸门缓冲行 → 真实流的适配器（先补放缓冲，再接原流）。

    gemini 转换器只用 ``aiter_lines()``/``aclose()``，实现这两个就够了，
    转换器零改动。
    """

    def __init__(self, resp: httpx.Response, buffered: list[str]) -> None:
        self._resp = resp
        self._buffered = list(buffered)

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._buffered:
            yield line
        async for line in self._resp.aiter_lines():
            yield line

    async def aclose(self) -> None:
        await self._resp.aclose()


async def _drain_and_close(resp: httpx.Response, stream: bool) -> None:
    """读完丢弃响应体并关闭（换号前必须回收连接，别挂着半开流）。"""
    try:
        if stream:
            await resp.aread()
    finally:
        await resp.aclose()


async def _error_detail(resp: httpx.Response, stream: bool) -> str:
    """从错误响应体抽 ``error.message``（只用于日志/客户端报错文案）。"""
    try:
        if stream:
            await resp.aread()
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 文案而已，拿不到就空着
        return ""
    err = payload.get("error") if isinstance(payload, dict) else None
    return str(err.get("message") or "") if isinstance(err, dict) else ""


# ---------------------------------------------------------------------------
# 冒烟自测：python -m buddy_proxy.antigravity.provider
# ---------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    p = AntigravityProvider()
    print("health:", json.dumps(p.health(), ensure_ascii=False))
    if not has_cred():
        print("未登录：先跑 buddy login antigravity")
        raise SystemExit(1)
    resp = asyncio.run(p.forward(
        {
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "只回复两个字：pong"}],
            "stream": False,
            "max_tokens": 32,
        },
        "openai",
    ))
    print("status:", resp.status_code)
    print(str(resp.body)[:400])
