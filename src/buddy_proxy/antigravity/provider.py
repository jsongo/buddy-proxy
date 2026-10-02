"""Antigravity provider —— cloudcode-pa 免费通道转发。

上游是 Gemini 原生协议的 antigravity 包装（``/v1internal:streamGenerateContent
?alt=sse``，envelope 见 convert.py），请求/响应双向转换的骨架与 gemini 通道
同款；差异：

- 端点 fallback：daily-cloudcode-pa 优先、cloudcode-pa 兜底，请求级网络错误
  /5xx/404 才换端点（4xx 业务错误换端点没意义）。
- reasoning_effort 自动映射成 gemini 3 系模型名的 effort 后缀。
- quota：上游有 ``fetchAvailableModels``（带各模型剩余比例与重置时间），
  可画真进度条；拿不到时退化为静态说明（与 gemini 同策略）。

认证：OAuth（buddy login antigravity），access_token 过期自动刷新（401 重试一次）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from ..providers.base import BaseProvider
from ..gemini.convert import gemini_response_to_chat
from .convert import apply_effort_suffix, chat_to_antigravity_request
from .credentials import AuthError, ensure_access_token, has_cred, load_cred

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


def _run_sync(factory):
    """协程工厂 → 独立事件循环执行（/ui 线程安全，见 mimo/provider._run_sync）。"""
    import concurrent.futures

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

        # token 刷新是同步 urllib；丢线程池避免卡事件循环
        access_token = await asyncio.to_thread(_token_or_raise)

        cred = load_cred() or {}
        project_id = str(cred.get("project_id") or "")
        if not project_id:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "antigravity 凭据缺 project_id，请重新 `buddy login antigravity`",
                        "type": "authentication_error",
                    }
                },
            )

        upstream_body = chat_to_antigravity_request(
            body, project_id=project_id, model=upstream_model
        )
        method = "streamGenerateContent" if stream else "generateContent"

        from .fingerprint import auth_headers

        client = await self._get_client()
        resp = await self._send_with_fallback(
            client, method, upstream_body, auth_headers(access_token, upstream_model, stream), stream
        )

        # 401：access_token 失效（比如文件被手工改过）——刷新重试一次
        if resp.status_code == 401:
            if stream:
                await resp.aread()
            await resp.aclose()
            access_token = await asyncio.to_thread(_refresh_or_raise)
            resp = await self._send_with_fallback(
                client, method, upstream_body, auth_headers(access_token, upstream_model, stream), stream
            )

        if resp.status_code >= 400:
            if stream:
                await resp.aread()
            try:
                return _upstream_error_response(resp)
            finally:
                await resp.aclose()

        if stream:
            from ..gemini.provider import _to_anthropic_stream, _to_openai_stream

            if protocol == "anthropic":
                return StreamingResponse(
                    _to_anthropic_stream(resp, upstream_model),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "close"},
                )
            return StreamingResponse(
                _to_openai_stream(resp, upstream_model),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "close"},
            )

        try:
            payload = resp.json()
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": {"message": "antigravity upstream returned non-JSON", "type": "bad_gateway"}},
            ) from exc
        finally:
            await resp.aclose()
        chat = gemini_response_to_chat(payload, model=upstream_model)
        if protocol == "anthropic":
            from ..protocols.anthropic_adapter import chat_completion_to_anthropic_message

            chat = chat_completion_to_anthropic_message(chat, original)
        return JSONResponse(content=chat)

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
        cred = load_cred() or {}
        return {
            "id": self.id,
            "name": self.name,
            "configured": has_cred(),
            "email": cred.get("email") or "",
            "project_id": cred.get("project_id") or "",
            "tier": cred.get("tier") or "",
            "models": list(DEFAULT_MODELS),
        }

    def quota(self) -> dict[str, Any] | None:
        """免费额度：fetchAvailableModels 带各模型剩余比例（可画真进度条）。

        同一组（gemini / claude-gpt）内模型共享 weekly + 5h 双池；上游把
        ``quotaInfo.remainingFraction``（0~1，两池取当前的合成水位）记在
        每个模型变体上（gemini 系按 effort 后缀分条）。这里按 upstream 名
        前缀归模型、按组聚合，组内取最小值代表该组水位。展示约定与前端
        ``quotaItemHtml`` 对齐：``percent`` 是**已用**百分比（进度条语义）、
        ``remaining/total`` 用千分制（0.9987 → 998.7/1000，小数看着直观）。
        拿不到（未登录/接口失败）退化为静态说明，不画假进度条。
        """
        if not has_cred():
            return None

        def _fetch() -> dict[str, Any] | None:
            try:
                return _run_sync(self._fetch_available_models)  # 传工厂，不是 coroutine
            except Exception:  # noqa: BLE001 - quota 展示失败不影响主链路
                return None

        data = _fetch()
        items: list[dict[str, Any]] = []
        if isinstance(data, dict) and isinstance(data.get("models"), dict):
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
            for group, fracs in by_group.items():
                label = "Gemini 组" if group == "gemini" else "Claude/GPT 组"
                worst = min(f for f, _ in fracs)
                reset = min((t for _, t in fracs if t > 0), default=0)
                items.append({
                    "label": f"{label}（组内共享 weekly + 5h 双池，取组内最紧水位）",
                    "used": None,
                    "total": 1000,
                    "remaining": round(worst * 1000, 1),
                    "percent": round((1 - worst) * 100, 2),  # 前端 percent=已用
                    "reset_ts": reset or None,
                })
        if not items:
            items.append({
                "label": _QUOTA_NOTE,
                "used": None,
                "total": None,
                "remaining": None,
                "percent": None,
                "reset_ts": None,
            })
        cred = load_cred() or {}
        return {
            "items": items,
            "level": str(cred.get("tier_name") or cred.get("tier") or "free-tier"),
        }

    async def _fetch_available_models(self) -> dict[str, Any]:
        """POST /v1internal:fetchAvailableModels（带各模型配额剩余）。

        用一次性 client：本方法经 ``_run_sync`` 在临时事件循环里跑（/ui 线程），
        复用缓存的 ``self._client`` 会把连接池绑到这个短命 loop 上——loop 关闭
        后主循环的转发请求复用它就 ``RuntimeError: Event loop is closed``
        （未捕获 → internal error）。quota 是低频管理操作，新建开销可忽略。
        """
        from .fingerprint import auth_headers

        token = await asyncio.to_thread(_token_or_raise)
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
# token 错误转 HTTP（与 gemini/provider 同款）
# ---------------------------------------------------------------------------

def _token_or_raise() -> str:
    try:
        return ensure_access_token()
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": str(exc), "type": "authentication_error"}},
        ) from exc


def _refresh_or_raise() -> str:
    from .credentials import refresh_cred

    cred = load_cred()
    if cred is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "antigravity 未登录", "type": "authentication_error"}},
        )
    try:
        cred = refresh_cred(cred)
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": f"刷新凭据失败: {exc}", "type": "authentication_error"}},
        ) from exc
    return cred["access_token"]


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
