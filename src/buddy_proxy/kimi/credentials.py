"""Kimi Code 凭证：多账号存储、刷新、token 获取。

结构照抄 antigravity（多账号 index + 每账号一份 cred 文件）：

    ~/.buddy-proxy/kimi/
    ├── index.json              # 账号清单：[{id, name, priority, added_at}]
    └── <account_id>.json       # 每账号一份 cred（0600）

单账号 cred 与 kimi cli 导出的 token JSON 尽量同形（导入近乎透传）：

    {
      "account_id": "cv9qnmhrqpmb0sr6prt0",   # 稳定坐标 = 文件名
      "type": "kimi",
      "access_token": "...",                  # 15 分钟
      "refresh_token": "...",                 # 30 天
      "expired": "2026-10-03T06:06:17Z",      # ISO（沿用 kimi cli 的字段名）
      "last_refresh": 1730000000,
      "token_type": "Bearer",
      "scope": "kimi-code",
      "base_url": "https://api.kimi.com/coding",   # 不带 /v1，调用时 normalize
      "oauth_host": "https://auth.kimi.com",       # 按账号存（global 区是 auth.kimi.ai）
      "device_id": "<uuid4>",                      # 每账号固定；X-Msh-Device-Id 用它
      "user_id": "cv9qnmhrqpmb0sr6prt0",           # /v1/me 回填（展示 + id 派生源）
      "nickname": "..."                            # /v1/me 缓存（面板显示名，可选）
    }

``account_id`` 首选 user_id（同一账号重登/换设备后仍稳定）；导入的 JSON 没
有 user_id 且 /v1/me 又拿不到时退 ``kimi-<device_id>``，再退 refresh_token
哈希兜底。kimi 是否轮换 refresh_token 未确证——刷新回写时「payload 带了才
更新」，没带保留旧值（防御性，与 antigravity 同策略）。
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import pathlib
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from buddy_proxy.core.paths import state_dir

from . import oauth
from .upstream import cred_expired_epoch

log = logging.getLogger(__name__)

#: 提前这个量刷新 access_token——寿命只有 15 分钟，2 分钟提前量意味着
#: 每个账号约 13 分钟刷一次，量级可忽略。
_EXPIRY_SKEW = timedelta(minutes=2)

#: 账号数上限（与 antigravity 同值：手动授权/导入的操作，8 个远超正常用量）。
_MAX_ACCOUNTS = 8

#: account_id 形状：既是文件名（无 ``/``，天然防路径穿越）也是 UI 账号坐标。
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")


class AuthError(RuntimeError):
    """刷新失败（网络/被拒/refresh_token 作废）。"""


@dataclass(frozen=True)
class AccountRef:
    """账号清单条目（不含任何秘密，可直接进 UI/health）。"""

    id: str
    name: str
    priority: int
    added_at: int
    # 本地别名（管理页 ✎ 改）：只改显示名，cred 里的 nickname 不动；
    # 空串 = 未设置（显示回退 name/id）。upsert/重登不碰它。
    alias: str = ""


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def kimi_state_dir() -> pathlib.Path:
    """多账号状态目录（``KIMI_STATE_DIR`` 可覆盖；测试隔离点）。"""
    env = os.environ.get("KIMI_STATE_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    return state_dir() / "kimi"


def index_path() -> pathlib.Path:
    return kimi_state_dir() / "index.json"


def account_cred_path(account_id: str) -> pathlib.Path:
    """单账号 cred 文件路径。id 形状在生成处已校验，这里再防一道手滑。"""
    if not _ID_RE.fullmatch(account_id):
        raise ValueError(f"非法账号 id: {account_id!r}")
    return kimi_state_dir() / f"{account_id}.json"


# ---------------------------------------------------------------------------
# 索引读写（全部在 _index_lock 下进行）
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()


class _FileLock:
    """跨进程文件锁（fcntl.flock，包在 .lock 文件上）。

    ``_index_lock`` 只挡进程内——网关进程和 CLI（``buddy login kimi`` /
    面板导入是另一个进程入口）会同时读-改-写同一个 index.json，各自拿旧
    快照写回就会丢更新（面板显示幽灵账号、顺位错乱）。flock 让读-改-写整体
    成为一个跨进程临界区；flock 与进程内锁可组合（同一进程重复 flock 同一
    文件会被内核拒绝，故必须先拿进程内锁再拿文件锁）。
    """

    def __init__(self, path: pathlib.Path):
        self._path = path
        self._fh = None

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+")  # noqa: SIM115 - 句柄在 __exit__ 关
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: object) -> bool:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
        return False


def _index_file_lock() -> _FileLock:
    return _FileLock(kimi_state_dir() / "index.lock")


def _atomic_write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    """0600 原子写（临时文件 + rename，与 antigravity/gemini 同款）。"""
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

def derive_account_id(cred: dict[str, Any]) -> str:
    """从 cred 派生稳定账号 id；已有合法 id 原样保留。

    user_id 首选（/v1/me 拿到后回填进 cred；同一账号重登/换设备都稳定）；
    拿不到退 ``kimi-<device_id>``（uuid 带连字符，过 _ID_RE），再退
    refresh_token 哈希——保证任何导入路径都落得了盘。
    """
    existing = str(cred.get("account_id") or "").strip()
    if existing and _ID_RE.fullmatch(existing):
        return existing
    user_id = str(cred.get("user_id") or "").strip()
    if user_id and _ID_RE.fullmatch(user_id):
        return user_id
    device_id = str(cred.get("device_id") or "").strip()
    if device_id:
        slug = re.sub(r"[^A-Za-z0-9._@-]", "-", device_id)[:48]
        candidate = f"kimi-{slug}"
        if _ID_RE.fullmatch(candidate):
            return candidate
    digest = hashlib.sha256(str(cred.get("refresh_token") or "").encode()).hexdigest()[:12]
    return f"acct-{digest}"


# ---------------------------------------------------------------------------
# 多账号 API
# ---------------------------------------------------------------------------

def list_accounts() -> list[AccountRef]:
    """枚举全部账号（按 priority 稳定排序）。

    唯一的账号枚举入口：索引自愈（cred 文件被删/损坏的条目剔除——删文件即
    退出该账号）挂在这里，login/forward/quota/UI 走同一个口。
    """
    with _index_lock, _index_file_lock():
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
                log.warning("kimi: 账号 %s 的凭据文件缺失或损坏，已从账号清单剔除", aid)
                continue
            kept.append(AccountRef(
                id=aid,
                name=str(e.get("name") or ""),
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

    匹配顺序：account_id → refresh_token（kimi 没有邮箱这类天然唯一键，
    不做 name 匹配）。命中即更新该账号（priority/added_at 不变——重新导入
    不改变 failover 顺位），全不命中追加为新账号（priority 排到最后）。
    cred 缺 account_id 时现场生成并写回原 dict（调用方拿到即可用）。
    """
    with _index_lock, _index_file_lock():
        return _save_account_cred_unlocked(cred)


