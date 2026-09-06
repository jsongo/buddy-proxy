"""CodeBuddy 计费流水接口（get-user-request-usage）离线测试。

覆盖 usage_records 的请求参数构造、响应归一化、错误透传。
接口本身 2026-09-06 已用真实 Bearer token 实测 200（copilot.tencent.com 与
www.workbuddy.cn 双 host），本测试不访问远程。

运行：
    .venv/bin/python -m pytest test_workbuddy_usage_records.py -v
"""
from __future__ import annotations

import sys
import pathlib
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).parent / "src"))

from buddy_proxy import codebuddy_provider as cbp  # noqa: E402

FIXTURE = {
    "code": 0, "msg": "OK",
    "data": {
        "total": 2,
        "data": [
            {"requestId": "crb-aaa", "credit": 0.11, "model": "glm-5.3-flash",
             "client": "Visual Studio Code", "requestTime": "2026-09-06 15:32:00",
             "inputTrunc": "hello truncated", "input": "hello full",
             "agentPurpose": ""},
            {"requestId": "crb-bbb", "credit": 0, "model": "glm-5.3",
             "client": "TraeCode", "requestTime": "2026-09-06 15:31:00",
             "input": "x" * 500, "agentPurpose": ""},
        ],
    },
}


def _patch_state(monkeypatch, api_post_return):
    client = mock.MagicMock()
    client.api_post.return_value = api_post_return
    monkeypatch.setattr(cbp, "get_state", lambda: SimpleNamespace(
        client=client, ensure_auth=lambda: None))
    return client


def test_usage_records_params_and_normalization(monkeypatch):
    client = _patch_state(monkeypatch, FIXTURE)
    p = cbp.CodeBuddyProvider()

    out = p.usage_records(start="2026-08-30 00:00:00",
                          end="2026-09-06 23:59:59", page_num=2, page_size=10)

    path, body = client.api_post.call_args[0]
    assert path == "/billing/meter/get-user-request-usage"
    assert body == {"startTime": "2026-08-30 00:00:00",
                    "endTime": "2026-09-06 23:59:59",
                    "pageNum": 2, "pageSize": 10}

    assert out["total"] == 2
    r0 = out["records"][0]
    assert r0["request_id"] == "crb-aaa"
    assert r0["credit"] == 0.11
    assert r0["model"] == "glm-5.3-flash"
    assert r0["request_time"] == "2026-09-06 15:32:00"
    assert r0["input"] == "hello truncated"  # 优先用截断版
    assert len(out["records"][1]["input"]) == 200  # 长文按 200 字截断


def test_usage_records_default_time_range(monkeypatch):
    client = _patch_state(monkeypatch, FIXTURE)
    p = cbp.CodeBuddyProvider()

    p.usage_records()

    _, body = client.api_post.call_args[0]
    assert body["pageNum"] == 1 and body["pageSize"] == 20
    # 缺省时间窗：最近 7 天，格式 "YYYY-MM-DD HH:MM:SS"
    for key in ("startTime", "endTime"):
        assert len(body[key]) == 19 and body[key][4] == "-" and body[key][10] == " "


def test_usage_records_error_passthrough(monkeypatch):
    _patch_state(monkeypatch, {"code": 401, "msg": "unauthorized"})
    p = cbp.CodeBuddyProvider()

    import pytest
    with pytest.raises(RuntimeError, match="unauthorized"):
        p.usage_records()
