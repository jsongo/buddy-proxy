"""2026-09-06 Claude Code 接入 11148 badcase 回归。

现象：Claude Code（/v1/messages，anthropic 协议）+ codebuddy/deepseek-v4-flash
间歇性 400：

    {"code":11148,"msg":"tool calls and tool results do not match, ...",
     "extError":{"code":"tool_call_sequence_broken", ...}}

根因（anthropic_adapter 请求侧转换产生非法 tool 配对，上游 DeepSeek 系严格
校验器拒单；当日日志 logs/proxy.log 三次复现，glm-5.3 同形历史不拒）：

    1. user 消息混排 text + tool_result 时，转换器把 user 文本排在 tool
       结果之前 → assistant(tool_calls) 后被 user 文本隔断才出现 tool 消息；
    2. 中断/上下文裁剪后的历史存在无结果的 tool_use、无 call 的孤儿
       tool_result、空 tool_use_id；
    3. 纯 thinking 的 assistant 历史被转成 content=None 的空 assistant 消息。

修复：_convert_anthropic_message 调整 user 内部顺序（tool 结果先于文本）；
新增 repair_tool_sequence 兜底层，保证发往上游的序列满足：
每个 tool_call 恰有一条紧跟的同 id tool 消息，无孤儿 tool 消息，无空 assistant。
"""
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src"))
from buddy_proxy.protocols.anthropic_adapter import (  # noqa: E402
    anthropic_request_to_chat,
    repair_tool_sequence,
)

fails = []


def check(name, cond, extra=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    if not cond:
        fails.append(name)


def test_badcase_20260906_regression():
    """原独立脚本的整体回归流程（一次跑完所有 check，失败汇总断言）。"""
    def strict_validate(messages):
        """模拟 DeepSeek 系上游校验器：返回第一个配对违规的描述，合法则 None。"""
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                expected = sorted(t["id"] for t in m["tool_calls"])
                i += 1
                got = []
                while i < len(messages) and messages[i].get("role") == "tool":
                    got.append(messages[i].get("tool_call_id"))
                    i += 1
                if sorted(got) != expected:
                    return f"call/result mismatch: expected={expected} got={got}"
            elif m.get("role") == "tool":
                return f"orphan tool msg at #{i}"
            elif m.get("role") == "assistant" and m.get("content") is None:
                return f"null-content assistant at #{i}"
            else:
                i += 1
        return None


    def convert(body):
        chat = anthropic_request_to_chat(body)
        return chat, chat["messages"]


    # ---------------- badcase 1：user 混排 text + tool_result（主嫌疑） ----------------
    c1, m1 = convert({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"},
            {"type": "text", "text": "继续"}]},
    ]})
    check("b1配对合法", strict_validate(m1) is None, strict_validate(m1))
    check("b1顺序 tool→user文本",
          [(m["role"], m.get("content")) for m in m1] == [
              ("user", "hi"), ("assistant", None),
              ("tool", "file.txt"), ("user", "继续")],
          [(m["role"], str(m.get("content"))[:20]) for m in m1])

    # ---------------- badcase 2：tool_use 无结果（中断/裁剪） ----------------
    c2, m2 = convert({"messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {}},
            {"type": "tool_use", "id": "toolu_b", "name": "Read", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"}]},
    ]})
    check("b2配对合法", strict_validate(m2) is None, strict_validate(m2))
    tids = [m["tool_call_id"] for m in m2 if m["role"] == "tool"]
    check("b2缺的结果补占位", sorted(tids) == ["toolu_a", "toolu_b"], tids)
    ph = [m for m in m2 if m["role"] == "tool" and m["tool_call_id"] == "toolu_b"]
    check("b2占位标注中断", ph and "interrupted" in ph[0]["content"])

    # ---------------- badcase 3：孤儿 tool_result（call 已被裁掉） ----------------
    c3, m3 = convert({"messages": [
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_ghost", "content": "late"}]},
    ]})
    check("b3配对合法", strict_validate(m3) is None, strict_validate(m3))
    check("b3孤儿转user保留内容",
          any(m["role"] == "user" and "late" in (m.get("content") or "") for m in m3))

    # ---------------- badcase 4：纯 thinking assistant → 空 assistant 消息 ----------------
    c4, m4 = convert({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig"}]},
        {"role": "user", "content": "hello?"},
    ]})
    check("b4空assistant丢弃", [m["role"] for m in m4] == ["user", "user"]
          and strict_validate(m4) is None)

    # ---------------- badcase 5：中场 system 消息（CC 兼容客户端 reminder） ----------------
    c5, m5 = convert({"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_x", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_x", "content": "out"}]},
        {"role": "system", "content": [{"type": "text", "text": "todo reminder"}]},
    ]})
    check("b5配对合法且system保留", strict_validate(m5) is None
          and m5[-1]["role"] == "system" and m5[-1]["content"] == "todo reminder")

    # ---------------- badcase 6：tool_use/tool_use_id 缺 id ----------------
    c6, m6 = convert({"messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "", "content": "x"}]},
    ]})
    check("b6配对合法", strict_validate(m6) is None, strict_validate(m6))
    check("b6空id结果被认领", [m["role"] for m in m6] == ["assistant", "tool"],
          [(m["role"], str(m.get("content"))[:20]) for m in m6])

    # ---------------- badcase 7：重复 id 的结果去重 ----------------
    c7, m7 = convert({"messages": [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "a"},
            {"type": "tool_result", "tool_use_id": "t1", "content": "b"}]},
    ]})
    tools7 = [m for m in m7 if m["role"] == "tool"]
    check("b7重复结果去重", len(tools7) == 1 and tools7[0]["content"] == "a"
          and strict_validate(m7) is None)

    # ---------------- 回归：健康历史无损、多轮乱序也能修 ----------------
    clean = {"messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "checking"},
            {"type": "tool_use", "id": "tA", "name": "Bash", "input": {}},
            {"type": "tool_use", "id": "tB", "name": "Read", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tA", "content": "a"},
            {"type": "tool_result", "tool_use_id": "tB", "content": "b"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "all done"}]},
        {"role": "user", "content": "thanks"},
    ]}
    _, m8 = convert(clean)
    check("健康历史无损", [(m["role"], m.get("tool_calls") and tuple(
        t["id"] for t in m["tool_calls"]) or m.get("content")) for m in m8] == [
        ("user", "hi"), ("assistant", ("tA", "tB")),
        ("tool", "a"), ("tool", "b"),
        ("assistant", "all done"), ("user", "thanks")])

    # repair_tool_sequence 本身幂等
    check("repair幂等", repair_tool_sequence(m8) == m8)

    # 带 system/tools 的完整请求不受影响
    c9, m9 = convert({
        "system": "you are helpful",
        "tools": [{"name": "Bash", "description": "run",
                   "input_schema": {"type": "object"}}],
        "messages": clean["messages"],
    })
    check("system/tools保留", m9[0]["role"] == "system"
          and c9["tools"][0]["function"]["name"] == "Bash" and strict_validate(m9) is None)
    assert not fails, f"FAILED: {fails}"
