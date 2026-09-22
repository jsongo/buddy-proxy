"""Trae Work 一键登录服务 —— 监听 127.0.0.1:18080 捕获回调，自动完成换 token。

用法：
  1. 先运行本脚本（后台/前台均可，监听 18080）
  2. 打开登录链接（trae_work_login.py 生成的），浏览器登录
  3. 登录成功后授权页用 JS fetch 回调 http://127.0.0.1:18080/authorize?...
  4. 本服务捕获回调 → ExchangeToken → GetUserInfo → 落盘 ~/.buddy-proxy/trae_work.json
  5. 自动用 Work 通道（solo_work_lite）发一条测试消息验证

两个必须成立的前提（2026-09-23 逐条实测确认）：

- **回调是跨源 fetch，不是整页跳转**（客户端用 ``redirect=0``）。所以本服务必须
  回 ``Access-Control-Allow-Origin``，否则浏览器直接拦掉请求，页面只会显示
  「登录失败 - 网络错误」，服务端连日志都看不到。
- **登录 URL 的 ``plugin_version`` 必须是 ``trae-handoff-1.0``**（见
  ``trae_work_login.PLUGIN_VERSION``）。传版本号会被授权页改写 URL 并丢弃
  ``auth_callback_url``，回调压根不会发起。
"""
from __future__ import annotations

import html
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from buddy_proxy.auth.trae_work_login import _write_secret, extract_refresh_token  # noqa: E402

CLIENT_ID = "en1oxy7wnw8j9n"
APP_VERSION = "0.1.43"
# OAuth 域：仅用于 ExchangeToken / GetUserInfo（/cloudide/api/v3/trae/*），
# 也被写进凭证的 api_host 字段。**不是聊天网关**——实测它对该路径一律 404。
API_HOST = "https://api.trae.com.cn"
# 聊天网关（与 trae/config.py 的 BASE_URL_CN 一致）：/api/agent/v3/llm_utils_chat
CHAT_HOST = "https://trae-api-cn.mchost.guru"
OUT_PATH = Path.home() / ".buddy-proxy" / "trae_work.json"
# 与 trae_work_login.py 共享的本次登录状态（nonce + machine_id/device_id）
STATE_PATH = Path("/tmp/trae_work_login_state.json")
# 与 trae_work_login.py 共享的本次登录终态（成功/失败都写）。
# CLI 靠它立刻拿到结果——只靠「STATE 被删」当成功信号的话，
# 任何失败路径都会让 CLI 干等到 15 分钟超时（2026-09-23 实测踩过）。
RESULT_PATH = Path("/tmp/trae_work_login_result.json")
STATE_TTL = 900  # 状态有效期（秒）
PORT = 18080


def _write_result(ok: bool, message: str, *, nonce: str = "", **extra) -> None:
    """落盘本次登录的终态，供 buddy login trae 立刻收割。

    ``nonce`` 标明这是哪一次尝试的结果：CLI 只认跟自己对得上的，免得上一轮
    迟到的结果误杀新一轮登录。

    走 ``_write_secret``（0600）：payload 含 nonce，不该被同机其他进程读到。
    """
    payload = {
        "ok": ok,
        "message": message,
        "nonce": nonce,
        "at": int(time.time()),
        **extra,
    }
    try:
        _write_secret(RESULT_PATH, payload)
    except Exception as e:  # noqa: BLE001 — 结果文件写失败不该盖掉真实错误
        print(f"[!] 写登录结果文件失败: {e}", file=sys.stderr)


def _redact_url(path: str) -> str:
    """日志脱敏：refreshToken / userJwt / credential 等只留长度，其余原样。

    ``credential`` 必须在内：它是客户端实际回传的载体，内含 RefreshToken
    明文（URL-encoded 的 JSON）。漏掉它等于把令牌打进日志（2026-09-23 实测
    发现 ``_redact_url`` 只挡了 refreshToken/userJwt，credential 明文外泄）。

    ``userinfo`` / ``data`` 同样在内：实测回调的 ``userInfo`` 是 URL-encoded
    JSON，含手机号、邮箱、昵称、UserID、TenantID 等 PII，``data`` 是不透明
    签名串——都不该进日志。
    """
    sensitive = {
        "refreshtoken",
        "userjwt",
        "credential",
        "userinfo",
        "data",
        "token",
        "access_token",
        "refresh_token",
    }
    parsed = urllib.parse.urlparse(path)
    if not parsed.query:
        return path
    kept = []
    for part in parsed.query.split("&"):
        if not part:
            continue
        k, _, v = part.partition("=")
        if k.lower() in sensitive:
            kept.append(f"{k}=<redacted {len(v)} chars>")
        else:
            kept.append(part)
    return parsed.path + ("?" + "&".join(kept) if kept else "")


