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

# 模型目录唯一来源：仓库根 src/buddy_proxy/models_config.json 中
# 带 "provider": "traepat" 的条目。加模型只改 JSON（热加载，无需重启）：
#   gateway:        "plus"（扩展网关）| "public"（默认公网网关，PAT 身份）
#   upstream_model: 上游请求体里的 model 字段
#   config_name:    上游请求体里的 config_name（大小写敏感，实测 DeepSeek-/Doubao- 大写开头）
_MODEL_CONFIG_FILE = pathlib.Path(__file__).resolve().parents[1] / "models_config.json"

# 以下三个 dict 由 _reload_pat_models() 按 JSON 原地重建（引用保持稳定），
# 其它模块的 `from .pat import PAT_MODELS` 拿到的始终是最新内容。
PAT_MODELS: dict[str, tuple[str, str]] = {}
PAT_PLUS_MODELS: dict[str, tuple[str, str]] = {}
PAT_PUBLIC_MODELS: dict[str, tuple[str, str]] = {}
_config_mtime: float | None = None


def _reload_pat_models() -> None:
    """从 models_config.json 热加载 PAT 模型表（mtime 变化才重读）。"""
    global _config_mtime
    try:
        mtime = _MODEL_CONFIG_FILE.stat().st_mtime
    except OSError:
        return
    if _config_mtime == mtime:
        return
    plus: dict[str, tuple[str, str]] = {}
    public: dict[str, tuple[str, str]] = {}
    try:
        data = json.loads(_MODEL_CONFIG_FILE.read_text("utf-8"))
        for m in data.get("models", []):
            if m.get("provider") != "traepat":
                continue
            mid = str(m.get("id") or "").strip()
            um, cn = str(m.get("upstream_model") or mid), str(m.get("config_name") or mid)
            if not mid or not um:
                continue
            (plus if m.get("gateway") == "plus" else public)[mid] = (um, cn)
    except Exception as e:
        log.warning("PAT 模型配置解析失败（沿用上次内容）: %s", e)
        return
    PAT_PLUS_MODELS.clear(); PAT_PLUS_MODELS.update(plus)
    PAT_PUBLIC_MODELS.clear(); PAT_PUBLIC_MODELS.update(public)
    PAT_MODELS.clear(); PAT_MODELS.update({**plus, **public})
    _config_mtime = mtime
    log.info("PAT 模型目录已加载：扩展 %d + 公网 %d", len(plus), len(public))


def pat_model_names() -> list[str]:
    """PAT 通道对外提供的模型名（供 provider.models() 条目注册）。"""
    _reload_pat_models()
    return list(PAT_MODELS)


def pat_gateway_is_plus(model: str) -> bool:
    """该模型是否走扩展网关（False = 默认公网网关）。"""
    _reload_pat_models()
    return model in PAT_PLUS_MODELS


def is_pat_model(model: str) -> bool:
    """模型是否属于 PAT 通道目录（热加载后判断）。"""
    _reload_pat_models()
    return model in PAT_MODELS


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


# ───────────────────────── 额度查询 ─────────────────────────

_QUOTA_CACHE_FILE = pathlib.Path.home() / ".ethan" / "trae_pat_quota_cache.json"
_last_ent_usage: list[dict[str, Any]] | None = None


def _quota_cache_load() -> list[dict[str, Any]] | None:
    """磁盘缓存：上次在可达网络下查到的余量（家里查不到时兜底展示）。"""
    try:
        f = pathlib.Path(_QUOTA_CACHE_FILE)
        if f.exists():
            return json.loads(f.read_text("utf-8"))
    except Exception:
        pass
    return None


def _quota_cache_save(items: list[dict[str, Any]]) -> None:
    try:
        f = pathlib.Path(_QUOTA_CACHE_FILE)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(items, ensure_ascii=False), "utf-8")
    except Exception:
        pass


def fetch_pat_ent_usage() -> list[dict[str, Any]]:
    """查 PAT 账号各权益包余量，返回 [{label, used, total, remaining, reset_ts}]。

    注意：用量取 **pack 顶层 usage**（``quota.usage`` 是滞后旧视图，勿用）。
    端点在扩展网关上（真实余量只在它有；公网权益接口是另一个无数字的视图）。
    网络不可达时回退「内存 -> 磁盘」缓存，条目 label 追加「·缓存」标记。
    """
    global _last_ent_usage
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 通道未配置 TRAE_PAT_PLUS_GATEWAY")
    token, uid = get_pat_credentials()
    headers = {**_build_headers(token, uid), "Accept": "application/json"}
    url = f"{plus}/trae/api/v1/pay/ide_user_ent_usage"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        # 不可达/失败：内存缓存 -> 磁盘缓存，逐级兜底（label 加「·缓存」）
        for cached, mark in ((_last_ent_usage, "内存"), (_quota_cache_load(), "磁盘")):
            if cached:
                return [dict(it, label=f"{it['label']}·缓存") for it in cached]
        if isinstance(e, urllib.error.HTTPError):
            raise HTTPException(status_code=502,
                                detail=f"PAT 额度查询失败: {e.code} {e.read().decode()[:150]}") from e
        raise HTTPException(status_code=502, detail=f"PAT 额度查询失败: {e}") from e

    items: list[dict[str, Any]] = []
    for pack in data.get("user_entitlement_pack_list") or []:
        base = pack.get("entitlement_base_info") or {}
        quota = base.get("quota") or {}
        usage = pack.get("usage") or {}
        limit = quota.get("basic_usage_limit")
        used = usage.get("basic_usage_amount") or 0
        if not isinstance(limit, (int, float)) or limit <= 0:
            continue
        eid = str(base.get("entitlement_id") or "pack")
        kind = "周包" if "weekly" in eid else ("日包" if "daily" in eid else "包")
        # 实测（2026-09-08）：日包实际是账号级「高级模型日额度」，gemini/openrouter
        # 的调用也扣它，ID 里的模型片段不代表归属，标签统一写「高级模型共享」
        label = "PAT 周包（未占用）" if kind == "周包" else "PAT 日包（高级模型共享）"
        end_ts = base.get("end_time") or 0
        items.append({
            "label": label,
            "used": round(used, 2),
            "total": limit,
            "remaining": round(limit - used, 2),
            "percent": round(used / limit * 100) if limit else None,
            "reset_ts": int(end_ts) if end_ts else None,
        })
    _last_ent_usage = items
    _quota_cache_save(items)
    return items


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
    _reload_pat_models()
    if model not in PAT_MODELS:
        raise HTTPException(status_code=400, detail=f"模型 {model} 不在 PAT 通道目录内")
    upstream_model, config = PAT_MODELS[model]
    if pat_gateway_is_plus(model):
        base = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
        if not base:
            raise HTTPException(status_code=503, detail=(
                f"模型 {model} 需要在 .env 配置 TRAE_PAT_PLUS_GATEWAY 后可用（见 .token.md）"))
    else:
        base = str(BASE_URL_CN).rstrip("/")
    token, uid = get_pat_credentials()
    body = _build_pat_body(native_msgs, upstream_model, config, stream, tools)
    headers = {**_build_headers(token, uid),
               "Accept": "text/event-stream" if stream else "application/json"}
    url = f"{base}/api/agent/v3/llm_utils_chat"
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


# 模块导入时先加载一次，保证任何首调用前目录可用
_reload_pat_models()
