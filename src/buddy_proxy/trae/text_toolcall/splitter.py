"""流式工具调用切分器：正文按 chunk 透传，调用块整段扣留到 flush。

各检测器只决定「从哪里开始扣」（_first_tag_pos / _bare_pos / 行首
表达式 / 冒号连写 / 散装 KV / 尾部发展前缀 / 行内强证据），扣留的
缓冲在 flush 时统一交给 parse._parse_tool_calls 解析。
"""

from __future__ import annotations

import re
from typing import Any

from .exprs import (
    _BARE_CANDS,
    _BARE_RE,
    _LINE_CALL_EXPR_RE,
    _colon_marker_re,
)
from .parse import _parse_tool_calls

# 工具调用流式标签（教学格式 + Trae 原生格式的标签名）
# tool_callback：deepseek-v4-flash 实测的伪回调头（开标签 + 意图文本、不闭合），
# 不扣留的话会当正文流出（下游 UI 直接看到 <tool_callback>…）；扣留后交给
# _parse_tool_calls 统一剥离
_TOOL_STREAM_TAGS = ("tool_call", "tool_calls", "tool_action", "tool_name", "tool_callback", "command", "arg_key", "arg_value", "seed:tool_call", "function", "parameter")


def _loose_kv_plausible(seg: str, tools: frozenset[str]) -> bool:
    """seg（从行首 name 起）是否仍是散装调用形态的前缀（含完整形态）。

    形态：?name?[ \t]*: "工具名" <空白/逗号/换行> arguments[ \t]*: {...
    name 键可带引号（实测变体：{"name": "x", "arguments": {...}} 外层
    大括号被删后剩下行首 "name": ...）。逐段消费，任一段与允许的前缀
    不符即发散。工具名已闭合但不在已知列表 → 发散（放行，正文里的
    YAML/示例不吞）。
    """
    seg = seg.lstrip(" \t")
    if not seg:
        return False
    if seg[0] == '"':
        # 引号键：必须是 "name" 的发展前缀（"na / "name / "name" / "name":）
        kw = '"name"'
        if not kw.startswith(seg):
            if not seg.startswith(kw):
                return False
            seg = seg[len(kw):]
        else:
            return True
    else:
        if not seg.startswith("name"):
            return bool(seg) and "name".startswith(seg)
        seg = seg[4:]
        if seg.startswith('"'):
            seg = seg[1:]        # name": 形态（外层 { 被删的损坏变体）
    n = len(seg)

    def eat_ws(j: int) -> int:
        while j < n and seg[j] in " \t":
            j += 1
        return j

    i = eat_ws(0)
    if i >= n:
        return True          # 冒号还在路上
    if seg[i] != ":":
        return False
    i = eat_ws(i + 1)
    if i >= n:
        return True
    if seg[i] != '"':
        return False
    q = seg.find('"', i + 1)
    if q == -1:
        return "\n" not in seg[i:]   # 工具名未闭合：跨行即发散
    name = seg[i + 1:q]
    if not name or name not in tools:
        return False
    # 工具名之后：空白/逗号/换行 + arguments 关键字（带不带引号都认）
    k = q + 1
    while k < n and seg[k] in " \t\r\n,":
        k += 1
    if k >= n:
        return True          # 等待 arguments
    if seg[k] == '"':
        k += 1
        if k >= n:
            return True
    if not seg.startswith("arguments", k):
        return "arguments".startswith(seg[k:])
    k += len("arguments")
    if k < n and seg[k] == '"':
        k += 1
    k = eat_ws(k)
    if k >= n:
        return True
    if seg[k] != ":":
        return False
    k = eat_ws(k + 1)
    if k >= n:
        return True
    return seg[k] == "{"     # 对象已开：扣到 flush 统一解析


