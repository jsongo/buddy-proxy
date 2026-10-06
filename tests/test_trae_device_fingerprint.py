"""trae 签到设备指纹：per-account 稳定指纹 + 9074 换机重试 + 9095 幂等。

2026-10-06 实测：trae2api-cn 原版写死的「ASUS TUF + windows」指纹已被上游
风控拉黑（新 device 一律 9074「当前参与用户太多」，换 brand/type 立即成功；
9074 不是活动热度——同刻 status 畅通、错误码随 device 变化）。本表锁三个行为：
指纹每账号稳定、9074 依次换机成功后固化、9095 按「设备已签」幂等成功。

conftest 已隔离 TRAE_WORK_STATE_DIR，不碰真实账号/devices.json。
"""
from __future__ import annotations

import json
import os

import pytest

from buddy_proxy.trae import benefits_api
from buddy_proxy.trae.benefits_api import (
    _DEVICE_ALT_N,
    _device_for,
    _load_device_overrides,
    _save_device_override,
    claim_checkin_credits,
)


# -- 指纹派生 ----------------------------------------------------------------


def test_device_for_stable_per_identity(monkeypatch):
    """同一 identity 每次派生同一台设备（换设备=风控画像极差）。"""
    a = _device_for("tok", "acct-1")
    b = _device_for("tok2", "acct-1")
    assert a == b
    assert len(a["device_id"]) == 16 and a["device_id"].isdigit()
    assert a["brand"] and a["type"]


def test_device_for_differs_across_accounts():
    """不同账号不同 device_id；机型来自真实机型池（不含被拉黑的原版串）。"""
    a = _device_for("t1", "acct-1")
    b = _device_for("t2", "acct-2")
    assert a["device_id"] != b["device_id"]
    pool = {f"{brand}/{dtype}" for brand, dtype in benefits_api._DEVICE_POOL}
    assert f"{a['brand']}/{a['type']}" in pool
    assert "ASUS" not in a["brand"] and "ASUS" not in b["brand"]


def test_device_for_alt_changes_both_id_and_model():
    """alt 序列换机时 device_id 与机型一起换（上游按组合画像）。"""
    seen = {_device_for("tok", "acct-1", alt=n)["device_id"] for n in (0,) + _DEVICE_ALT_N}
    assert len(seen) == 1 + len(_DEVICE_ALT_N)


def test_device_for_override_wins(monkeypatch):
    """固化表优先：迁移进来/首签成功的组合永远原样用。"""
    fixed = {"device_id": "0988991591997378", "brand": "MacBookPro18,3", "type": "macos"}
    _save_device_override("acct-1", fixed)
    assert _device_for("tok", "acct-1") == fixed
    # alt 重试不受固化影响（换机就是要离开当前这台）
    assert _device_for("tok", "acct-1", alt=1)["device_id"] != fixed["device_id"]


def test_overrides_missing_or_corrupt_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "nope.json")
    assert _load_device_overrides() == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: bad)
    assert _load_device_overrides() == {}


def test_override_file_is_0600(monkeypatch, tmp_path):
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "devices.json")
    _save_device_override("acct-1", {"device_id": "1" * 16, "brand": "b", "type": "t"})
    p = tmp_path / "devices.json"
    assert (os.stat(p).st_mode & 0o777) == 0o600
    assert json.loads(p.read_text(encoding="utf-8"))["accounts"]["acct-1"]["brand"] == "b"


# -- claim：9074 换机 / 9095 幂等 ---------------------------------------------


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(benefits_api.time, "sleep", lambda s: None)


