"""Qoder 桌面活动日志兜底：直连缺签到时只补同 uid 的当前窗口。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from buddy_proxy.qoder import campaigns
from buddy_proxy.qoder.campaigns import (
    CLAIM_ACTION,
    Campaign,
    _desktop_campaigns,
    _merge_desktop_campaigns,
)

NOW = 1_800_000_000.0
UID = "u-global-1"


def _campaign(cid: str, action: str = CLAIM_ACTION, status: str = "CLAIMABLE", end: int = int(NOW + 600)):
    return {
        "campaignId": cid,
        "campaignKey": f"act-{cid}",
        "actionType": action,
        "claimStatus": status,
        "startAt": int(NOW - 600),
        "endAt": end,
        "benefit": {"kind": "CREDITS", "amount": 100},
    }


def _write_log(tmp_path: Path, monkeypatch, *events: dict) -> Path:
    monkeypatch.setattr(campaigns.Path, "home", classmethod(lambda cls: tmp_path))
    path = (tmp_path / "Library/Application Support/com.qoder.app.stable/logs/run/main.log")
    path.parent.mkdir(parents=True)
    lines = [
        "[2027-01-15T00:00:00Z] [INFO] [main] [Campaign] 活动状态请求返回 "
        + json.dumps(event, ensure_ascii=False)
        for event in events
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (NOW, NOW))
    return path


def _event(uid: str = UID, campaigns_list=None, status: int = 200) -> dict:
    return {
        "method": "GET",
        "statusCode": status,
        "payload": {"uid": uid, "campaigns": campaigns_list or [_campaign("daily")]},
    }


def test_desktop_fallback_empty_logs_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(campaigns.Path, "home", classmethod(lambda cls: tmp_path))
    log_dir = tmp_path / "Library/Application Support/com.qoder.app.stable/logs"
    log_dir.mkdir(parents=True)
    (log_dir / "empty-run").mkdir()

    assert _desktop_campaigns("global", UID, now=NOW) == []


def test_desktop_fallback_loads_same_uid_current_window(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, _event())
    out = _desktop_campaigns("global", UID, now=NOW)
    assert [(c.id, c.action_type, c.claim_status, c.amount) for c in out] == [
        ("daily", CLAIM_ACTION, "CLAIMABLE", 100.0)
    ]


def test_desktop_fallback_skips_other_uid_and_expired_or_bad_rows(tmp_path, monkeypatch):
    _write_log(
        tmp_path,
        monkeypatch,
        _event(uid="someone-else"),
        _event(campaigns_list=[_campaign("expired", end=int(NOW - 1))]),
    )
    assert _desktop_campaigns("global", UID, now=NOW) == []
    path = tmp_path / "Library/Application Support/com.qoder.app.stable/logs/run/main.log"
    path.write_text("[Campaign] 活动状态请求返回 {broken\n", encoding="utf-8")
    os.utime(path, (NOW, NOW))
    assert _desktop_campaigns("global", UID, now=NOW) == []


def test_desktop_fallback_skips_future_window(tmp_path, monkeypatch):
    future = _campaign("future")
    future["startAt"] = int(NOW + 1)
    future["endAt"] = int(NOW + 601)
    _write_log(tmp_path, monkeypatch, _event(campaigns_list=[future]))
    assert _desktop_campaigns("global", UID, now=NOW) == []


def test_desktop_fallback_skips_stale_log(tmp_path, monkeypatch):
    path = _write_log(tmp_path, monkeypatch, _event())
    stale = NOW - campaigns._DESKTOP_LOG_MAX_AGE_S - 1
    os.utime(path, (stale, stale))
    assert _desktop_campaigns("global", UID, now=NOW) == []


def test_merge_only_when_direct_has_no_checkin(tmp_path, monkeypatch):
    _write_log(tmp_path, monkeypatch, _event())
    view = Campaign.parse(_campaign("view", action="VIEW_DETAILS", status="CLAIMED"))
    assert view is not None
    merged = _merge_desktop_campaigns([view], "global", UID, now=NOW)
    assert [c.id for c in merged] == ["view", "daily"]

    # 上游可能把同一活动降级成 VIEW_DETAILS；桌面 CLAIM_BENEFIT 必须替换它，
    # 不能因 campaign id 去重而仍留下“有活动但不可领”的误报。
    downgraded = Campaign.parse(_campaign("daily", action="VIEW_DETAILS", status="CLAIMED"))
    assert downgraded is not None
    replaced = _merge_desktop_campaigns([downgraded], "global", UID, now=NOW)
    assert len(replaced) == 1
    assert replaced[0].id == "daily"
    assert replaced[0].action_type == CLAIM_ACTION
    assert replaced[0].claim_status == "CLAIMABLE"

    direct = Campaign.parse(_campaign("direct", status="CLAIMED"))
    assert direct is not None
    unchanged = _merge_desktop_campaigns([direct], "global", UID, now=NOW)
    assert unchanged == [direct]