def _save_account_cred_unlocked(cred: dict[str, Any]) -> AccountRef:
    entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
    aid = str(cred.get("account_id") or "")

    def _find_existing() -> dict[str, Any] | None:
        """按「账号身份」找回索引里的既有条目。

        kimi 没有天然唯一键，靠两个近似键依次找回：
        ``refresh_token``（同一次授权的凭据，最强信号）与 ``user_id``
        （/v1/me 给的稳定账号 id——重登时 RT 已滚动、device_id 也换了，只有
        它跨登录不变）。少了后一个，「重新登录」这条最常见的路径会落成新
        id → 同一账号占两个顺位：旧那个（死 RT）每轮 failover 白打一次上
        游，真正能用的还排在末尾。
        """
        by_id = next((e for e in entries if str(e.get("id") or "") == aid), None)
        if by_id is not None:
            return by_id
        rt = str(cred.get("refresh_token") or "")
        uid = str(cred.get("user_id") or "").strip()
        if not rt and not uid:
            return None
        for e in entries:
            other = load_account_cred(str(e.get("id") or "")) or {}
            if rt and other.get("refresh_token") == rt:
                return e
            if uid and str(other.get("user_id") or "").strip() == uid:
                return e
        return None

    target = _find_existing()
    if target is None and not _ID_RE.fullmatch(aid):
        # cred 没带 account_id（cli 导出 JSON 就是这种）时现场派生。派生用的是
        # device_id 等稳定字段，**可能得到与既有账号相同的 id**——导入同一份
        # 导出的最新 token（refresh_token 已滚动、匹配不上旧 RT）正好走这条：
        # _find_existing 用空 aid 查不到，派生出的 id 却和索引里那条一模一样。
        # 这里补一次按派生 id 的查找，否则同一 account_id 会被追加成第二个顺位
        # （真机踩过：面板同一个号显示 Kimi #1 和 Kimi #3）。
        cred["account_id"] = derive_account_id(cred)  # 就地写回，调用方可见
        aid = str(cred["account_id"])
        target = next((e for e in entries if str(e.get("id") or "") == aid), None)
    if target is not None:
        aid = str(target["id"])
        cred["account_id"] = aid
    else:
        if not _ID_RE.fullmatch(aid):
            cred["account_id"] = derive_account_id(cred)  # 就地写回，调用方可见
            aid = str(cred["account_id"])
        if len(entries) >= _MAX_ACCOUNTS:
            raise AuthError(
                f"kimi 账号数已达上限 {_MAX_ACCOUNTS}，"
                f"请先在管理页删除不用的账号")
        ref = AccountRef(
            id=aid,
            name=str(cred.get("nickname") or ""),
            priority=max((int(e.get("priority") or 0) for e in entries), default=-1) + 1,
            added_at=int(time.time()),
        )
        entries.append(asdict(ref))
        target = asdict(ref)
    target["name"] = str(cred.get("nickname") or target.get("name") or "")
    # alias 不在 upsert 覆写之列：它是用户手起的本地别名，重登不改
    ref = AccountRef(id=aid, name=target["name"],
                     priority=int(target.get("priority") or 0),
                     added_at=int(target.get("added_at") or 0),
                     alias=str(target.get("alias") or ""))
    _atomic_write_json(account_cred_path(ref.id), dict(cred))
    _atomic_write_json(index_path(), _index_payload(entries))
    return ref


