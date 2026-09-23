"""DSML 变体回归：deepseek(codebuddy) 实测泄漏 badcase（2026-09-24）。

badcase 特征：前缀与标签名之间带空格、包装标签缺 tool_ 前缀：
  <｜｜DSML｜｜ calls> <｜｜DSML｜｜ invoke name="shell"> ...
修复前 match_tool_markup_name 消费 DSML 前缀后遇空 → 整段当普通文本泄漏。
"""
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))

from buddy_proxy.protocols.dsml_parser import (  # noqa: E402
    ToolCallStreamBuffer,
    parse_tool_calls,
)
from buddy_proxy.protocols.anthropic_adapter import AnthropicStreamConverter  # noqa: E402

P = "｜｜DSML｜｜"  # DSML 前缀

SNIPPET = (
    f'<{P} calls> <{P} invoke name="shell"> '
    f'<{P} parameter name="command" string="true">'
    'unset GH_TOKEN GITHUB_TOKEN; cd /tmp echo ok | head -40'
    f'</{P} parameter> '
    f'<{P} parameter name="intent" string="true">梳理tab功能'
    f'</{P} parameter> '
    f'</{P} invoke> </{P} calls>'
)


def test_single_shot_parse_spaced_dsml():
    calls = parse_tool_calls(SNIPPET)
    assert len(calls) == 1, calls
    fn = calls[0]["function"]
    assert fn["name"] == "shell"
    args = json.loads(fn["arguments"])
    assert args["command"].startswith("unset GH_TOKEN")
    assert args["intent"] == "梳理tab功能"


def test_streaming_no_leak_and_parses():
    buf = ToolCallStreamBuffer()
    out_parts, detected = [], None
    for i in range(0, len(SNIPPET), 7):
        cleaned, calls = buf.add_chunk(SNIPPET[i:i + 7])
        out_parts.append(cleaned)
        if calls:
            detected = calls
    out_parts.append(buf.flush())
    text = "".join(out_parts)
    assert detected, "流式路径未检出工具调用"
    assert detected[0]["function"]["name"] == "shell"
    for marker in ("DSML", "invoke", "parameter", "tool_calls"):
        assert marker not in text, f"标记泄漏: {marker!r} in {text!r}"


def test_streaming_partial_prefix_is_held():
    # `>` 尚未到达时不得把标签头当普通文本吐出
    buf = ToolCallStreamBuffer()
    out, calls = buf.add_chunk(f"前文<{P} call")
    assert calls is None
    assert "call" not in out, f"标签头被提前泄漏: {out!r}"


def test_no_space_variant_still_works():
    s = (
        f'<{P}tool_calls><{P}invoke name="bash">'
        f'<{P}parameter name="cmd">ls</{P}parameter>'
        f'</{P}invoke></{P}tool_calls>'
    )
    calls = parse_tool_calls(s)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "bash"
    assert json.loads(calls[0]["function"]["arguments"]) == {"cmd": "ls"}


def test_prose_angle_bracket_not_held():
    buf = ToolCallStreamBuffer()
    out, calls = buf.add_chunk('占比 <50% 了')
    assert calls is None
    assert out == '占比 <50% 了'


def test_anthropic_stop_reason_tool_use_despite_late_stop():
    # 中途注入 tool_calls 后，上游最终 chunk 又给 stop，末尾仍必须是 tool_use
    conv = AnthropicStreamConverter('deepseek-v4.1-flash')
    conv.feed_chunk({'choices': [{
        'finish_reason': 'tool_calls',
        'delta': {'content': '好的'},
    }]})
    conv.feed_chunk({'choices': [{
        'finish_reason': 'tool_calls',
        'delta': {'tool_calls': [{
            'index': 0,
            'id': 'call_abc',
            'type': 'function',
            'function': {'name': 'shell', 'arguments': '{"command":"ls"}'},
        }]},
    }]})
    conv.feed_chunk({'choices': [{'finish_reason': 'stop', 'delta': {}}]})
    events = conv.finish()
    msg_delta = [d for name, d in events if name == 'message_delta']
    assert msg_delta, events
    assert msg_delta[-1]['delta']['stop_reason'] == 'tool_use'


def test_plain_tool_calls_still_works():
    s = ('<tool_calls><invoke name="shell">'
         '<parameter name="command">pwd</parameter></invoke></tool_calls>')
    calls = parse_tool_calls(s)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "shell"


def test_anthropic_finish_length_keeps_max_tokens():
    # finish=length 截断语义优先，不能被 tool_blocks 提升成 tool_use
    conv = AnthropicStreamConverter('deepseek-v4.1-flash')
    conv.feed_chunk({'choices': [{
        'finish_reason': 'tool_calls',
        'delta': {'tool_calls': [{
            'index': 0,
            'id': 'call_abc',
            'type': 'function',
            'function': {'name': 'shell', 'arguments': '{"command":"ls"}'},
        }]},
    }]})
    conv.feed_chunk({'choices': [{'finish_reason': 'length', 'delta': {}}]})
    events = conv.finish()
    msg_delta = [d for name, d in events if name == 'message_delta']
    assert msg_delta, events
    assert msg_delta[-1]['delta']['stop_reason'] == 'max_tokens'


def test_dsml_injection_index_increments_across_chunks():
    # 跨 chunk 注入必须自增 index，否则 Anthropic 转换器按 index 开槽会合并两次调用
    from buddy_proxy.codebuddy_provider.pipeline import _build_dsml_tool_call_deltas

    def _call(name):
        return {
            'id': f'call_{name}',
            'type': 'function',
            'function': {'name': name, 'arguments': '{}'},
        }

    d1, nxt = _build_dsml_tool_call_deltas([_call('a')], 0)
    d2, nxt2 = _build_dsml_tool_call_deltas([_call('b')], nxt)
    assert [d['index'] for d in d1] == [0]
    assert [d['index'] for d in d2] == [1]
    assert [d['function']['name'] for d in d2] == ['b']
    assert nxt2 == 2
