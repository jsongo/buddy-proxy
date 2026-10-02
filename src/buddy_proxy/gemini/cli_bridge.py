"""与本机 Gemini CLI 互通：读写 ``~/.gemini`` 下的登录态。

两条链路用的是**同一个 OAuth client**（credentials.py 的 CLIENT_ID/SECRET
与 gemini CLI 0.33.1 oauth2.js 一致）、同一组 scopes，凭证天然互认：

- CLI 默认（未设 ``GEMINI_FORCE_ENCRYPTED_FILE_STORAGE=true``）把凭证明文放
  ``~/.gemini/oauth_creds.json``，google-auth-library 的 Credentials 形态：
  ``{access_token, refresh_token, scope, token_type, expiry_date(毫秒)}``。
  CLI 启动时读入并 setCredentials，过期由 google-auth-library 用
  refresh_token 自动刷新（刷新结果 CLI 自己回写，我们不用管）。
- 非交互运行（``gemini -p "..."``）硬性要求 settings.json 里
  ``security.auth.selectedType == "oauth-personal"``，否则直接报
  "Please set an Auth method"；交互模式没选过也会弹选择框。
- ``google_accounts.json``（``{active, old[]}``）缓存账号邮箱；CLI 首次
  加载会自己拉 userinfo 补写，我们写上只是让账号名立刻显示正确。

CLI 的 homedir 认 ``GEMINI_CLI_HOME`` 环境变量（utils/paths.js 同款语义），
本模块同样遵循——测试靠它把路径指到临时目录，不碰真实 CLI 配置。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: CLI settings.json 里 oauth-personal 的字段值（AuthType.LOGIN_WITH_GOOGLE）
AUTH_TYPE_OAUTH_PERSONAL = "oauth-personal"

#: CLI 判断 token 临期的缓冲（oauth2.js isTokenExpired 同款 5 分钟）。
#: 我们判断「没过期」用同一口径，避免临界点上两边互相认为对方过期。
EXPIRY_SKEW_MS = 5 * 60 * 1000


def gemini_home() -> Path:
    """CLI 的配置目录（``<homedir>/.gemini``；homedir 认 GEMINI_CLI_HOME）。"""
    env = os.environ.get("GEMINI_CLI_HOME", "").strip()
    home = Path(env).expanduser() if env else Path.home()
    return home / ".gemini"


def cli_creds_path() -> Path:
    return gemini_home() / "oauth_creds.json"


def load_cli_creds() -> dict[str, Any] | None:
    """读 CLI 的 oauth_creds.json；缺失/损坏/没有 access_token 返回 None。"""
    try:
        data = json.loads(cli_creds_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        return None
    return data


def cli_cached_email() -> str:
    """google_accounts.json 的 active 邮箱（展示用；没有返回空串）。"""
    try:
        data = json.loads((gemini_home() / "google_accounts.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    email = data.get("active") if isinstance(data, dict) else None
    return str(email) if isinstance(email, str) else ""


def cli_creds_usable(creds: dict[str, Any]) -> tuple[bool, str]:
    """判断 CLI 登录态能否直接采用，返回 ``(可用?, 状态说明)``。

    有 refresh_token 就算 access token 过期也可用（导入时自动刷新）；
    只剩裸 access token 时按 expiry_date（5 分钟缓冲）判断。
    """
    if str(creds.get("refresh_token") or "").strip():
        if _ms_not_expired(creds.get("expiry_date")):
            return True, "有效"
        return True, "access token 已过期，导入时会用 refresh token 自动刷新"
    if _ms_not_expired(creds.get("expiry_date")):
        return True, "access token 有效（无 refresh token，过期后需重新授权）"
    return False, "已过期且无 refresh token"


def _ms_not_expired(expiry_date: Any) -> bool:
    """expiry_date（毫秒）距到期还有 5 分钟以上才算没过期（与 CLI 同口径）。"""
    try:
        ms = int(expiry_date)
    except (TypeError, ValueError):
        return False
    return ms > time.time() * 1000 + EXPIRY_SKEW_MS


def _ms_from_iso(iso: str) -> int | None:
    try:
        dt = datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def to_buddy_format(creds: dict[str, Any]) -> dict[str, Any]:
    """CLI 的 oauth_creds 字段 → 我们 gemini_oauth.json 的形态（毫秒→ISO）。"""
    out: dict[str, Any] = {
        "access_token": str(creds.get("access_token") or ""),
        "refresh_token": str(creds.get("refresh_token") or ""),
        "token_type": str(creds.get("token_type") or "Bearer"),
        "scope": str(creds.get("scope") or ""),
    }
    try:
        ms = int(creds.get("expiry_date"))
    except (TypeError, ValueError):
        ms = None
    if ms:
        out["expiry"] = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    return out


def to_cli_format(cred: dict[str, Any]) -> dict[str, Any]:
    """我们的凭据 → CLI 的 oauth_creds 字段（ISO→毫秒；过期字段尽力转换）。"""
    out: dict[str, Any] = {
        "access_token": cred["access_token"],
        "token_type": cred.get("token_type") or "Bearer",
    }
    if str(cred.get("refresh_token") or "").strip():
        out["refresh_token"] = cred["refresh_token"]
    if str(cred.get("scope") or "").strip():
        out["scope"] = cred["scope"]
    ms = _ms_from_iso(str(cred.get("expiry") or ""))
    if ms is not None:
        out["expiry_date"] = ms
    return out


def sync_to_cli(cred: dict[str, Any]) -> list[str]:
    """把我们登录得到的凭证同步给本机 Gemini CLI（让它直接有登录态）。

    三步各自尽力而为，失败只记说明不抛——我们的登录不该因为 CLI 同步
    失败而失败。返回做了什么的说明列表（给 CLI 输出）。
    """
    notes: list[str] = []
    home = gemini_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"创建 {home} 失败，CLI 互通跳过: {exc}"]

    # 1) oauth_creds.json：合并写（保留 CLI 已有字段如 id_token），0600
    try:
        existing: dict[str, Any] = {}
        if cli_creds_path().exists():
            loaded = json.loads(cli_creds_path().read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        payload = {**existing, **to_cli_format(cred)}
        _write_json(cli_creds_path(), payload, mode=0o600)
        notes.append(f"已写入 {cli_creds_path()}（本机 gemini CLI 可直接使用）")
    except (OSError, json.JSONDecodeError) as exc:
        notes.append(f"写 {cli_creds_path()} 失败: {exc}")

    # 2) settings.json：security.auth.selectedType（CLI 非交互启动的硬要求）
    try:
        notes.append(_ensure_selected_type())
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        notes.append(f"更新 settings.json 失败: {exc}")

    # 3) google_accounts.json：active 邮箱（cacheGoogleAccount 同款合并语义）
    email = str(cred.get("email") or "").strip()
    if email:
        try:
            notes.append(_cache_account(email))
        except (OSError, json.JSONDecodeError) as exc:
            notes.append(f"写 google_accounts.json 失败: {exc}")
    return notes


def _ensure_selected_type() -> str:
    """settings.json 补 ``security.auth.selectedType=oauth-personal``。

    必须读改写保留用户已有配置（hooks 等）——整份覆盖会悄悄删掉用户的
    自定义设置。结构异常时宁可不改也不覆盖。
    """
    path = gemini_home() / "settings.json"
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("settings.json 顶层不是对象，拒绝改动")
    security = data.get("security")
    if security is not None and not isinstance(security, dict):
        raise ValueError("settings.json 的 security 不是对象，拒绝改动")
    auth = (security or {}).get("auth")
    if auth is not None and not isinstance(auth, dict):
        raise ValueError("settings.json 的 security.auth 不是对象，拒绝改动")
    if isinstance(auth, dict) and auth.get("selectedType") == AUTH_TYPE_OAUTH_PERSONAL:
        return "settings.json 已选择 oauth-personal，无需改动"
    data.setdefault("security", {}).setdefault("auth", {})["selectedType"] = AUTH_TYPE_OAUTH_PERSONAL
    _write_json(path, data)
    return f"已设置 {path} 的 security.auth.selectedType={AUTH_TYPE_OAUTH_PERSONAL}"


def _cache_account(email: str) -> str:
    """google_accounts.json 记录 active 邮箱（照抄 CLI cacheGoogleAccount）。

    旧 active 推入 old（去重）、新邮箱从 old 移除、active 换新。
    """
    path = gemini_home() / "google_accounts.json"
    accounts: dict[str, Any] = {"active": None, "old": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            accounts = {
                "active": loaded.get("active"),
                "old": loaded.get("old") if isinstance(loaded.get("old"), list) else [],
            }
    prev = accounts.get("active")
    if isinstance(prev, str) and prev and prev != email and prev not in accounts["old"]:
        accounts["old"].append(prev)
    accounts["old"] = [e for e in accounts["old"] if e != email]
    accounts["active"] = email
    _write_json(path, accounts)
    return f"已更新 {path}（active={email}）"


def _write_json(path: Path, payload: dict[str, Any], mode: int | None = None) -> None:
    """原子写 JSON：临时文件 + rename，避免写一半被 CLI 读到残缺文件。

    mode=None 表示沿用已有文件权限（新文件按 CLI 惯例 0644；
    oauth_creds.json 明确传 0600）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    final_mode = mode
    if final_mode is None:
        final_mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, final_mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, final_mode)  # umask 可能收窄了 open 的权限，显式纠正
    tmp.replace(path)