class _StreamToolCallSplitter:
    """流式工具调用切分器：正文按 chunk 透传，工具调用块整段扣留。

    调用块（无论是否已闭合）都要在 flush 时统一解析成 OpenAI tool_calls，
    不能当正文放行——所以从首个疑似标签起全部扣留（与泄漏清洗器的
    "闭合即放行"语义不同）。

    known_tools：请求携带的工具名集合。传入后额外扣留「行首裸 JSON」候选
    （模型偶尔省略标签、直接输出 {"name": ...} 裸调用，实测 deepseek-v4-pro
    连续多轮调用后会出现这种偷懒写法）。
    """

    def __init__(self, known_tools: frozenset[str] | None = None) -> None:
        self._buf = ""
        self._tools = frozenset(known_tools) if known_tools else None
        self._bare_hold = None

    @staticmethod
    def _first_tag_pos(buf: str) -> int:
        low = buf.lower()
        best = -1
        for tag in _TOOL_STREAM_TAGS:
            start = 0
            # 开标签 + 闭标签都要扣（</arg_value> 类闭合残骸实测会夹正文流出）
            for opener in ("<" + tag, "</" + tag):
                while True:
                    i = low.find(opener, start)
                    if i == -1:
                        break
                    after = low[i + len(opener): i + len(opener) + 1]
                    if after in ("", ">", "/", " ", "\t", "\n"):
                        if best == -1 or i < best:
                            best = i
                        break
                    start = i + 1
        # 部分标签前缀（跨 chunk 分裂 / 标签名中间被塞了残骸标签）：取最早
        # 的未闭合 "<"。片段取到下一个 "<" 或 ">" 为止——用 rfind 取最后
        # 一个 "<" 会把前面的半截标签（如 "<tool_c<tool_callback>all>"
        # 里的 "<tool_c"）当安全文本放出去
        j = low.find("<")
        while j != -1:
            nxt_lt = low.find("<", j + 1)
            frag = buf[j + 1: nxt_lt if nxt_lt != -1 else len(buf)]
            if ">" in frag:
                if nxt_lt == -1:
                    break
                j = nxt_lt
                continue
            partial = frag.lower().lstrip("/ \t")
            if any(t.startswith(partial) for t in _TOOL_STREAM_TAGS):
                if best == -1 or j < best:
                    best = j
            break
        return best

    def _bare_pos(self, buf: str) -> int:
        """行首裸 JSON 调用候选位置（含跨 chunk 分裂的前缀）。"""
        if not self._tools:
            return -1
        best = -1
        m = _BARE_RE.search(buf)
        if m:
            best = m.start(1)
        # 尾部行可能是分裂中的候选前缀（如 buf 以 '{"na' 结尾）；整候选开头
        # （如 '{"name"' 恰好等于尾行）也扣——flush 端 _BARE_RE 能处理
        last_nl = buf.rfind("\n")
        line = buf[last_nl + 1:]
        ls = line.lstrip()
        if ls and any(c.startswith(ls) or ls.startswith(c) for c in _BARE_CANDS):
            p = last_nl + 1 + (len(line) - len(ls))
            if best == -1 or p < best:
                best = p
        return best

    def _loose_kv_pos(self, buf: str) -> int:
        """行首散装 name:/arguments: 调用的扣留位置（deepseek-v4-flash 实测）。

        形态：name: "shell"
              arguments: {"command": ...}
        外层大括号与标签全省；name 键可带引号（外层 { 被删的损坏变体）。
        行首 name 且后续与该形态前缀吻合时扣留；发散（name 非已知工具、
        后续不是 arguments:、工具名跨行未闭合）立即返回 -1 放行，
        误伤窗口只有一两行。
        """
        if not self._tools:
            return -1
        for m in re.finditer(r"(?m)^[ \t]*\"?name\b", buf):
            # 限窗切片：plausible 只消费形态前缀，无需 buf 全尾（无界切片
            # 在多 name 行的大 payload 下是二次方放大，实测 69KB/2000 行 870ms）
            if _loose_kv_plausible(buf[m.start():m.start() + 2048], self._tools):
                return m.start()
        # 尾部行可能是发展中的 name 前缀（跨 chunk 分裂，如 buf 以 'na' 结尾）
        last_nl = buf.rfind("\n")
        line = buf[last_nl + 1:]
        ls = line.lstrip()
        if ls and len(ls) <= len('"name"') and (
                '"name"'.startswith(ls) or "name".startswith(ls)):
            return last_nl + 1 + (len(line) - len(ls))
        return -1

    def _inline_intent_pos(self, buf: str) -> int:
        """行内强证据调用的扣留位置（正文与调用连写无换行的变体）。

        与闸门的行内模式一致：已知工具名 + arguments 键 / 参数赋值同现，
        单独出现工具名不扣（正文里提到工具名很常见）。
        """
        if not self._tools:
            return -1
        if not hasattr(self, "_inline_re"):
            # tools_alt 与 _tail_hold_pos 共用惰性缓存（避免每 chunk 重拼正则串）
            tools_alt = getattr(self, "_tools_alt", None)
            if not tools_alt:
                tools_alt = "|".join(
                    sorted((re.escape(t) for t in self._tools), key=len, reverse=True))
                self._tools_alt = tools_alt
            self._inline_re = re.compile(
                r'\{\s*"?name"?\s*:[ \t]*"(?:' + tools_alt + r')"[^\n]{0,200}?"arguments"'
                r'|name\s*:\s*"(?:' + tools_alt + r')"[ \t\r\n]*,?[ \t\r\n]*arguments\s*:'
                r'|\b(?:' + tools_alt + r')[ \t]*\([ \t]{0,40}[A-Za-z_]\w*[ \t]*=')
        m = self._inline_re.search(buf)
        return m.start() if m else -1

    def _call_expr_pos(self, buf: str) -> int:
        """行首已知工具名调用表达式的扣留位置（glm-5.3 裸函数语法变体）。

        模型偶尔连 <tool_call> 标签都省掉，直接在行首输出
        web_search(query="...", ...)（可连续多个，尾随孤立 </arg_value>）。
        行首 + 已知工具名 + 紧跟 "(" 的组合在正文里极其罕见，值得扣留。
        含跨 chunk 分裂的前缀（尾行是纯工具名前缀、可能 ( 在下一个 chunk）。
        """
        if not self._tools:
            return -1
        best = -1
        for m in _LINE_CALL_EXPR_RE.finditer(buf):
            if m.group(1) in self._tools:
                if best == -1 or m.start() < best:
                    best = m.start()
        # 尾部行可能是分裂中的候选前缀（如 buf 以行首 'web_sear' 结尾）
        last_nl = buf.rfind("\n")
        line = buf[last_nl + 1:]
        ls = line.lstrip()
        if ls and re.fullmatch(r"[A-Za-z_][\w.]*", ls) and len(ls) <= max(
                len(t) for t in self._tools):
            if any(t.startswith(ls) for t in self._tools):
                p = last_nl + 1 + (len(line) - len(ls))
                if best == -1 or p < best:
                    best = p
        return best

    def _colon_pos(self, buf: str) -> int:
        """行首冒号连写调用的扣留位置（glm-5.3 web_searchquery: 变体）。

        完整标记 web_searchquery: 直接正则定位；跨 chunk 分裂的前缀
        （web_searchqu / web_searchquery / web_searchquery:）用尾行
        前缀匹配扣住。
        """
        if not self._tools:
            return -1
        if not hasattr(self, "_colon_re"):
            self._colon_re = _colon_marker_re(self._tools)
        best = -1
        m = self._colon_re.search(buf)
        if m:
            best = m.start("tool")
        last_nl = buf.rfind("\n")
        line = buf[last_nl + 1:]
        ls = line.lstrip()
        if ls:
            for t in self._tools:
                if ls.startswith(t):
                    tail = ls[len(t):]
                    if tail and re.fullmatch(r"[a-z0-9_]*:?", tail):
                        p = last_nl + 1 + (len(line) - len(ls))
                        if best == -1 or p < best:
                            best = p
                        break
        return best

    # 「发展中前缀」三正则天然只关心尾部（合法匹配最长 ~250 字符），限定
    # 窗口防止每 chunk 对全量 buffer 回溯——实测不限定时二次方放大
    # （500KB 扣留 payload 流式路径 105s CPU，200KB 15.7s）。
    _TAIL_WINDOW = 512

    def _tail_hold_pos(self, buf: str) -> int:
        """尾部发展中候选的扣留位置（行内调用证据天然跨 chunk）。

        正文与调用连写（无换行）时，「{"name": "tool"...arguments」这类
        证据要若干 chunk 才凑齐——按完整证据扣留会先把开头当正文放出。
        这里检查缓冲区尾部是否仍是某个调用形态的"发展前缀"：是则从候选
        起点扣住，下一个 chunk 发散（变成普通正文）立即释放；到 flush 还
        扣着就交给统一解析/闸门。尾部有界（几十~几百字符），误扣不会
        无限拖延输出；到 flush 仍未发散的尾部由解析/闸门统一处置。
        """
        if not self._tools:
            return -1
        if not hasattr(self, "_tail_res"):
            tools_alt = "|".join(
                sorted((re.escape(t) for t in self._tools), key=len, reverse=True))
            # arguments 的逐字符可选发展：a(?:r(?:g(?:...(?:s)?)?)...)?
            arg_dev = "a" + "".join("(?:%s" % ch for ch in "rguments") + ")?" * 8
            self._tools_alt = tools_alt
            self._tail_res = (
                # {"name 逐字符发展（值可有可无——{ 一出现就扣，发散即放）
                re.compile(r'\{\s*"?\s*(?:n(?:a(?:m(?:e)?)?)?)?\s*"?\s*'
                           r':?[ \t]*"?(?:[A-Za-z_][\w.\-]{0,40})?"?$'),
                # name: "tool" [\\s,]* arguments 发展（值可有可无——
                # name: 一出现就扣，发散即放）
                re.compile(r'\bname"?\s*:[ \t]*"?(?:[A-Za-z_][\w.\-]{0,40})?"?'
                           r'[ \t\r\n,]*(?:' + arg_dev + r')?\s*:?\s*\{?$'),
                # 已知工具名 + ( 参数发展
                re.compile(r'\b(?:' + tools_alt + r')\s*\(\s*[^)\n]{0,200}$'),
            )
            self._bare_anchor_re = re.compile(
                r'\{\s*"?name"?\s*:\s*"?(?:' + tools_alt + r')')
        best = -1
        # 尾部正则只在窗口内搜（$ 锚定，窗口外命中不可能也不需要）
        offset = max(0, len(buf) - self._TAIL_WINDOW)
        tail = buf[offset:]
        for pat in self._tail_res:
            m = pat.search(tail)
            if m:
                p = offset + m.start()
                if best == -1 or p < best:
                    best = p
        # 裸 JSON 参数区深度扣留（不限长，大 payload 的 content 可达数百 KB）：
        # {"name": "已知工具" 前缀一旦出现，扣住直到参数区闭合或流结束。
        # 增量扫描版：锚点只搜一次，之后每 chunk 只扫新增尾部
        bp = self._bare_hold_pos(buf)
        if bp != -1 and (best == -1 or bp < best):
            best = bp
        # 工具名前缀尾部（含单字符、完整名）：tool( 表达式跨 chunk 发展；
        # 完整名后跟非 ( 时下一步即发散，误扣窗口一两个 chunk
        m2 = re.search(r'[A-Za-z_][\w.]{0,31}$', tail)
        if m2 and any(t.startswith(m2.group(0)) for t in self._tools):
            p = offset + m2.start()
            if best == -1 or p < best:
                best = p
        return best

    def _bare_hold_pos(self, buf: str) -> int:
        """裸 JSON 扣留的增量扫描实现（替代每 chunk 全量 anchor+extent）。

        状态（_bare_hold）以 buf 绝对坐标保存：扣留期间 feed 只会从 pos≤at
        处切片（at 是本检测器的扣留点，feed 取各检测器最小值），锚点之前
        不会被放出，因此坐标只需在 feed 切片时整体平移（_shift_bare_hold）。
        闭合释放后进入 closed 态：锚点只在新增尾部（scan 之后）重搜，避免
        对已闭合区域反复全量扫描。
        """
        st = self._bare_hold
        if st is not None and st.get("closed"):
            m = self._bare_anchor_re.search(buf, st["scan"])
            if not m:
                st["scan"] = len(buf)
                return -1
            st = self._bare_hold = {
                "at": m.start(), "b": -1, "scan": m.end(),
                "depth": 0, "instr": False, "closed": False,
            }
        elif st is None:
            m = self._bare_anchor_re.search(buf)
            if not m:
                return -1
            st = self._bare_hold = {
                "at": m.start(), "b": -1, "scan": m.end(),
                "depth": 0, "instr": False, "closed": False,
            }
        at = st["at"]
        i, n = st["scan"], len(buf)
        depth, instr, b = st["depth"], st["instr"], st["b"]
        while i < n:
            ch = buf[i]
            if b == -1:
                # arguments 对象的 '{' 还没出现
                if ch == "{":
                    b, depth = i, 1
                i += 1
                continue
            if instr:
                if ch == "\\":
                    if i + 1 >= n:
                        # 反斜杠是本 buffer 最后一字符：转义对跨 chunk 分裂，
                        # scan 原地停在反斜杠处，下 chunk 重读后再跳过整对
                        break
                    i += 2
                    continue
                if ch == '"':
                    instr = False
            elif ch == '"':
                instr = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth <= 0:
                    # 对象闭合：释放扣留（闭合的完整对象交给正常解析路径）
                    st.update(scan=i + 1, depth=depth, instr=instr, b=b, closed=True)
                    return -1
            i += 1
        st.update(scan=i, depth=depth, instr=instr, b=b)
        return at

    def _shift_bare_hold(self, pos: int, reset: bool) -> None:
        """feed() 切片后平移裸扣留状态的 buf 坐标；buffer 重置时清状态。"""
        if reset or pos <= 0 or not self._bare_hold:
            if reset:
                self._bare_hold = None
            return
        st = self._bare_hold
        st["at"] -= pos
        st["scan"] -= pos
        if st["b"] != -1:
            st["b"] -= pos
        if st["at"] < 0:  # 防御：正常不会发生（pos ≤ at）
            self._bare_hold = None

    def feed(self, text: str) -> str:
        self._buf += text
        pos = self._first_tag_pos(self._buf)
        if self._tools:
            bp = self._bare_pos(self._buf)
            if bp != -1 and (pos == -1 or bp < pos):
                pos = bp
            ep = self._call_expr_pos(self._buf)
            if ep != -1 and (pos == -1 or ep < pos):
                pos = ep
            cp = self._colon_pos(self._buf)
            if cp != -1 and (pos == -1 or cp < pos):
                pos = cp
            lp = self._loose_kv_pos(self._buf)
            if lp != -1 and (pos == -1 or lp < pos):
                pos = lp
            tp = self._tail_hold_pos(self._buf)
            if tp != -1 and (pos == -1 or tp < pos):
                pos = tp
            ip = self._inline_intent_pos(self._buf)
            if ip != -1 and (pos == -1 or ip < pos):
                pos = ip
        if pos == -1:
            safe, self._buf = self._buf, ""
            self._shift_bare_hold(0, reset=True)
        else:
            safe, self._buf = self._buf[:pos], self._buf[pos:]
            self._shift_bare_hold(pos, reset=False)
        return safe

    def flush(self) -> tuple[str, list[dict[str, Any]]]:
        rest, calls = _parse_tool_calls(self._buf, self._tools)
        self._buf = ""
        self._bare_hold = None
        return rest, calls
