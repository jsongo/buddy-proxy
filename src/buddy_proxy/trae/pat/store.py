"""PAT 账号状态存储：v2 原子 JSON（~/.buddy-proxy/trae_pat_token.json）。

缓存读写在 ``_cache_lock`` 下进行，写入走「临时文件 + fsync + rename」原子替换；
每个账号一把刷新锁（``_refresh_lock``），避免并发交换惊群。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import tempfile
import threading
import uuid
from typing import Any

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _token_file, ensure_pat_config

log = logging.getLogger(__name__)

# ───────────────────────── 原子账号缓存与刷新锁 ─────────────────────────

_cache_lock = threading.RLock()
_refresh_locks_guard = threading.Lock()
_refresh_locks: dict[str, threading.Lock] = {}


def _refresh_lock(account_id: str) -> threading.Lock:
    with _refresh_locks_guard:
        return _ns._refresh_locks.setdefault(account_id, threading.Lock())


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
