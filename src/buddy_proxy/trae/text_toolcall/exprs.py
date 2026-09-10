"""调用形态解析：函数表达式 / XML 属性标签 / 冒号连写 / 裸 JSON 校验。

只做「文本形态 → (name, args, 位置)」的提取，不含修复与流式状态；
被 parse（统一解析入口）与 splitter（流式扣留检测）共同复用。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# 裸 JSON 工具调用候选前缀：模型偶尔省略标签，直接在行首输出
# {"name": ...} 或 [{"name": ...}]（数组包多个调用）
# deepseek-v4-pro 偶尔在 { 和 " 之间加空格：{ "name": ... }
_BARE_CANDS = ('{"name"', '{ "name"', '[{"name"', '[{ "name"', '[ {"name"')
_BARE_RE = re.compile(r'(?m)^[ \t]*(\[\s*\{\s*"name"|\{\s*"name")')

# XML 属性风格工具调用：<tool_call name="..." command="..." />
# 属性值里可能含 '>'（如 shell 命令 2>/dev/null），不能用简单正则匹配整标签，
# 用起始正则定位 + 引号感知扫描
_ATTR_TAG_START_RE = re.compile(r"<(?:tool_call|tool_calls|tool_action)\b", re.I)
_ATTR_VAL_RE = re.compile(r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")

# 函数调用表达式参数值：k="v" / k='v' / k=7（数字）/ k=true|false|null（裸字面量）
_KV_VAL_RE = re.compile(
    r"(\w+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|(-?\d+(?:\.\d+)?)|(true|false|null))",
    re.I,
)

# 行首函数调用表达式（裸函数语法兜底的流式扣留锚点）
_LINE_CALL_EXPR_RE = re.compile(r"(?m)^[ \t]*([A-Za-z_][\w.]*)[ \t]*\(")

_CALL_NAME_RE = re.compile(r"([A-Za-z_][\w.]*)\s*\(")


def _mk_tool_call(idx: int, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": f"call_{idx:02d}_{uuid.uuid4().hex[:20]}",
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _parse_kv_args(body: str) -> dict[str, Any]:
    """解析函数调用表达式参数体 k=v, k2=v2 -> dict（保留键原大小写）。

    相比 _ATTR_VAL_RE 额外支持未加引号的数字/布尔/null（glm-5.3 实测输出
    web_search(query="...", max_results=7, ...)，max_results 不带引号，
    旧正则会直接丢掉这个参数）。
    """
    args: dict[str, Any] = {}
    for m in _KV_VAL_RE.finditer(body):
        key = m.group(1)
        if m.group(2) is not None:
            val: Any = m.group(2)
        elif m.group(3) is not None:
            val = m.group(3)
        elif m.group(4) is not None:
            f = float(m.group(4))
            val = int(f) if f.is_integer() else f
        else:
            val = {"true": True, "false": False, "null": None}[
                m.group(5).lower()]
        args[key] = val
    return args


def _find_call_exprs(
    text: str,
    names: frozenset[str] | set[str] | None = None,
) -> list[tuple[str, dict[str, Any], int, int]]:
    """扫描文本里的函数调用表达式 name(k=v, ...)。

    引号感知 + 括号配对扫描（参数值里可能含括号/逗号），连续多个调用
    （空格/换行分隔）逐个返回。names 传入时只接受已知工具名（裸文本
    防误伤）；None 时任意名字都收（调用块内模型已显式标记是调用）。
    返回 [(name, args_dict, start, end), ...]，end 为右括号后一位。
    """
    results: list[tuple[str, dict[str, Any], int, int]] = []
    pos = 0
    n = len(text)
    while True:
        m = _CALL_NAME_RE.search(text, pos)
        if not m:
            break
        name = m.group(1)
        # 引号感知扫描到配对的右括号
        j = m.end()
        depth = 1
        quote: str | None = None
        end = -1
        while j < n:
            ch = text[j]
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end == -1:
            # 括号不闭合（截断流）：跳过名字继续扫
            pos = m.end()
            continue
        if names is None or name in names:
            body = text[m.end():end]
            args = _parse_kv_args(body)
            if not args:
                args = {"input": body.strip()}
            results.append((name, args, m.start(), end + 1))
        pos = end + 1
    return results


def _colon_marker_re(tools: frozenset[str] | set[str], anchored: bool = True) -> re.Pattern:
    """glm-5.3 连写变体「工具名+参数名+冒号」的起始标记正则。

    模型省略括号/引号/等号，输出 web_searchquery: <自由文本>；工具名与
    参数名之间无空格。按工具名长度降序拼接分支避免前缀撞名。
    anchored=True 时首个标记必须在行首；连续调用时下一个标记会与
    上一个值粘连（methodsweb_searchquery:），用 anchored=False 续扫。
    命名捕获组：tool=工具名，param=参数名。
    """
    alt = "|".join(
        sorted((re.escape(t) for t in tools), key=len, reverse=True))
    body = rf"(?P<tool>(?:{alt}))(?P<param>[a-z_][a-z0-9_]*)\s*:"
    if anchored:
        return re.compile(rf"(?m)^[ \t]*{body}")
    return re.compile(body)


def _find_colon_joined_calls(
    text: str,
    tools: frozenset[str] | set[str],
) -> list[tuple[str, dict[str, Any], int, int]]:
    """解析 web_searchquery: <自由文本> 连写形态的工具调用。

    glm-5.3 实测：模型连括号都省掉，直接写
    ``web_searchquery: how to ... methodsweb_searchquery: 检测套壳...``
    （连续多个时上一个值直接拼到下一个标记前，无分隔符）。
    首个标记必须位于行首；后续标记由上一个值的结束边界界定。
    返回 [(name, args_dict, start, end), ...]，end 为值结束位置。
    """
    first = _colon_marker_re(tools).search(text)
    if not first:
        return []
    marks = [first]
    follow_re = _colon_marker_re(tools, anchored=False)
    while True:
        m2 = follow_re.search(text, marks[-1].end())
        if not m2:
            break
        marks.append(m2)
    results: list[tuple[str, dict[str, Any], int, int]] = []
    n = len(text)
    for i, mk in enumerate(marks):
        val_start = mk.end()
        val_end = marks[i + 1].start() if i + 1 < len(marks) else n
        value = text[val_start:val_end]
        # 去掉值末尾的 markdown 水平分隔线行（如模型另起一行写的 ---）
        value = re.sub(r"(?m)^[ \t]*-{3,}[ \t]*$", "", value)
        # 自由文本压成单行
        value = re.sub(r"\s+", " ", value).strip(" \t\r\n-")
        if not value:
            continue
        results.append((
            mk.group("tool"),
            {mk.group("param"): value},
            mk.start("tool"),
            val_end,
        ))
    return results


def _extract_attr_calls(rest: str, calls: list[dict[str, Any]]) -> str:
    """提取 XML 属性风格的工具调用标签，返回剩余文本。

    形如 <tool_call name="shell" command="ls" intent="..." />（自闭合或
    开标签均可）。属性值内的 '>' 不会截断标签（扫描时跳过引号内字符）。
    """
    out: list[str] = []
    pos = 0
    n = len(rest)
    while True:
        m = _ATTR_TAG_START_RE.search(rest, pos)
        if not m:
            out.append(rest[pos:])
            break
        i = m.start()
        # 扫描到真正的 '>'（跳过引号内的字符）
        j = i + 1
        quote = None
        end = -1
        while j < n:
            ch = rest[j]
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
            elif ch == ">":
                end = j
                break
            j += 1
        if end == -1:
            out.append(rest[pos:])
            break
        tag_text = rest[i:end + 1]
        attrs: dict[str, str] = {}
        for k, v1, v2 in _ATTR_VAL_RE.findall(tag_text):
            attrs[k.lower()] = v1 if v2 == "" else v2
        name = attrs.get("name") or attrs.get("tool") or ""
        if not name:
            # 没有名字（教学格式的开标签/复数容器壳）：保留原文交给后续清理
            out.append(rest[pos:end + 1])
            pos = end + 1
            continue
        raw = attrs.get("command") or attrs.get("arguments") or attrs.get("args") or ""
        if raw.strip().startswith("{"):
            try:
                args = json.loads(raw)
            except ValueError:
                args = {"command": raw}
        else:
            args = {"command": raw}
            if attrs.get("intent"):
                args["intent"] = attrs["intent"]
        out.append(rest[pos:i])
        calls.append(_mk_tool_call(
            len(calls), name, json.dumps(args, ensure_ascii=False)))
        pos = end + 1
    return "".join(out)


def _tool_names(tools: list[dict[str, Any]] | None) -> frozenset[str] | None:
    """从 OpenAI tools 定义里提取工具名集合。"""
    if not tools:
        return None
    names = set()
    for t in tools:
        f = t.get("function") if isinstance(t, dict) else None
        if isinstance(f, dict) and f.get("name"):
            names.add(f["name"])
    return frozenset(names) or None


def _valid_bare_call_obj(obj: Any, known_tools: frozenset[str]) -> bool:
    """裸 JSON 是否是合法的工具调用对象。

    严格校验（避免误伤正文里的普通 JSON）：name 必须是已知工具名。
    两种形态放行：
    - 标准形态：带 arguments/args，且除 name/tool/intent 外无杂键
      （intent 是 ethan 教的说明字段，放行）；
    - 扁平形态（glm-5.3 实测）：name 与参数平铺——
      {"name": "web_search", "query": "...", "max_results": 10}，
      无 arguments 包裹层，但至少带一个参数键。
    """
    if not isinstance(obj, dict):
        return False
    name = obj.get("name")
    if not isinstance(name, str) or name not in known_tools:
        return False
    if "arguments" in obj or "args" in obj:
        return set(obj) <= {"name", "arguments", "args", "intent", "tool"}
    # 扁平形态：name/tool 之外还有键即视为参数平铺
    return bool(set(obj) - {"name", "tool"})


def _flatten_call_args(obj: dict[str, Any]) -> dict[str, Any]:
    """从裸 JSON 调用对象提取参数；扁平形态（无 arguments 层）取剩余键。

    glm-5.3 还会把 arguments 输出成 JSON 字符串（而非对象）：
    {"name": "web_search", "arguments": "{\"query\": \"...\", \"max_results\": 10}"}
    —— 解包成 dict，避免下游拿到 {"input": "<整段 JSON 字符串>"}。
    """
    args = obj.get("arguments", obj.get("args"))
    if args is None:
        args = {k: v for k, v in obj.items() if k not in ("name", "tool")}
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                args = parsed
        except Exception:
            pass
    return args if isinstance(args, dict) else {"input": args}