def _extract_nonce(path: str, qs: dict) -> str:
    """从回调里取 nonce：``loginTraceID`` 优先，其次 path 段 / ``state`` / ``nonce``。

    主通道是 **``loginTraceID``**（驼峰）：授权页回调时会**重建整个 query**，
    只保留它自己认识的键，``state`` 会被丢掉；唯一能原样穿回来的就是我们为了
    这个目的放进 ``login_trace_id`` 的值（2026-09-23 实测回调形态见
    ``trae_work_login.build_login_url`` 注释）。发出去用下划线、回来用驼峰，
    所以两种写法都要认。

    其余分支只是兼容：path 段（老链接 ``/authorize/<nonce>``）、``state`` 与
    ``nonce``（历史实现用过，保留以免旧链接直接失效）。
    """
    for key in ("loginTraceID", "login_trace_id", "state", "nonce"):
        val = (qs.get(key) or [""])[0]
        if val:
            return val
    segs = [s for s in urllib.parse.urlparse(path).path.split("/") if s]
    # 形如 ['authorize', '<nonce>']
    if len(segs) >= 2 and segs[0] == "authorize" and segs[1]:
        return segs[1]
    return ""


def _load_login_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _write_cred_secure(path: Path, data: dict) -> None:
    """以 0600 权限原子创建凭证文件（避免 write_text 后 chmod 前的短暂 0644 窗口）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))


def _http_post_json(url: str, body: dict, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    data = json.dumps(body).encode()
    with urllib.request.urlopen(req, data, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


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


def test_work_chat(work: dict) -> str:
    """用 Work 通道发一条测试消息。

    请求头复用 ``trae.credentials._work_headers``，与真实聊天路径完全一致：
    手写头会漏掉 ``User-Agent``，上游按异常客户端限流返回 4011（实测），
    把「网络/凭证正常」误报成失败。
    """
    import uuid

    from buddy_proxy.trae.credentials import _work_headers

    body = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": "1+1等于几"}]}],
        "model": "glm-5.2",
        "function": "solo_work_lite",
        "stream": True,
        "request_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
    }
    headers = {
        **_work_headers(work),
        "Accept": "text/event-stream",
    }
    # 聊天走专用网关，不能用 work["api_host"]：那是 OAuth 域，
    # 对 /api/agent/v3/llm_utils_chat 恒返回 404（实测）。
    url = CHAT_HOST.rstrip("/") + "/api/agent/v3/llm_utils_chat"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    #: 授权页从 https://www.trae.cn 用 JS fetch 打本机回调（redirect=0，
    #: 与 Trae CN 客户端 handleHandoffExternalSso 一致），因此**必须**回 CORS
    #: 头，否则浏览器直接拦掉、请求根本到不了这里，页面只会显示
    #: 「登录失败 - 网络错误」。``Access-Control-Allow-Headers: *`` 是为了
    #: 预检里可能带的 x-* 头。
    CORS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }

    def _cors(self) -> None:
        for k, v in self.CORS.items():
            self.send_header(k, v)

    def do_OPTIONS(self):  # noqa: N802 — BaseHTTPRequestHandler 约定
        """预检请求：只回 CORS 头，不记业务日志。"""
        self.send_response(200)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _reject(self, page_html: str, reason: str, *, notify: bool, nonce: str = "") -> None:
        """拒绝回调。

        ``page_html`` 给浏览器看，``reason`` 是纯文本——分开是因为 RESULT 里的
        文案会被 CLI 直接打印，混进 HTML 会糊一整行标签。
        ``notify=True`` 时写失败终态（有进行中的登录尝试，必须让 CLI 立刻知道）；
        没有进行中的尝试（如成功后旧标签页重放）就不写，免得覆盖掉已有的成功结果。
        """
        self.send_response(403)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page_html.encode())
        print(f"[!] 拒绝回调: {reason}", file=sys.stderr)
        if notify:
            _write_result(False, reason, nonce=nonce)

    def do_GET(self):
        """捕获 /authorize 回调。

        关于 STATE 的生命周期：**只有成功路径删 STATE**（一次性消费）。
        失败路径（nonce 不匹配 / 缺 refreshToken / 换票报错 / 状态过期）一律保留，
        因为失败常常是上游抖动（或用户复制错了回调链接），此时保留 nonce 让用户
        能直接在浏览器重试或重新粘贴，不必回 CLI 重跑一轮；反正它有 15 分钟
        ``STATE_TTL`` 兜底。这不是遗漏——改动这里前先想清楚重试路径。
        """
        if not self.path.startswith("/authorize"):
            self.send_response(404)
            self._cors()
            self.end_headers()
            self.wfile.write(b"not found")
            return
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        # 与客户端同样的优先级取令牌：credential → userJwt → refreshToken
        refresh_token = extract_refresh_token(self.path)

        # nonce 校验：回调必须携带 trae_work_login.py 生成的 state 文件中
        # 的一次性 nonce，防止本机恶意网页直接 GET 注入伪造 refreshToken
        # 覆盖用户凭证（否则后续对话会被发到攻击者账号）
        login_state = _load_login_state()
        expected_nonce = login_state.get("nonce") or ""
        created_at = int(login_state.get("created_at") or 0)
        callback_nonce = _extract_nonce(self.path, qs)
        # 有 expected_nonce 才算「有进行中的尝试」——失败要通知 CLI
        notify = bool(expected_nonce)
        if not expected_nonce:
            self._reject(
                "<h3>回调被拒绝</h3><p>未找到登录状态（nonce 缺失）。"
                "请先运行 trae_work_login.py 生成登录链接后再走服务器回调流程</p>",
                "未找到登录状态（当前没有进行中的登录尝试）",
                notify=False,
            )
            return
        if time.time() - created_at > STATE_TTL:
            self._reject(
                "<h3>回调被拒绝</h3><p>登录状态已过期（超过 15 分钟），"
                "请重新运行 trae_work_login.py 生成新的登录链接</p>",
                "登录状态已过期（超过 15 分钟），请重新发起登录",
                notify=notify,
                nonce=expected_nonce,
            )
            return
        if not callback_nonce or not secrets.compare_digest(callback_nonce, expected_nonce):
            # 这条最值得看：多半是上游改写了回调 URL 的 query/path，
            # 把我们埋的 nonce 冲掉了（而不是真的有攻击者）
            detail = (
                f"nonce 校验失败（回调 nonce 长度={len(callback_nonce)}，"
                f"期望长度={len(expected_nonce)}）：多半是授权服务器拼回调时"
                "改写了路径/query，把我们埋的 nonce 冲掉了"
            )
            self._reject(
                f"<h3>回调被拒绝</h3><p>{detail}</p><p>请把本页地址栏发给维护者排查</p>",
                detail,
                notify=notify,
                nonce=expected_nonce,
            )
            return

        if not refresh_token:
            # 先写终态再回包：免得客户端读完 body 就返回、结果文件还没落盘
            reason = "回调缺少 refreshToken（授权服务器没把令牌带上）"
            _write_result(False, reason, nonce=expected_nonce)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<h3>登录失败：回调缺少 refreshToken</h3><p>请检查链接是否完整</p>".encode()
            )
            print(f"[!] {reason}", file=sys.stderr)
            return

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        try:
            cred = exchange_token(refresh_token)
            user = get_user_info(cred["access_token"])
            out = {
                "uid": user.get("uid") or "",
                "nickname": user.get("nickname") or "",
                "enterprise_id": user.get("enterprise_id") or "",
                "access_token": cred["access_token"],
                "refresh_token": cred["refresh_token"],
                "expires_at": cred["expires_at"],
                # OAuth 域，**不是聊天网关**（见文件头 API_HOST 注释）。
                "api_host": API_HOST,
                "machine_id": login_state.get("machine_id", ""),
                "device_id": login_state.get("device_id", ""),
            }
            _write_cred_secure(OUT_PATH, out)
            # 先写终态再删 STATE：CLI 优先读 RESULT，删除 STATE 是旧版兜底信号
            _write_result(
                True,
                "登录成功",
                nonce=expected_nonce,
                uid=out["uid"],
                nickname=out["nickname"],
                expires_at=out["expires_at"],
            )
            # 一次性消费：成功落盘后删除状态文件，重放/重复回调一律拒绝
            STATE_PATH.unlink(missing_ok=True)
            msg = f"<h3>登录成功！</h3><p>uid={html.escape(out['uid'])} nickname={html.escape(out['nickname'])}</p><p>凭证已保存，可以关闭此页面</p>"
            self.wfile.write(msg.encode())
            print(f"\n[OK] 凭证已保存: {OUT_PATH}")
            print(f"    uid={out['uid']} nickname={out['nickname']}")
            print(f"    expires_at={out['expires_at']}")

            # 自动测试 Work 通道
            print("\n[*] 测试 Work 通道 (solo_work_lite)...")
            try:
                raw = test_work_chat(out)
                print("=== Work chat 响应 ===")
                print(raw[:500])
            except Exception as e:
                print(f"[!] Work chat 测试失败: {e}")

            # 登录闭环完成：自动退出（ThreadingHTTPServer 的 handler 在子线程，
            # 可安全调 shutdown），buddy login trae 以进程退出作为收割信号
            self.wfile.flush()
            print("\n[*] 登录完成，回调服务自动退出")
            self.server.shutdown()
        except Exception as e:
            msg = f"<h3>换 token 失败</h3><p>{html.escape(str(e))}</p>"
            self.wfile.write(msg.encode())
            print(f"[!] 失败: {e}", file=sys.stderr)
            _write_result(False, f"换 token 失败: {e}", nonce=expected_nonce)

    def log_message(self, fmt, *args):
        """记一条访问日志（脱敏）。

        以前这里是 ``pass``：回调失败时完全无法判断浏览器到底有没有打过来、
        带了什么参数，只能靠「STATE 还在 + 凭证没更新」反推。必须留痕。
        """
        try:
            target = _redact_url(self.path)
            print(f"[srv] {self.command} {target}", file=sys.stderr)
        except Exception:
            pass


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[*] 回调监听启动: http://127.0.0.1:{PORT}/authorize")
    print("[*] 现在打开登录链接，登录成功后会自动回调到这里")
    print("[*] 等待回调...（Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
