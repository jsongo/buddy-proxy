"""kimi 多账号凭据存储测试（KIMI_STATE_DIR 由 conftest autouse 隔离到 tmp_path）。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from buddy_proxy.kimi import credentials, oauth
from buddy_proxy.kimi.credentials import (
    AuthError,
    account_cred_path,
    delete_account,
    derive_account_id,
    ensure_account_token,
    index_path,
    list_accounts,
    load_account_cred,
    refresh_account_cred,
    reorder_accounts,
    save_account_cred,
)


def _cred(acct_id: str, **over):
    cred = {
        "account_id": acct_id,
        "type": "kimi",
        "access_token": f"at-{acct_id}",
        "refresh_token": f"rt-{acct_id}",
        "expired": "2099-01-01T00:00:00Z",
        "base_url": "https://api.kimi.com/coding",
        "oauth_host": "https://auth.kimi.com",
        "device_id": f"dev-{acct_id}",
    }
    cred.update(over)
    return cred


# ---------------------------------------------------------------------------
# 存取往返
# ---------------------------------------------------------------------------

def test_roundtrip_and_permissions():
    ref = save_account_cred(_cred("u1"))
    assert ref.id == "u1" and ref.priority == 0
    path = account_cred_path("u1")
    assert path.exists()
    assert path.stat().st_mode & 0o777 == 0o600  # 凭据文件必须 0600
    loaded = load_account_cred("u1")
    assert loaded["refresh_token"] == "rt-u1"
    assert loaded["base_url"] == "https://api.kimi.com/coding"


def test_load_missing_or_corrupt_returns_none():
    assert load_account_cred("ghost") is None
    save_account_cred(_cred("u1"))
    account_cred_path("u1").write_text("{broken", encoding="utf-8")
    assert load_account_cred("u1") is None
    # 缺 refresh_token 也算损坏（没有它账号就救不活）
    account_cred_path("u1").write_text(json.dumps({"access_token": "x"}), encoding="utf-8")
    assert load_account_cred("u1") is None


# ---------------------------------------------------------------------------
# account_id 派生（四级优先）
# ---------------------------------------------------------------------------

def test_derive_account_id_priority():
    # 1. 已有合法 id 原样保留
    assert derive_account_id(_cred("mine")) == "mine"
    # 2. user_id（同一账号重登/换设备仍稳定）
    assert derive_account_id({"user_id": "u-9", "refresh_token": "rt"}) == "u-9"
    # 3. kimi-<device_id slug>
    got = derive_account_id({"device_id": "8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1",
                             "refresh_token": "rt"})
    assert got == "kimi-8f0b9a52-6a83-4b39-9d0a-47fbb0f34ea1"
    # 4. refresh_token 哈希兜底（任何导入路径都落得了盘）
    got = derive_account_id({"refresh_token": "secret"})
    assert got.startswith("acct-") and len(got) == len("acct-") + 12


# ---------------------------------------------------------------------------
# upsert / 顺位 / 上限 / 自愈
# ---------------------------------------------------------------------------

def test_upsert_keeps_priority_and_added_at():
    ref1 = save_account_cred(_cred("u1", nickname="旧名"))
    save_account_cred(_cred("u2"))
    time.sleep(0.01)
    # 同 account_id 重存（重新导入）：priority/added_at 不变，仅 name 更新
    ref2 = save_account_cred(_cred("u1", nickname="新名"))
    assert ref2.id == ref1.id
    assert ref2.priority == ref1.priority
    assert ref2.added_at == ref1.added_at
    assert list_accounts()[0].name == "新名"
    # 同 refresh_token、不同 account_id：命中原账号（kimi 没有天然唯一键）
    stranger = _cred("someone-else", refresh_token="rt-u1", nickname="rt 匹配")
    ref3 = save_account_cred(stranger)
    assert ref3.id == "u1"
    assert not account_cred_path("someone-else").exists()  # 文件名跟索引 id 走
    assert stranger["account_id"] == "u1"  # cred 里的 id 被写回对齐


def test_new_account_appends_with_next_priority():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    save_account_cred(_cred("u3"))
    refs = list_accounts()
    assert [(r.id, r.priority) for r in refs] == [("u1", 0), ("u2", 1), ("u3", 2)]


def test_account_cap():
    for i in range(8):
        save_account_cred({"user_id": f"user{i}", "refresh_token": f"rt-{i}"})
    with pytest.raises(AuthError, match="上限"):
        save_account_cred({"user_id": "user8", "refresh_token": "rt-8"})


def test_index_self_heals_when_cred_file_deleted():
    save_account_cred(_cred("u1"))
    save_account_cred(_cred("u2"))
    account_cred_path("u1").unlink()  # 直接删文件 = 退出该账号
    refs = list_accounts()
    assert [r.id for r in refs] == ["u2"]
    index = json.loads(index_path().read_text(encoding="utf-8"))
    assert [e["id"] for e in index["accounts"]] == ["u2"]  # 索引已重写


def test_delete_account():
    save_account_cred(_cred("u1"))
    assert delete_account("u1") is True
    assert list_accounts() == []
    assert delete_account("u1") is False  # 幂等


def test_reorder_requires_full_id_list():
    for aid in ("u1", "u2", "u3"):
        save_account_cred(_cred(aid))
    refs = reorder_accounts(["u3", "u1", "u2"])
    assert [(r.id, r.priority) for r in refs] == [("u3", 0), ("u1", 1), ("u2", 2)]
    # 少一个 / 多一个 / 重复：全量校验拒绝
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1", "u2"])
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1", "u2", "u3", "u4"])
    with pytest.raises(ValueError, match="完整"):
        reorder_accounts(["u1", "u1", "u3"])


# ---------------------------------------------------------------------------
# 刷新（oauth.refresh_token 打桩，不触网）
# ---------------------------------------------------------------------------

def test_refresh_keeps_refresh_token_when_payload_lacks_it(monkeypatch):
    """kimi 是否轮换 refresh_token 未确证：payload 没带就保留旧值。"""
    save_account_cred(_cred("u1", refresh_token="rt-old"))

    def fake_refresh(oauth_host, refresh_token, *, device_id="", timeout=30.0):
        assert oauth_host == "https://auth.kimi.com"
        assert refresh_token == "rt-old"
        return {"access_token": "at-new", "expires_in": 900,
                "token_type": "Bearer", "scope": "kimi-code"}

    monkeypatch.setattr(oauth, "refresh_token", fake_refresh)
    got = refresh_account_cred(load_account_cred("u1"))
    assert got["access_token"] == "at-new"
    assert got["refresh_token"] == "rt-old"
    assert load_account_cred("u1")["access_token"] == "at-new"  # 回写落盘


def test_refresh_rolls_refresh_token_when_payload_has_one(monkeypatch):
    save_account_cred(_cred("u1"))
    monkeypatch.setattr(oauth, "refresh_token", lambda *a, **k: {
        "access_token": "at-new", "refresh_token": "rt-new", "expires_in": 900})
    got = refresh_account_cred(load_account_cred("u1"))
    assert got["refresh_token"] == "rt-new"
    assert load_account_cred("u1")["refresh_token"] == "rt-new"


def test_refresh_uses_account_own_oauth_host(monkeypatch):
    """多账号混用 cn/global 域：刷新永远打 cred 自己的 host。"""
    save_account_cred(_cred("u1", oauth_host="https://auth.kimi.ai"))
    seen = {}

    def fake_refresh(oauth_host, *a, **k):
        seen["host"] = oauth_host
        return {"access_token": "at", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake_refresh)
    refresh_account_cred(load_account_cred("u1"))
    assert seen["host"] == "https://auth.kimi.ai"


# ---------------------------------------------------------------------------
# ensure_account_token（转发主路径）
# ---------------------------------------------------------------------------

def test_ensure_token_skips_refresh_when_valid(monkeypatch):
    save_account_cred(_cred("u1"))
    calls: list[int] = []

    def fake(*a, **k):
        calls.append(1)
        return {"access_token": "x", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake)
    token, cred = ensure_account_token("u1")
    assert token == "at-u1"
    assert cred["base_url"] == "https://api.kimi.com/coding"  # token 与 cred 同源
    assert calls == []


def test_ensure_token_refreshes_when_expired(monkeypatch):
    save_account_cred(_cred("u1"))
    stale = load_account_cred("u1")
    stale["expired"] = "2020-01-01T00:00:00Z"
    save_account_cred(stale)
    calls: list[int] = []

    def fake(*a, **k):
        calls.append(1)
        return {"access_token": "at-fresh", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake)
    token, _ = ensure_account_token("u1")
    assert token == "at-fresh"
    token, _ = ensure_account_token("u1")  # 刚刷完落盘：双检命中，不再刷
    assert token == "at-fresh"
    assert len(calls) == 1


def test_ensure_token_concurrent_single_refresh(monkeypatch):
    """并发同时发现过期：每账号锁 + 锁内重读盘，refresh 只发一次。"""
    save_account_cred(_cred("u1"))
    stale = load_account_cred("u1")
    stale["expired"] = "2020-01-01T00:00:00Z"
    save_account_cred(stale)
    calls: list[int] = []
    guard = threading.Lock()

    def fake(*a, **k):
        with guard:
            calls.append(1)
        return {"access_token": "at-fresh", "expires_in": 900}

    monkeypatch.setattr(oauth, "refresh_token", fake)
    tokens: list[str] = []
    errors: list[Exception] = []

    def worker():
        try:
            tokens.append(ensure_account_token("u1")[0])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(calls) == 1
    assert tokens == ["at-fresh"] * 4


def test_force_refresh_bypasses_validity(monkeypatch):
    save_account_cred(_cred("u1"))
    monkeypatch.setattr(
        oauth, "refresh_token", lambda *a, **k: {"access_token": "at-forced", "expires_in": 900})
    token, _ = ensure_account_token("u1", force_refresh=True)
    assert token == "at-forced"


def test_access_token_valid_skew():
    # 提前 2 分钟就算过期（15 分钟寿命 + 刷新开销）
    soon = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    assert credentials.access_token_valid({"expired": soon}) is False
    comfy = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat()
    assert credentials.access_token_valid({"expired": comfy}) is True
    assert credentials.access_token_valid({}) is False