def test_claim_rotates_device_on_9074_and_saves_override(monkeypatch, tmp_path):
    """原指纹 9074 → 换备选机 → 成功后固化该组合，下次直接用同一台。"""
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "devices.json")
    seen: list[dict] = []

    calls = {"n": 0}

    def _post(path, token="", account_id="", region=None, device=None):
        seen.append(device)
        calls["n"] += 1
        if calls["n"] == 1:  # 仅首次 claim 的首台拒，换机即过；之后一直 success
            return {"code": 9074, "message": "当前参与用户太多，请稍后再试"}
        return {"code": 0, "message": "success", "credits_granted": 100}

    monkeypatch.setattr(benefits_api, "_post_ug", _post)
    out = claim_checkin_credits(token="tok", account_id="acct-1", region="cn")
    assert out["code"] == 0 and len(seen) == 2
    # 固化的是成功那台，而不是派生主设备
    assert _load_device_overrides()["acct-1"] == seen[1]
    # 下一次 claim 直接命中固化表（只发一次请求）
    seen.clear()
    out2 = claim_checkin_credits(token="tok", account_id="acct-1", region="cn")
    assert out2["code"] == 0 and len(seen) == 1


def test_claim_exhausts_pool_marks_device_rejected(monkeypatch, tmp_path):
    """池尽仍 9074：带 device_rejected 标记返回（外层据此跳过限流退避）。"""
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(
        benefits_api, "_post_ug",
        lambda *a, **k: {"code": 9074, "message": "当前参与用户太多，请稍后再试"})
    out = claim_checkin_credits(token="tok", account_id="acct-1", region="cn")
    assert out["device_rejected"] is True and out["code"] == 9074
    assert _load_device_overrides() == {}


def test_claim_9095_maps_to_idempotent_success(monkeypatch, tmp_path):
    """9095「当前设备今日已经签到」→ code 0 + already_device_signed。"""
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(
        benefits_api, "_post_ug",
        lambda *a, **k: {"code": 9095, "message": "当前设备今日已经签到，请明日再来哦～"})
    out = claim_checkin_credits(token="tok", account_id="acct-1", region="cn")
    assert out["code"] == 0 and out["already_device_signed"] is True
    assert out["checked_in"] is True and out["extra_credits"] is None
    assert "设备今日已签" in out["message"]
    # 9095 不是成功首签，不该固化
    assert _load_device_overrides() == {}


def test_claim_other_error_passthrough(monkeypatch, tmp_path):
    """非 9074/9095 的错误（token 失效等）原样返回，外层照常分类。"""
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(
        benefits_api, "_post_ug", lambda *a, **k: {"code": 1005, "message": "无权限"})
    assert claim_checkin_credits(token="tok", account_id="acct-1", region="cn") == {
        "code": 1005, "message": "无权限"}


def _jwt(uid: str) -> str:
    import base64

    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    payload = b64(json.dumps({"data": {"id": uid}}).encode())
    return f"{b64(b'{}')}.{payload}.sig"


def test_claim_legacy_path_resolves_token_before_identity(monkeypatch, tmp_path):
    """legacy 单账号（不传 token/account_id）：先 _auth() 再提 identity——
    固化表按 uid 命中，换机循环也不再发空 device_id。"""
    monkeypatch.setattr(benefits_api, "_devices_path", lambda: tmp_path / "devices.json")
    fixed = {"device_id": "2" * 16, "brand": "Mac14,6", "type": "macos"}
    _save_device_override("legacy-uid", fixed)
    monkeypatch.setattr(benefits_api, "_auth", lambda: (_jwt("legacy-uid"), None))
    seen: list[dict] = []
    monkeypatch.setattr(
        benefits_api, "_post_ug",
        lambda path, token="", account_id="", region=None, device=None:
            (seen.append(device), {"code": 0, "message": "success"})[1])
    out = claim_checkin_credits(region="cn")
    assert out["code"] == 0 and seen == [fixed]


def test_provider_no_backoff_after_device_rejected(monkeypatch):
    """外层对 device_rejected 不再按限流退避（同样 4 台设备重试救不回黑名单）。"""
    from buddy_proxy.trae import provider as trae_provider

    # 无标记的 9074 文案仍按限流（异常路径等老行为不变）
    assert trae_provider._claim_rate_limited(
        {"message": "当前参与用户太多，请稍后再试"}) is True
    # provider 循环里的判断：device_rejected 置位时不算限流、不退避
    data = {"code": 9074, "message": "当前参与用户太多，请稍后再试",
            "device_rejected": True}
    assert not ((not data.get("device_rejected")) and trae_provider._claim_rate_limited(data))
