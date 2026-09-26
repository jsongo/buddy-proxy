"""Qoder 凭据：持久化、解析、刷新、登录（device flow）。

凭证来源优先级：

1. 环境变量 ``QODER_TOKEN``（临时覆盖，便于排查）
2. 状态文件 ``~/.buddy-proxy/qoder_auth.json``（``buddy login qoder`` 写入）
3. 复用 **Qoder 桌面端**已登录的 ``dt-`` device token
   （解密 Electron safeStorage 的 ``auth.v1.dat``）

device token（``dt-``）有效期约 30 天，配 ``drt-`` refresh token 可续；
本模块在 token 临近过期时自动刷新并回写状态文件。

登录走官方 device flow（PKCE S256）：生成 verifier/challenge → 用户在浏览器
授权 → 轮询 ``/api/v1/deviceToken/poll``（404 = 等待中，200 = 成功）。

> 为什么不去偷读 CN 版桌面的 ``auth.v1.dat``：新版 Electron safeStorage
> 在本机推不出密钥（PBKDF2 的 password 已非空串），而 device flow 对
> 全球版/CN 版通用、且不依赖桌面端在跑。safeStorage 只作为**尽力而为**的
> 便捷路径保留。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from buddy_proxy.core.paths import state_file

from .config import CLIENT_ID, Region, resolve_region, with_cached_endpoints

log = logging.getLogger(__name__)

#: 状态文件名。
AUTH_STATE_NAME = "qoder_auth.json"

#: device token 默认有效期兜底（服务端会给 expires_at）。
DEFAULT_TTL_MS = 30 * 24 * 3600 * 1000

#: 提前多久刷新（5 分钟）。
REFRESH_MARGIN_MS = 5 * 60 * 1000

#: device flow 轮询间隔与总时长。
POLL_INTERVAL_S = 5.0
POLL_TIMEOUT_S = 600.0


class AuthError(RuntimeError):
    """凭据缺失、无效或刷新失败。"""


@dataclass
class Credential:
    """一条可用的 Qoder 凭据。"""

    token: str
    uid: str = ""
    machine_id: str = ""
    refresh_token: str = ""
    expires_at_ms: int = 0
    name: str = ""
    email: str = ""
    region: str = "cn"
    plan: str = ""
    source: str = "state"  # state | env | desktop
    extra: dict = field(default_factory=dict)

    def is_expired(self, margin_ms: int = REFRESH_MARGIN_MS) -> bool:
        """是否已过期或即将过期（无 expires_at 时视为未过期）。"""
        if not self.expires_at_ms:
            return False
        return self.expires_at_ms - margin_ms <= int(time.time() * 1000)

    def describe(self) -> str:
        """脱敏描述（只进日志）。"""
        masked = f"{self.token[:6]}…{self.token[-3:]}" if self.token else ""
        return (
            f"region={self.region} source={self.source} uid={self.uid or '?'} "
            f"token={masked} plan={self.plan or '?'}"
        )


# ---------------------------------------------------------------------------
# 状态文件读写
# ---------------------------------------------------------------------------


def auth_state_path() -> Path:
    """状态文件路径（可用 ``QODER_AUTH_FILE`` 覆盖）。"""
    override = os.environ.get("QODER_AUTH_FILE")
    if override:
        return Path(override).expanduser()
    return state_file(AUTH_STATE_NAME)


def load_state() -> dict:
    """读状态文件；不存在或损坏时返回空 dict。"""
    path = auth_state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(data: dict) -> Path:
    """写状态文件（0600，含 token）。"""
    path = auth_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


# ---------------------------------------------------------------------------
# 桌面端 credential 复用（尽力而为）
# ---------------------------------------------------------------------------

#: 桌面端凭据文件（Electron safeStorage 加密）。
_DESKTOP_AUTH_RELPATH = "Library/Application Support/{bundle}/auth.v1.dat"

#: 各区域的桌面端 bundle id。
_DESKTOP_BUNDLES = {"global": "com.qoder.app.stable", "cn": "com.qodercn.app.stable"}

#: 候选解密口令（Electron safeStorage 在各平台的默认值）。
_SAFESTORAGE_PASSWORDS = (b"", b"peanuts", b"Chrome Safe Storage")


def _decrypt_safestorage(blob: bytes) -> bytes | None:
    """尝试解 Electron safeStorage（macOS：PBKDF2-HMAC-SHA1 + AES-128-CBC）。

    失败返回 ``None``——新版 Electron 的口令不再是空串，本机可能解不开，
    调用方需容忍（这正是 device flow 存在的意义）。
    """
    try:
        from buddy_proxy.trae.aes_pure import aes_cbc_decrypt
    except ImportError:  # pragma: no cover - 环境异常
        return None
    for password in _SAFESTORAGE_PASSWORDS:
        try:
            key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)
            out = aes_cbc_decrypt(key, b" " * 16, blob)
        except Exception:  # noqa: BLE001 - 解密失败属预期
            continue
        if out[:1] in (b"{", b'"'):
            return out
    return None


def _from_desktop(region: Region) -> Credential | None:
    """从 Qoder 桌面端登录态里取 device token（解不开就返回 None）。"""
    bundle = _DESKTOP_BUNDLES.get(region.key)
    if not bundle:
        return None
    path = Path.home() / _DESKTOP_AUTH_RELPATH.format(bundle=bundle)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    # "v10" magic 后紧跟密文；有的版本第 4 字节是密文首字节，故两种偏移都试。
    for offset in (3, 4):
        plain = _decrypt_safestorage(raw[offset:])
        if plain is None:
            continue
        # PKCS#7 去填充
        pad = plain[-1]
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        try:
            data = json.loads(plain.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        token = _find_token(data)
        if not token:
            continue
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        return Credential(
            token=token,
            uid=str(user.get("id") or user.get("uid") or ""),
            machine_id=_machine_id(region),
            refresh_token=str(data.get("refreshToken") or ""),
            expires_at_ms=int(data.get("expiresAt") or 0),
            name=str(user.get("name") or ""),
            email=str(user.get("email") or ""),
            region=region.key,
            source="desktop",
        )
    return None


def _find_token(node: object, _depth: int = 0) -> str:
    """在嵌套结构里找第一个以 ``dt-`` 开头的字符串。"""
    if _depth > 6:
        return ""
    if isinstance(node, str):
        return node if node.startswith("dt-") else ""
    if isinstance(node, dict):
        for value in node.values():
            found = _find_token(value, _depth + 1)
            if found:
                return found
        return ""
    if isinstance(node, list):
        for value in node:
            found = _find_token(value, _depth + 1)
            if found:
                return found
    return ""


def machine_id(region: Region) -> str:
    """读该区域的 machine_id（桌面端/CLI 都会写，缺失则现造一个）。"""
    return _machine_id(region)


def _machine_id(region: Region) -> str:
    path = Path.home() / region.config_dirname / ".auth" / "machine_id"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    return _generate_machine_id()


def _generate_machine_id() -> str:
    """按桌面端同款风格造一个 machine_id（分组 UUID 形态）。"""
    import uuid

    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def resolve_credential(region: Region | None = None) -> Credential:
    """解析出一条可用凭据；都不可用时抛 ``AuthError``。

    只读不写：需要刷新的场景请用 ``ensure_credential``（异步）。
    """
    env_token = (os.environ.get("QODER_TOKEN") or "").strip()
    if env_token:
        reg = region or resolve_region()
        return Credential(
            token=env_token,
            uid=(os.environ.get("QODER_UID") or "").strip(),
            machine_id=_machine_id(reg),
            region=reg.key,
            source="env",
        )

    state = load_state()
    token = str(state.get("token") or "").strip()
    if token:
        reg = region or resolve_region(state.get("region"))
        return Credential(
            token=token,
            uid=str(state.get("uid") or ""),
            machine_id=str(state.get("machine_id") or "") or _machine_id(reg),
            refresh_token=str(state.get("refresh_token") or ""),
            expires_at_ms=int(state.get("expires_at_ms") or 0),
            name=str(state.get("name") or ""),
            email=str(state.get("email") or ""),
            region=reg.key,
            plan=str(state.get("plan") or ""),
            source="state",
        )

    # 兜底：复用桌面端登录态（可能因 safeStorage 换钥而失败）
    reg = region or resolve_region()
    desktop = _from_desktop(reg)
    if desktop is not None:
        return desktop

    raise AuthError(
        "qoder 未登录：请先跑 `buddy login qoder`（浏览器授权），"
        "或设置 QODER_TOKEN 环境变量"
    )


async def ensure_credential(region: Region | None = None) -> Credential:
    """取凭据，临近过期时自动刷新并回写状态文件。"""
    cred = resolve_credential(region)
    if cred.source != "state" or not cred.refresh_token or not cred.is_expired():
        return cred
    try:
        refreshed = await refresh_credential(cred, region)
    except AuthError as exc:
        log.warning("qoder token 刷新失败，沿用旧 token: %s", exc)
        return cred
    return refreshed


async def refresh_credential(cred: Credential, region: Region | None = None) -> Credential:
    """用 refresh token 换新 device token，并回写状态文件。"""
    reg = with_cached_endpoints(region or resolve_region(cred.region))
    if not cred.refresh_token:
        raise AuthError("缺少 refresh_token，无法刷新；请重新 `buddy login qoder`")
    payload = {"refresh_token": cred.refresh_token}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                reg.device_refresh_url(),
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AuthError(f"刷新请求失败: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(f"刷新失败 HTTP {resp.status_code}: {resp.text[:200]}")
    data = _parse_token_response(resp.json(), cred)
    data.source = "state"
    data.region = reg.key
    _persist(data)
    log.info("qoder token 已刷新 (%s)", data.describe())
    return data


def _parse_token_response(body: dict, base: Credential | None = None) -> Credential:
    """把 deviceToken 各端点的响应统一成 ``Credential``。"""
    body = body if isinstance(body, dict) else {}
    token = str(body.get("token") or body.get("device_token") or body.get("access_token") or "")
    expires_at_ms = _to_ms(body.get("expires_at") or body.get("expiresAt"))
    if not expires_at_ms:
        expires_in = body.get("expires_in")
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            expires_at_ms = int(time.time() * 1000) + int(expires_in) * 1000
        else:
            expires_at_ms = int(time.time() * 1000) + DEFAULT_TTL_MS
    return Credential(
        token=token,
        uid=str(body.get("user_id") or body.get("uid") or (base.uid if base else "")),
        machine_id=str(body.get("machine_id") or (base.machine_id if base else "")),
        refresh_token=str(
            body.get("refresh_token") or body.get("refreshToken") or (base.refresh_token if base else "")
        ),
        expires_at_ms=expires_at_ms,
        name=str(body.get("name") or (base.name if base else "")),
        email=str(body.get("email") or (base.email if base else "")),
        region=str((base.region if base else "") or ""),
        source=str(base.source if base else "state"),
    )


def _to_ms(value: object) -> int:
    """把时间戳（秒/毫秒/RFC3339）统一成毫秒。"""
    if isinstance(value, (int, float)) and value > 0:
        # 10 位数是秒，13 位是毫秒
        return int(value * 1000) if value < 1e11 else int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            from datetime import datetime

            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def _persist(cred: Credential) -> None:
    """把凭据写进状态文件（合并已有字段，避免丢账号信息）。"""
    state = load_state()
    state.update(
        {
            "token": cred.token,
            "uid": cred.uid,
            "machine_id": cred.machine_id,
            "refresh_token": cred.refresh_token,
            "expires_at_ms": cred.expires_at_ms,
            "name": cred.name,
            "email": cred.email,
            "region": cred.region,
            "plan": cred.plan,
            "updated_at_ms": int(time.time() * 1000),
        }
    )
    save_state(state)


# ---------------------------------------------------------------------------
# 登录（device flow / PKCE）
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    """生成 (verifier, challenge)；challenge = base64url(sha256(verifier))。"""
    # 官方用 43~128 位、字符集为 base64 字母表的 verifier。
    length = 43 + secrets.randbelow(86)
    verifier = "".join(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"[
            secrets.randbelow(64)
        ]
        for _ in range(length)
    )
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@dataclass
class DeviceFlow:
    """一次待授权的 device flow。"""

    auth_url: str
    verifier: str
    nonce: str
    machine_id: str
    region: str

    def format(self) -> str:
        return self.auth_url


def start_device_flow(region: Region | None = None) -> DeviceFlow:
    """构造授权 URL（用户在自己的 Qoder 账号下打开并确认）。"""
    reg = with_cached_endpoints(region or resolve_region())
    verifier, challenge = _pkce_pair()
    nonce = _uuid()
    mid = _machine_id(reg)
    params = urllib.parse.urlencode(
        {
            "challenge": challenge,
            "challenge_method": "S256",
            "nonce": nonce,
            "machine_id": mid,
            "client_id": CLIENT_ID,
        }
    )
    return DeviceFlow(
        auth_url=f"{reg.auth_url()}?{params}",
        verifier=verifier,
        nonce=nonce,
        machine_id=mid,
        region=reg.key,
    )


async def poll_device_flow(
    flow: DeviceFlow,
    *,
    timeout_s: float = POLL_TIMEOUT_S,
    interval_s: float = POLL_INTERVAL_S,
    on_tick: object = None,
) -> Credential:
    """轮询直到用户在浏览器里完成授权。

    服务端语义：``404`` = 尚未授权（继续等），``200`` = 成功并带回 token。
    """
    import asyncio

    reg = with_cached_endpoints(resolve_region(flow.region))
    params = urllib.parse.urlencode(
        {"nonce": flow.nonce, "verifier": flow.verifier, "challenge_method": "S256"}
    )
    url = f"{reg.device_poll_url()}?{params}"
    deadline = time.monotonic() + timeout_s

    async with httpx.AsyncClient(timeout=30) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url, headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                log.debug("qoder device poll 网络异常，重试: %s", exc)
                await asyncio.sleep(interval_s)
                continue
            if resp.status_code == 200:
                data = _parse_token_response(resp.json())
                data.machine_id = data.machine_id or flow.machine_id
                data.region = reg.key
                data.source = "state"
                _persist(data)
                return data
            if resp.status_code != 404:
                raise AuthError(f"授权轮询失败 HTTP {resp.status_code}: {resp.text[:200]}")
            if callable(on_tick):
                on_tick()
            await asyncio.sleep(interval_s)

    raise AuthError("授权超时（10 分钟）：请重新执行 `buddy login qoder`")


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# 辅助：从 PAT（pt-）换 device token
# ---------------------------------------------------------------------------


async def exchange_personal_token(personal_token: str, region: Region | None = None) -> Credential:
    """用个人访问令牌（``pt-``）换 device token。"""
    reg = with_cached_endpoints(region or resolve_region())
    mid = _machine_id(reg)
    payload = {
        "personal_token": personal_token,
        "machine_id": mid,
        "machine_token": mid,
        "machine_type": 5,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                reg.job_token_exchange_url(),
                json=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise AuthError(f"PAT 换取失败: {exc}") from exc
    if resp.status_code != 200:
        raise AuthError(f"PAT 换取失败 HTTP {resp.status_code}: {resp.text[:200]}")
    cred = _parse_token_response(resp.json())
    cred.machine_id = cred.machine_id or mid
    cred.region = reg.key
    cred.source = "state"
    _persist(cred)
    return cred
