"""豆包 agent 管线离线测试：payload 构造语义（不启动 CDP、不访问上游）。

覆盖 PR#28 的关键 wire 语义（App 抓包实测结论）：
- agent 管线 vs 经典管线的结构差异（agent_mode / model_config / aggregate_params）
- runtime_type 会话语义：新会话 2 / 续聊 1 + need_modify_conversation 取反
- reasoning_effort 传递与默认值
- 第三方模型（cis provider）字段

运行：PYTHONPATH=src python3 -m pytest tests/test_doubao_agent_pipeline.py -v
"""
from __future__ import annotations

import json

import pytest

from buddy_proxy.doubao.cdp_client import CDPDoubaoClient
from buddy_proxy.doubao_provider import _DOUBAO_CHAT_MODELS


def _agent_payload(model_spec: dict, need_create: bool = True) -> dict:
    # _build_agent_payload 不依赖实例状态，用 None 作为 self 直接调用
    return CDPDoubaoClient._build_agent_payload(
        None, "你好", model_spec, need_create, "bot-1", "conv-1", 1700000000000, 1700000000,
    )


def test_agent_payload_core_fields():
    spec = {"item_key": "5", "extra": {"total_window_size": "256000"}, "provider": "",
            "reasoning_effort": 4}
    p = _agent_payload(spec, need_create=True)
    opt = p["option"]
    assert opt["agent_mode"] is 1 or opt["agent_mode"] == 1
    assert opt["model_config"] == {
        "model_item_key": "5",
        "model_extra_params": {"total_window_size": "256000"},
        "reasoning_effort": 4,
    }
    assert opt["aggregate_params"]["model_item_key"] == "5"
    assert opt["aggregate_params"]["agent_mode"] == "1"
    assert opt["aggregate_params"]["reasoning_effort"] == "4"
    assert opt["aggregate_params"]["provider_id"] == ""
    ext = p["ext"]
    assert ext["agent_mode"] == "1"
    # agent 管线里 use_deep_think 携带的是 model_item_key，不是 0/1/3 思考枚举
    assert ext["use_deep_think"] == "5"
    assert opt["need_deep_think"] == 5
    # 消息体走 block_type 10000 文本块
    assert p["messages"][0]["content_block"][0]["block_type"] == 10000
    assert p["messages"][0]["content_block"][0]["content"]["text_block"]["text"] == "你好"


def test_agent_payload_third_party_provider():
    """第三方模型（Gemini/GPT）带 provider_id=cis 与 memory_profile。"""
    spec = _DOUBAO_CHAT_MODELS["gemini-3.7-flash"]["agent"] | {"reasoning_effort": 3}
    p = _agent_payload(spec, need_create=True)
    assert p["option"]["aggregate_params"]["provider_id"] == "cis"
    assert p["option"]["model_config"]["model_extra_params"]["provider_id"] == "cis"
    # 大数 item_key 原样传递（gemini 的 model_item_key 是长数字串）
    assert p["option"]["model_config"]["model_item_key"] == "1946880770"
    assert p["ext"]["use_deep_think"] == "1946880770"


def test_agent_payload_runtime_type_new_vs_followup():
    """新会话 runtime_type=2 + 不改会话；续聊 runtime_type=1 + need_modify=True。"""
    gtp_new = json.loads(_agent_payload({"item_key": "9"}, need_create=True)["ext"]["general_task_param"])
    assert gtp_new["runtime_type"] == 2
    assert gtp_new["agent_task_param"]["runtime_type"] == 2
    assert gtp_new["need_modify_conversation"] is False

    gtp_follow = json.loads(_agent_payload({"item_key": "9"}, need_create=False)["ext"]["general_task_param"])
    assert gtp_follow["runtime_type"] == 1
    assert gtp_follow["agent_task_param"]["runtime_type"] == 1
    assert gtp_follow["need_modify_conversation"] is True

    # 续聊时 client_meta 带上会话 id；新会话 local_conversation_id 为空串
    p_follow = _agent_payload({"item_key": "9"}, need_create=False)
    assert p_follow["client_meta"]["conversation_id"] == "conv-1"
    assert p_follow["client_meta"]["local_conversation_id"] == ""


