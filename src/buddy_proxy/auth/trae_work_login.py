"""Trae Work (SOLO) 登录脚本 —— 生成登录链接 → 换 token → 落盘凭证。

流程：
1. 生成 machine_id / device_id
2. 构造登录 URL（带 127.0.0.1 回调 + client_id=en1oxy7wnw8j9n）
3. 用户在浏览器登录 → 回跳本机 18080（server 模式）
   或复制回调链接粘贴回来（手动模式，本文件 ``main()``）
4. ExchangeToken（refreshToken → access_token）
5. GetUserInfo 确认 uid → 落盘 ~/.buddy-proxy/trae_work.json

**登录 URL 的参数必须与 Trae CN 客户端 ``handleHandoffExternalSso`` 逐字对齐**
（见 ``/Applications/Trae CN.app/Contents/Resources/app/out/main.js``）。最关键的
是 ``plugin_version="trae-handoff-1.0"``：它是触发「本机回调」分支的哨兵值，
换成数字形态（如 ``"2.3.6"``）会被授权页当版本号规范化并**丢弃
``auth_callback_url`` 等全部附加参数** —— 浏览器照常显示「登录成功」，却
永远不回跳本机，CLI 就一直停在等待界面（2026-09-23 实测根因）。

用法：
  python3 -m buddy_proxy.auth.trae_work_login          # 手动粘贴模式
  python3 -m buddy_proxy.auth.trae_work_login_server   # 一键回调模式
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "en1oxy7wnw8j9n"  # SOLO stable
APP_VERSION = "0.1.43"
API_HOST = "https://api.trae.com.cn"
AUTH_HOST = "https://www.trae.cn/authorization"
#: 触发授权页「本机回调」分支的**唯一**特殊值。授权页把它当版本号解析：
#: 传数字形态（如 "2.3.6"）会被规范化成 "2.3.62834" 并**丢弃 auth_callback_url
#: 等附加参数**，页面就永远不回跳本机（2026-09-23 用真浏览器实测：t0→t20 后
#: query 里 auth_callback_url 消失）；传 "trae-handoff-1.0" 则原样保留全部参数。
#: 该值来自 Trae CN 客户端 out/main.js 的 handleHandoffExternalSso。
PLUGIN_VERSION = "trae-handoff-1.0"
#: 客户端用的回调路径常量（无 nonce 段）。见 main.js: lf.AUTHORIZE="/authorize"
CALLBACK_PATH = "/authorize"
OUT_PATH = Path.home() / ".buddy-proxy" / "trae_work.json"
# 供 trae_work_login_server.py 读取的本次登录状态（含 nonce，按机密对待：
# 落盘走 _write_secret，权限 0600）。放 /tmp 是为了让 CLI 与回调服务两个
# 进程共享，但**不要**用 write_text——那会落成 0644，同机任何进程可读。
STATE_PATH = Path("/tmp/trae_work_login_state.json")
# 本次登录的终态（成功/失败都要写）。CLI 靠它立刻知道结果，
# 不能只看 STATE 是否被删——那样任何失败都会让 CLI 干等到超时。
RESULT_PATH = Path("/tmp/trae_work_login_result.json")
# 登录状态有效期（秒）：超时后 server 拒绝回调
STATE_TTL = 900


def _write_secret(path: Path, payload: dict) -> None:
    """以 0600 权限写 json（避免 write_text 落成 0644，同机可读）。

    STATE/RESULT 都含 nonce（防伪造用）与登录元数据，按机密对待；
    凭证文件同样走 0600（见 server 的 ``_write_cred_secure``）。
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False))


