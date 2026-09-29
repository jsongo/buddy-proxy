"""小米账号 SSO：passToken → serviceToken 两阶段换票。

从 MiMo Desktop 的 app.asar 提取（``ServiceTokenManager``，自述对齐小米
SSO_curl.cpp）。流程：

1. **Phase 1**  GET ``https://account.xiaomi.com/pass/serviceLogin``
   query: ``_locale=zh_CN&_snsNone=true&sid=<sid>&_json=true``
   Cookie: ``passToken`` / ``userId`` / ``cUserId``
   → 响应体是 ``&&&START&&&{json}`` 前缀包装，内含 ``code`` / ``loc`` /
     ``ssecurity`` / ``nonce`` / ``bSecondValidation`` / ``notificationUrl``。

2. **Phase 2**  GET ``<loc>&clientSign=<sign>``
   **不带 Cookie**（对齐 SSO_curl.cpp 的 ``cookies.clear()``）。
   ``clientSign = urlencode(base64(sha1("nonce=" + nonce + "&" + ssecurity)))``
   注意 ssecurity 是**裸跟在 & 后**，不是 ``&ssecurity=...``。
   → 响应 ``Set-Cookie`` 里取 ``serviceToken`` 或 ``<sid>_serviceToken``。

后续业务请求把 serviceToken 按 ``Cookie: userId=..; cUserId=..; serviceToken=..``
带上即可。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from buddy_proxy.core.paths import state_file

from .config import SSO_SERVICE_LOGIN, SSO_SID, SSO_UA, cookie_db_path

log = logging.getLogger(__name__)

# asar 里 phase1 响应的固定前缀
_PHASE1_PREFIX = "&&&START&&&"
# nonce 是数字字面量，JSON 解析前先用正则兜底抽出来
_NONCE_RE = re.compile(r'"nonce"\s*:\s*(\d+)')
#: serviceToken 本地缓存 TTL（秒）。asar 里 phase2 的 expiredAt 传 void 0
#: （不设过期），这里保守取 6 小时，过期自动重换。
_TOKEN_TTL_S = 6 * 3600


class SsoError(RuntimeError):
    """SSO 换票失败（凭据缺失 / 二次验证 / 上游拒绝）。"""


@dataclass
class AccountCookies:
    """从 cookie 库读到的小米账号三件套。"""

    pass_token: str
    user_id: str
    c_user_id: str = ""

    def phase1_cookie(self) -> str:
        """Phase 1 请求的 Cookie 头（asar: ServiceTokenManager.buildCookieHeader）。"""
        parts = [f"passToken={self.pass_token}", f"userId={self.user_id}"]
        if self.c_user_id:
            parts.append(f"cUserId={self.c_user_id}")
        return "; ".join(parts)


@dataclass
class ServiceToken:
    """某一 sid 的 serviceToken 及 Phase 2 附带的额外 cookie。"""

    sid: str
    token: str
    extra_cookies: dict[str, str] = field(default_factory=dict)
    obtained_at: float = 0.0

    def cookie_header(self, account: AccountCookies | None = None) -> str:
        """业务请求的 Cookie 头。两种 cookie 名都带上，兼容命名差异。"""
        parts: list[str] = []
        seen: set[str] = set()

        def add(name: str, value: str) -> None:
            if name in seen or not value:
                return
            seen.add(name)
            parts.append(f"{name}={value}")

        if account is not None:
            add("userId", account.user_id)
            add("cUserId", account.c_user_id)
        add("serviceToken", self.token)
        add(f"{self.sid}_serviceToken", self.token)
        for k, v in self.extra_cookies.items():
            add(k, v)
        return "; ".join(parts)


# ---------------------------------------------------------------------------
# cookie 库读取（明文 value，无需解密）
# ---------------------------------------------------------------------------


def load_account_cookies(db_path: Path | None = None) -> AccountCookies | None:
    """读小米账号凭据：**先看 ``buddy login mimo`` 落盘的文件，再退回桌面 cookie 库**。

    文件优先是有意的：新机器上不装 MiMo 桌面也能用（登录一次即可），装了桌面的
    老机器行为不变（没有文件就照旧读 cookie 库）。

    桌面 cookie 库路径下：2026-09-22 实测这几个 cookie 存在 ``value`` 列且为明文
    （``encrypted_value`` 长度为 0），直接 sqlite 读即可。库被 Electron
    持有时用只读 URI 打开，避免锁冲突。
    """
    # 延迟 import：login.py 从本模块取 AccountCookies，模块级互相 import 会成环
    from .login import load_saved_account

    saved = load_saved_account()
    if saved is not None:
        return saved

    path = db_path or cookie_db_path()
    if not path:
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name, value FROM cookies WHERE value <> ''"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("mimo cookie db read failed: %s", exc)
        return None

    jar = {name: value for name, value in rows}
    # 同名 cookie 可能同时挂在 .account.xiaomi.com / .xiaomi.com 两个 host 上，
    # dict 后者覆盖前者；两者值相同，取任一即可。
    pass_token = jar.get("passToken") or ""
    user_id = jar.get("userId") or ""
    if not pass_token or not user_id:
        return None
    return AccountCookies(
        pass_token=pass_token,
        user_id=user_id,
        c_user_id=jar.get("cUserId") or "",
    )


def load_all_cookies(db_path: Path | None = None) -> dict[str, str]:
    """读 cookie 库里全部非空 cookie（参考实现的兜底做法）。"""
    path = db_path or cookie_db_path()
    if not path:
        return {}
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT name, value FROM cookies WHERE value <> ''"
            ).fetchall()
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return {}
    return {name: value for name, value in rows}


# ---------------------------------------------------------------------------
# serviceToken 本地缓存
# ---------------------------------------------------------------------------


def _cache_path() -> Path:
    return state_file("mimo_sso_token.json")


def load_cached_token(
    sid: str = SSO_SID, user_id: str | None = None
) -> ServiceToken | None:
    """读缓存的 serviceToken；``user_id`` 能给就给。

    **必须带上 user_id**：换票结果只对签发它的那个账号有效，而缓存文件是
    全局一份。不校验的话，``buddy login mimo`` 换号后旧 token 依然命中，
    请求会带着 A 的票去访问 B 的账号（轻则额度统计错乱，重则 401 后
    重换票才发现——而那时已经白跑了一轮）。缺 user_id 的老缓存直接判失效，
    宁可多换一次票。
    """
    path = _cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("sid") != sid:
        return None
    if user_id is not None and str(data.get("user_id") or "") != str(user_id):
        return None
    token = data.get("token") or ""
    if not token:
        return None
    obtained_at = float(data.get("obtained_at") or 0.0)
    if obtained_at and (time.time() - obtained_at) > _TOKEN_TTL_S:
        return None
    return ServiceToken(
        sid=sid,
        token=token,
        extra_cookies=dict(data.get("extra_cookies") or {}),
        obtained_at=obtained_at,
    )


def save_cached_token(st: ServiceToken, user_id: str = "") -> None:
    """写缓存。``user_id`` 一并记下，供 ``load_cached_token`` 校验归属。"""
    path = _cache_path()
    try:
        path.write_text(
            json.dumps(
                {
                    "sid": st.sid,
                    "token": st.token,
                    "user_id": str(user_id or ""),
                    "extra_cookies": st.extra_cookies,
                    "obtained_at": st.obtained_at or time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
    except OSError as exc:
        log.warning("mimo sso token cache write failed: %s", exc)


def invalidate_cache(sid: str = SSO_SID) -> None:
    """作废缓存（上游 401 时强制重换）。"""
    path = _cache_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 两阶段换票
# ---------------------------------------------------------------------------


def client_sign(nonce: str, ssecurity: str) -> str:
    """Phase 2 的 clientSign（已 URL 编码，可直接拼进 query）。

    asar 原文：``a = "nonce=" + nonce; ssecurity && (a += "&" + ssecurity)``
    然后 ``encodeURIComponent(sha1(a).toString("base64"))``。
    """
    raw = f"nonce={nonce}"
    if ssecurity and ssecurity.strip():
        raw += f"&{ssecurity}"
    digest = hashlib.sha1(raw.encode("utf-8")).digest()
    b64 = base64.b64encode(digest).decode("ascii")
    return urllib.parse.quote(b64, safe="")


def _parse_phase1_body(text: str) -> dict[str, Any]:
    body = text.strip()
    if body.startswith(_PHASE1_PREFIX):
        body = body[len(_PHASE1_PREFIX) :]
    # nonce 是数字字面量，JSON 解析可能失败——先正则兜底，再 merge。
    nonce_m = _NONCE_RE.search(body)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            data = {}
    except json.JSONDecodeError:
        data = {}
    if nonce_m:
        data.setdefault("nonce", int(nonce_m.group(1)))
    return data


def _parse_set_cookie_service_token(
    headers: httpx.Headers, sid: str
) -> tuple[str, dict[str, str]]:
    """从 Phase 2 的 Set-Cookie 里挑出 serviceToken，并收集额外 cookie。"""
    wanted = ("serviceToken", f"{sid}_serviceToken")
    extra: dict[str, str] = {}
    token = ""
    for raw in headers.get_list("set-cookie"):
        # 只取第一段 name=value；属性（Path/Expires…）丢掉
        first = raw.split(";", 1)[0]
        if "=" not in first:
            continue
        name, value = first.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        if name in wanted and not token:
            token = value
        else:
            extra[name] = value
    return token, extra


async def fetch_service_token(
    account: AccountCookies,
    sid: str = SSO_SID,
    client: httpx.AsyncClient | None = None,
) -> ServiceToken:
    """执行 Phase 1 + Phase 2，换回指定 sid 的 serviceToken。"""
    owns = client is None
    http = client or httpx.AsyncClient(timeout=20.0, follow_redirects=False)
    try:
        # ── Phase 1 ──
        params = {"_locale": "zh_CN", "_snsNone": "true", "sid": sid, "_json": "true"}
        resp = await http.get(
            SSO_SERVICE_LOGIN,
            params=params,
            headers={"Cookie": account.phase1_cookie(), "User-Agent": SSO_UA},
        )
        if resp.status_code != 200:
            raise SsoError(f"Phase 1 HTTP {resp.status_code}: {resp.text[:200]}")
        p1 = _parse_phase1_body(resp.text)
        code = p1.get("code")
        if code not in (0, "0", None):
            raise SsoError(f"Phase 1 error: code={code}")
        if p1.get("bSecondValidation") and p1.get("notificationUrl"):
            raise SsoError(f"需要二次验证: {p1['notificationUrl']}")
        loc = p1.get("location") or p1.get("loc") or ""
        nonce = str(p1.get("nonce") or "")
        ssecurity = str(p1.get("ssecurity") or "")
        if not loc or not nonce:
            raise SsoError("Phase 1 未返回 location/nonce（登录态可能已失效）")

        # ── Phase 2（不带 Cookie）──
        sign = client_sign(nonce, ssecurity)
        sep = "&" if ("?" in loc) else "?"
        p2 = await http.get(
            f"{loc}{sep}clientSign={sign}",
            headers={"User-Agent": SSO_UA},
        )
        token, extra = _parse_set_cookie_service_token(p2.headers, sid)
        if not token:
            # asar 明确警告：HTTP 200 不保证 Set-Cookie 里有 serviceToken
            raise SsoError(
                f"Phase 2 未在 Set-Cookie 中拿到 serviceToken (status={p2.status_code})"
            )
        st = ServiceToken(
            sid=sid, token=token, extra_cookies=extra, obtained_at=time.time()
        )
        save_cached_token(st, user_id=account.user_id)
        return st
    finally:
        if owns:
            await http.aclose()


async def ensure_service_token(
    sid: str = SSO_SID,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> ServiceToken:
    """取 serviceToken：优先本地缓存，miss/过期/**换号**则现换。"""
    account = load_account_cookies()
    if not force:
        # 带上当前账号校验：缓存只对签发它的账号有效，换号后必须重换票
        cached = load_cached_token(sid, user_id=account.user_id if account else None)
        if cached:
            return cached
    if account is None:
        raise SsoError(
            "未找到 MiMo 桌面登录态（passToken/userId），请先在 MiMo Desktop 登录小米账号"
        )
    return await fetch_service_token(account, sid=sid, client=client)
