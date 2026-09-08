"""坏 JSON 工具调用的修复管线：非法转义 / 值内未转义引号 / 尾部截断。

三类损坏都来自实测泄漏 badcase（见各函数 docstring 的会话编号）；
只依赖标准库，被 parse 与 splitter 的 flush 路径共同复用。
"""

from __future__ import annotations

import json
from typing import Any


def _quote_is_close(s: str, i: int) -> bool:
    """Heuristic: does the ``"`` at position *i* close the current string?

    Tolerates unescaped quotes inside values (deepseek v4 pro leak): a quote
    followed by ``}`` / ``]`` only closes when valid structure tokens follow
    the bracket — a ``}`` immediately followed by more string content
    (e.g. ``echo "}" && ls``) is an inner quote, not the end of the value.
    """
    n = len(s)
    j = i + 1
    while j < n and s[j] in " \t":
        j += 1
    if j >= n:
        return True
    c = s[j]
    if c in ":,\n":
        return True
    if c in "}]":
        k = j + 1
        while k < n and s[k] in " \t\r\n":
            k += 1
        if k >= n or s[k] in ",}]\n":
            return True
        return False
    return False


def _find_obj_extent(text: str, start: int) -> int:
    """Best-effort brace-depth scan to find the end of a JSON object.

    Returns the index *after* the matching ``}`` or -1 if unbalanced.
    Tracks string state with :func:`_quote_is_close` so stray braces inside
    broken string values (e.g. ``"command": "echo "}" && ls"``) don't
    prematurely zero the depth.  If the boundary still can't be found the
    caller degrades gracefully (no repair, raw text preserved).
    """
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    in_string = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if ch == '"':
            if not in_string:
                in_string = True
            elif _quote_is_close(text, i):
                in_string = False
            i += 1
            continue
        if not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    return -1


def _repair_json_quotes(s: str) -> str | None:
    """Try to fix unescaped double-quotes inside JSON string values.

    DeepSeek v4 pro occasionally emits shell commands with unescaped ``"``
    (e.g. ``echo "---"``), which makes the JSON invalid.  Walk through the
    text tracking string-state; quotes that don't look like a real string
    close (per :func:`_quote_is_close`) are escaped in place.

    Returns the repaired string, or *None* if no repair was applied or the
    input doesn't look like a JSON object/array.
    """
    stripped = s.lstrip()
    if not stripped or stripped[0] not in "{[":
        return None
    out: list[str] = []
    i = 0
    n = len(s)
    in_string = False
    repaired_any = False
    while i < n:
        ch = s[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            out.append(s[i : i + 2])
            i += 2
            continue
        if ch == '"':
            if _quote_is_close(s, i):
                out.append(ch)
                in_string = False
            else:
                out.append('\\"')
                repaired_any = True
            i += 1
            continue
        out.append(ch)
        i += 1
    if not repaired_any:
        return None
    return "".join(out)


_JSON_VALID_ESCAPES = set('"\\/bfnrtu')
_JSON_HEX = set("0123456789abcdefABCDEF")


def _repair_invalid_escapes(s: str) -> str:
    r"""修复 JSON 字符串值里非法的 \ 转义：\X -> \\X。

    glm-5.3-flash 实测（s_20260804_0200_eb1d）：command 值里嵌 python /
    正则脚本，脚本自身的 \d \s . 等单反斜杠序列对 JSON 是非法转义
    （json.loads 报 Invalid \escape），现有引号修复管不了这类。把非法
    \X 双写成 \\X，解码后无损还原为原文 \X。
    """
    if "\\" not in s:
        return s
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            if i + 1 >= n:
                # 尾部孤立反斜杠（截断流常见）：按字面量处理
                out.append("\\\\")
                i += 1
                continue
            nxt = s[i + 1]
            if nxt == "u":
                hexrun = s[i + 2:i + 6]
                if len(hexrun) == 4 and all(c in _JSON_HEX for c in hexrun):
                    out.append(s[i:i + 6])
                    i += 6
                    continue
            elif nxt in _JSON_VALID_ESCAPES:
                out.append(s[i:i + 2])
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _repair_truncated_json(s: str) -> Any | None:
    """修复被截断的 JSON 对象/数组（流断在结构中间）。

    实测（s_20260905_0141_3a19）：流断在裸调用尾部，最外层 } 缺失。
    策略：字符串状态机扫描记录括号栈；结束仍在字符串里 → 补闭合引号；
    按栈深逆序补 } / ] 后尝试解析。仍失败则逐步回退到最后几个结构
    分隔符（, : { } [ ]，字符串外的）之前重试——会丢尾部半截键值，
    但保住前面已完整的参数。返回解析结果，修不动返回 None。
    """
    stripped = s.strip()
    if not stripped or stripped[0] not in "{[":
        return None

    def scan(text: str) -> tuple[list[str], bool, list[int]]:
        stack: list[str] = []
        seps: list[int] = []
        in_string = False
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                if not in_string:
                    in_string = True
                elif _quote_is_close(text, i):
                    in_string = False
                i += 1
                continue
            if not in_string:
                if ch in "{[":
                    stack.append("}" if ch == "{" else "]")
                    seps.append(i)
                elif ch in "}]":
                    if stack:
                        stack.pop()
                    seps.append(i)
                elif ch in ",:":
                    seps.append(i)
            i += 1
        return stack, in_string, seps

    fixed = _repair_invalid_escapes(stripped)
    _, in_string, seps = scan(fixed)
    base = fixed + '"' if in_string else fixed
    # 从破坏最小的候选开始：整段补齐括号 → 逐个回退到结构分隔符之前
    cut_points = [len(base)]
    cut_points += [p for p in reversed(seps) if p < len(base)][:6]
    for cut in dict.fromkeys(cut_points):
        frag = base[:cut]
        st, ins, _ = scan(frag)
        if ins:
            frag += '"'
            st, _, _ = scan(frag)
        try:
            return json.loads(frag + "".join(reversed(st)), strict=False)
        except ValueError:
            continue
    return None


def _repair_call_json(s: str) -> tuple[Any, int] | None:
    """统一修复管线：坏 JSON 调用 → (解析结果, 消费到的原始偏移)。

    覆盖三类实测损坏（此前都会整段泄漏进正文）：
    a) 非法 \\ 转义（脚本/正则的单反斜杠）——_repair_invalid_escapes；
    b) 字符串值里未转义双引号——_repair_json_quotes；
    c) 尾部截断（缺最外层 } / 断在字符串中间）——_repair_truncated_json。
    边界平衡（_find_obj_extent 找得到）走 a+b 后 strict=False 解析
    （容忍字面换行）；不平衡走 c。返回 None 表示修不动。
    """
    if not s or s[0] not in "{[":
        return None
    raw_end = _find_obj_extent(s, 0)
    if raw_end != -1:
        fixed = _repair_invalid_escapes(s[:raw_end])
        qr = _repair_json_quotes(fixed)
        if qr is not None:
            fixed = qr
        try:
            return json.loads(fixed, strict=False), raw_end
        except ValueError:
            pass
        return None
    obj = _repair_truncated_json(s)
    if obj is None:
        return None
    return obj, len(s)
