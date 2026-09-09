"""PAT 原生转发：响应识别（错误码/假成功）与稳定主备流式/非流式发送。

failover 语义：首个语义事件提交后绝不重放（防重复计费）；白名单额度码
短冷却换号；通道级 4031 全账号拦截时快速失败；上游「假成功」（done 但
零语义）换号重试且不标冷却。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterator

import httpx
from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from buddy_proxy.trae.config import BASE_URL_CN
from buddy_proxy.trae.native_tools import _content_blocks, _native_tools_payload
from buddy_proxy.trae.sse import _parse_sse, _SSEDecoder

from .config import (
    _CHAT_TIMEOUT_S,
    PatCredentials,
    _CONNECT_RETRY_DELAYS_S,
    _CONNECT_RETRY_ERRNOS,
    _FAILOVER_SSE_CODES,
    _PLUS_GATEWAY,
)
from .cooldown import (
    _mark_account_cooldown,
    _mark_channel_exhausted,
    _mark_cooldown,
    _ordered_available_profiles,
    _quota_class,
    _raise_channel_exhausted,
)
from .credentials import _pat_headers
from .keeper import start_token_keeper
from .models import pat_gateway_is_plus

log = logging.getLogger(__name__)

def _json_error_code(text: str) -> int | None:
    """裸 JSON 错误体的 code；仅用于识别是否属于可切号闭集。"""
    try:
        data = json.loads(text)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return int(data.get("code"))
    except (TypeError, ValueError):
        return None


def _sse_failover_code(raw: str) -> int | None:
    """识别额度耗尽业务码；返回 None 表示不切换。"""
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        code = _json_error_code(stripped)
        return code if code is not None and code in _FAILOVER_SSE_CODES else None
    try:
        for event, data in _parse_sse(raw):
            if event != "error" or not isinstance(data, dict):
                continue
            code = data.get("code")
            try:
                numeric = int(code)
            except (TypeError, ValueError):
                continue
            if numeric in _FAILOVER_SSE_CODES:
                return numeric
    except Exception:
        pass
    return None


def _sse_has_semantic_content(raw: str) -> bool:
    """非流式整段 SSE 是否含任何语义内容（text/tool_calls/reasoning）。

    用于「假成功」检测：上游正常返回 done（甚至带 token_usage）但全文
    零语义——2026-09-09 实测 gpt-5.6-sol 间歇性出现，此时换号重试一次
    往往能拿到正常响应。含显式 error 事件的响应不属于假成功（旧逻辑
    已按错误码处理），不在此重试。
    """
    stripped = raw.lstrip()
    if stripped.startswith("{"):
        # 裸 JSON：只有携带语义字段才算有内容（错误 JSON 已在别处拦截）。
        try:
            data = json.loads(stripped)
        except Exception:
            return False
        if not isinstance(data, dict):
            return False
        # 上游裸 JSON 可能走 OpenAI choices 形态，也可能直接返回 Trae 原生
        # 字段（response/reasoning_content/tool_calls，或嵌在 data 下）；两种
        # 都算语义内容，避免把有效原生响应误判为空而多余换号重试。
        native_nodes = [data]
        if isinstance(data.get("data"), dict):
            native_nodes.append(data["data"])
        for node in native_nodes:
            if node.get("response") or node.get("reasoning_content") or node.get("tool_calls"):
                return True
        choices = data.get("choices") or []
        for choice in choices:
            message = (choice or {}).get("message") or {}
            if (message.get("content") or message.get("reasoning_content")
                    or message.get("tool_calls")):
                return True
        return False
    try:
        for event, data in _parse_sse(raw):
            if event == "error":
                return True  # 显式错误走原有错误处理，不是假成功
            if _semantic_event(event, data):
                return True
    except Exception:
        pass
    return False


# ───────────────────────── 聊天转发 ─────────────────────────

def _build_pat_body(native_msgs: list[dict[str, Any]], model: str, config: str,
                    stream: bool, tools: list[dict[str, Any]] | None,
                    model_id: str = "") -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    # 个别模型只在特定 function 下开放（gpt-6-astra 仅 solo_agent），目录里
    # 可按模型覆盖；其余走全局默认。
    function = _ns.PAT_MODEL_FUNCTIONS.get(model_id) or os.environ.get(
        "WB_TRAE_NATIVE_FUNCTION", "chat_v3")
    body: dict[str, Any] = {
        "messages": native_msgs,
        "model": model,
        "config_name": config,
        "function": function,
        "stream": stream,
        "request_id": session_id,
        "session_id": session_id,
    }
    tools_payload = _native_tools_payload(tools)
    if tools_payload:
        body["tools"] = tools_payload
    return body


def _chat_base(model: str) -> str:
    if pat_gateway_is_plus(model):
        base = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
        if not base:
            raise HTTPException(status_code=503, detail="PAT 模型服务未配置")
        return base
    return str(BASE_URL_CN).rstrip("/")


def _is_retryable_connect_error(exc: BaseException) -> bool:
    """仅识别请求尚未建立连接时可安全重放的错误。

    不包含 TimeoutError/socket.timeout/ConnectionResetError：这些可能发生在请求已经
    发出或上游已开始生成之后，重放会带来重复计费或重复副作用。
    """
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return True
    return isinstance(reason, OSError) and reason.errno in _CONNECT_RETRY_ERRNOS


def _post_chat(url: str, payload: bytes, credentials: PatCredentials, stream: bool) -> str:
    """非流式/兼容发送；流式入口使用 ``stream_pat_native`` 增量读取。"""
    headers = _pat_headers(credentials, "text/event-stream" if stream else "application/json")
    delays = (0.0, *_CONNECT_RETRY_DELAYS_S)
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            time.sleep(delay)
        request = urllib.request.Request(url, data=payload, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=_CHAT_TIMEOUT_S) as response:
                return response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt >= len(delays) or not _is_retryable_connect_error(exc):
                raise
            log.warning(
                "PAT chat 建连失败（%s），%.1fs 后重试 %d/%d",
                type(exc.reason if isinstance(exc, urllib.error.URLError) else exc).__name__,
                delays[attempt], attempt, len(_CONNECT_RETRY_DELAYS_S),
            )
    raise AssertionError("unreachable")


def _stream_profile_events(
    url: str,
    payload: bytes,
    credentials: PatCredentials,
    stop: threading.Event,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """打开单账号响应并逐个产生完整 SSE 事件，不缓冲整轮响应。"""
    timeout = httpx.Timeout(
        connect=min(20.0, float(_CHAT_TIMEOUT_S)),
        read=min(30.0, float(_CHAT_TIMEOUT_S)),
        write=min(30.0, float(_CHAT_TIMEOUT_S)),
        pool=min(20.0, float(_CHAT_TIMEOUT_S)),
    )
    headers = _pat_headers(credentials, "text/event-stream")
    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", url, headers=headers, content=payload) as response:
            response.raise_for_status()
            decoder = _SSEDecoder()
            for chunk in response.iter_bytes():
                if stop.is_set():
                    return
                for event in decoder.feed(chunk):
                    yield event
            if not stop.is_set():
                yield from decoder.finish()


def _event_error_code(event: str, data: dict[str, Any]) -> int | None:
    if event != "error" or not isinstance(data, dict):
        return None
    try:
        return int(data.get("code"))
    except (TypeError, ValueError):
        return None


def _semantic_event(event: str, data: dict[str, Any]) -> bool:
    return event == "output" and bool(
        data.get("reasoning_content") or data.get("response") or data.get("tool_calls")
    )


# 上游「假成功」防护：done 正常到达但全程零语义内容（无 text/tool_calls/
# reasoning）的响应换号重试。2026-09-09 实测 gpt-5.6-sol 间歇性出现该形态
# （upstream_done=true、chunk 很多但 response 全空），当时没有任何账号级
# 429/403——是上游服务端抖动，不是额度故障。因此：
# - 不标记账号冷却（避免误伤额度正常的账号）；
# - 最多换号重试 2 次（防空响应风暴放大上游故障）；
# - 重试同样受「未提交」约束——首个语义事件出现后绝不重放（防重复计费）。
_EMPTY_SUCCESS_RETRIES = 2


def stream_pat_native(
    native_msgs: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None = None,
    *,
    stop: threading.Event | None = None,
    meta: dict[str, Any] | None = None,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """稳定主备的 PAT 真流式入口；首个语义事件后永不重放或拼流。

    ``meta`` 为请求级账号 holder（metrics.ACCOUNT_META 的 dict）：每次开始
    使用某账号时写入 ``meta["account"]``，failover 后被覆盖为最终账号。
    """
    start_token_keeper()  # 幂等：请求路径兜底拉起自愈循环
    _ns._reload_pat_models()
    if model not in _ns.PAT_MODELS:
        raise HTTPException(status_code=400, detail=f"模型 {model} 不在 PAT 通道目录内")
    quota_class = _quota_class(model)
    _raise_channel_exhausted(quota_class)  # 通道级 4031 限制期：快速失败，不逐账号探测
    profiles = _ordered_available_profiles(quota_class)
    if not profiles:
        raise HTTPException(status_code=429, detail=f"PAT {quota_class} 账号均在额度冷却中")

    upstream_model, config = _ns.PAT_MODELS[model]
    body = _build_pat_body(native_msgs, upstream_model, config, True, tools, model_id=model)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = f"{_chat_base(model)}/api/agent/v3/llm_utils_chat"
    stop = stop or threading.Event()
    last_status: int | None = None
    empty_retries = 0

    have_credentials = False
    for profile in profiles:
        try:
            credentials = _ns._get_profile_credentials(profile)
        except HTTPException:
            # 同非流式路径：单账号换不到凭据时跳过，不让它拖垮整个通道。
            continue
        have_credentials = True
        if meta is not None:
            meta["account"] = profile.id
        refreshed = False
        rejected_token: str | None = None
        while True:
            committed = False
            saw_terminal = False
            try:
                for event, data in _ns._stream_profile_events(url, payload, credentials, stop):
                    if stop.is_set():
                        return
                    code = _event_error_code(event, data)
                    if code is not None:
                        if not committed and code in _FAILOVER_SSE_CODES:
                            if code == 4031:
                                _mark_channel_exhausted(quota_class)
                            _mark_cooldown(profile, quota_class, code=code)
                            last_status = code
                            break
                        yield event, data
                        return
                    if _semantic_event(event, data):
                        committed = True
                    if event == "done":
                        if not committed:
                            # 上游假成功：done 已到但零语义内容。未向客户端
                            # 提交过任何事件，换号重放无副作用；不是额度
                            # 故障，不标冷却。达到重试上限后显式报 502。
                            empty_retries += 1
                            if empty_retries <= _EMPTY_SUCCESS_RETRIES:
                                log.warning(
                                    "PAT stream 空响应假成功（done 无内容），换号重试 "
                                    "%d/%d，账号序号=%d",
                                    empty_retries, _EMPTY_SUCCESS_RETRIES, profile.index)
                                break
                            raise HTTPException(
                                status_code=502,
                                detail="trae PAT stream returned no content "
                                       f"(after {empty_retries - 1} retries)")
                        saw_terminal = True
                        yield event, data
                        return
                    yield event, data
                else:
                    if stop.is_set():
                        return
                    if not committed:
                        # 流自然耗尽也无语义内容：与 done 分支同处理。
                        empty_retries += 1
                        if empty_retries <= _EMPTY_SUCCESS_RETRIES:
                            log.warning(
                                "PAT stream 流耗尽零内容，换号重试 %d/%d，账号序号=%d",
                                empty_retries, _EMPTY_SUCCESS_RETRIES, profile.index)
                            break
                        raise HTTPException(
                            status_code=502, detail="trae PAT stream returned no content")
                    if not saw_terminal:
                        raise HTTPException(
                            status_code=502, detail="trae PAT stream ended before completion")
                    return
                # 只有首个语义事件前的白名单错误才会走到这里并尝试下一账号。
                break
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                last_status = status
                if status == 401 and not refreshed:
                    refreshed = True
                    rejected_token = credentials.token
                    try:
                        credentials = _ns._get_profile_credentials(
                            profile, force_refresh=True, rejected_token=rejected_token)
                    except HTTPException:
                        raise HTTPException(
                            status_code=502, detail="trae PAT credential refresh unavailable") from None
                    continue
                if status in (403, 429):
                    _mark_account_cooldown(profile, retry_after=exc.response.headers.get("Retry-After"))
                    break
                if status == 401 and credentials.token != rejected_token:
                    _mark_account_cooldown(profile)
                    break
                raise HTTPException(
                    status_code=502, detail=f"trae PAT chat failed: HTTP {status}") from None
            except HTTPException:
                raise
            except Exception as exc:
                log.warning("PAT stream 传输失败，账号序号=%d（%s）", profile.index, type(exc).__name__)
                raise HTTPException(status_code=502, detail="trae PAT chat transport failed") from None

    if not have_credentials:
        raise HTTPException(status_code=502, detail="trae PAT credential unavailable")
    if empty_retries > 0:
        # 所有账号均返回空响应假成功：不是额度故障，报 502 而非 401，
        # 避免误导客户端去重新登录。
        raise HTTPException(
            status_code=502,
            detail="trae PAT stream returned no content (empty success on "
                   f"{empty_retries} account(s))")
    status = 429 if last_status in (403, 429) or last_status in _FAILOVER_SSE_CODES else 401
    raise HTTPException(status_code=status, detail="trae PAT 所有账号均不可用")


def send_pat_native(native_msgs: list[dict[str, Any]], model: str, stream: bool,
                    tools: list[dict[str, Any]] | None = None,
                    meta: dict[str, Any] | None = None) -> str:
    """稳定主备发送；同一序列化 payload/session 在所有账号和重放间保持不变。

    ``meta`` 语义同 :func:`stream_pat_native`：记录最终使用的账号 id。
    """
    start_token_keeper()  # 幂等：请求路径兜底拉起自愈循环
    _ns._reload_pat_models()
    if model not in _ns.PAT_MODELS:
        raise HTTPException(status_code=400, detail=f"模型 {model} 不在 PAT 通道目录内")
    upstream_model, config = _ns.PAT_MODELS[model]
    quota_class = _quota_class(model)
    _raise_channel_exhausted(quota_class)  # 通道级 4031 限制期：快速失败，不逐账号探测
    profiles = _ordered_available_profiles(quota_class)
    if not profiles:
        raise HTTPException(status_code=429, detail=f"PAT {quota_class} 账号均在额度冷却中")

    # 必须在账号循环外构造一次，保证 messages/tools/request_id/session_id 完全相同。
    body = _build_pat_body(native_msgs, upstream_model, config, stream, tools, model_id=model)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = f"{_chat_base(model)}/api/agent/v3/llm_utils_chat"
    last_status: int | None = None
    empty_retries = 0

    have_credentials = False
    for profile in profiles:
        try:
            credentials = _ns._get_profile_credentials(profile)
        except HTTPException:
            # 换不到凭据（如该账号从未换到 token 且交换端点当前不可达）不是额度
            # 信号，不标记冷却；也不能让单个账号拖垮整个通道——后面账号可能仍有
            # 未过期缓存 token（交换端点离线时这是唯一可用凭据）。全部账号都
            # 拿不到凭据时才整体失败。
            continue
        have_credentials = True
        if meta is not None:
            meta["account"] = profile.id
        refreshed = False
        rejected_token: str | None = None
        while True:
            try:
                raw = _ns._post_chat(url, payload, credentials, stream)
            except urllib.error.HTTPError as exc:
                status = exc.code
                last_status = status
                # 消耗响应体但不记录、不回显，避免上游把秘密带进诊断。
                try:
                    exc.read()
                except Exception:
                    pass
                if status == 401 and not refreshed:
                    refreshed = True
                    rejected_token = credentials.token
                    try:
                        credentials = _ns._get_profile_credentials(
                            profile, force_refresh=True, rejected_token=rejected_token)
                    except HTTPException:
                        # 强刷交换失败不是账号故障；保持在当前账号并返回中性错误。
                        raise HTTPException(
                            status_code=502, detail="trae PAT credential refresh unavailable") from None
                    continue
                if status in (403, 429):
                    _mark_account_cooldown(
                        profile,
                        retry_after=exc.headers.get("Retry-After") if exc.headers else None,
                    )
                    break
                if status == 401:
                    # 只有确实取得不同的新 token 后仍被拒绝，才证明是账号级故障。
                    # 此时允许尝试下一账号，并记录短暂的账号级 cooldown。
                    if credentials.token != rejected_token:
                        _mark_account_cooldown(profile)
                        break
                    raise HTTPException(status_code=401, detail="trae PAT authentication failed") from None
                raise HTTPException(status_code=502,
                                    detail=f"trae PAT chat failed: HTTP {status}") from None
            except Exception as exc:
                log.warning("PAT chat 传输失败，账号序号=%d（%s）", profile.index, type(exc).__name__)
                raise HTTPException(status_code=502, detail="trae PAT chat transport failed") from None
            failover_code = _sse_failover_code(raw)
            if failover_code is not None:
                if failover_code == 4031:
                    _mark_channel_exhausted(quota_class)
                _mark_cooldown(profile, quota_class, code=failover_code)
                last_status = failover_code
                break
            # 非白名单错误也可能包在裸 JSON 里（解析层认不出 SSE 事件）；
            # 保持原样返回会变成“空响应”，改为带码号的脱敏错误。
            if raw.lstrip().startswith("{"):
                code = _json_error_code(raw.lstrip())
                if code is not None:
                    raise HTTPException(status_code=502,
                                        detail=f"trae PAT chat failed: code {code}") from None
            # 上游「假成功」防护：请求成功返回但全文零语义内容（2026-09-09
            # 实测 gpt-5.6-sol 间歇性出现）。非流式响应尚未提交给客户端，
            # 换号重放无重复计费风险；不是额度故障，不标冷却。
            if not _sse_has_semantic_content(raw):
                empty_retries += 1
                if empty_retries <= _EMPTY_SUCCESS_RETRIES:
                    log.warning(
                        "PAT chat 空响应假成功（零语义内容），换号重试 %d/%d，账号序号=%d",
                        empty_retries, _EMPTY_SUCCESS_RETRIES, profile.index)
                    break
                # ≥3 个账号时重试次数可能在账号循环中途耗尽；必须在这里
                # 显式报 502，不能落回 return raw 把空 SSE 伪装成 200。
                raise HTTPException(
                    status_code=502,
                    detail="trae PAT chat returned no content (empty success on "
                           f"{empty_retries} account(s))")
            return raw

    if not have_credentials:
        raise HTTPException(status_code=502, detail="trae PAT credential unavailable")
    if empty_retries > 0:
        # 所有账号均返回空响应假成功：不是额度故障，报 502 而非 401，
        # 避免误导客户端去重新登录。
        raise HTTPException(
            status_code=502,
            detail="trae PAT chat returned no content (empty success on "
                   f"{empty_retries} account(s))")
    status = 429 if last_status in (403, 429) or last_status in _FAILOVER_SSE_CODES else 401
    raise HTTPException(status_code=status, detail="trae PAT 所有账号均不可用")


def send_pat_chat(messages: list[dict[str, Any]], model: str, stream: bool,
                  tools: list[dict[str, Any]] | None = None,
                  meta: dict[str, Any] | None = None) -> str:
    native_msgs = [{"role": message.get("role", "user"),
                    "content": _content_blocks(message.get("content"))}
                   for message in messages]
    return send_pat_native(native_msgs, model, stream, tools, meta=meta)
