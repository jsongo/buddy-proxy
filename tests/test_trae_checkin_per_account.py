"""trae work 多账号签到的 per-account 明细。

聚合态（checked_in/claimable）说不清「哪个账号没签上/查询失败」——用户在管理页
只看到整体成功，个别账号挂了完全不可见。现在 checkin_status / checkin_claim
各带 ``accounts`` 列表（index/name/状态/失败原因），前端逐账号渲染。

conftest 已隔离 TRAE_WORK_STATE_DIR / TRAE_WORK_CRED_PATH，不碰真实账号。
"""
from __future__ import annotations

import time

from buddy_proxy.trae import failover, provider
from buddy_proxy.trae.credentials import save_account_cred
from buddy_proxy.trae.provider import TraeProvider


def _cred(uid: str, nickname: str = "T") -> dict:
    return {
        "uid": uid, "nickname": nickname,
        "access_token": f"at-{uid}", "refresh_token": f"rt-{uid}",
        "expires_at": int(time.time()) + 3600,
        "machine_id": "m", "device_id": "d", "api_host": "h",
        "enterprise_id": "",
    }


def _seed(*uids: str):
    return [save_account_cred(_cred(u, u.title())) for u in uids]


# -- checkin_status.accounts ---------------------------------------------------


def test_status_multi_account_reports_each(monkeypatch):
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})

    def _fetch(token="", account_id="", region=""):
        if account_id == "u2":
            return {"checked_in": False, "enable": True, "message": ""}
        return {"checked_in": True, "enable": True, "message": "success"}

    monkeypatch.setattr("buddy_proxy.trae.provider.fetch_checkin_status", _fetch)
    st = TraeProvider().checkin_status()

    assert len(st["accounts"]) == 2
    a1, a2 = st["accounts"]
    assert (a1["index"], a1["name"]) == (1, "U1")
    assert a1["checked_in"] is True and a1["claimable"] is False
    assert a2["checked_in"] is False and a2["claimable"] is True
    # 明细带 id：签到行的 ✎ 改名按钮要拿它定位账号
    assert a1["id"] == "u1" and a2["id"] == "u2"
    # 聚合态不受影响：任一可领 → 整体可领
    assert st["claimable"] is True and st["checked_in"] is False


def test_status_name_prefers_alias(monkeypatch):
    """显示名 alias 优先：✎ 改的名要和额度面板一致（全链路同一个名字）。"""
    from buddy_proxy.trae.credentials import rename_account

    _seed("u1", "u2")
    rename_account("u1", "签到主力")
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.fetch_checkin_status",
        lambda token="", account_id="": {"checked_in": True, "enable": True, "message": ""})
    st = TraeProvider().checkin_status()
    assert st["accounts"][0]["name"] == "签到主力"
    assert st["accounts"][1]["name"] == "U2"  # 没别名的照旧 nickname


def test_status_marks_failed_accounts(monkeypatch):
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})

    def _fetch(token="", account_id="", region=""):
        if account_id == "u2":
            raise RuntimeError("token 刷新失败")
        return {"checked_in": True, "enable": True, "message": "success"}

    monkeypatch.setattr("buddy_proxy.trae.provider.fetch_checkin_status", _fetch)
    st = TraeProvider().checkin_status()

    a2 = st["accounts"][1]
    # error 带具体原因（不再是干巴巴的「查询失败」），排查 token/网络有据可依
    assert "token 刷新失败" in a2["error"]
    assert "checked_in" not in a2
    assert st["checked_in"] is True  # u1 签了 → 聚合已签（无 claimable）
    assert "1/2 个账号查询失败" in st["message"]


def test_status_single_account_has_no_accounts_list(monkeypatch):
    """单账号不加明细：徽标本身就是它的状态，重复展示纯属噪音。"""
    _seed("u1")
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.fetch_checkin_status",
        lambda token="", account_id="", region="": {"checked_in": True, "enable": True})
    st = TraeProvider().checkin_status()
    assert "accounts" not in st


# -- checkin_claim.accounts -----------------------------------------------------


def test_claim_multi_account_reports_each(monkeypatch):
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})

    def _claim(token="", account_id="", region=""):
        if account_id == "u1":
            return {"code": 0, "credits_granted": 100, "message": "OK"}
        return {"code": 1, "message": "already signed"}

    monkeypatch.setattr("buddy_proxy.trae.provider.claim_checkin_credits", _claim)
    st = TraeProvider().checkin_claim()

    assert st["checked_in"] is True
    a1, a2 = st["accounts"]
    assert a1["ok"] is True and a1["credits"] == 100
    assert a2["ok"] is False and "already" in a2["message"]


def test_claim_failure_still_carries_account_detail(monkeypatch):
    """单个账号抛异常：明细里记失败原因，不阻塞其它账号（原有行为）。"""
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})

    def _claim(token="", account_id="", region=""):
        if account_id == "u2":
            raise RuntimeError("网络超时")
        return {"code": 0, "credits_granted": 100, "message": "OK"}

    monkeypatch.setattr("buddy_proxy.trae.provider.claim_checkin_credits", _claim)
    st = TraeProvider().checkin_claim()

    assert st["accounts"][0]["ok"] is True
    assert st["accounts"][1]["ok"] is False
    assert "网络超时" in st["accounts"][1]["message"]


