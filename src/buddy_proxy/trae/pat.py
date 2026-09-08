"""TRAE PAT 多账号凭证、缓存、配额隔离与稳定主备 failover。

``TRAE_PAT_BEARER_PROFILES`` 是严格 JSON 数组，每项必须包含 ``bearer``，
可选 ``id``、``priority``，不接受其他字段。配置存在时不会回退旧的
``TRAE_PAT_BEARER``；旧变量只在 profiles 完全未设置时兼容。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import errno
import hashlib
import json
import logging
import os
import pathlib
import re
import socket
import tempfile
import threading
import time
from email.utils import parsedate_to_datetime
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

from .config import BASE_URL_CN
from .credentials import _build_headers
from .native_tools import _content_blocks, _native_tools_payload
from .sse import _parse_sse, _SSEDecoder

log = logging.getLogger(__name__)

_BEARER = "TRAE_PAT_BEARER"
_BEARER_PROFILES = "TRAE_PAT_BEARER_PROFILES"
_AUTH_URL = "TRAE_PAT_AUTH_URL"
_TOKEN_URL = "TRAE_PAT_TOKEN_URL"
_PLUS_GATEWAY = "TRAE_PAT_PLUS_GATEWAY"
_TOKEN_FILE = "TRAE_PAT_TOKEN_FILE"

_MAX_PROFILES = 16
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REFRESH_MARGIN_S = 2 * 3600
_EXCHANGE_TIMEOUT_S = 20
_CHAT_TIMEOUT_S = 180
# 仅连接建立前可安全重放的瞬时错误；总尝试次数 = 首次 + 3 次重试。
_CONNECT_RETRY_DELAYS_S = (0.3, 1.0, 2.0)
_CONNECT_RETRY_ERRNOS = frozenset({errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH})
# 账号级认证/限流的短冷却；429 可由 Retry-After 覆盖。
_ACCOUNT_COOLDOWN_S = 60
_FAILOVER_SSE_CODES = frozenset({4008, 4009, 4031, 4220, 4221, 4222, 4223, 4224, 4227})


@dataclass(frozen=True, slots=True)
class PatProfile:
    id: str
    bearer: str
    priority: int
    index: int

    @property
    def cache_key(self) -> str:
        # id 可调整展示或排序；凭据归属必须绑定 bearer，避免配置重排后串用 token。
        digest = hashlib.sha256(self.bearer.encode()).hexdigest()[:20]
        return f"profile-{digest}"


@dataclass(frozen=True, slots=True)
class PatCredentials:
    token: str
    uid: str
    machine_id: str
    device_id: str


class _PatConfigError(ValueError):
    """配置错误；消息必须保持脱敏。"""


def _profiles_configured() -> bool:
    return _BEARER_PROFILES in os.environ


def _load_profiles() -> tuple[PatProfile, ...]:
    """读取并严格校验账号配置，结果按 ``priority/index`` 稳定排序。"""
    if _profiles_configured():
        raw = os.environ.get(_BEARER_PROFILES, "")
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 不是合法 JSON") from exc
        if not isinstance(parsed, list) or not parsed:
            raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 必须是非空 JSON 数组")
        if len(parsed) > _MAX_PROFILES:
            raise _PatConfigError(f"TRAE_PAT_BEARER_PROFILES 最多允许 {_MAX_PROFILES} 个账号")
        profiles: list[PatProfile] = []
        ids: set[str] = set()
        bearers: set[str] = set()
        allowed = {"bearer", "id", "priority"}
        for index, item in enumerate(parsed):
            if not isinstance(item, dict) or "bearer" not in item or not set(item) <= allowed:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 项字段无效")
            account_id = item.get("id", f"profile-{index}")
            bearer = item.get("bearer")
            priority = item.get("priority", index)
            if not isinstance(account_id, str) or not _PROFILE_ID_RE.fullmatch(account_id):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 id")
            if (not isinstance(bearer, str) or not bearer or bearer != bearer.strip()
                    or len(bearer) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in bearer)):
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含非法 bearer")
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 1000:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES priority 必须是 0..1000 的整数")
            if account_id in ids or bearer in bearers:
                raise _PatConfigError("TRAE_PAT_BEARER_PROFILES 含重复账号")
            ids.add(account_id)
            bearers.add(bearer)
            profiles.append(PatProfile(account_id, bearer, priority, index))
        return tuple(sorted(profiles, key=lambda profile: (profile.priority, profile.index)))

    bearer = os.environ.get(_BEARER, "").strip()
    if not bearer:
        return ()
    return (PatProfile("legacy", bearer, 0, 0),)


def _configuration_error(exc: Exception) -> HTTPException:
    # 不拼接原始 JSON、bearer 或底层异常，防止秘密进入 HTTP 响应和日志。
    return HTTPException(status_code=503, detail=f"PAT 多账号配置无效：{exc}")


def ensure_pat_config() -> tuple[PatProfile, ...]:
    """仅校验本地配置，不读网络，供 provider.ensure_auth 使用。"""
    try:
        profiles = _load_profiles()
    except _PatConfigError as exc:
        raise _configuration_error(exc) from None
    if not profiles:
        raise HTTPException(status_code=401, detail="PAT 通道未配置服务账号密钥")
    return profiles


def pat_enabled() -> bool:
    """是否存在 PAT 配置意图；配置错误留给请求期校验明确报告。

    profiles 变量一旦存在便不回退旧 bearer。即使其内容无效，也注册 provider，
    避免请求静默落入其他通道；真正转发前由 ``ensure_pat_config`` fail-closed。
    """
    if _profiles_configured():
        return True
    return bool(os.environ.get(_BEARER, "").strip())


def _token_file() -> pathlib.Path:
    configured = os.environ.get(_TOKEN_FILE, "")
    return pathlib.Path(configured) if configured else pathlib.Path.home() / ".ethan" / "trae_pat_token.json"


# ───────────────────────── 模型目录 ─────────────────────────

_MODEL_CONFIG_FILE = pathlib.Path(__file__).resolve().parents[1] / "models_config.json"
PAT_MODELS: dict[str, tuple[str, str]] = {}
PAT_PLUS_MODELS: dict[str, tuple[str, str]] = {}
PAT_PUBLIC_MODELS: dict[str, tuple[str, str]] = {}
# 模型 -> function 覆盖（个别模型只在特定 function 下开放，如 gpt-6-astra 仅
# solo_agent；缺省走 WB_TRAE_NATIVE_FUNCTION / chat_v3）
PAT_MODEL_FUNCTIONS: dict[str, str] = {}
_config_mtime: float | None = None


def _reload_pat_models() -> None:
    global _config_mtime
    try:
        mtime = _MODEL_CONFIG_FILE.stat().st_mtime
    except OSError:
        return
    if _config_mtime == mtime:
        return
    plus: dict[str, tuple[str, str]] = {}
    public: dict[str, tuple[str, str]] = {}
    functions: dict[str, str] = {}
    try:
        data = json.loads(_MODEL_CONFIG_FILE.read_text("utf-8"))
        for model in data.get("models", []):
            if model.get("provider") != "traepat":
                continue
            model_id = str(model.get("id") or "").strip()
            upstream = str(model.get("upstream_model") or model_id)
            config = str(model.get("config_name") or model_id)
            if model_id and upstream:
                (plus if model.get("gateway") == "plus" else public)[model_id] = (upstream, config)
            override = str(model.get("function") or "").strip()
            if model_id and override:
                functions[model_id] = override
    except Exception as exc:
        log.warning("PAT 模型配置解析失败（沿用上次内容）: %s", type(exc).__name__)
        return
    PAT_PLUS_MODELS.clear()
    PAT_PLUS_MODELS.update(plus)
    PAT_PUBLIC_MODELS.clear()
    PAT_PUBLIC_MODELS.update(public)
    PAT_MODEL_FUNCTIONS.clear()
    PAT_MODEL_FUNCTIONS.update(functions)
    PAT_MODELS.clear()
    PAT_MODELS.update({**plus, **public})
    _config_mtime = mtime
    log.info("PAT 模型目录已加载：扩展 %d + 公网 %d", len(plus), len(public))


def pat_model_names() -> list[str]:
    _reload_pat_models()
    return list(PAT_MODELS)


def pat_model_meta() -> dict[str, dict[str, Any]]:
    _reload_pat_models()
    try:
        data = json.loads(_MODEL_CONFIG_FILE.read_text("utf-8"))
        return {str(model.get("id")): model for model in data.get("models", [])
                if model.get("provider") == "traepat" and model.get("id")}
    except Exception:
        return {}


def pat_gateway_is_plus(model: str) -> bool:
    _reload_pat_models()
    return model in PAT_PLUS_MODELS


def is_pat_model(model: str) -> bool:
    _reload_pat_models()
    return model in PAT_MODELS


# ───────────────────────── 原子账号缓存与刷新锁 ─────────────────────────

_cache_lock = threading.RLock()
_refresh_locks_guard = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}


def _refresh_lock(account_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


def _read_cache_unlocked() -> dict[str, Any]:
    path = _token_file()
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {"version": 2, "profiles": {}}
    except Exception as exc:
        log.warning("PAT 缓存解析失败: %s", type(exc).__name__)
        return {"version": 2, "profiles": {}}
    if isinstance(data, dict) and isinstance(data.get("profiles"), dict):
        return {"version": 2, "profiles": data["profiles"]}
    # 旧根对象没有可验证的账号归属；即使当前只有一个账号，也可能刚替换过 bearer。
    # 为避免继续使用旧身份，一律丢弃并重新交换。
    if isinstance(data, dict) and data.get("cloud_ide_token"):
        return {"version": 2, "profiles": {}}
    return {"version": 2, "profiles": {}}


def _load_cache() -> dict[str, Any]:
    with _cache_lock:
        return _read_cache_unlocked()


def _atomic_write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _mutate_account(account_id: str, mutate: Any) -> dict[str, Any]:
    with _cache_lock:
        document = _read_cache_unlocked()
        profiles = document.setdefault("profiles", {})
        state = profiles.get(account_id)
        if not isinstance(state, dict):
            state = {}
            profiles[account_id] = state
        mutate(state)
        _atomic_write_json(_token_file(), document)
        return dict(state)


def _account_state(account_id: str) -> dict[str, Any]:
    document = _load_cache()
    state = document.get("profiles", {}).get(account_id, {})
    return dict(state) if isinstance(state, dict) else {}


def _ensure_fingerprint(account_id: str) -> tuple[str, str]:
    state = _account_state(account_id)
    machine_id = state.get("machine_id")
    device_id = state.get("device_id")
    if isinstance(machine_id, str) and machine_id and isinstance(device_id, str) and device_id:
        return machine_id, device_id
    machine_id = uuid.uuid4().hex
    device_id = hashlib.sha256(machine_id.encode()).hexdigest()[:32]

    def store(current: dict[str, Any]) -> None:
        current.setdefault("machine_id", machine_id)
        current.setdefault("device_id", device_id)

    state = _mutate_account(account_id, store)
    return str(state["machine_id"]), str(state["device_id"])


# 旧内部函数兼容：读取当前首账号状态（落盘已统一为 v2 原子格式）。
def _load_cached() -> dict[str, Any] | None:
    profiles = ensure_pat_config()
    return _account_state(profiles[0].cache_key) or None


def _parse_expired_at(text: str, fallback: float) -> float:
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return fallback


def _exchange(bearer: str) -> tuple[str, str, float]:
    """两步交换；异常消息永不包含 bearer/JWT/token。"""
    auth_url = os.environ.get(_AUTH_URL, "").strip()
    token_url = os.environ.get(_TOKEN_URL, "").strip()
    if not auth_url or not token_url:
        raise HTTPException(status_code=503, detail="PAT 换 token 缺少端点配置")
    request = urllib.request.Request(
        auth_url, data=b"", method="GET",
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json",
                 "User-Agent": "ByteDanceCLI/1.0"},
    )
    with urllib.request.urlopen(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
        jwt = (response.headers.get("x-jwt-token") or "").strip()
    if not jwt:
        raise HTTPException(status_code=502, detail="PAT 第一步交换未返回令牌")
    request = urllib.request.Request(
        token_url, data=b"{}", method="POST",
        headers={"x-jwt-token": jwt, "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "ByteDanceCLI/1.0"},
    )
    with urllib.request.urlopen(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    result = payload.get("Result") or payload.get("result") or {}
    token = result.get("Token") or ""
    uid = result.get("UserID") or result.get("userid") or ""
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=502, detail="PAT 第二步交换未返回 token")
    if not isinstance(uid, str):
        uid = ""
    expires_at = _parse_expired_at(
        str(result.get("ExpiredAt") or ""), fallback=time.time() + 6.5 * 86400)
    return token, uid, expires_at


def _credentials_from_state(state: dict[str, Any]) -> PatCredentials | None:
    token = state.get("cloud_ide_token")
    if not isinstance(token, str) or not token:
        return None
    return PatCredentials(token, str(state.get("uid") or ""),
                          str(state.get("machine_id") or ""),
                          str(state.get("device_id") or ""))


def _get_profile_credentials(
    profile: PatProfile,
    *,
    force_refresh: bool = False,
    rejected_token: str | None = None,
) -> PatCredentials:
    """账号级取 token；锁内二次检查避免普通刷新和并发 401 强刷惊群。"""
    machine_id, device_id = _ensure_fingerprint(profile.cache_key)
    with _refresh_lock(profile.cache_key):
        state = _account_state(profile.cache_key)
        current = _credentials_from_state(state)
        try:
            expires_at = float(state.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        now = time.time()
        cache_is_fresh = current is not None and expires_at - now > _REFRESH_MARGIN_S
        if not force_refresh and cache_is_fresh:
            return current
        # 并发请求都拿旧 token 收到 401 时，仅首个线程交换；其余复用新 token。
        if force_refresh and rejected_token and current and current.token != rejected_token \
                and expires_at - now > 300:
            return current
        try:
            token, uid, expires_at = _exchange(profile.bearer)
        except Exception as exc:
            # 交换服务失败不是账号故障，绝不切号。普通临期刷新可沿用仍有效缓存；
            # 401 后的强刷若仍是被拒 token，则必须直接失败，不能把它当新凭据重放。
            may_reuse_current = (
                current is not None
                and expires_at - now > 300
                and (not force_refresh or current.token != rejected_token)
            )
            if may_reuse_current:
                log.warning("PAT token 刷新失败，账号序号=%d，沿用未过期缓存（%s）",
                            profile.index, type(exc).__name__)
                return current
            if isinstance(exc, HTTPException):
                raise
            log.warning("PAT token 刷新失败，账号序号=%d（%s）",
                        profile.index, type(exc).__name__)
            raise HTTPException(status_code=502, detail="PAT credential refresh unavailable") from None

        def store(account: dict[str, Any]) -> None:
            account.update({"cloud_ide_token": token, "uid": uid,
                            "expires_at": expires_at, "refreshed_at": time.time(),
                            "machine_id": machine_id, "device_id": device_id})

        state = _mutate_account(profile.cache_key, store)
        log.info("PAT token 已刷新，账号序号=%d", profile.index)
        credentials = _credentials_from_state(state)
        assert credentials is not None
        return credentials


def get_pat_credentials(
    force_refresh: bool = False,
    *,
    account_id: str | None = None,
    rejected_token: str | None = None,
) -> tuple[str, str]:
    """兼容旧接口，默认使用稳定首账号；可显式指定账号。"""
    profiles = ensure_pat_config()
    profile = next((item for item in profiles if item.id == account_id), None) if account_id else profiles[0]
    if profile is None:
        raise HTTPException(status_code=401, detail="PAT 账号不存在")
    credentials = _get_profile_credentials(
        profile, force_refresh=force_refresh, rejected_token=rejected_token)
    return credentials.token, credentials.uid


def _pat_headers(credentials: PatCredentials, accept: str) -> dict[str, str]:
    return {**_build_headers(credentials.token, credentials.uid,
                             machine_id=credentials.machine_id,
                             device_id=credentials.device_id),
            "Accept": accept}


# ───────────────────────── 模型负载查询 ─────────────────────────

_MODEL_STATUS_TTL_S = 600
_model_status_cache: dict[str, Any] = {"fetched_at": 0.0, "data": None}


def fetch_pat_model_status(force: bool = False) -> dict[str, Any]:
    """手动触发的模型负载查询（用首个账号凭证；结果短 TTL 缓存）。

    返回 {models: [{id, workload(0-100%或None), credits, max_input}], fetched_at}；
    只有 plus 网关目录接口提供负载数据。workload 为 None 表示该模型无负载信息。
    """
    now = time.time()
    cached = _model_status_cache.get("data")
    if (not force and cached and now - float(_model_status_cache.get("fetched_at") or 0)
            < _MODEL_STATUS_TTL_S):
        return cached
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 模型服务未配置")
    profile = ensure_pat_config()[0]
    credentials = _get_profile_credentials(profile)
    request = urllib.request.Request(
        f"{plus}/api/ide/v1/get_detail_param",
        data=json.dumps({"function": os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3"),
                         "need_prompt": False, "poly_prompt": False}).encode("utf-8"),
        method="POST",
        headers=_pat_headers(credentials, "*/*"),
    )
    try:
        with urllib.request.urlopen(request, timeout=_EXCHANGE_TIMEOUT_S) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:
            pass
        raise HTTPException(status_code=502,
                            detail=f"PAT 模型负载查询失败（HTTP {exc.code}）") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HTTPException(status_code=502,
                            detail=f"PAT 模型负载查询失败（网络错误：{reason}）") from None
    meta = pat_model_meta()
    hot_by_config: dict[str, float | None] = {}
    for entry in raw.get("config_info_list") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("config_name")
        if not isinstance(name, str) or not name:
            continue
        hot = ((entry.get("display_config") or {}).get("hot_info") or {}).get("hot")
        hot_by_config[name] = round(min(max(float(hot), 0.0), 100.0), 1) if isinstance(hot, (int, float)) else None
    models: list[dict[str, Any]] = []
    for model_id in pat_model_names():
        if not pat_gateway_is_plus(model_id):
            continue
        info = meta.get(model_id) or {}
        _, config_name = PAT_PLUS_MODELS.get(model_id, ("", model_id))
        models.append({
            "id": model_id,
            "name": info.get("name") or model_id,
            "workload": hot_by_config.get(config_name),
            "credits": info.get("credits"),
            "max_input": info.get("max_input"),
            "reasoning": bool(info.get("reasoning")),
        })
    result = {"models": models, "fetched_at": now}
    _model_status_cache.update({"fetched_at": now, "data": result})
    return result


# ───────────────────────── 账号/额度冷却 ─────────────────────────


def _quota_class(model: str) -> str:
    return "advanced" if pat_gateway_is_plus(model) else "standard"


def _cooldown_until(profile: PatProfile, quota_class: str) -> float:
    state = _account_state(profile.cache_key)
    cooldowns = state.get("cooldowns") if isinstance(state.get("cooldowns"), dict) else {}
    try:
        return max(float(cooldowns.get("account") or 0),
                   float(cooldowns.get(quota_class) or 0))
    except (TypeError, ValueError):
        return 0


def _next_day_timestamp(now: float) -> float:
    current = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
    return (current.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).timestamp()


def _retry_after_seconds(value: str | None, now: float) -> int | None:
    if not value:
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                return None
            seconds = int(target.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(seconds, 1), 7 * 86400)


def _quota_reset_timestamp(now: float, quota_class: str) -> float:
    # 两类模型池都按已确认的日界自动恢复；池仍独立记录，互不影响。
    del quota_class
    return _next_day_timestamp(now)


def _mark_cooldown(profile: PatProfile, quota_class: str) -> None:
    until = _quota_reset_timestamp(time.time(), quota_class)

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get(quota_class) or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns[quota_class] = max(old, until)

    _mutate_account(profile.cache_key, store)


def _mark_account_cooldown(profile: PatProfile, *, retry_after: str | None = None) -> None:
    now = time.time()
    until = now + (_retry_after_seconds(retry_after, now) or _ACCOUNT_COOLDOWN_S)

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get("account") or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns["account"] = max(old, until)

    _mutate_account(profile.cache_key, store)


def _ordered_available_profiles(quota_class: str) -> tuple[PatProfile, ...]:
    now = time.time()
    return tuple(profile for profile in ensure_pat_config()
                 if _cooldown_until(profile, quota_class) <= now)


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
        choices = data.get("choices") or []
        for choice in choices:
            message = (choice or {}).get("message") or {}
            if message.get("content") or message.get("tool_calls"):
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


# ───────────────────────── 额度查询 ─────────────────────────

def _cached_quota(profile: PatProfile, quota_class: str) -> list[dict[str, Any]] | None:
    state = _account_state(profile.cache_key)
    quotas = state.get("quota") if isinstance(state.get("quota"), dict) else {}
    value = quotas.get(quota_class) if isinstance(quotas, dict) else None
    items = value.get("items") if isinstance(value, dict) else None
    return items if isinstance(items, list) else None


def _save_quota(profile: PatProfile, quota_class: str, items: list[dict[str, Any]]) -> None:
    def store(state: dict[str, Any]) -> None:
        quotas = state.setdefault("quota", {})
        quotas[quota_class] = {"items": items, "fetched_at": time.time()}
    _mutate_account(profile.cache_key, store)


def _quota_items(data: dict[str, Any], profile: PatProfile, multi: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pack in data.get("user_entitlement_pack_list") or []:
        base = pack.get("entitlement_base_info") or {}
        quota = base.get("quota") or {}
        usage = pack.get("usage") or {}
        limit = quota.get("basic_usage_limit")
        used = usage.get("basic_usage_amount") or 0
        if not isinstance(limit, (int, float)) or limit <= 0:
            continue
        entitlement_id = str(base.get("entitlement_id") or "pack")
        if "weekly" in entitlement_id:
            label = "PAT 周包（通用额度）"
        elif "daily" in entitlement_id:
            # 同一账号会同时返回多个日包（例如 GPT-5.6 与 GPT-6），不能都叫
            # “高级模型共享”，否则 UI 看起来像重复额度。只从 entitlement_id 的
            # 已知后缀提取公开模型系列，不展示账号标识或其它原始字段。
            family = ""
            if entitlement_id.endswith("_gpt_56_sol"):
                family = "GPT-5.6 Sol"
            elif entitlement_id.endswith("_gpt_6"):
                family = "GPT-6"
            label = f"PAT 日包（{family or '高级模型'}）"
        else:
            label = "PAT 额度包"
        if multi:
            label = f"PAT #{profile.index + 1} · {label[4:]}"
        end_time = base.get("end_time") or 0
        items.append({"label": label, "used": round(used, 2), "total": limit,
                      "remaining": round(limit - used, 2),
                      "percent": round(used / limit * 100),
                      "reset_ts": int(end_time) if end_time else None})
    return items


def fetch_pat_ent_usage() -> list[dict[str, Any]]:
    """按账号查询 advanced 额度；失败只使用该账号自己的缓存。"""
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 通道未配置 TRAE_PAT_PLUS_GATEWAY")
    profiles = ensure_pat_config()
    all_items: list[dict[str, Any]] = []
    failures = 0
    for profile in profiles:
        try:
            credentials = _get_profile_credentials(profile)
            request = urllib.request.Request(
                f"{plus}/trae/api/v1/pay/ide_user_ent_usage", data=b"{}", method="POST",
                headers=_pat_headers(credentials, "application/json"))
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
            items = _quota_items(data, profile, len(profiles) > 1)
            _save_quota(profile, "advanced", items)
            all_items.extend(items)
        except Exception as exc:
            failures += 1
            cached = _cached_quota(profile, "advanced")
            if cached:
                all_items.extend(dict(item, label=f"{item['label']}·缓存") for item in cached)
            log.warning("PAT 额度查询失败，账号序号=%d（%s）", profile.index, type(exc).__name__)
    if not all_items and failures:
        raise HTTPException(status_code=502, detail="PAT 额度查询失败")
    return all_items


# ───────────────────────── 聊天转发 ─────────────────────────

def _build_pat_body(native_msgs: list[dict[str, Any]], model: str, config: str,
                    stream: bool, tools: list[dict[str, Any]] | None,
                    model_id: str = "") -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    # 个别模型只在特定 function 下开放（gpt-6-astra 仅 solo_agent），目录里
    # 可按模型覆盖；其余走全局默认。
    function = PAT_MODEL_FUNCTIONS.get(model_id) or os.environ.get(
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
) -> Iterator[tuple[str, dict[str, Any]]]:
    """稳定主备的 PAT 真流式入口；首个语义事件后永不重放或拼流。"""
    start_token_keeper()  # 幂等：请求路径兜底拉起自愈循环
    _reload_pat_models()
    if model not in PAT_MODELS:
        raise HTTPException(status_code=400, detail=f"模型 {model} 不在 PAT 通道目录内")
    quota_class = _quota_class(model)
    profiles = _ordered_available_profiles(quota_class)
    if not profiles:
        raise HTTPException(status_code=429, detail=f"PAT {quota_class} 账号均在额度冷却中")

    upstream_model, config = PAT_MODELS[model]
    body = _build_pat_body(native_msgs, upstream_model, config, True, tools, model_id=model)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    url = f"{_chat_base(model)}/api/agent/v3/llm_utils_chat"
    stop = stop or threading.Event()
    last_status: int | None = None
    empty_retries = 0

    have_credentials = False
    for profile in profiles:
        try:
            credentials = _get_profile_credentials(profile)
        except HTTPException:
            # 同非流式路径：单账号换不到凭据时跳过，不让它拖垮整个通道。
            continue
        have_credentials = True
        refreshed = False
        rejected_token: str | None = None
        while True:
            committed = False
            saw_terminal = False
            try:
                for event, data in _stream_profile_events(url, payload, credentials, stop):
                    if stop.is_set():
                        return
                    code = _event_error_code(event, data)
                    if code is not None:
                        if not committed and code in _FAILOVER_SSE_CODES:
                            _mark_cooldown(profile, quota_class)
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
                        credentials = _get_profile_credentials(
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
                    tools: list[dict[str, Any]] | None = None) -> str:
    """稳定主备发送；同一序列化 payload/session 在所有账号和重放间保持不变。"""
    start_token_keeper()  # 幂等：请求路径兜底拉起自愈循环
    _reload_pat_models()
    if model not in PAT_MODELS:
        raise HTTPException(status_code=400, detail=f"模型 {model} 不在 PAT 通道目录内")
    upstream_model, config = PAT_MODELS[model]
    quota_class = _quota_class(model)
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
            credentials = _get_profile_credentials(profile)
        except HTTPException:
            # 换不到凭据（如该账号从未换到 token 且交换端点当前不可达）不是额度
            # 信号，不标记冷却；也不能让单个账号拖垮整个通道——后面账号可能仍有
            # 未过期缓存 token（交换端点离线时这是唯一可用凭据）。全部账号都
            # 拿不到凭据时才整体失败。
            continue
        have_credentials = True
        refreshed = False
        rejected_token: str | None = None
        while True:
            try:
                raw = _post_chat(url, payload, credentials, stream)
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
                        credentials = _get_profile_credentials(
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
                _mark_cooldown(profile, quota_class)
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
                  tools: list[dict[str, Any]] | None = None) -> str:
    native_msgs = [{"role": message.get("role", "user"),
                    "content": _content_blocks(message.get("content"))}
                   for message in messages]
    return send_pat_native(native_msgs, model, stream, tools)


# ───────────────────────── 凭证自愈（后台保活） ─────────────────────────
#
# 背景故障模式：换 token 端点只在特定网络环境可达；离线期间新加的账号永远
# 换不到 token，缓存 token 过期后也只能等网络恢复。靠请求路径被动补签会让
# 第一个撞上的用户吃到 502——改为后台循环主动补：环境恢复后自动把缺失/
# 临期的 token 补齐，并把状态透出给 UI，故障可自愈也可观测。

_KEEPALIVE_INTERVAL_S = max(0, int(os.environ.get("WB_TRAE_TOKEN_KEEPALIVE_S", "600")))
_keeper_thread: threading.Thread | None = None
_keeper_lock = threading.Lock()
_keeper_round_lock = threading.Lock()
_keeper_last: dict[str, Any] = {"at": 0.0, "env_ready": None, "refreshed": [], "waiting": []}


def _exchange_env_ready(timeout: float = 3.0) -> bool:
    """探测换 token 端点当前是否可达（DNS+TCP 层）；不可达说明本轮无法签发。"""
    for key in (_AUTH_URL, _TOKEN_URL):
        raw = os.environ.get(key, "").strip()
        host = urllib.parse.urlsplit(raw).hostname if raw else None
        if not host:
            continue
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            return False
        for info in infos[:2]:
            try:
                # sockaddr 可能是 IPv6 四元组，create_connection 只接受 (host, port)
                with socket.create_connection(info[4][:2], timeout=timeout):
                    break
            except OSError:
                continue
        else:
            return False
    return True


def _needs_refresh(state: dict[str, Any], now: float) -> bool:
    current = _credentials_from_state(state)
    if current is None:
        return True
    try:
        expires_at = float(state.get("expires_at") or 0)
    except (TypeError, ValueError):
        return True
    return expires_at - now <= _REFRESH_MARGIN_S


def _keeper_round() -> dict[str, Any]:
    """一轮自愈：环境可达时补签缺失/临期 token；不可达时短路与记录。"""
    now = time.time()
    profiles = _load_profiles()
    refreshed: list[str] = []
    waiting: list[str] = []
    if not _exchange_env_ready():
        for profile in profiles:
            if _needs_refresh(_account_state(profile.cache_key), now):
                waiting.append(profile.id)
        return {"at": time.time(), "env_ready": False, "refreshed": [], "waiting": waiting}
    for profile in profiles:
        if not _needs_refresh(_account_state(profile.cache_key), now):
            continue
        try:
            _get_profile_credentials(profile)
        except Exception as exc:
            waiting.append(profile.id)
            log.warning("PAT 凭证自愈未成功，账号 %s（%s）", profile.id, type(exc).__name__)
        else:
            refreshed.append(profile.id)
            log.info("PAT 凭证自愈：账号 %s 已补签 token", profile.id)
    return {"at": time.time(), "env_ready": True, "refreshed": refreshed, "waiting": waiting}


def refresh_missing_tokens() -> dict[str, Any]:
    """立即执行一轮凭证补签并返回脱敏状态，供 UI 手动触发。

    与后台循环共用互斥锁，避免用户连点、定时轮询和真实请求同时惊群。
    只处理缺失或临期账号；健康 token 不会被强制刷新。
    """
    with _keeper_round_lock:
        result = _keeper_round()
        with _keeper_lock:
            _keeper_last.update(result)
    return {**result, "accounts": accounts_status(start_keeper=False)["accounts"]}


def _keeper_loop(interval: int) -> None:
    while True:
        try:
            refresh_missing_tokens()
        except Exception as exc:
            log.warning("PAT 凭证自愈循环异常（%s）", type(exc).__name__)
        time.sleep(interval)


def start_token_keeper() -> None:
    """启动后台凭证自愈循环（幂等）。``WB_TRAE_TOKEN_KEEPALIVE_S=0`` 可关闭。"""
    global _keeper_thread
    if _KEEPALIVE_INTERVAL_S <= 0 or not pat_enabled():
        return
    with _keeper_lock:
        if _keeper_thread is not None and _keeper_thread.is_alive():
            return
        _keeper_thread = threading.Thread(
            target=_keeper_loop, args=(_KEEPALIVE_INTERVAL_S,),
            name="pat-token-keeper", daemon=True)
        _keeper_thread.start()


def accounts_status(*, start_keeper: bool = True) -> dict[str, Any]:
    """各账号本地凭证/冷却状态 + 自愈循环最近一轮结果（不触网、无秘密）。"""
    if start_keeper:
        start_token_keeper()  # 幂等：兜底保证查看状态时循环一定已拉起
    now = time.time()
    accounts: list[dict[str, Any]] = []
    try:
        profiles = _load_profiles()
    except _PatConfigError:
        profiles = ()
    for profile in profiles:
        state = _account_state(profile.cache_key)
        current = _credentials_from_state(state)
        try:
            expires_at = float(state.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0
        cooling = []
        cooldowns = state.get("cooldowns") if isinstance(state.get("cooldowns"), dict) else {}
        for kind, until in cooldowns.items():
            try:
                minutes_left = (float(until) - now) / 60
            except (TypeError, ValueError):
                continue
            if minutes_left > 0:
                cooling.append({"kind": kind, "minutes_left": round(minutes_left)})
        hours_left = (expires_at - now) / 3600 if current else None
        accounts.append({
            "id": profile.id,
            "priority": profile.priority,
            "token": ("ok" if current and hours_left > _REFRESH_MARGIN_S / 3600
                      else "expiring" if current else "missing"),
            "hours_left": round(hours_left, 1) if current else None,
            "refreshed_at": state.get("refreshed_at"),
            "cooling": cooling,
        })
    with _keeper_lock:
        keeper = dict(_keeper_last)
    return {"accounts": accounts, "keeper": keeper,
            "keepalive_s": _KEEPALIVE_INTERVAL_S, "enabled": pat_enabled()}


_reload_pat_models()