def delete_account(account_id: str) -> bool:
    """移除一个账号（索引条目 + cred 文件）；返回是否真的删了。"""
    with _index_lock, _index_file_lock():
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

    入参必须是**完整**的当前账号 id 列表（少一个/多一个/重复即拒绝）；
    priority 重写为 0..n-1，added_at 原样保留。返回重排后的 list_accounts()。
    """
    list_accounts()  # 先走唯一的枚举入口：自愈完再重排，别在残缺索引上动刀
    with _index_lock, _index_file_lock():
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
    list_accounts()  # 先走唯一的枚举入口：自愈完再改，别在残缺索引上动刀
    with _index_lock, _index_file_lock():
        entries = [e for e in (_read_index().get("accounts") or []) if isinstance(e, dict)]
        target = next((e for e in entries if str(e.get("id") or "") == aid), None)
        if target is None:
            raise ValueError(f"账号 {aid} 不存在")
        target["alias"] = text
        _atomic_write_json(index_path(), _index_payload(entries))
    return list_accounts()


def has_cred() -> bool:
    return bool(list_accounts())


# ---------------------------------------------------------------------------
# token 刷新与获取（转发主路径）
# ---------------------------------------------------------------------------

def _expiry_iso(expires_in: Any) -> str:
    """payload ``expires_in`` → ISO UTC 绝对时刻（写进 cred 的 ``expired``）。"""
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        seconds = 900.0  # 实测 access token 寿命 15 分钟
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def access_token_valid(cred: dict[str, Any]) -> bool:
    """access_token 是否还在有效期内（含 2 分钟提前量）。"""
    exp = cred_expired_epoch(cred)
    if exp <= 0:
        return False
    return time.time() < exp - _EXPIRY_SKEW.total_seconds()


def refresh_account_cred(cred: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    """用 refresh_token 换新 access_token，更新并写回该账号的文件。

    刷新永远打 cred 自己的 ``oauth_host``（多账号混用 cn/global 域不串）。
    返回更新后的 cred（原 dict 就地更新）。写回走 :func:`save_account_cred`
    的幂等 upsert——同 account_id/refresh_token 必然命中原账号，priority
    不变。kimi 是否轮换 refresh_token 未确证：payload 带了才更新，没带
    保留旧值。刷新失败抛 :class:`AuthError`。
    """
    oauth_host = str(cred.get("oauth_host") or oauth.DEFAULT_OAUTH_HOST)
    try:
        payload = oauth.refresh_token(
            oauth_host, str(cred["refresh_token"]),
            device_id=str(cred.get("device_id") or ""), timeout=timeout)
    except oauth.OAuthError as exc:
        raise AuthError(str(exc)) from exc
    # 刷新是网络往返（最长 timeout 秒），期间用户可能在面板删了这个号或
    # 导入了新凭据。先重读盘：账号已被删就别把它"复活"（save_account_cred
    # 会当成新账号追加），发现 RT 已被换掉（刚导入的新凭据）就整份放弃回写，
    # 免得拿旧快照把 base_url/oauth_host/device_id 全退回旧值。
    with _index_lock, _index_file_lock():
        current = load_account_cred(str(cred.get("account_id") or ""))
        if current is None or current.get("refresh_token") != cred.get("refresh_token"):
            log.debug("kimi: 刷新期间账号已被删除或凭据已变更，放弃回写: %s",
                      cred.get("account_id"))
            cred["access_token"] = payload["access_token"]
            cred["expired"] = _expiry_iso(payload.get("expires_in"))
            cred["last_refresh"] = int(time.time())
            return cred
    cred["access_token"] = payload["access_token"]
    cred["expired"] = _expiry_iso(payload.get("expires_in"))
    cred["last_refresh"] = int(time.time())
    cred["token_type"] = payload.get("token_type") or "Bearer"
    if payload.get("scope"):
        cred["scope"] = payload["scope"]
    if payload.get("refresh_token"):
        cred["refresh_token"] = payload["refresh_token"]
    save_account_cred(cred)
    return cred


_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock(account_id: str) -> threading.Lock:
    """每账号一把刷新锁：并发请求同时发现 token 过期时只有一个去刷新。"""
    with _refresh_locks_guard:
        return _refresh_locks.setdefault(account_id, threading.Lock())


def ensure_account_token(account_id: str, *, force_refresh: bool = False) -> tuple[str, dict[str, Any]]:
    """拿该账号可用的 access_token，返回 ``(token, cred 快照)``。

    cred 与 token 同源是本函数存在的理由：转发要 base_url/device_id（账号级
    身份），多账号下「token、cred 各读一次盘」会串号。刷新在每账号锁内做
    并重读盘双检——别的线程刚刷完落盘就不再刷。
    """
    cred = load_account_cred(account_id)
    if cred is None:
        raise AuthError(f"kimi 账号 {account_id} 的凭据不存在或已损坏")
    if not force_refresh and access_token_valid(cred):
        return cred["access_token"], dict(cred)
    with _refresh_lock(account_id):
        cred = load_account_cred(account_id) or cred  # 别的线程可能刚刷完落盘
        if force_refresh or not access_token_valid(cred):
            refresh_account_cred(cred)
        return cred["access_token"], dict(cred)


def new_device_id() -> str:
    """新建设备 id（登录新账号/导入缺 device_id 时用；每账号固定不再变）。"""
    return str(uuid.uuid4())