def test_model_table_shape():
    """模型表约束：agent 条目字段齐全；经典条目走 deep_think 枚举。"""
    classic = {k: v for k, v in _DOUBAO_CHAT_MODELS.items() if "agent" not in v}
    agent = {k: v for k, v in _DOUBAO_CHAT_MODELS.items() if "agent" in v}
    assert {"doubao", "doubao-pro", "doubao-think", "doubao-expert"} <= set(classic)
    assert {"doubao-auto", "doubao-2.1-turbo", "doubao-2.1-pro",
            "orange-5.0", "gemini-3.7-flash", "gpt-5.6-sol"} <= set(agent)
    for mid, spec in agent.items():
        assert "item_key" in spec["agent"], mid
        assert "desc" in spec, mid
    for mid, spec in classic.items():
        assert spec["deep_think"] in (0, 1, 3), mid


def test_classic_payload_has_no_agent_fields():
    """经典管线不携带 agent 字段（服务端忽略模型字段，固定默认豆包）。"""
    p = CDPDoubaoClient._build_classic_payload(
        None, "你好", 0, True, "bot-1", "conv-1", 1700000000000, 1700000000,
    )
    assert "agent_mode" not in p["option"]
    assert "model_config" not in p["option"]
    assert "aggregate_params" not in p["option"]


# ---------------------------------------------------------------------------
# 目标选择优先级（2026-09-09：App 聊天窗口是工具可用的上下文，优先直连）
# ---------------------------------------------------------------------------

def _tgt(url: str, type_: str = "page", ws: bool = True) -> dict:
    t = {"url": url, "type": type_, "id": url[:8]}
    if ws:
        t["webSocketDebuggerUrl"] = f"devtools/browser/{url[:8]}"
    return t


def _wait_for_target_once(targets: list[dict], timeout: float = 1.0) -> dict | None:
    import asyncio

    client = CDPDoubaoClient()
    client._fetch_targets = lambda: targets  # type: ignore[method-assign]
    return asyncio.run(client._wait_for_target(timeout=timeout))


def test_target_prefers_app_chat_window():
    """App 自己的聊天窗口优先于豆包域名页（工具只在 App 上下文可用）。"""
    picked = _wait_for_target_once([
        _tgt("https://www.doubao.com/chat/123"),
        _tgt("doubaowork://doubaowork-background/"),
        _tgt("doubaowork://doubaowork-chat/chat/456"),
    ])
    assert picked is not None
    assert "doubaowork-chat/chat" in picked["url"]


def test_target_falls_back_to_doubao_page():
    """没有 App 聊天窗口时用豆包域名页。"""
    picked = _wait_for_target_once([
        _tgt("doubaowork://doubaowork-background/"),
        _tgt("https://www.doubao.com/chat/123"),
    ])
    assert picked is not None
    assert "doubao.com" in picked["url"]


def test_target_ignores_iframe_and_non_page():
    """iframe 与无 WS 的 target 不参与选择；只剩普通页时作兜底。"""
    picked = _wait_for_target_once([
        _tgt("https://www.doubao.com/drive-iframe/drive/home/", type_="iframe"),
        _tgt("doubaowork://doubaowork-chat/cross-site-support/", type_="other"),
        _tgt("doubaowork://doubaowork-background/"),
    ])
    assert picked is not None
    assert "doubaowork-background" in picked["url"]


def test_usable_chat_href():
    from buddy_proxy.doubao.cdp_client import _usable_chat_href
    assert _usable_chat_href("chrome://doubaowork-chat/chat/123")
    assert _usable_chat_href("doubaowork://doubaowork-chat/chat/123")
    assert _usable_chat_href("https://www.doubao.com/chat/")
    # App 内部服务页不可直接发请求（否则会走到新开标签页/报错路径）
    assert not _usable_chat_href("doubaowork://doubaowork-background/")
    assert not _usable_chat_href("chrome://doubaowork-chat/cross-site-support/")
    assert not _usable_chat_href("")
    assert not _usable_chat_href(None)


