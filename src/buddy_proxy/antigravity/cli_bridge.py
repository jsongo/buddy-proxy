"""与本机 Antigravity CLI（agy）互通：**读取**它的 keyring 登录态。

agy（官方 Antigravity CLI，Go）把 OAuth token 存系统 keyring（超时/故障才落
文件，常态没有文件可读）：

- macOS：login keychain 的 generic password，**service=``gemini``、
  account=``antigravity``**（antigravity-cli 归在 gemini 产品线下，service
  名就叫 gemini；二进制 strings + 本机 keychain 实测确认）。
  内容是 ``go-keyring-base64:<base64(JSON)>``：

      {"token": {"access_token", "token_type", "refresh_token",
                 "expiry": "2026-10-02T23:55:02.382106+08:00"},
       "auth_method": "consumer", "id_token": "<JWT>"}

  expiry 是 ISO 带时区，与我们 cred 的 ``expiry`` 字段同格式，转换免了。
- Linux：go-keyring 走 Secret Service（secret-tool，schema
  org.gnome.keyring.NetworkPassword，attributes service/username），实验性
  支持；Windows 无对应 CLI，提示手动。

只读不写：agy 没有明文配置文件可回写（keyring 写入格式/加密策略是它内部
实现），且我们不该动别家工具的凭据。刷新后的新 token 只存我们自己的
``~/.buddy-proxy/antigravity_oauth.json``——agy 下次自己刷新时天然不冲突
（两边共享同一个 refresh_token，Google 安装型 client 不轮换 refresh_token，
见 credentials.py 文档）。
"""

from __future__ import annotations

import base64
import json
import platform
import shutil
import subprocess
from typing import Any

#: agy 的 keyring 条目坐标（macOS security / Linux secret-tool 同名）。
KEYRING_SERVICE = "gemini"
KEYRING_ACCOUNT = "antigravity"

#: 内容前缀（agy 的 keyring 封装给 payload 加了 base64 标记）。
_B64_PREFIX = "go-keyring-base64:"


class KeyringReadError(RuntimeError):
    """keyring 读取/解析失败（条目不存在、权限拒绝、内容损坏）。"""


def _security_args() -> list[list[str]] | None:
    """当前平台读取 keyring 的命令（arg 列表集合，依次尝试）；不支持返回 None。"""
    system = platform.system().lower()  # macOS 是 "Darwin"，统一小写再比
    if system == "darwin":
        if not shutil.which("security"):
            return None
        return [
            ["security", "find-generic-password",
             "-s", KEYRING_SERVICE, "-a", KEYRING_ACCOUNT, "-w"],
        ]
    if system == "linux":
        if not shutil.which("secret-tool"):
            return None
        # go-keyring 的 Secret Service 后端：schema 属性以 service/username 记
        return [
            ["secret-tool", "lookup",
             "service", KEYRING_SERVICE, "username", KEYRING_ACCOUNT],
        ]
    return None


def load_cli_creds() -> dict[str, Any] | None:
    """读 agy 的 keyring 登录态，解析成 ``{"token": {...}, "id_token": ...}``。

    没有命令/条目不存在/内容损坏都返回 None（调用方提示改走浏览器授权）。
    """
    for argv in _security_args() or []:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            # 44 = item not found（macOS security）；其他码（权限拒绝等）
            # 对另一个候选命令没意义，直接当不存在处理——不弹误导性提示。
            continue
        raw = (proc.stdout or "").strip()
        if not raw:
            continue
        parsed = _parse_payload(raw)
        if parsed is not None:
            return parsed
    return None


def _parse_payload(raw: str) -> dict[str, Any] | None:
    """keyring 值 → payload dict；格式不认识返回 None。"""
    if raw.startswith(_B64_PREFIX):
        try:
            raw = base64.b64decode(raw[len(_B64_PREFIX):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    token = payload.get("token")
    if not isinstance(token, dict) or not str(token.get("access_token") or "").strip():
        return None
    return payload


def cli_token(payload: dict[str, Any]) -> dict[str, Any]:
    """payload["token"] 原样取出（expiry 已是 ISO，不用转换）。"""
    return dict(payload.get("token") or {})


def cli_cached_email(payload: dict[str, Any]) -> str:
    """从 id_token（JWT payload 的 email claim）拿邮箱；拿不到返回空串。"""
    id_token = str(payload.get("id_token") or "")
    if id_token.count(".") < 2:
        return ""
    try:
        seg = id_token.split(".")[1]
        seg += "=" * (-len(seg) % 4)  # base64url 补齐
        claims = json.loads(base64.urlsafe_b64decode(seg).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    email = claims.get("email") if isinstance(claims, dict) else None
    return str(email) if isinstance(email, str) else ""


def to_buddy_format(token: dict[str, Any]) -> dict[str, Any]:
    """agy token → 我们 cred 形态（expiry 字段名相同，仅挑字段+兜底）。"""
    out: dict[str, Any] = {
        "access_token": str(token.get("access_token") or ""),
        "refresh_token": str(token.get("refresh_token") or ""),
        "token_type": str(token.get("token_type") or "Bearer"),
    }
    expiry = str(token.get("expiry") or "").strip()
    if expiry:
        out["expiry"] = expiry
    return out


def cli_creds_usable(payload: dict[str, Any]) -> tuple[bool, str]:
    """判断 agy 登录态能否直接采用，返回 ``(可用?, 状态说明)``。

    有 refresh_token 就算 access token 过期也可用（导入时自动刷新）。
    """
    token = cli_token(payload)
    if str(token.get("refresh_token") or "").strip():
        from .credentials import access_token_valid

        if access_token_valid(to_buddy_format(token)):
            return True, "有效"
        return True, "access token 已过期，导入时会用 refresh token 自动刷新"
    return False, "没有 refresh token，无法导入（请在 agy 里重新登录后重试）"