def _http_post_json(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def build_login_url(port: int = 18080) -> tuple[str, str, str, int]:
    """构造 trae.cn 授权页 URL（对齐客户端 ``handleHandoffExternalSso``）。

    返回 ``(url, machine_id, device_id, port)``。

    参数必须与 Trae CN 客户端一致，否则授权页走不到「本机回调」分支：
    最要命的是 ``plugin_version``——传数字形态会被授权页规范化并丢掉
    ``auth_callback_url``（页面照常显示登录成功，却永不回跳本机）。
    """
    machine_id = secrets.token_hex(16)
    device_id = secrets.token_hex(16)
    # 一次性 nonce：server 只接受带匹配 nonce 的回调，防止本机恶意网页
    # 直接 GET /authorize 注入伪造 refreshToken 覆盖凭证。
    # 它**复用 login_trace_id 这个字段**承载（见下方注释），不再另发参数。
    nonce = secrets.token_hex(16)
    params = {
        "login_version": "1",
        "auth_from": "solo",
        "login_channel": "native_ide",
        # 见 PLUGIN_VERSION 注释：这是「本机回调」分支的开关，不能换成版本号
        "plugin_version": PLUGIN_VERSION,
        "auth_type": "local",
        "client_id": CLIENT_ID,
        # 客户端用 "0"。回调是页面 JS fetch 到本机，所以本地服务必须回 CORS 头
        # （trae_work_login_server 已统一加 Access-Control-Allow-Origin: *）。
        "redirect": "0",
        # nonce 走 login_trace_id：这是**唯一**能穿过授权页并原样回传的字段。
        # 2026-09-23 实测回调形态（授权页自己重建 query，只保留它认识的键）：
        #   /authorize?isRedirect=true&scope=solo&data=..&refreshToken=..
        #     &loginTraceID=<我们发的那串>&host=..&refreshExpireAt=..&userJwt=..
        # 注意：① `state` 被授权页**丢掉**（发出去时是 OAuth 惯例字段，但回调
        # 里没有），所以不能靠它；② 回来时的键名是驼峰 `loginTraceID`，而我们
        # 发出去的是下划线 `login_trace_id`，两边都要认（见 _extract_nonce）。
        "login_trace_id": nonce,
        # 路径固定 /authorize，与客户端 lf.AUTHORIZE 一致（不能再塞 path 段）
        "auth_callback_url": f"http://127.0.0.1:{port}{CALLBACK_PATH}",
        "machine_id": machine_id,
        "device_id": device_id,
    }
    url = AUTH_HOST + "?" + urllib.parse.urlencode(params)
    # 状态文件供 trae_work_login_server.py 使用：machine_id/device_id 必须
    # 复用同一对（避免每请求随机指纹触发风控），nonce 用于回调防伪造。
    # 走 _write_secret（0600）：nonce 泄漏就等于防伪形同虚设。
    _write_secret(STATE_PATH, {
        "machine_id": machine_id,
        "device_id": device_id,
        "nonce": nonce,
        "port": port,
        "created_at": int(time.time()),
    })
    # 新一次尝试：清掉上一轮终态，免得 CLI 一启动就吃到旧结果
    RESULT_PATH.unlink(missing_ok=True)
    return url, machine_id, device_id, port


def extract_refresh_token(callback_url: str) -> str:
    """从回调 URL / query 串里取 refreshToken（兼容客户端真实回传形态）。

    客户端解析回调时按这个优先级取（见 main.js handleHandoffExternalSso）：
    ``credential``（URL-encoded 的 JSON 或 form 串，内含 userJwt/refreshToken）
    → ``userJwt``（JSON 串，内含 RefreshToken）→ 顶层 ``refreshToken``。
    我们历史实现只认最后一种，实际回传常常是前两种，所以照抄优先级。
    """
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    if not qs and "=" in callback_url:
        qs = urllib.parse.parse_qs(callback_url)

    def _from_blob(blob: str) -> str:
        """credential 既可能是 JSON，也可能是 form 编码串。"""
        blob = urllib.parse.unquote(blob)
        try:
            obj = json.loads(blob)
            if isinstance(obj, dict):
                jwt = obj.get("userJwt")
                if isinstance(jwt, dict) and jwt.get("RefreshToken"):
                    return str(jwt["RefreshToken"])
                if obj.get("refreshToken"):
                    return str(obj["refreshToken"])
        except Exception:
            pass
        inner = urllib.parse.parse_qs(blob)
        if inner.get("refreshToken"):
            return inner["refreshToken"][0]
        try:
            jwt = json.loads(inner.get("userJwt", [""])[0])
            return str(jwt.get("RefreshToken") or "")
        except Exception:
            return ""

    cred = (qs.get("credential") or [""])[0]
    if cred:
        got = _from_blob(cred)
        if got:
            return got

    user_jwt_raw = (qs.get("userJwt") or [""])[0]
    if user_jwt_raw:
        try:
            jwt = json.loads(urllib.parse.unquote(user_jwt_raw))
            if jwt.get("RefreshToken"):
                return str(jwt["RefreshToken"])
        except Exception:
            pass

    return (qs.get("refreshToken") or [""])[0]


def exchange_token(refresh_token: str) -> dict:
    body = {"ClientID": CLIENT_ID, "RefreshToken": refresh_token, "ClientSecret": "-", "UserID": ""}
    resp = _http_post_json(
        API_HOST + "/cloudide/api/v3/trae/oauth/ExchangeToken",
        body,
        {"Content-Type": "application/json", "User-Agent": f"Trae/{APP_VERSION}"},
    )
    result = resp.get("Result") or {}
    token = result.get("Token") or ""
    if not token:
        raise RuntimeError(f"ExchangeToken 失败: {json.dumps(resp, ensure_ascii=False)[:300]}")
    new_refresh = result.get("RefreshToken") or refresh_token
    expires_at = int(result.get("TokenExpireAt") or 0)
    if expires_at > 10**12:
        expires_at //= 1000
    if expires_at <= time.time():
        expires_at = int(time.time()) + int(result.get("TokenExpireDuration") or 1209600)
    return {"access_token": token, "refresh_token": new_refresh, "expires_at": expires_at}


def get_user_info(token: str) -> dict:
    try:
        ui = _http_post_json(
            API_HOST + "/cloudide/api/v3/trae/GetUserInfo",
            {"ReqSource": "IDE", "IDEVersion": APP_VERSION},
            {"Content-Type": "application/json", "x-cloudide-token": token,
             "User-Agent": f"Trae/{APP_VERSION}"},
        )
        u = ui.get("Result") or ui
        return {
            "uid": str(u.get("UserID") or ""),
            "nickname": str(u.get("ScreenName") or ""),
            "enterprise_id": str(u.get("EnterpriseID") or ""),
        }
    except Exception as e:
        print(f"[*] GetUserInfo 失败: {e}", file=sys.stderr)
        return {}


def main() -> int:
    url, machine_id, device_id, _port = build_login_url()
    print("=" * 60)
    print("Trae Work (SOLO) 登录")
    print("=" * 60)
    print()
    print("步骤：")
    print("  1. 在浏览器打开下面链接，用手机号/验证码登录")
    print("  2. 登录成功后浏览器会跳到打不开的 127.0.0.1 地址")
    print("  3. 复制浏览器地址栏的完整链接，粘贴到下面（不回显）")
    print()
    print("登录链接：")
    print(f"  {url}")
    print()

    import getpass

    callback = getpass.getpass("登录完成后，粘贴回调链接: ")
    if not callback:
        print("未输入，已取消")
        return 1

    refresh_token = extract_refresh_token(callback)
    if not refresh_token:
        print("[!] 回调链接缺少 refreshToken", file=sys.stderr)
        return 1

    cred = exchange_token(refresh_token)
    user = get_user_info(cred["access_token"])

    out = {
        "uid": user.get("uid") or "",
        "nickname": user.get("nickname") or "",
        "enterprise_id": user.get("enterprise_id") or "",
        "access_token": cred["access_token"],
        "refresh_token": cred["refresh_token"],
        "expires_at": cred["expires_at"],
        # OAuth 域（ExchangeToken/GetUserInfo 用），**不是聊天网关**：
        # 聊天固定连 trae/config.py 的 BASE_URL_CN，读本字段会 404（实测）。
        "api_host": API_HOST,
        "machine_id": machine_id,
        "device_id": device_id,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 直接以 0600 权限创建（避免 write_text 后 chmod 前的短暂 0644 窗口）
    fd = os.open(OUT_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    print()
    print(f"[OK] 凭证已保存: {OUT_PATH}")
    print(f"    uid={out['uid']} nickname={out['nickname']} expires={out['expires_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
