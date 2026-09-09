"""管理 UI / 指标收集 / 设置持久化 冒烟测试（完全离线，不访问远程）。

覆盖：
- /ui 页面与 /ui/api/* （overview/models/stats/settings/test）
- 默认模型设置：持久化、校验、forward_chat 自动补齐
- MetricsCollector：聚合、按模型统计、JSONL 落盘重启恢复、流式 chunk 计数

运行：
    .venv/bin/python -m pytest test_ui_admin.py -v
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy import state as st
from buddy_proxy import settings as settings_mod
from buddy_proxy.benefits import BenefitsManager, CheckinHistory
from buddy_proxy.metrics import MetricsCollector, SSEUsageExtractor, normalize_usage
from buddy_proxy.providers import BaseProvider
from buddy_proxy.zcode_provider import _quota_items


# ---------------------------------------------------------------------------
# 假 provider / 假 state
# ---------------------------------------------------------------------------
class FakeProvider(BaseProvider):
    id = "fakeprov"
    name = "Fake Provider"

    def __init__(self):
        self.last_body = None

    def models(self):
        return [{"id": "fake-model", "description": "Fake Model"}]

    def ensure_auth(self):
        pass

    async def forward(self, body, protocol, original=None):
        self.last_body = dict(body)
        return JSONResponse({
            "id": "chatcmpl-fake",
            "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })


class FakeStreamProvider(FakeProvider):
    id = "fakestream"

    def models(self):
        return [{"id": "fake-stream-model", "description": "Fake Stream Model"}]

    async def forward(self, body, protocol, original=None):
        async def gen():
            for i in range(3):
                yield f"data: {i}\n\n".encode()
        return StreamingResponse(gen(), media_type="text/event-stream")


def _make_state(providers, tmp_path):
    client = mock.MagicMock()
    client.endpoint = "https://fake.endpoint.invalid"
    client.auth_headers.return_value = {}
    client.session = {"auth": {"accessToken": "t", "expiresAt": int((time.time() + 3600) * 1000)}}
    return SimpleNamespace(
        client=client,
        providers=providers,
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        default_provider="codebuddy",
        default_model=None,
        disabled_models=set(),
        metrics=MetricsCollector(tmp_path / "metrics.jsonl"),
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "test", "system_version": "test",
                      "python_version": "test", "machine": "test"},
    )


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """本文件所有测试都隔离设置文件，绝不读写真实 ~/.buddy-proxy/settings.json。"""
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离的设置文件 + 假 state（含 codebuddy/fakeprov/fakestream 三个通道）。"""
    fake, fake_stream = FakeProvider(), FakeStreamProvider()
    state = _make_state({"fakeprov": fake, "fakestream": fake_stream}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    return SimpleNamespace(state=state, fake=fake, fake_stream=fake_stream, client=client)


# ---------------------------------------------------------------------------
# /ui 页面与只读 API
# ---------------------------------------------------------------------------
def test_ui_page_served(env):
    r = env.client.get("/ui")
    assert r.status_code == 200
    assert "Buddy Proxy 控制台" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_root_redirects_to_ui(env):
    assert env.client.get("/", follow_redirects=False).headers["location"] == "/ui"


def test_overview_shape(env):
    body = env.client.get("/ui/api/overview").json()
    assert body["uptime_seconds"] >= 0
    assert body["default_provider"] == "codebuddy"
    assert set(body["providers"]) == {"fakeprov", "fakestream"}


def test_models_grouped_by_provider(env):
    body = env.client.get("/ui/api/models").json()
    by_id = {g["id"]: g for g in body["groups"]}
    assert set(by_id) == {"codebuddy", "fakeprov", "fakestream"}
    assert any(m["id"] == "glm-5.3" for m in by_id["codebuddy"]["models"])
    assert any(m["id"] == "fake-model" for m in by_id["fakeprov"]["models"])


def test_stats_empty(env):
    body = env.client.get("/ui/api/stats").json()
    assert body["models"] == []
    assert len(body["daily"]) == 14


# ---------------------------------------------------------------------------
# 设置：默认模型
# ---------------------------------------------------------------------------
def test_settings_set_and_clear_default_model(env):
    r = env.client.post("/ui/api/settings",
                        json={"default_model": "fakeprov/fake-model"})
    assert r.status_code == 200
    assert env.state.default_model == "fakeprov/fake-model"

    # 落盘 + 重启恢复
    saved = settings_mod.load_settings()
    assert saved["default_model"] == "fakeprov/fake-model"

    r = env.client.post("/ui/api/settings", json={"default_model": ""})
    assert r.status_code == 200
    assert env.state.default_model is None


def test_settings_rejects_unknown_model(env):
    r = env.client.post("/ui/api/settings",
                        json={"default_model": "fakeprov/nope"})
    assert r.status_code == 400
    r = env.client.post("/ui/api/settings",
                        json={"default_model": "nosuch/x"})
    assert r.status_code == 400


def test_default_model_fills_missing_model_field(env):
    env.client.post("/ui/api/settings", json={"default_model": "fakeprov/fake-model"})
    r = env.client.post("/v1/chat/completions",
                        json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    # 前缀被剥掉后转发给 provider
    assert env.fake.last_body["model"] == "fake-model"


# ---------------------------------------------------------------------------
# 模型停用/启用
# ---------------------------------------------------------------------------
def test_model_toggle_disable_and_enable(env):
    # 停用
    r = env.client.post("/ui/api/model-toggle",
                        json={"provider": "fakeprov", "model": "fake-model", "disabled": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model": "fakeprov/fake-model", "disabled": True}
    assert "fakeprov/fake-model" in env.state.disabled_models
    # 落盘
    assert settings_mod.load_settings()["disabled_models"] == ["fakeprov/fake-model"]
    # /ui/api/models 反映停用标记
    models = env.client.get("/ui/api/models").json()["groups"]
    fp = next(g for g in models if g["id"] == "fakeprov")
    assert next(m for m in fp["models"] if m["id"] == "fake-model")["disabled"] is True

    # 启用
    r = env.client.post("/ui/api/model-toggle",
                        json={"provider": "fakeprov", "model": "fake-model", "disabled": False})
    assert r.status_code == 200 and r.json()["disabled"] is False
    assert "fakeprov/fake-model" not in env.state.disabled_models


def test_model_toggle_defaults_to_flip(env):
    r1 = env.client.post("/ui/api/model-toggle",
                         json={"provider": "fakeprov", "model": "fake-model"})
    assert r1.json()["disabled"] is True
    r2 = env.client.post("/ui/api/model-toggle",
                         json={"provider": "fakeprov", "model": "fake-model"})
    assert r2.json()["disabled"] is False


def test_model_toggle_rejects_unknown(env):
    r = env.client.post("/ui/api/model-toggle",
                        json={"provider": "fakeprov", "model": "nope", "disabled": True})
    assert r.status_code == 400


def test_disabled_model_call_fails(env):
    env.client.post("/ui/api/model-toggle",
                    json={"provider": "fakeprov", "model": "fake-model", "disabled": True})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    assert "停用" in json.dumps(r.json(), ensure_ascii=False)
    # 启用后恢复正常
    env.client.post("/ui/api/model-toggle",
                    json={"provider": "fakeprov", "model": "fake-model", "disabled": False})
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakeprov/fake-model",
                              "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 一键测试
# ---------------------------------------------------------------------------
def test_ui_test_ok(env):
    body = env.client.post("/ui/api/test",
                           json={"provider": "fakeprov", "model": "fake-model"}).json()
    assert body["ok"] is True
    assert body["content"] == "pong"
    assert body["usage"]["prompt_tokens"] == 3
    assert env.fake.last_body["model"] == "fake-model"


def test_ui_test_provider_error(env):
    class ErrProvider(FakeProvider):
        id = "fakeerr"

        async def forward(self, body, protocol, original=None):
            raise HTTPException(status_code=401, detail={"error": {"message": "token 过期"}})

    env.state.providers["fakeerr"] = ErrProvider()
    body = env.client.post("/ui/api/test",
                           json={"provider": "fakeerr", "model": "fake-model"}).json()
    assert body["ok"] is False
    assert body["status"] == 401
    assert "token 过期" in body["error"]


# ---------------------------------------------------------------------------
# 指标收集
# ---------------------------------------------------------------------------
def test_metrics_recorded_on_nonstream(env):
    env.client.post("/ui/api/test", json={"provider": "fakeprov", "model": "fake-model"})
    snap = env.state.metrics.snapshot()
    top = snap["models"][0]
    assert (top["provider"], top["model"]) == ("fakeprov", "fake-model")
    assert top["count"] == 1
    assert top["prompt_tokens"] == 3 and top["completion_tokens"] == 2
    assert snap["summary"]["total_24h"] == 1


def test_metrics_recorded_on_stream(env):
    r = env.client.post("/v1/chat/completions",
                        json={"model": "fakestream/fake-stream-model",
                              "messages": [{"role": "user", "content": "hi"}],
                              "stream": True})
    assert "text/event-stream" in r.headers["content-type"]
    snap = env.state.metrics.snapshot()
    top = snap["models"][0]
    assert (top["provider"], top["model"]) == ("fakestream", "fake-stream-model")
    # 最近请求里能看到 chunk 计数
    recent = snap["recent"][0]
    assert recent["stream"] is True and recent["chunk_count"] == 3


def test_metrics_error_recorded(env):
    env.client.post("/ui/api/test", json={"provider": "fakeerr", "model": "x"}) \
        if "fakeerr" in env.state.providers else None
    # 未启用 provider 的测试 → forward_chat 路由兜底到 codebuddy 会失败，这里只验证
    # MetricsCollector 自身的错误聚合
    env.state.metrics.record(provider="fakeprov", model="fake-model",
                             status=500, error="boom", duration_ms=5)
    snap = env.state.metrics.snapshot()
    m = next(x for x in snap["models"] if x["model"] == "fake-model")
    assert m["errors"] >= 1


def test_metrics_persist_and_reload(tmp_path):
    path = tmp_path / "metrics.jsonl"
    m1 = MetricsCollector(path)
    m1.record(provider="zcode", model="glm-5.3", protocol="openai",
              status=200, duration_ms=120, prompt_tokens=7, completion_tokens=9,
              ttft_ms=45, cached_tokens=3, credit=0.5)
    m1.record(provider="trae", model="glm-5.2", protocol="openai",
              status=429, duration_ms=30, error="rate limited")
    # 模拟重启：重新加载同一个文件
    m2 = MetricsCollector(path)
    snap = m2.snapshot()
    by_model = {(x["provider"], x["model"]): x for x in snap["models"]}
    assert by_model[("zcode", "glm-5.3")]["count"] == 1
    assert by_model[("zcode", "glm-5.3")]["completion_tokens"] == 9
    assert by_model[("trae", "glm-5.2")]["errors"] == 1
    # TTFT / 缓存 / 积分 在最近请求明细里
    rec = next(r for r in snap["recent"] if r["model"] == "glm-5.3")
    assert rec["ttft_ms"] == 45 and rec["cached_tokens"] == 3 and rec["credit"] == 0.5


def test_metrics_record_account_and_default_empty(env):
    # traepat 多账号：record 带 account，最近请求里可见；其它通道缺省为空串
    env.state.metrics.record(provider="traepat", model="gpt-6", protocol="openai",
                             status=200, duration_ms=10, account="primary")
    env.state.metrics.record(provider="codebuddy", model="glm-5.3",
                             status=200, duration_ms=10)
    recent = env.state.metrics.snapshot()["recent"]
    by_provider = {r["provider"]: r for r in recent}
    assert by_provider["traepat"]["account"] == "primary"
    assert by_provider["codebuddy"]["account"] == ""


# ---------------------------------------------------------------------------
# 流式 usage 提取（SSE）
# ---------------------------------------------------------------------------
def test_sse_usage_extractor_openai():
    """CodeBuddy/OpenAI 形态：usage 在末尾 chunk 一次性给。"""
    ex = SSEUsageExtractor()
    ex.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    ex.feed(b'data: {"choices":[],"usage":{"prompt_tokens":18,"completion_tokens":2,'
            b'"prompt_tokens_details":{"cached_tokens":9},"credit":0.35}}\n\n')
    ex.feed(b"data: [DONE]\n\n")
    assert ex.usage == {"prompt_tokens": 18, "completion_tokens": 2,
                        "cached_tokens": 9, "credit": 0.35}


def test_sse_usage_extractor_anthropic_and_split_lines():
    """Anthropic 形态（input/output 分事件）+ usage 行跨 chunk 断开。

    口径统一：Anthropic 的 input_tokens 不含缓存，提取层要加总成
    OpenAI 口径（prompt = input + cache_read + cache_creation），
    zcode 直通流才能与 codebuddy/trae 横向对比。
    """
    ex = SSEUsageExtractor()
    ex.feed(b'event: message_start\ndata: {"message":{"usage":{"input_tokens":120,'
            b'"cache_read_input_tokens":30}}}\n\n')
    # 故意把 usage 行从中间劈开
    ex.feed(b'event: message_delta\ndata: {"usage":{"output_tok')
    ex.feed(b'ens": 55}}\n\n')
    assert ex.usage["prompt_tokens"] == 150
    assert ex.usage["completion_tokens"] == 55
    assert ex.usage["cached_tokens"] == 30


def test_sse_usage_extractor_str_chunks():
    """Trae 流产出 str chunk（codebuddy 是 bytes）——两者都必须能喂。"""
    ex = SSEUsageExtractor()
    ex.feed('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    ex.feed('data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,'
            '"total_tokens":10}}\n\n')
    ex.feed("data: [DONE]\n\n")
    assert ex.usage["prompt_tokens"] == 7 and ex.usage["completion_tokens"] == 3


def test_metrics_record_ttft_and_credit_in_recent(env):
    env.state.metrics.record(provider="fakeprov", model="fake-model",
                             stream=True, status=200, duration_ms=900,
                             ttft_ms=180, prompt_tokens=50, completion_tokens=20,
                             cached_tokens=10, credit=1.5)
    snap = env.state.metrics.snapshot()
    recent = snap["recent"][0]
    assert recent["ttft_ms"] == 180
    assert recent["cached_tokens"] == 10
    assert recent["credit"] == 1.5


def test_normalize_usage_shapes():
    n = normalize_usage({"prompt_tokens": 10, "completion_tokens": 5,
                         "prompt_tokens_details": {"cached_tokens": 4}, "credit": 0.2})
    assert n == {"prompt_tokens": 10, "completion_tokens": 5,
                 "cached_tokens": 4, "credit": 0.2}
    a = normalize_usage({"input_tokens": 8, "output_tokens": 3,
                         "cache_read_input_tokens": 2})
    # Anthropic 形态统一为 OpenAI 口径：prompt = input + cache_read
    assert a == {"prompt_tokens": 10, "completion_tokens": 3,
                 "cached_tokens": 2, "credit": None}
    # Anthropic 形态含缓存创建（5m/1h 写入）也要计入全量输入
    aw = normalize_usage({"input_tokens": 8, "output_tokens": 3,
                          "cache_creation_input_tokens": 5,
                          "cache_read_input_tokens": 2})
    assert aw == {"prompt_tokens": 15, "completion_tokens": 3,
                  "cached_tokens": 2, "credit": None}
    # Responses API 形态：input_tokens_details.cached_tokens
    r = normalize_usage({"input_tokens": 9, "output_tokens": 1,
                         "input_tokens_details": {"cached_tokens": 7}, "credit": 0.3})
    assert r["cached_tokens"] == 7 and r["credit"] == 0.3


def test_responses_converter_carries_credit_and_cache():
    """Responses 流式转换完成事件要带真实 cached_tokens 与 credit。"""
    from buddy_proxy.responses_adapter import ResponsesStreamConverter

    conv = ResponsesStreamConverter(model="glm-5.3-flash")
    for chunk in (
        {"choices": [{"delta": {"content": "hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34,
                   "prompt_tokens_details": {"cached_tokens": 28}, "credit": 0.9}},
    ):
        conv.feed_chunk(chunk)
    events = conv.finish()
    completed = [d for n, d in events if n == "response.completed"][0]
    usage = completed["response"]["usage"]
    assert usage["input_tokens"] == 30
    assert usage["input_tokens_details"]["cached_tokens"] == 28
    assert usage["credit"] == 0.9

    # 指标层能从转出的 responses 事件流里提取到同一组数据
    ex = SSEUsageExtractor()
    for name, data in events:
        ex.feed(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
    assert ex.usage["credit"] == 0.9
    assert ex.usage["cached_tokens"] == 28


def test_trae_models_carry_credits():
    """Trae 模型应带积分倍率（知识库《模型及成本整理》2026-09-05 版）。"""
    from buddy_proxy.trae_provider import TraeProvider
    models = {m["id"]: m for m in TraeProvider().models()}
    assert models["glm-5.3"]["credits"] == "x0.40"
    assert models["glm-5.3-flash"]["credits"] == "x0.06"
    assert models["DeepSeek-V4-Flash"]["credits"] == "x0.08"
    assert models["kimi-k3"]["credits"] == "x1.83"
    # 别名跟随内部模型的倍率
    assert models["deepseek-v4-flash"]["credits"] == "x0.08"
    # 官方价目已下架的模型不硬造倍率
    assert models["glm-5"]["credits"] is None


def test_metrics_daily_series_zero_filled(tmp_path):
    m = MetricsCollector(tmp_path / "metrics.jsonl")
    m.record(provider="zcode", model="glm-5.3", status=200, duration_ms=10)
    snap = m.snapshot(days=14)
    assert len(snap["daily"]) == 14
    assert snap["daily"][-1]["total"] == 1
    assert snap["daily"][0]["total"] == 0
    assert snap["daily"][-1]["by_provider"] == {"zcode": 1}


# ---------------------------------------------------------------------------
# settings 模块
# ---------------------------------------------------------------------------
def test_settings_roundtrip_and_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "s.json"))
    assert settings_mod.load_settings() == {}
    settings_mod.save_settings({"default_model": "workbuddy/glm-5.3"})
    # workbuddy 别名归一为 codebuddy
    assert settings_mod.normalize_default_model("workbuddy/glm-5.3") == "codebuddy/glm-5.3"
    assert settings_mod.load_settings()["default_model"] == "workbuddy/glm-5.3"


# ---------------------------------------------------------------------------
# 打卡 / 额度
# ---------------------------------------------------------------------------
class FakeCheckinProvider(FakeProvider):
    """支持打卡与额度的假 provider。"""
    id = "fakecheckin"
    name = "Fake Checkin"
    supports_checkin = True

    def __init__(self):
        super().__init__()
        self.claim_calls = 0

    def checkin_status(self):
        return {"checked_in": self.claim_calls > 0, "claimable": True,
                "inactive": False, "streak_days": 1, "message": "success"}

    def checkin_claim(self):
        self.claim_calls += 1
        return {"checked_in": True, "credits": 250, "extra_credits": 50,
                "message": "success"}

    def quota(self):
        return {"items": [{"label": "总额度", "used": 30, "total": 100,
                           "remaining": 70, "percent": 30, "reset_ts": None}],
                "level": "pro"}


def _benefits_state(tmp_path, providers):
    client = mock.MagicMock()
    client.session = {}
    state = SimpleNamespace(
        client=client, providers=providers, mock_dir=None,
        started_at=time.time(), enable_desensitize=False,
        enable_optimize_context=False, verbose_llm=False,
        default_provider="codebuddy", default_model=None,
        metrics=MetricsCollector(None),
        write_log=mock.MagicMock(), ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(), json_logger=mock.MagicMock(),
        runtime_info={},
    )
    manager = BenefitsManager(tmp_path / "checkin.jsonl", state)
    state.benefits = manager
    return state, manager


def test_checkin_history_and_manager(tmp_path):
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    result = asyncio.run(manager.claim_now("fakecheckin"))
    assert result["ok"] is True
    # 历史落盘 + 日历今天有记录（含当日领取积分）
    history = CheckinHistory(tmp_path / "checkin.jsonl")
    dates = history.ok_dates_by_provider()
    assert len(dates["fakecheckin"]) == 1
    cal = history.calendar(7)
    assert cal[-1]["providers"] == ["fakecheckin"]
    assert cal[-1]["credits"] == {"fakecheckin": 50}
    assert cal[0]["providers"] == []
    # snapshot：done_today 且额度展示出来
    snap = asyncio.run(manager.snapshot())
    entry = next(p for p in snap["providers"] if p["id"] == "fakecheckin")
    assert entry["checkin"]["supported"] is True
    assert entry["checkin"]["done_today"] is True
    assert entry["quota"]["supported"] is True
    assert entry["quota"]["items"][0]["percent"] == 30
    assert entry["quota"]["items"][0]["remaining"] == 70


def test_checkin_claim_upstream_error_recorded(tmp_path):
    class ErrCheckinProvider(FakeCheckinProvider):
        id = "fakeerrcheckin"

        def checkin_claim(self):
            raise RuntimeError("boom")

    state, manager = _benefits_state(tmp_path, {"fakeerrcheckin": ErrCheckinProvider()})
    result = asyncio.run(manager.claim_now("fakeerrcheckin"))
    assert result["ok"] is False
    history = CheckinHistory(tmp_path / "checkin.jsonl")
    assert history.entries()[-1]["ok"] is False
    assert "boom" in history.entries()[-1]["message"]


def test_auto_checkin_tick_claims_once(tmp_path):
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    import asyncio
    asyncio.run(manager._tick())
    provider = state.providers["fakecheckin"]
    assert provider.claim_calls == 1
    # 当天已完成 → 再次巡检不会重复打卡
    asyncio.run(manager._tick())
    assert provider.claim_calls == 1
    # auto_checkin 关闭 → 不打
    settings_mod.save_settings({"auto_checkin": False})
    asyncio.run(manager._tick())
    assert provider.claim_calls == 1


def test_auto_checkin_skips_inactive_provider(tmp_path):
    """上游当天无签到活动（claimable=False）→ 不 claim、不留失败噪音。"""
    class InactiveCheckinProvider(FakeCheckinProvider):
        id = "fakeinactive"

        def checkin_status(self):
            return {"checked_in": False, "claimable": False, "inactive": True,
                    "streak_days": 0, "message": "活动未开始"}

    state, manager = _benefits_state(tmp_path, {"fakeinactive": InactiveCheckinProvider()})
    import asyncio
    asyncio.run(manager._tick())
    provider = state.providers["fakeinactive"]
    assert provider.claim_calls == 0
    assert CheckinHistory(tmp_path / "checkin.jsonl").entries() == []


def test_auto_checkin_records_upstream_checked_in(tmp_path):
    """上游已签到（历史没有记录）→ 补记 ok，且不再 claim。"""
    class AlreadyCheckinProvider(FakeCheckinProvider):
        id = "fakealready"

        def checkin_status(self):
            return {"checked_in": True, "claimable": False, "inactive": False,
                    "streak_days": 3, "message": "已签到"}

    state, manager = _benefits_state(tmp_path, {"fakealready": AlreadyCheckinProvider()})
    import asyncio
    asyncio.run(manager._tick())
    assert state.providers["fakealready"].claim_calls == 0
    history = CheckinHistory(tmp_path / "checkin.jsonl")
    assert history.entries()[-1]["ok"] is True
    assert "已签到" in history.entries()[-1]["message"]


def test_checkin_disabled_provider_not_touched(tmp_path):
    # 用假的 codebuddy 顶掉 _default_codebuddy 注入（真实 codebuddy 现在支持打卡）
    state, manager = _benefits_state(tmp_path, {"codebuddy": FakeProvider(),
                                                "fakeprov": FakeProvider()})
    assert manager.checkin_providers() == {}
    import asyncio
    asyncio.run(manager._tick())  # 不应抛异常
    assert CheckinHistory(tmp_path / "checkin.jsonl").entries() == []


def test_zcode_quota_items_normalization():
    data = {"limits": [
        {"type": "CREDIT_LIMIT", "usage": 10000, "currentValue": 4242,
         "remaining": 5757, "percentage": 42, "nextResetTime": 1789035412996},
        {"type": "CREDIT_LIMIT", "usage": 2000, "currentValue": 1249,
         "remaining": 750, "percentage": 62, "nextResetTime": 1788603896427},
    ], "level": "lite"}
    items = _quota_items(data)
    # 按 nextResetTime 升序：5 小时窗口在前
    assert items[0]["label"] == "5 小时窗口"
    assert items[0]["percent"] == 62 and items[0]["used"] == 1249
    assert items[0]["remaining"] == 750
    assert items[1]["label"] == "每周窗口"
    assert items[1]["percent"] == 42 and items[1]["remaining"] == 5757


def test_ui_checkin_endpoint(env, tmp_path):
    env.state.providers["fakecheckin"] = FakeCheckinProvider()
    env.state.benefits = BenefitsManager(tmp_path / "c.jsonl", env.state)
    body = env.client.post("/ui/api/checkin",
                           json={"provider": "fakecheckin"}).json()
    assert body["ok"] is True
    r = env.client.post("/ui/api/checkin", json={"provider": "fakeprov"}).json()
    assert r["ok"] is False  # 未声明 supports_checkin


def test_settings_auto_checkin_validation(env):
    r = env.client.post("/ui/api/settings",
                        json={"auto_checkin": False, "checkin_time": "08:05"})
    assert r.status_code == 200
    saved = settings_mod.load_settings()
    assert saved["auto_checkin"] is False and saved["checkin_time"] == "08:05"
    r = env.client.post("/ui/api/settings", json={"checkin_time": "99:00"})
    assert r.status_code == 400


def test_lifespan_starts_and_stops_benefits_loop(tmp_path, monkeypatch):
    """app 启动时拉起自动打卡循环，关闭时停止（回归：曾因未导入符号启动即崩）。"""
    state, manager = _benefits_state(tmp_path, {"fakecheckin": FakeCheckinProvider()})
    manager.start = mock.MagicMock()
    manager.stop = mock.MagicMock()
    monkeypatch.setattr(st, "proxy_state", state)
    with TestClient(m.app):
        manager.start.assert_called_once()
    manager.stop.assert_called_once()


def test_zcode_retries_transient_disconnect():
    """上游在返回任何字节前断连 → 原地重发一次，成功即正常返回。"""
    import asyncio
    import httpx
    from buddy_proxy.zcode_provider import ZcodeProvider

    p = ZcodeProvider(api_key="k", base_url="https://open.bigmodel.cn/api/anthropic")
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "pong"}}]}

    class FlakyClient:
        def build_request(self, *a, **k):
            return object()
        async def send(self, req, stream=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            return FakeResp()

    async def fake_get_client():
        return FlakyClient()
    p._get_client = fake_get_client

    resp = asyncio.run(p.forward({"model": "glm-5.3-flash",
                                  "messages": [{"role": "user", "content": "hi"}]}, "openai"))
    assert calls["n"] == 2
    assert b"pong" in resp.body
