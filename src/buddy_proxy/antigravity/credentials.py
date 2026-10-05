"""Antigravity OAuth 凭证：多账号存储、刷新、上游账户信息。

多账号存储（2026-10-03 起）：每个 Google 账号一份完整 cred 文件 + 一份索引：

    ~/.buddy-proxy/antigravity/
    ├── index.json              # 账号清单：[{id, email, priority, added_at}]
    └── <account_id>.json       # 每账号一份 cred（0600）

- 索引按 ``(priority, added_at, id)`` 稳定排序，登录顺序即 failover 优先级；
- ``account_id`` 由 email 规范化而来（拿不到 email 时用 refresh_token 哈希
  兜底），一旦落盘不再变化，凭据更新不影响坐标（同 trae PAT 的 cache_key
  思路，但绑的是完整 cred 文件名）；
- 历史单账号文件 ``~/.buddy-proxy/antigravity_oauth.json`` 首次访问时自动
  copy 迁移为账号 #1（copy 而非 move——旧文件留作备份，回滚旧版本仍能用，
  refresh_token 不轮换所以长期有效）。

单账号 cred 文件格式：

    {
      "account_id": "u_x.com",                  # 稳定账号坐标
      "access_token": "...",
      "refresh_token": "...",
      "expiry": "2026-10-03T12:00:00+00:00",   # ISO，UTC
      "token_type": "Bearer",
      "scope": "...",
      "email": "you@gmail.com",
      "project_id": "...",                      # onboardUser 分配的托管项目
      "tier": "free-tier",
      "saved_at": 1730000000
    }

OAuth 三方与 Antigravity 客户端一致（安装型应用公开 client）：
client ``1071006060591-tmhssin2h21lcre235vtolojh4g403ep``，scopes 比 gemini
多 ``cclog`` + ``experimentsandconfigs`` 共 5 个。refresh_token 不轮换
（Google 对安装型应用默认如此），长期持有即可。

agy CLI 的登录态在系统 keyring（黑盒、跨应用读取要弹权限框），不做互通，
独立走 OAuth。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from buddy_proxy.core.paths import state_dir, state_file

log = logging.getLogger(__name__)

CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo?alt=json"

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]

#: 提前这个量刷新 access_token，避免临界点请求带着过期票出门。
_EXPIRY_SKEW = timedelta(minutes=2)

#: 账号数上限（登录是一次次手动点浏览器授权的操作，8 个远超正常用量）。
_MAX_ACCOUNTS = 8

#: account_id 只允许这一种形状：既是文件名（无 ``/``，天然防路径穿越），
#: 也是 UI/metrics 里的账号坐标。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


class AuthError(RuntimeError):
    """刷新失败（网络/被拒/refresh_token 作废）。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。"""

    id: str
    email: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：antigravity 本无 name 字段，别名是唯一的
    # 「起名」入口；空串 = 未设置（显示回退 email/id）。upsert/重登不碰它。
    alias: str = ""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def ag_state_dir() -> pathlib.Path:
    """多账号状态目录（``ANTIGRAVITY_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("ANTIGRAVITY_STATE_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_dir() / "antigravity"


def index_path() -> pathlib.Path:
    """账号清单文件：``<state_dir>/index.json``。"""
    return ag_state_dir() / "index.json"


def cred_path() -> "pathlib.Path":
    """历史单账号文件路径（现仅作为迁移源；``ANTIGRAVITY_OAUTH_JSON`` 覆盖）。"""
    env = os.environ.get("ANTIGRAVITY_OAUTH_JSON", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_file("antigravity_oauth.json")


def account_cred_path(account_id: str) -> pathlib.Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    if not _ID_RE.fullmatch(account_id):
        raise ValueError(f"非法账号 id: {account_id!r}")
    return ag_state_dir() / f"{account_id}.json"


# ---------------------------------------------------------------------------
# 索引读写（全部在 _index_lock 下进行）
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()


def _atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """0600 原子写（临时文件 + rename，与 gemini/save_cred 同款）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_index() -> dict[str, Any]:
    try:
        data = json.loads(index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _index_payload(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": 1, "accounts": entries}


# ---------------------------------------------------------------------------
# account_id 派生
# ---------------------------------------------------------------------------

def _derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    email 是首选来源（人可读）；拿不到 email（userinfo 失败/adopt 中间态）
    退回 refresh_token 哈希——Google 安装型 client 不轮换 refresh_token，
    同一账号哈希稳定，不同账号必不同。
    """
    existing = str(cred.get("account_id") or "").strip()
    if existing and _ID_RE.fullmatch(existing):
        return existing
    email = str(cred.get("email") or "").strip().lower()
    if email:
        slug = re.sub(r"[^a-z0-9._@-]", "_", email)[:64]
        if _ID_RE.fullmatch(slug):
            return slug
        return "u_" + slug
    digest = hashlib.sha256(str(cred.get("refresh_token") or "").encode()).hexdigest()[:12]
    return f"acct-{digest}"


# ---------------------------------------------------------------------------
# 迁移（历史单账号文件 → 多账号目录）
# ---------------------------------------------------------------------------

def load_legacy_cred() -> dict[str, Any] | None:
    """读历史单账号文件；没有/损坏/缺 refresh_token 返回 None。"""
    try:
        data = json.loads(cred_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("refresh_token") or "").strip():
        return None
    return data


def _migrate_legacy_unlocked() -> None:
    """旧单账号文件 → 账号 #1（copy 语义，旧文件保留）。

    index.json 的存在本身就是「已迁移」标记。失败只 warning 不阻断——
    最坏表现是未登录（401 引导重登），legacy 文件原封未动，凭据没有丢。
    """
    if index_path().exists():
        return
    legacy = load_legacy_cred()
    if legacy is None:
        return
    try:
        ref = _save_account_cred_unlocked(legacy)
        log.info("antigravity: 已迁移旧凭据 %s → %s（旧文件保留作备份）",
                 cred_path(), account_cred_path(ref.id))
    except (OSError, ValueError) as exc:
        log.warning("antigravity: 迁移旧凭据失败（%s），请重新 `buddy login antigravity`", exc)


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------

def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一的账号枚举入口（对齐 trae PAT 的 ``ensure_pat_config``）：迁移、
    索引自愈（cred 文件被删/损坏的条目剔除——删文件即退出该账号）都挂在
    这里，login/forward/quota/UI 走同一个口，不存在「半迁移」状态。
    """
    with _index_lock:
        _migrate_legacy_unlocked()
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        kept: list[AccountRef] = []
        changed = False
        for e in entries:
            aid = str(e.get("id") or "")
            if not _ID_RE.fullmatch(aid):
                changed = True
                continue
            if load_account_cred(aid) is None:
                changed = True
                log.warning("antigravity: 账号 %s 的凭据文件缺失或损坏，已从账号清单剔除", aid)
                continue
            kept.append(AccountRef(
                id=aid,
                email=str(e.get("email") or ""),
                priority=int(e.get("priority") or 0),
                added_at=int(e.get("added_at") or 0),
                alias=str(e.get("alias") or ""),
            ))
        if changed:
            _atomic_write_json(index_path(), _index_payload([asdict(a) for a in kept]))
        kept.sort(key=lambda a: (a.priority, a.added_at, a.id))
        return kept


def load_account_cred(account_id: str) -> dict[str, Any] | None:
    """读单账号 cred；没有/损坏/缺 refresh_token 返回 None。"""
    if not _ID_RE.fullmatch(account_id):
        return None
    try:
        data = json.loads(account_cred_path(account_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("refresh_token") or "").strip():
        return None
    return data


def save_account_cred(cred: dict[str, Any]) -> AccountRef:
    """落盘一个账号的凭据（幂等 upsert），返回账号引用。

    匹配顺序：account_id → email → refresh_token。命中即更新该账号
    （priority/added_at 不变——重新登录不改变 failover 顺位），全不命中
    追加为新账号（priority 排到最后）。cred 缺 account_id 时现场生成并
    写回原 dict（调用方拿到即可用）。
    """
    with _index_lock:
        return _save_account_cred_unlocked(cred)


def _save_account_cred_unlocked(cred: dict[str, Any]) -> AccountRef:
    if not _ID_RE.fullmatch(str(cred.get("account_id") or "")):
        cred["account_id"] = _derive_account_id(cred)  # 就地写回，调用方可见
    entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
    aid = str(cred["account_id"])
    target = next((e for e in entries if str(e.get("id") or "") == aid), None)
    if target is None:
        email = str(cred.get("email") or "").strip().lower()
        if email:
            target = next(
                (e for e in entries if str(e.get("email") or "").strip().lower() == email), None)
    if target is None:
        refresh_token = str(cred.get("refresh_token") or "")
        if refresh_token:
            target = next(
                (e for e in entries
                 if (load_account_cred(str(e.get("id") or "")) or {}).get("refresh_token")
                 == refresh_token),
                None)
    if target is not None:
        # 命中已有账号：文件名跟索引里的 id 走，cred 里的 account_id 对齐
        aid = str(target["id"])
        cred["account_id"] = aid
        target["email"] = str(cred.get("email") or target.get("email") or "")
        # alias 不在 upsert 覆写之列：它是用户手起的本地别名，重登不改
        ref = AccountRef(id=aid, email=target["email"],
                         priority=int(target.get("priority") or 0),
                         added_at=int(target.get("added_at") or 0),
                         alias=str(target.get("alias") or ""))
    else:
        if len(entries) >= _MAX_ACCOUNTS:
            raise AuthError(
                f"antigravity 账号数已达上限 {_MAX_ACCOUNTS}，"
                f"请先删除不用的账号（删 {ag_state_dir()} 下对应 .json 即可）")
        ref = AccountRef(
            id=aid,
            email=str(cred.get("email") or ""),
            priority=max((int(e.get("priority") or 0) for e in entries), default=-1) + 1,
            added_at=int(time.time()),
        )
        entries.append(asdict(ref))
    _atomic_write_json(account_cred_path(ref.id), dict(cred))
    _atomic_write_json(index_path(), _index_payload(entries))
    return ref


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    with _index_lock:
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        kept = [e for e in entries if str(e.get("id") or "") != account_id]
        if len(kept) == len(entries):
            return False
        _atomic_write_json(index_path(), _index_payload(kept))
    try:
        account_cred_path(account_id).unlink()
    except OSError:
        pass
    return True


def reorder_accounts(ordered_ids: list[str]) -> list[AccountRef]:
    """按给定 id 顺序重写全部账号的 priority（failover 顺位）。

    入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）——
    局部重排语义模糊（没提到的账号排哪？），要求全量提交让调用方（UI 上移
    下移按钮、拖拽）自己算好完整顺序，这里只做校验与落盘。

    priority 重写为 0..n-1；added_at 原样保留（它是「登录时间」的事实，
    与顺位解耦后仍可用于展示/审计）。返回重排后的 list_accounts()。
    """
    list_accounts()  # 先走唯一的枚举入口：迁移/自愈完再重排，别在残缺索引上动刀
    with _index_lock:
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        current = [str(e.get("id") or "") for e in entries]
        if sorted(ordered_ids) != sorted(current) or len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError(
                f"重排必须提交完整的账号 id 列表（当前 {len(current)} 个，"
                f"收到 {len(ordered_ids)} 个）")
        rank = {aid: i for i, aid in enumerate(ordered_ids)}
        for e in entries:
            e["priority"] = rank[str(e.get("id") or "")]
        _atomic_write_json(index_path(), _index_payload(entries))
    return list_accounts()


def rename_account(account_id: str, alias: str) -> AccountRef:
    """改一个账号的本地别名（管理页 ✎）。

    只改 index.json 条目的 ``alias`` 字段：cred 文件（原始凭据）与
    priority/added_at 都不动。``alias`` 去首尾空白；空串 = 清除别名，
    显示回退默认名。账号不存在抛 ``ValueError``。返回改后的 list_accounts()。
    """
    aid = str(account_id or "")
    text = str(alias or "").strip()
    list_accounts()  # 先走唯一的枚举入口：迁移/自愈完再改，别在残缺索引上动刀
    with _index_lock:
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        target = next((e for e in entries if str(e.get("id") or "") == aid), None)
        if target is None:
            raise ValueError(f"账号 {aid} 不存在")
        target["alias"] = text
        _atomic_write_json(index_path(), _index_payload(entries))
    return list_accounts()


def primary_cred() -> dict[str, Any] | None:
    """优先级最高的账号 cred（转发/旧 API 的默认账号）。"""
    accounts = list_accounts()
    return load_account_cred(accounts[0].id) if accounts else None


def has_accounts() -> bool:
    return bool(list_accounts())


def refresh_account_cred(cred: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """用 refresh_token 换新 access_token，更新并写回该账号的文件。

    返回更新后的 cred（原 dict 就地更新）。写回走 :func:`save_account_cred`
    的幂等 upsert——同 account_id/refresh_token 必然命中原账号，不会在索引
    里复制出新条目，priority 也不变。刷新失败抛 :class:`AuthError`。
    """
    data = {
        "grant_type": "refresh_token",
        "refresh_token": cred["refresh_token"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    payload = _token_request(data, timeout=timeout)
    cred["access_token"] = payload["access_token"]
    cred["expiry"] = _expiry_from(payload.get("expires_in"))
    cred["token_type"] = payload.get("token_type") or "Bearer"
    if payload.get("scope"):
        cred["scope"] = payload["scope"]
    # Google 偶尔会滚动新的 refresh_token；没给就保留旧的（常态）。
    if payload.get("refresh_token"):
        cred["refresh_token"] = payload["refresh_token"]
    save_account_cred(cred)
    return cred


# ---------------------------------------------------------------------------
# token 获取（转发主路径）
# ---------------------------------------------------------------------------

_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(account_id: str) -> threading.Lock:
    """每账号一把刷新锁：并发请求同时发现 token 过期时只有一个去刷新。"""
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


def ensure_account_token(account_id: str, *, force_refresh: bool = False) -> tuple[str, dict[str, Any]]:
    """拿该账号可用的 access_token，返回 ``(token, cred 快照)``。

    cred 与 token 同源是本函数存在的理由：转发要 project_id（账号级身份），
    旧的「token、cred 各读一次盘」写法靠同一个文件隐式保证一致，多账号下
    会串号——现在一次调用拿齐。刷新在每账号锁内做并重读盘双检，拿到的
    cred 快照（dict 拷贝）供调用方读 project_id/tier 等账号字段。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"antigravity 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and access_token_valid(cred):
        return cred["access_token"], dict(cred)
    with _refresh_lock(account_id):
        cred = load_account_cred(account_id) or cred  # 别的线程可能刚刷完落盘
        if force_refresh or not access_token_valid(cred):
            refresh_account_cred(cred)
        return cred["access_token"], dict(cred)


# ---------------------------------------------------------------------------
# token 协议（纯函数）
# ---------------------------------------------------------------------------

def _token_request(data: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - urllib 报错种类多，统一转 AuthError
        raise AuthError(f"Google token 接口请求失败: {exc}") from exc
    if "error" in payload and "access_token" not in payload:
        raise AuthError(f"Google token 接口拒绝: {payload.get('error')}")
    return payload


def _expiry_from(expires_in: Any) -> str:
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        seconds = 3600.0
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _expiry_dt(cred: dict[str, Any]) -> datetime | None:
    raw = str(cred.get("expiry") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def access_token_valid(cred: dict[str, Any]) -> bool:
    """access_token 是否还在有效期内（含 2 分钟提前量）。"""
    exp = _expiry_dt(cred)
    if exp is None:
        return False
    return datetime.now(timezone.utc) < exp - _EXPIRY_SKEW


def fetch_user_email(access_token: str, timeout: float = 15.0) -> str:
    """拉账号邮箱（展示用；失败返回空串，不阻断登录）。"""
    req = urllib.request.Request(
        USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - 展示字段，拿不到就算了
        return ""
    return str(data.get("email") or "")


# ---------------------------------------------------------------------------
# 兼容薄壳：旧单账号 API → 多账号 API（调用方迁移完成后删除）
# ---------------------------------------------------------------------------

def save_cred(cred: dict[str, Any]) -> "pathlib.Path":
    """旧 API：写「当前主账号」（实际是 upsert，可能追加新账号）。"""
    ref = save_account_cred(cred)
    return account_cred_path(ref.id)


def load_cred() -> dict[str, Any] | None:
    """旧 API：读优先级最高的账号。"""
    return primary_cred()


def has_cred() -> bool:
    return has_accounts()


def refresh_cred(cred: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """旧 API：刷新并落盘（等价 refresh_account_cred）。"""
    return refresh_account_cred(cred, timeout=timeout)


def ensure_access_token(cred: dict[str, Any] | None = None) -> str:
    """旧 API：拿一个可用的 access_token（cred 缺省 = 主账号）。"""
    if cred is None:
        cred = primary_cred()
        if cred is None:
            raise AuthError("antigravity 未登录：请先运行 `buddy login antigravity`")
    if not access_token_valid(cred):
        refresh_cred(cred)
    return cred["access_token"]