# ---------------------------------------------------------------------------
# 新会话引导（2026-09-09：runtime_type=2 的新会话未完成运行时握手，
# 首条消息的工具调用会卡死；先纯文本建会话再续聊发正式任务）
# ---------------------------------------------------------------------------

class _FakeClient:
    """记录 chat_completion 调用并回放脚本化 SSE 事件。"""

    def __init__(self, script):
        self.script = script  # list[list[dict]]：每次调用的水位事件
        self.calls: list[dict] = []
        self.failures = 0

    def record_failure(self, code=0):
        self.failures += 1

    def record_success(self):
        pass

    @staticmethod
    def extract_conversation_id(event):
        return event.get("ack_client_meta", {}).get("conversation_id")

    async def chat_completion(self, text, conversation_id=None, bot_id=None,
                              use_deep_think=0, model_spec=None):
        self.calls.append({"text": text, "conversation_id": conversation_id})
        for ev in self.script[len(self.calls) - 1]:
            yield ev


def _run_agent_task(provider, task, session_id=None):
    import asyncio

    async def _collect():
        return [chunk async for chunk in
                provider.stream_agent_task(task, session_id, "doubao-auto",
                                           {"item_key": "9", "extra": {}, "provider": ""})]
    return asyncio.run(_collect())


def _provider_with(fake_client):
    from buddy_proxy.doubao_provider import DoubaoProvider
    p = DoubaoProvider.__new__(DoubaoProvider)  # 跳过 __init__（不起 CDP）
    p._client = fake_client
    p._agent_session = None
    p._started = True
    return p


def test_new_session_bootstraps_then_sends_task_as_continuation():
    boot = [{"ack_client_meta": {"conversation_id": "conv-boot"}},
            {"_event": "CHUNK_DELTA", "text": "就绪"}]
    real = [{"ack_client_meta": {"conversation_id": "conv-boot"}},
            {"_event": "CHUNK_DELTA", "text": "答案"}]
    p = _provider_with(_FakeClient([boot, real]))
    chunks = _run_agent_task(p, "帮我读一下桌面文件")
    types = [__import__("json").loads(c[6:])["type"] for c in chunks]
    assert types[0] == "start"
    assert "session" in types
    # 第一次调用无会话 id（引导建会话），第二次续聊引导出的会话
    assert p._client.calls[0]["conversation_id"] is None
    assert p._client.calls[1]["conversation_id"] == "conv-boot"
    # 引导消息不是任务文本；正式任务以续聊发出
    assert p._client.calls[0]["text"] != "帮我读一下桌面文件"
    assert p._client.calls[1]["text"] == "帮我读一下桌面文件"
    # 引导回复不透出，正式回复透出
    texts = "".join(chunks)
    assert "就绪" not in texts
    assert "答案" in texts
    assert types[-1] == "done"
    # 默认会话跟随引导出的会话
    assert p._agent_session == "conv-boot"


def test_existing_session_skips_bootstrap():
    real = [{"ack_client_meta": {"conversation_id": "conv-x"}},
            {"_event": "CHUNK_DELTA", "text": "ok"}]
    p = _provider_with(_FakeClient([real]))
    chunks = _run_agent_task(p, "任务", session_id="conv-x")
    assert len(p._client.calls) == 1
    assert p._client.calls[0]["conversation_id"] == "conv-x"
    assert "ok" in "".join(chunks)


def test_bootstrap_upstream_error_fails_task():
    boot = [{"error": True, "status": 502, "body": "boom"}]
    p = _provider_with(_FakeClient([boot]))
    chunks = _run_agent_task(p, "任务", session_id="new")
    assert len(p._client.calls) == 1  # 引导失败即终止，不发正式任务
    assert '"type":"error"' in "".join(chunks).replace(" ", "")
