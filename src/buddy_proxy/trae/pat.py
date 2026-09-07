"""PAT 凭证通道：独立服务账号接入，用于扩展模型目录（GPT/Gemini 等）。

与 Work/IDE 凭证（trae_work.json / storage.json 解密）完全独立的第二账号体系：
- 凭据来源走环境变量（在仓库根 .env 配置，该文件不入库）；
- CloudIDE token 有约 7 天硬过期，临期自动重换（重换需能连上交换端点，
  连不上时沿用缓存旧 token 直至真正过期）；
- 换 token 手册与 .env 配置说明见仓库根 ``.token.md``（本地文件，勿提交）。

环境变量（全部可选；``TRAE_PAT_BEARER`` 未设置时本通道整体停用）：
- ``TRAE_PAT_BEARER``         服务账号密钥
- ``TRAE_PAT_AUTH_URL``       两步交换第一步端点（短期 JWT 在响应头返回）
- ``TRAE_PAT_TOKEN_URL``      两步交换第二步端点（返回 Result.Token/UserID）
- ``TRAE_PAT_PLUS_GATEWAY``   扩展模型目录的网关 base（缺省扩展模型不可用）
- ``TRAE_PAT_TOKEN_FILE``     token 缓存文件路径（默认 ~/.ethan/trae_pat_token.json）

请求形状与 native 通道完全一致（同 headers、同 body、同端点路径），
仅 base_url 与可用模型目录不同，按模型名自动路由。
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from fastapi import HTTPException

from .config import BASE_URL_CN
from .credentials import _build_headers
from .native_tools import _content_blocks, _native_tools_payload

log = logging.getLogger(__name__)

# ───────────────────────── 配置（全部来自环境变量） ─────────────────────────

_BEARER = "TRAE_PAT_BEARER"
_AUTH_URL = "TRAE_PAT_AUTH_URL"
_TOKEN_URL = "TRAE_PAT_TOKEN_URL"
_PLUS_GATEWAY = "TRAE_PAT_PLUS_GATEWAY"
_TOKEN_FILE = "TRAE_PAT_TOKEN_FILE"

# 缓存 token 剩余寿命低于该值时尝试重换（秒）。重换失败不阻塞：旧 token 仍可用。
_REFRESH_MARGIN_S = 2 * 3600
# 交换请求超时（秒）
_EXCHANGE_TIMEOUT_S = 20
# 聊天请求超时（秒）
_CHAT_TIMEOUT_S = 180


def pat_enabled() -> bool:
    """PAT 通道是否启用（配置了服务账号密钥即视为启用）。"""
    return bool(os.environ.get(_BEARER, "").strip())


def _token_file() -> pathlib.Path:
    return pathlib.Path(os.environ.get(_TOKEN_FILE, "")) if os.environ.get(_TOKEN_FILE) \
        else pathlib.Path.home() / ".ethan" / "trae_pat_token.json"


# ───────────────────────── 扩展模型目录 ─────────────────────────

# 外部模型名 -> (上游 model, 上游 config_name)。这些模型只在扩展网关提供，
# 请求经 send_pat_chat 自动路由。目录来自上游模型列表接口（2026-09-07 实测）。
PAT_MODELS: dict[str, tuple[str, str]] = {
    "gpt-5.6-sol-max": ("gpt-5.6-sol__max", "gpt-5.6-sol"),
    "gpt-5.6-sol": ("gpt-5.6-sol", "gpt-5.6-sol"),
    "gpt-5.6-luna-max": ("gpt-5.6-luna__max", "gpt-5.6-luna"),
    "gpt-5.6-terra-max": ("gpt-5.6-terra__max", "gpt-5.6-terra"),
    "gpt-5.5-max": ("gpt-5.5__max", "gpt-5.5"),
    "gpt-5.4": ("gpt-5.4", "gpt-5.4"),
    "gpt-5.2": ("gpt-5.2", "gpt-5.2"),
    "gpt-6-astra-max": ("gpt-6-astra__max", "gpt-6-astra"),
    "gemini-3.1-pro": ("gemini-3.1-pro", "gemini-3.1-pro"),
    "gemini-3-flash": ("gemini-3-flash", "gemini-3-flash"),
    "openrouter-3o-max": ("openrouter-3o__max", "openrouter-3o"),
    "openrouter-2o-max": ("openrouter-2o__max", "openrouter-2o"),
    "openrouter-1o": ("openrouter-1o", "openrouter-1o"),
    "openrouter-1": ("openrouter-1", "openrouter-1"),
}


def pat_model_names() -> list[str]:
    """PAT 通道对外提供的模型名（供 provider.models() 条目注册）。"""
    return list(PAT_MODELS)


# ───────────────────────── token 缓存与两步交换 ─────────────────────────

def _load_cached() -> dict[str, Any] | None:
    f = _token_file()
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text("utf-8"))
    except Exception as e:
        log.warning("PAT token 缓存解析失败: %s", e)
        return None


def _save_cached(token: str, uid: str, expires_at: float) -> None:
    f = _token_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "cloud_ide_token": token,
        "uid": uid,
        "expires_at": expires_at,
        "refreshed_at": time.time(),
    }, ensure_ascii=False, indent=1), "utf-8")
    try:  # 缓存含密钥，收紧权限
        f.chmod(0o600)
    except OSError:
        pass


def _exchange(bearer: str) -> tuple[str, str, float]:
    """两步交换：bearer -> 短期 JWT（响应头） -> CloudIDE token。返回 (token, uid, exp)。"""
    auth_url = os.environ.get(_AUTH_URL, "").strip()
    token_url = os.environ.get(_TOKEN_URL, "").strip()
    if not auth_url or not token_url:
        raise HTTPException(status_code=503, detail=(
            "PAT 换 token 缺少端点配置：请在 .env 设置 TRAE_PAT_AUTH_URL / TRAE_PAT_TOKEN_URL"
            "（见 .token.md）；当前网络也可能不可达，配置好的缓存 token 未过期前仍可用"))
    req = urllib.request.Request(auth_url, data=b"",
                                 headers={"Authorization": f"Bearer {bearer}",
                                          "Accept": "application/json",
                                          "User-Agent": "ByteDanceCLI/1.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=_EXCHANGE_TIMEOUT_S) as resp:
        jwt = (resp.headers.get("x-jwt-token") or "").strip()
    if not jwt:
        raise HTTPException(status_code=502, detail="PAT 第一步交换未返回令牌（检查密钥与端点配置）")

    req = urllib.request.Request(token_url, data=b"{}",
                                 headers={"x-jwt-token": jwt, "Accept": "application/json",
                                          "Content-Type": "application/json",
                                          "User-Agent": "ByteDanceCLI/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=_EXCHANGE_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    result = payload.get("Result") or payload.get("result") or {}
    token = (result.get("Token") or "").strip()
    if not token:
        raise HTTPException(status_code=502, detail="PAT 第二步交换未返回 token（账号可能缺模型权益）")
    uid = (result.get("UserID") or result.get("userid") or "").strip()
    exp = result.get("ExpiredAt") or ""
    expires_at = _parse_expired_at(exp, fallback=time.time() + 6.5 * 86400)
    return token, uid, expires_at


def _parse_expired_at(text: str, fallback: float) -> float:
    """解析 Go RFC3339Nano 时间串（如 2026-09-14T23:44:06.57+08:00），失败给兜底值。"""
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return fallback


def get_pat_credentials(force_refresh: bool = False) -> tuple[str, str]:
    """取 (CloudIDE token, uid)，带文件缓存与临期自动重换。

    重换需要网络可达交换端点；不可达时若旧 token 仍有剩余寿命（>5 分钟）则沿用，
    否则抛 401 提示换网/检查配置。
    """
    cached = _load_cached() or {}
    token = cached.get("cloud_ide_token", "")
    uid = cached.get("uid", "")
    expires_at = float(cached.get("expires_at") or 0)
    now = time.time()

    if not force_refresh and token and expires_at - now > _REFRESH_MARGIN_S:
        return token, uid

    bearer = os.environ.get(_BEARER, "").strip()
    if not bearer:
        raise HTTPException(status_code=401, detail="PAT 通道未配置服务账号密钥")
    try:
        token, uid, expires_at = _exchange(bearer)
        _save_cached(token, uid, expires_at)
        log.info("PAT token 已刷新，有效期至 %s", time.strftime("%m-%d %H:%M", time.localtime(expires_at)))
        return token, uid
    except HTTPException:
        raise
    except Exception as e:
        # 网络不可达（如离开了交换端点所在网络）：旧 token 还能撑就沿用
        if token and expires_at - now > 300:
            log.warning("PAT token 刷新失败（沿用缓存，剩余 %.1f 小时）: %s",
                        (expires_at - now) / 3600, e)
            return token, uid
        raise HTTPException(status_code=401, detail=(
            "PAT token 已过期且自动刷新失败（需在能访问交换端点的网络下重试，见 .token.md）"))


# ───────────────────────── 聊天转发 ─────────────────────────

def _build_pat_body(native_msgs: list[dict[str, Any]], model: str, config: str,
                    stream: bool, tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    """与 native 通道同形状（content 数组 + model/config_name 成对 + function）。

    native_msgs 必须已是线上格式（content 为 block 数组），与
    native_tools._native_messages 的输出同构，不做二次转换。
    """
    sid = str(uuid.uuid4())
    body: dict[str, Any] = {
        "messages": native_msgs,
        "model": model,
        "config_name": config,
        "function": os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3"),
        "stream": stream,
        "request_id": sid,
        "session_id": sid,
    }
    tools_payload = _native_tools_payload(tools)
    if tools_payload:
        body["tools"] = tools_payload
    return body


def send_pat_native(native_msgs: list[dict[str, Any]], model: str, stream: bool,
                    tools: list[dict[str, Any]] | None = None) -> str:
    """PAT 通道发送（线上格式 messages 直通，供 native 通道拦截复用）。

    扩展目录模型走 TRAE_PAT_PLUS_GATEWAY（未配置则报 503）；端点路径、
    headers、body 形状与 native 通道一致，返回原始 SSE 文本。
    """
    if model not in PAT_MODELS:
        raise HTTPException(status_code=400, detail=f"模型 {model} 不在 PAT 通道目录内")
    upstream_model, config = PAT_MODELS[model]
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail=(
            f"模型 {model} 需要在 .env 配置 TRAE_PAT_PLUS_GATEWAY 后可用（见 .token.md）"))
    token, uid = get_pat_credentials()
    body = _build_pat_body(native_msgs, upstream_model, config, stream, tools)
    headers = {**_build_headers(token, uid),
               "Accept": "text/event-stream" if stream else "application/json"}
    url = f"{plus}/api/agent/v3/llm_utils_chat"
    payload = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, data=payload, headers=headers, method="POST"),
                timeout=_CHAT_TIMEOUT_S) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        if e.code in (401, 403):
            # token 可能刚过期：强制重换一次再试
            try:
                token, uid = get_pat_credentials(force_refresh=True)
                headers = {**_build_headers(token, uid),
                           "Accept": "text/event-stream" if stream else "application/json"}
                with urllib.request.urlopen(urllib.request.Request(
                        url, data=payload, headers=headers, method="POST"),
                        timeout=_CHAT_TIMEOUT_S) as resp:
                    return resp.read().decode("utf-8", errors="replace")
            except HTTPException:
                raise
            except Exception as e2:
                raise HTTPException(status_code=401, detail=f"trae PAT auth failed: {e2}") from e2
        raise HTTPException(status_code=502, detail=f"trae PAT chat failed: {e.code} {detail}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"trae PAT chat failed: {e}") from e


def send_pat_chat(messages: list[dict[str, Any]], model: str, stream: bool,
                  tools: list[dict[str, Any]] | None = None) -> str:
    """PAT 通道发送（OpenAI 风格 messages 入参），与 send_trae_chat 同契约。"""
    native_msgs = [{"role": m.get("role", "user"), "content": _content_blocks(m.get("content"))}
                   for m in messages]
    return send_pat_native(native_msgs, model, stream, tools)
