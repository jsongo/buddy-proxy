"""工具调用统一解析入口：教学格式主路径 + 全形态兜底 + 泄漏闸门。

_parse_tool_calls 是文本协议工具调用的唯一出口（流式端 flush 后也走
这里），按优先级依次尝试：教学格式 JSON 块 → 块内函数表达式 → Trae
原生 XML → 属性标签 → 裸 JSON → 散装 KV → 裸函数表达式 → 冒号连写，
最后由泄漏闸门丢弃/补收残留坏段。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .exprs import (
    _BARE_RE,
    _LINE_CALL_EXPR_RE,
    _extract_attr_calls,
    _find_call_exprs,
    _find_colon_joined_calls,
    _flatten_call_args,
    _mk_tool_call,
    _parse_kv_args,
    _valid_bare_call_obj,
)
from .repair import _find_obj_extent, _repair_call_json

log = logging.getLogger(__name__)

# prompt-based function calling 的教学格式标签。选 XML 标签 + JSON 参数：
# 标签与模型原生 SOLO 协议同构（遵循度最高），JSON 参数适配任意工具 schema
_TC_OPEN = '<tool_call>'
_TC_CLOSE = '</tool_call>'
_TOOL_DECODER = json.JSONDecoder()

# 散装键值调用（deepseek-v4-flash 实测 s_20260902_2130_c8bd）：外层大括号
# 与标签全省，name: "shell" 与 arguments: {...} 直接散装两行
_LOOSE_KV_RE = re.compile(
    r'(?m)^[ \t]*name[ \t]*:[ \t]*"(?P<name>[^"\n]+)"'
    r'[ \t\r\n]*,?[ \t\r\n]*arguments[ \t]*:[ \t]*\{')

# 泄漏闸门的证据提取：从调用开头段里取工具名。宽松匹配——坏调用的
# name 键本身可能已损坏（缺引号/缺冒号/值截断），行首调用语法开头 +
# 已知工具名的组合已经足够强，键的形态不苛求
_GATE_NAME_RES = (
    re.compile(r'[ \t]*(?:\{[\s]*|[{,][ \t]*)"?name"?[ \t]*:[ \t]*"?([A-Za-z_][\w.-]*)'),
    re.compile(r'[ \t]*name"?[ \t]*:[ \t]*"?([A-Za-z_][\w.-]*)'),
)

# word_tool 兜底的搜索窗口：工具名作为「调用开头」证据，只在段首找
_GATE_WORD_WINDOW = 120


def _peek_call_name(s: str) -> str:
    """从坏调用原文里提取工具名（修复全失败时判断是否丢弃的依据）。"""
    m = re.search(r'"(?:name|tool)"\s*:\s*"([^"\n]*)"', s)
    return m.group(1) if m else ""


def _gate_tool_name(seg: str) -> str:
    """闸门证据提取：从调用开头段里取出工具名（JSON 键 / 散装 name:）。"""
    for pat in _GATE_NAME_RES:
        m = pat.match(seg)
        if m:
            return m.group(1)
    return ""


def _gate_word_tool(seg: str, known: frozenset[str]) -> str:
    """证据兜底：段里出现独立成词的已知工具名（键语法损坏时）。

    只在段首 _GATE_WORD_WINDOW 内找：工具名属于「调用开头」的证据，
    离 `{`/锚点太远的命中（比如参数值或后文提到别的工具名）不足以
    把整段判成调用——误伤正文会静默发伪 tool_call，比泄漏更糟。
    """
    head = seg[:_GATE_WORD_WINDOW]
    for t in known:
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(t) + r"(?![A-Za-z0-9_])", head):
            return t
    return ""


def _gate_expr_end(rest: str, p: int) -> int:
    """函数表达式残段的结束位置：引号感知扫到匹配的 ')'，扫不到取行尾。"""
    i = rest.find("(", p)
    if i == -1:
        eol = rest.find("\n", p)
        return eol if eol != -1 else len(rest)
    depth = 0
    quote = None
    j = i
    n = len(rest)
    while j < n:
        ch = rest[j]
        if quote is not None:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        elif ch == "\n" and depth == 0:
            return j
        j += 1
    return n


def _gate_loose_block_end(rest: str, p: int) -> int:
    """无对象的散装残段结束位置：name 行 + 后随的 arguments 前缀行。

    只在两类情况下允许丢弃：a) name 行之后跟着 arguments（含其截断前缀）
    开头的行——参数区已开始；b) name 行是全文最后一行——流断在调用开头。
    其他情况（name 行后面是普通正文）返回 -1 不动，避免吞正文。
    """
    eol = rest.find("\n", p)
    if eol == -1:
        return len(rest)          # b) 截断到全文尾
    end = eol
    k = eol + 1
    while k < len(rest):
        eol2 = rest.find("\n", k)
        line = rest[k:eol2 if eol2 != -1 else len(rest)]
        ls = line.lstrip(" \t")
        if not ls:
            break
        if "arguments:".startswith(ls) or ls.startswith("arguments"):
            end = eol2 if eol2 != -1 else len(rest)
            k = end + 1
            # arguments: 行带出内容（如无大括号的散参数）就到此为止
            if not ls.startswith("arguments") or len(ls) > len("arguments"):
                break
            continue
        break
    if end == eol:
        # name 行后面不是 arguments：仅当其余部分是纯空白（name 行是最后
        # 一个非空行，流断在调用开头）才丢；有正文则不动
        if not rest[eol + 1:].strip():
            return len(rest)
        return -1
    return end


def _parse_tool_calls(
    content: str,
    known_tools: frozenset[str] | set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """从模型输出里解析工具调用块，返回 (剩余正文, tool_calls 列表)。

    主路径解析教学格式（JSON 参数，用 raw_decode 正确处理嵌套大括号）；
    兜底一：Trae 原生 SOLO XML（<tool_name>/<command>，名字对不上由
    下游 agent 报错纠偏）；
    兜底二：无标签裸 JSON（known_tools 提供时启用——模型连续多轮调用后
    会偷懒省略标签，直接在行首输出 {"name": ...}）。
    """
    # 容错归一化：模型偶尔把标签写成复数变体（开/闭合都可能，大小写不定），
    # 实测出现过「标准单数开标签 + 复数闭标签」的混搭——严格匹配会解析失败，
    # 兜底清理只剥开标签、留下复数闭标签和裸 JSON 整段泄漏进正文。
    # 先统一归一成教学的标准单数标签再进主解析。
    content = re.sub(r"</\s*tool_calls\s*>", _TC_CLOSE, content, flags=re.I)
    content = re.sub(r"<\s*tool_calls\s*>", _TC_OPEN, content, flags=re.I)
    # 伪回调头 <tool_callback>（deepseek-v4-flash 实测）必须在主解析前剥离：
    # 闭合块整块剥；未闭合剥到下一个 <tool_call> 为止——主循环会把
    # <tool_call> 消费掉，放后面做前瞻就没有锚点了。剥法与 _LEAK_RE 统一。
    content = re.sub(r"<tool_callback[^>]*>.*?</tool_callback>", "", content, flags=re.S)
    content = re.sub(r"<tool_callback[^>]*>.*?(?=<tool_call[>\s])", "", content, flags=re.S)
    # seed 协议块（doubao-seed-evolving 实测 s_20260905_0233_f291）：
    # <seed:tool_call>\n<function name="shell"><parameter name="command"
    # string="true">gh api ...</parameter><parameter name="intent"
    # string="true">...</parameter></function>...\n</seed:tool_call>
    # 参数值是标签体（命令含引号/反引号都不转义），按 XML 提取而非 JSON。
    # 未闭合（流截断）剥到全文尾。先于主循环做：调用量计入 calls 后，
    # 其余兜底的 not calls 门槛自然跳过
    calls: list[dict[str, Any]] = []

    def _grab_seed_block(m: re.Match) -> str:
        got_seed = False
        for fm in re.finditer(
                r'<function\s+name="([^"]+)"[^>]*>([\s\S]*?)</function>',
                m.group(1), re.I):
            args = {pm.group(1): pm.group(2) for pm in re.finditer(
                r'<parameter\s+name="([^"]+)"[^>]*>([\s\S]*?)</parameter>',
                fm.group(2), re.I)}
            calls.append(_mk_tool_call(
                len(calls), fm.group(1), json.dumps(args, ensure_ascii=False)))
            got_seed = True
        if got_seed:
            return ""
        # 块里没有完整 function（截断半截）：丢弃壳、留参数文本给闸门判断
        return m.group(1)

    content = re.sub(
        r"<seed:tool_call>\s*([\s\S]*?)(?:</seed:tool_call>|\Z)",
        _grab_seed_block, content, flags=re.I)
    # 记录原文是否带 arg_key/arg_value 残骸：glm-5.3 的坏调用块伴随孤立
    # </arg_value>，这是「这段文本确实是坏掉的调用」而非正文代码示例的强信号
    had_arg_debris = bool(re.search(r"</?\s*arg_(?:key|value)", content))
    rest_parts: list[str] = []
    pos = 0
    n = len(content)
    while pos < n:
        i = content.find(_TC_OPEN, pos)
        if i == -1:
            rest_parts.append(content[pos:])
            break
        rest_parts.append(content[pos:i])
        j = i + len(_TC_OPEN)
        block_calls: list[tuple[str, str]] = []
        closed = False
        expr_consumed = False  # 块内表达式路径已整块消费
        # 块内循环：连续解码多个 JSON（复数容器装多个调用），直到闭标签
        while True:
            while j < n and content[j] in " \t\r\n":
                j += 1
            if j >= n:
                break
            if content.startswith(_TC_CLOSE, j):
                j += len(_TC_CLOSE)
                closed = True
                break
            if content[j] == "{":
                try:
                    obj, end = _TOOL_DECODER.raw_decode(content, j)
                except ValueError:
                    # 修复段限制在块内（</tool_call> 之前）；块未闭合（流
                    # 截断）时扩到全文尾，交给截断修复
                    close = content.find(_TC_CLOSE, j)
                    seg = content[j:close] if close != -1 else content[j:]
                    rep = _repair_call_json(seg)
                    if rep is not None:
                        obj, rend = rep
                        if isinstance(obj, dict):
                            nm = obj.get("name") or obj.get("tool") or ""
                            ag = obj.get("arguments", obj.get("args", {}))
                            if not isinstance(ag, dict):
                                ag = {"input": ag}
                            if nm:
                                block_calls.append(
                                    (str(nm), json.dumps(ag, ensure_ascii=False)))
                                j = j + rend
                                continue
                    break
                name = obj.get("name") or obj.get("tool") or ""
                args = obj.get("arguments", obj.get("args", {}))
                if not isinstance(args, dict):
                    args = {"input": args}
                if name:
                    block_calls.append(
                        (str(name), json.dumps(args, ensure_ascii=False)))
                j = end
                continue
            # 不是 JSON：可能是 glm-5.3 的函数调用表达式写法——
            # <tool_call>web_search(query="...", max_results=7) web_search(...)</arg_value></tool_call>
            # （块内可连续多个调用表达式，混孤立 </arg_value> 残骸）。
            # 引号感知扫到块尾，全部转成 tool_calls 后整块消费。
            close = content.find(_TC_CLOSE, j)
            block_text = content[j:close if close != -1 else n]
            exprs = _find_call_exprs(block_text, None)
            if exprs:
                for name, args, _s, _e in exprs:
                    calls.append(_mk_tool_call(
                        len(calls), name, json.dumps(args, ensure_ascii=False)))
                pos = (close + len(_TC_CLOSE)) if close != -1 else n
                expr_consumed = True
                break
            break  # 既不是 JSON 也不是闭标签：块不合法
        # closed=正常闭合；j>=n=块内耗尽全文（流截断在块中间）——
        # 两种情况都要提交已修复出的调用，截断的未闭合块不再整块泄漏
        if (closed or j >= n) and block_calls:
            for name, args_json in block_calls:
                calls.append(_mk_tool_call(len(calls), name, args_json))
            pos = j
            continue
        if expr_consumed:
            continue
        # 不是合法调用块：保留原文继续扫描
        rest_parts.append(content[i:i + len(_TC_OPEN)])
        pos = i + len(_TC_OPEN)
    rest = "".join(rest_parts)

    # 残余噪音剥离（无论是否已解析出调用都要做）：
    # - 孤立 tool_callback 标签壳：未闭合到流尾的（模型放弃调用继续说正文）
    #   只剥壳、保留内部文本，不能整段吞掉
    # - 孤立 arg_key / arg_value 标签：glm-5.3 函数调用语法残骸
    rest = re.sub(r"</?\s*tool_callback[^>]*>", "", rest)
    rest = re.sub(r"</?\s*arg_(?:key|value)[^>]*>", "", rest)
    rest = re.sub(r"\n{3,}", "\n\n", rest)

    # 兜底：Trae 原生 <tool_name>/<command>（仅当教学格式没解析出任何调用时）
    if not calls:
        # 兜底三：函数调用语法（glm-5.3 实测：无视 JSON 教学，标签内直接写
        # skill_read(intent="...")，常混入孤立 </arg_value> 残骸）
        def _grab_fn_call(match: re.Match) -> str:
            name, body = match.group(1), match.group(2)
            args = _parse_kv_args(body)
            if not args:
                args = {"input": body.strip()}
            calls.append(_mk_tool_call(
                len(calls), name.strip(), json.dumps(args, ensure_ascii=False)))
            return ""

        # ) 与 </tool_call> 之间允许夹孤立残骸标签（</arg_value> 等）
        fn_call_re = re.compile(
            r"<tool_call>\s*([A-Za-z_][\w.]*)\s*\(([\s\S]*?)\)\s*"
            r"(?:</[^>]+>\s*)*</tool_call>",
            re.I,
        )
        rest = fn_call_re.sub(_grab_fn_call, rest)
        if calls:
            return rest.strip(), calls

        def _grab_native(match: re.Match) -> str:
            calls.append(_mk_tool_call(len(calls), match.group(1), json.dumps(
                {"command": match.group(2).strip()}, ensure_ascii=False)))
            return ""

        native_re = re.compile(
            r"<tool_name>\s*([^<\s]+)\s*</tool_name>\s*<command>\s*([\s\S]*?)\s*</command>",
            re.S,
        )
        rest = native_re.sub(_grab_native, rest)

        # 兜底：XML 属性风格 <tool_call name="..." command="..." />
        # （实测 deepseek-v4-pro 偶发输出这种自闭合属性标签）
        rest = _extract_attr_calls(rest, calls)
        rest = re.sub(r"</?tool_action[^>]*>", "", rest)
        rest = re.sub(re.escape(_TC_OPEN) + r"\s*", "", rest)
        rest = re.sub(r"\s*" + re.escape(_TC_CLOSE), "", rest)

    # 兜底二：无标签裸 JSON（仅 agent 请求、且前两种格式都没解析出调用）
    if not calls and known_tools:
        known = frozenset(known_tools)
        # 同一行连续多个裸 JSON（deepseek-v4-pro 实测）：在 } {"name" 边界
        # 插入换行，使后续对象也能被 _BARE_RE 的行首锚点匹配到
        rest = re.sub(r'}\s+(?=\{\s*"name")', '}\n', rest)
        rest_parts2: list[str] = []
        pos = 0
        while True:
            m = _BARE_RE.search(rest, pos)
            if not m:
                rest_parts2.append(rest[pos:])
                break
            js = m.start(1)
            try:
                obj, end = _TOOL_DECODER.raw_decode(rest, js)
            except ValueError:
                rep = _repair_call_json(rest[js:])
                if rep is not None:
                    obj, rend = rep
                    items = obj if isinstance(obj, list) else [obj]
                    if all(_valid_bare_call_obj(it, known) for it in items):
                        rest_parts2.append(rest[pos:m.start()].rstrip())
                        for it in items:
                            nm = it.get("name") or it.get("tool") or ""
                            ag = _flatten_call_args(it)
                            calls.append(_mk_tool_call(
                                len(calls), str(nm),
                                json.dumps(ag, ensure_ascii=False)))
                        pos = js + rend
                        continue
                # 修复失败：name 是已知工具 → 丢弃坏原文（实测坏调用整段
                # 泄进正文纯噪音，agent 侧重试即可）；未知 name 可能是正文
                # 里的普通 JSON，保留原文
                peek = _peek_call_name(rest[js:])
                if peek and peek in known:
                    rest_parts2.append(rest[pos:m.start()].rstrip())
                    end_drop = _find_obj_extent(rest, js)
                    if end_drop == -1:
                        # 边界不明：有界丢弃——最后一个 } 后的尾巴像正文
                        # （短、无 JSON 语法字符）则保留，否则吞到文尾防泄漏
                        seg2 = rest[js:js + 4096]
                        lb = seg2.rfind("}")
                        tail2 = rest[js + lb + 1:] if lb != -1 else ""
                        if lb != -1 and len(tail2) <= 60 and not re.search(
                                r'[{}\[\]":,]|\\', tail2):
                            end_drop = js + lb + 1
                        else:
                            end_drop = len(rest)
                    pos = end_drop
                    continue
                rest_parts2.append(rest[pos:m.end()])
                pos = m.end()
                continue
            items = obj if isinstance(obj, list) else [obj]
            if obj and all(_valid_bare_call_obj(it, known) for it in items):
                rest_parts2.append(rest[pos:m.start()].rstrip())
                for it in items:
                    name = it.get("name") or it.get("tool") or ""
                    args = _flatten_call_args(it)
                    calls.append(_mk_tool_call(
                        len(calls), str(name), json.dumps(args, ensure_ascii=False)))
                pos = end
            else:
                rest_parts2.append(rest[pos:m.end()])
                pos = m.end()
        rest = "".join(rest_parts2)

    # 兜底 2.5：散装键值形态（deepseek-v4-flash 实测 s_20260902_2130_c8bd）：
    # name: "shell" 与 arguments: {...} 散装两行。行首 name + 已知工具名 +
    # arguments: { 的组合在正文里极罕见，误伤风险低
    if not calls and known_tools:
        known = frozenset(known_tools)
        loose_out: list[str] = []
        lpos = 0
        for lm in _LOOSE_KV_RE.finditer(rest):
            if lm.group("name") not in known:
                continue
            j = lm.end() - 1  # 指向 arguments 对象的 '{'
            try:
                args_obj, lend = _TOOL_DECODER.raw_decode(rest, j)
            except ValueError:
                rep = _repair_call_json(rest[j:])
                if rep is None:
                    continue
                args_obj, lend = rep[0], min(j + rep[1], len(rest))
            if not isinstance(args_obj, dict):
                continue
            loose_out.append(rest[lpos:lm.start()])
            calls.append(_mk_tool_call(
                len(calls), lm.group("name"),
                json.dumps(args_obj, ensure_ascii=False)))
            lpos = lend
        if lpos:
            rest = "".join(loose_out) + rest[lpos:]

    # 兜底四：裸函数调用表达式（glm-5.3 实测 Scopus 会话变体：
    # web_search(query="...", max_results=7) web_search(...)</arg_value>
    # ——无 <tool_call> 包裹、连续多个调用 + 孤立 </arg_value> 残骸）。
    # 门槛（防误伤正文里的普通代码示例）：
    # a) 前面所有格式都没解出调用；b) 工具名在请求的 known_tools 里；
    # c) 原文带 arg_* 残骸（坏调用块的强信号），或表达式位于行首
    # （流式 splitter 对行首已知工具名调用会主动扣留，两端约定一致）。
    if not calls and known_tools and (had_arg_debris or _LINE_CALL_EXPR_RE.search(rest)):
        known = frozenset(known_tools)
        exprs = _find_call_exprs(rest, known)
        # 有残骸信号时全部收；没有残骸时只收行首的（inline 的可能是正文示例）
        if not had_arg_debris:
            exprs = [e for e in exprs
                     if e[2] == 0 or rest[e[2] - 1] == "\n"]
        if exprs:
            out: list[str] = []
            pos3 = 0
            for name, args, s, e in exprs:
                out.append(rest[pos3:s])
                calls.append(_mk_tool_call(
                    len(calls), name, json.dumps(args, ensure_ascii=False)))
                pos3 = e
            out.append(rest[pos3:])
            rest = "".join(out)

    # 兜底五：冒号连写形态（glm-5.3 实测：web_searchquery: <自由文本>，
    # 模型省略括号/引号/等号；连续多个时上一个值直接拼到下一个标记前）。
    # 门槛与裸表达式一致：仅在已知工具名、且前面格式都没解出调用时启用；
    # 首个标记必须在行首（splitter 端会主动扣留这类行首前缀）。
    if not calls and known_tools:
        colon_calls = _find_colon_joined_calls(rest, frozenset(known_tools))
        if colon_calls:
            out: list[str] = []
            pos5 = 0
            for name, args, s, e in colon_calls:
                out.append(rest[pos5:s])
                calls.append(_mk_tool_call(
                    len(calls), name, json.dumps(args, ensure_ascii=False)))
                pos5 = e
            out.append(rest[pos5:])
            rest = "".join(out)
            rest = re.sub(r"(?m)^[ \t]*-{3,}[ \t]*\n?", "", rest)

    # glm-5.3 实测：正文带 <think>...</think>\n\n</think>（多一个游离闭标签）。
    # agent 模式不走 _sanitize_agent_leak，think 清洗在这里兜住。
    if "<think>" in rest or "</think>" in rest:
        rest = re.sub(r"<think>.*?</think>", "", rest, flags=re.S)
        rest = re.sub(r"</?\s*think>", "", rest)
        rest = rest.strip()

    # 泄漏闸门（最后一道防线）：所有解析与修复都跑完后，残文里仍有
    # 「调用语法证据 + 已知工具名」的段——说明出现了未知的退化变体或
    # 修复全失败。先给合法对象最后一次补收机会（兜底二/2.5 在已有
    # calls 时不再跑，混合流里后续裸调用会漏到这里）；补收不了则丢弃
    # 该段（坏调用原文对下游是纯噪音，agent 下一轮重试即可），保住前后
    # 正文，并打 WARN——新变体从 proxy.log 搜 [toolcall-gate] 即可自动
    # 发现，不用等用户在聊天里撞见。
    if known_tools:
        known = frozenset(known_tools)
        # 先剥掉孤立 tool_call 标签壳（含损坏形态：开标签断 > 、只剩
        # 半截的），否则「JSON 已被兜底收走、壳留在正文」照样是泄漏。
        # 放在所有兜底之后，不影响 <tool_call> 块的正常解析
        rest = re.sub(r"</?\s*tool_call\b[^>\n]{0,40}>?", "", rest)
        tools_alt = "|".join(sorted((re.escape(t) for t in known), key=len, reverse=True))
        # 候选开头：行首（裸 JSON / 散装 name: / 引号键散装 / 标签族 /
        # 函数表达式）+ 行内强证据（正文与调用连写无换行：必须工具名与
        # arguments 键/参数赋值同现，防止误伤正文里恰好提到工具名的句子）
        gate_re = re.compile(
            r'(?m)^[ \t]*(?:'
            r'\{[\s]*"?name"?[ \t]*:[ \t]*"'
            r'|\{(?=[^\n]{0,80}"arguments")'
            r'|\{[\s]*"name"(?=[^\n]{0,100}(?:' + tools_alt + r'))'
            r'|name"?[ \t]*:[ \t]*"'
            r'|"(?:name|tool)"?[ \t]*:[ \t]*"'
            r'|</?\s*t(?:ool_action|ool_name|ool_callback|ool_call)[>\s]'
            r'|(?P<fnl>[A-Za-z_][\w.]*)[ \t]*\('
            r')'
            r'|\{\s*"?name"?\s*:[ \t]*"(?:' + tools_alt + r')"[^\n]{0,200}?"arguments"'
            r'|name"?\s*:[ \t]*"(?:' + tools_alt + r')"[ \t\r\n]*,?[ \t\r\n]*arguments\s*:'
            r'|\b(?P<fni>(?:' + tools_alt + r'))[ \t]*\([ \t]{0,40}[A-Za-z_]\w*[ \t]*=')
        gate_out: list[str] = []
        gpos = 0
        touched = False
        for gm in gate_re.finditer(rest):
            p = gm.start()
            if p < gpos:
                continue
            seg = rest[p:p + 512]
            fnm = gm.group("fnl") or gm.group("fni")
            if fnm:
                nm = fnm
                if nm not in known:
                    continue
                # 表达式：引号感知扫到行内匹配的 ')'，扫不到取行尾
                end = _gate_expr_end(rest, p)
                # 打捞机会：完整键值参数表达式直接收成调用
                exprs = _find_call_exprs(rest[p:end], known)
                if exprs:
                    gate_out.append(rest[gpos:p])
                    for name, args, _s2, _e2 in exprs:
                        calls.append(_mk_tool_call(
                            len(calls), name, json.dumps(args, ensure_ascii=False)))
                    gpos = end
                    touched = True
                    continue
            else:
                nm = _gate_tool_name(seg) or _gate_word_tool(seg, known)
                if not nm or nm not in known:
                    continue
                b = seg.find("{")
                if b == -1:
                    # 无对象：散装截断（name 行后面 arguments 还没来/坏了）。
                    # 只在「后随 arguments 前缀行」或「name 行已是全文最后一
                    # 行」（流截断）时才丢，避免吞正文里提到工具名的 YAML
                    end = _gate_loose_block_end(rest, p)
                    if end == -1:
                        continue
                else:
                    b_abs = p + b
                    e = _find_obj_extent(rest, b_abs)
                    end = e if e != -1 else len(rest)
                    # 补收机会：对象完整且能解码 → 直接收成调用。
                    # 对象本身带 name 键 = 裸 JSON 本体；不带 = 散装 kv 的
                    # arguments 对象（外层 { 被删的实测变体）。
                    # extent 找不到（结构引号双损）或解码失败 → 修复管线
                    # 再试一次（截断尾流实测能打捞出完整调用）
                    obj = None
                    if e != -1:
                        try:
                            cand, _ = _TOOL_DECODER.raw_decode(rest, b_abs)
                            obj = cand if isinstance(cand, dict) else None
                        except ValueError:
                            rep = _repair_call_json(rest[b_abs:e])
                            if rep is not None and isinstance(rep[0], dict):
                                obj = rep[0]
                    else:
                        # 边界不明（结构/引号双损）：有界丢弃——到段内最后
                        # 一个 } 为止，且其后的尾巴要像正文（短、无 JSON
                        # 语法）才保留，否则吞到文尾防泄漏
                        last_b = seg.rfind("}")
                        tail = rest[p + last_b + 1:] if last_b != -1 else ""
                        if last_b != -1 and len(tail) <= 60 and not re.search(
                                r'[{}\[\]":,]|\\', tail):
                            end = p + last_b + 1
                        else:
                            end = len(rest)
                        rep = _repair_call_json(rest[b_abs:end])
                        if rep is not None and isinstance(rep[0], dict) and (
                                rep[0].get("name") or rep[0].get("tool")
                                or any(k2 not in ("name", "tool") for k2 in rep[0])):
                            obj = rep[0]
                    if obj is not None:
                        call_nm = str(obj.get("name") or obj.get("tool") or nm)
                        if call_nm in known:
                            # 证据已足够强（调用开头 + 已知工具名 + 可解析对象），
                            # 放宽杂键限制——收回比丢弃/泄漏都好
                            ag = _flatten_call_args(obj) if obj.get("name") or obj.get("tool") else obj
                            calls.append(_mk_tool_call(
                                len(calls), call_nm, json.dumps(ag, ensure_ascii=False)))
                            # 打捞与丢弃同样入日志：误收伪调用比泄漏更难排查，
                            # [toolcall-gate] 一个 grep 应看到闸门的全部动作
                            log.warning(
                                "[toolcall-gate] 补收调用残段 tool=%s len=%d 片段=%r",
                                call_nm, end - p, rest[p:p + 80])
                        gate_out.append(rest[gpos:p])
                        gpos = end
                        touched = True
                        continue
                    if e == -1:
                        # 修不动且边界不明：有界丢弃——到段内最后一个 } 或
                        # " 为止，绝不吞后面的正文
                        last_b = max(seg.rfind("}"), seg.rfind('"'))
                        end = p + last_b + 1 if last_b != -1 else min(p + 256, len(rest))
            gate_out.append(rest[gpos:p])
            gpos = end
            touched = True
            log.warning(
                "[toolcall-gate] 丢弃无法解析的调用残段 tool=%s len=%d 片段=%r",
                nm, end - p, rest[p:p + 80])
        if touched:
            gate_out.append(rest[gpos:])
            rest = "".join(gate_out)
            # 丢弃后留下的孤立标签壳 / 参数残骸一并清掉
            rest = re.sub(r"</?\s*(?:tool_call|tool_callback|tool_action"
                          r"|tool_name|arg_key|arg_value)[^>]*>", "", rest)

    return rest.strip(), calls