def test_claim_single_account_has_no_accounts_list(monkeypatch):
    _seed("u1")
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr(
        "buddy_proxy.trae.provider.claim_checkin_credits",
        lambda token="", account_id="", region="": {"code": 0, "credits_granted": 100})
    st = TraeProvider().checkin_claim()
    assert "accounts" not in st


# -- 串行节流 + 限流退避（2026-10-06「当前参与用户太多」实测） --------------------


def test_claim_throttles_between_accounts(monkeypatch):
    """多账号串行节流：第 2 个账号起必须先 sleep 再打（上游短窗口频控）。"""
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})
    sleeps: list[float] = []
    monkeypatch.setattr("buddy_proxy.trae.provider.time.sleep",
                        lambda s: sleeps.append(s))
    calls: list[str] = []

    def _claim(token="", account_id="", region=""):
        calls.append(account_id)
        return {"code": 0, "credits_granted": 50, "message": "OK"}

    monkeypatch.setattr("buddy_proxy.trae.provider.claim_checkin_credits", _claim)
    st = TraeProvider().checkin_claim()

    assert calls == ["u1", "u2"], "按 failover 顺位串行打"
    assert sleeps, "多账号之间必须有节流 sleep"
    # 只有账号间节流、无限流重试：#1 前不等，#2 前等一次
    assert sleeps == [provider._CHECKIN_THROTTLE_S]
    assert st["accounts"][1]["ok"] is True


def test_claim_rate_limited_retries_then_succeeds(monkeypatch):
    """命中「参与用户太多」按退避表重试，重试成功则该账号照常计入已领。"""
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr("buddy_proxy.trae.provider.time.sleep", lambda s: None)
    attempts: list[str] = []

    def _claim(token="", account_id="", region=""):
        if account_id == "u2":
            attempts.append(account_id)
            if len(attempts) < 2:  # 首发限流，退避后成功
                return {"code": 1, "message": "当前参与用户太多，请稍后再试"}
        return {"code": 0, "credits_granted": 30, "message": "OK"}

    monkeypatch.setattr("buddy_proxy.trae.provider.claim_checkin_credits", _claim)
    st = TraeProvider().checkin_claim()

    a2 = st["accounts"][1]
    assert a2["ok"] is True and a2["credits"] == 30, "重试成功要算已领"
    assert len(attempts) == 2, "限流后正好多打一发"


def test_claim_rate_limited_exhausted_keeps_upstream_message(monkeypatch):
    """重试耗尽仍限流：ok=False、message 保留上游原话（「稍后再试」可手动补领）。"""
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr("buddy_proxy.trae.provider.time.sleep", lambda s: None)
    n = {"u2": 0}

    def _claim(token="", account_id="", region=""):
        if account_id == "u2":
            n["u2"] += 1
            return {"code": 1, "message": "当前参与用户太多，请稍后再试"}
        return {"code": 0, "credits_granted": 30, "message": "OK"}

    monkeypatch.setattr("buddy_proxy.trae.provider.claim_checkin_credits", _claim)
    st = TraeProvider().checkin_claim()

    a2 = st["accounts"][1]
    assert a2["ok"] is False
    assert "参与用户太多" in a2["message"], "失败原因保留上游文案"
    assert n["u2"] == len(provider._CHECKIN_RETRY_DELAYS) + 1, "按退避表打满重试次数"
    # #1 成功不受影响（单账号失败不阻塞其它账号的原语义）
    assert st["accounts"][0]["ok"] is True


def test_claim_device_rejected_translates_message(monkeypatch):
    """指纹池尽（device_rejected）：不再退避重试，message 翻译成真实原因——
    上游原话「当前参与用户太多」是烟幕（2026-10-06 实锤=设备指纹黑名单），
    照搬会把排查带去查限流；用户要知道的是指纹被拒、稍后自动重试。"""
    _seed("u1", "u2")
    monkeypatch.setattr(failover, "_cooldowns", {})
    monkeypatch.setattr("buddy_proxy.trae.provider.time.sleep", lambda s: None)
    n = {"u2": 0}

    def _claim(token="", account_id="", region=""):
        if account_id == "u2":
            n["u2"] += 1
            return {"code": 9074, "message": "当前参与用户太多，请稍后再试",
                    "device_rejected": True}
        return {"code": 0, "credits_granted": 30, "message": "OK"}

    monkeypatch.setattr("buddy_proxy.trae.provider.claim_checkin_credits", _claim)
    st = TraeProvider().checkin_claim()

    a2 = st["accounts"][1]
    assert a2["ok"] is False
    assert n["u2"] == 1, "池尽不再按限流退避重试（同样的 4 台设备重试也救不回）"
    assert "设备指纹" in a2["message"] and "参与用户太多" not in a2["message"], \
        "要翻译成真实原因，不能照搬上游烟幕文案"
    assert "参与用户太多" not in st["message"], "聚合 message 同样不带烟幕文案"
    assert st["accounts"][0]["ok"] is True
