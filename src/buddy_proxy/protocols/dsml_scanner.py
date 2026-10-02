"""DSML 工具标记的底层扫描原语（忽略区域 / fence / 标签定位）。

2026-10 自 ``dsml_parser.py`` 拆出：这里只做**文本层**扫描（字符位置、
CDATA/注释/fence 跳越、标签定位、属性解析），不产出工具调用；解析层
（``parse_tool_calls`` 等）仍在 ``dsml_parser.py``，旧路径对以下名字
保持 re-export 兼容。
"""

import html
import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class ToolMarkupTag:
    """工具标记标签"""
    start: int
    end: int
    name_start: int
    name_end: int
    name: str
    closing: bool
    self_closing: bool
    dsml_like: bool
    canonical: bool
    attributes: str = ""


# ============================================================================
# 常量定义
# ============================================================================

DSML_MARKER = "｜｜DSML｜｜"
DSML_VARIANTS = ["｜｜DSML｜｜", "||DSML||", "|DSML|"]

TOOL_MARKUP_NAMES = [
    ("tool_calls", "tool_calls", False),
    ("tool-calls", "tool_calls", True),
    ("toolcalls", "tool_calls", True),
    # deepseek 等模型会把包装标签写成 <｜｜DSML｜｜ calls>（缺 tool_ 前缀）。
    # dsml_only=False（裸 <calls> 也扣留）是刻意取舍：漏检 = 标记原文泄漏到
    # 客户端（更糟），多扣留几行 prose 到 flush 只是延迟，内容不丢
    ("calls", "tool_calls", False),
    ("invoke", "invoke", False),
    ("parameter", "parameter", False),
]

FENCE_MARKERS = ["```", "~~~"]
CDATA_START = "<![CDATA["
CDATA_END = "]]>"
COMMENT_START = "<!--"
COMMENT_END = "-->"


# ============================================================================
# 忽略区域检测
# ============================================================================

def skip_xml_ignored_section(text: str, i: int) -> Tuple[int, bool, bool]:
    """
    跳过 XML 忽略区域（CDATA、注释、处理指令）
    返回: (next_position, advanced, blocked)
    """
    if i >= len(text):
        return i, False, False
    
    if text[i:i+9] == CDATA_START:
        end = text.find(CDATA_END, i + 9)
        if end == -1:
            return len(text), False, True
        return end + 3, True, False
    
    if text[i:i+4] == COMMENT_START:
        end = text.find(COMMENT_END, i + 4)
        if end == -1:
            return len(text), False, True
        return end + 3, True, False
    
    if i + 1 < len(text) and text[i:i+2] == "<?":
        end = text.find("?>", i + 2)
        if end == -1:
            return len(text), False, True
        return end + 2, True, False
    
    return i, False, False


def markdown_code_span_end(text: str, start: int) -> Tuple[int, bool]:
    """检查是否是内联代码开始，如果是则找到结束位置"""
    if start >= len(text) or text[start] != '`':
        return start, False
    
    tick_count = 0
    i = start
    while i < len(text) and text[i] == '`':
        tick_count += 1
        i += 1
    
    if tick_count >= 3:
        return start, False
    
    end = i
    while end < len(text):
        if text[end] == '`':
            end_tick_count = 0
            j = end
            while j < len(text) and text[j] == '`':
                end_tick_count += 1
                j += 1
            
            if end_tick_count == tick_count:
                return j, True
            
            end = j
        else:
            end += 1
    
    return len(text), False


def last_unclosed_code_span(text: str) -> int:
    """返回最后一个未闭合的单/双反引号 code span 起始位置；无则 -1。

    流式场景下，内联代码 `` `<tool_calls>` `` 的反引号与标签会跨 chunk 到达，
    需要在标签完整前保留开头的反引号，否则 buffer 会丢失「处于代码内」的上下文，
    从而把 `<tool_calls>` 误判为工具调用开标签。
    """
    last = -1
    i = 0
    while i < len(text):
        if text[i] != '`':
            i += 1
            continue
        tick_count = 0
        j = i
        while j < len(text) and text[j] == '`':
            tick_count += 1
            j += 1
        if tick_count >= 3:
            i = j  # 三反引号 fence，交给 is_inside_markdown_fence 处理
            continue
        end, found = markdown_code_span_end(text, i)
        if found:
            i = end
            continue
        last = i  # 未闭合的单/双反引号
        i = j
    return last


def is_inside_markdown_fence(text: str, pos: int) -> bool:
    """检查位置是否在 Markdown fence 块内"""
    fence_depth = 0
    current_fence = None
    i = 0
    
    while i < pos:
        if i == 0 or text[i-1] == '\n':
            for marker in FENCE_MARKERS:
                if text[i:i+len(marker)] == marker:
                    if current_fence is None:
                        current_fence = marker
                        fence_depth += 1
                        line_end = text.find('\n', i)
                        if line_end == -1:
                            i = len(text)
                        else:
                            i = line_end + 1
                        break
                    elif text[i:i+len(current_fence)] == current_fence:
                        fence_depth -= 1
                        if fence_depth == 0:
                            current_fence = None
                        i = text.find('\n', i)
                        if i == -1:
                            i = len(text)
                        else:
                            i += 1
                        break
            else:
                i += 1
        else:
            i += 1

    return fence_depth > 0


class MarkdownFenceTracker:
    """按位置单调推进的 fence 状态跟踪器。

    语义等价于反复调用 ``is_inside_markdown_fence(text, pos)``，但整段只线性
    扫一遍。``find_tool_markup_tag_outside_ignored`` 会对**每个字符位置**调用
    一次 ``is_inside_markdown_fence``，而后者每次都从 0 重扫到 pos，整体退化
    为 O(n²)：实测 19KB 需 17s、2.4KB 需 0.26s（长度翻倍耗时 ×4）。大 payload
    会把 uvicorn 事件循环卡到 100% CPU 长达数分钟，表现为整个代理无响应。

    仅当 pos 单调不减时使用；pos 回退时自动退回原函数保证语义不变。
    """

    __slots__ = ("_text", "_i", "_depth", "_current")

    def __init__(self, text: str) -> None:
        self._text = text
        self._i = 0
        self._depth = 0
        self._current: Optional[str] = None

    def inside_at(self, pos: int) -> bool:
        if pos < self._i:
            # 位置回退：缓存已越过该点，退回全量重扫以保证结果正确。
            return is_inside_markdown_fence(self._text, pos)

        text = self._text
        i = self._i
        depth = self._depth
        current = self._current

        while i < pos:
            if i == 0 or text[i-1] == '\n':
                for marker in FENCE_MARKERS:
                    if text[i:i+len(marker)] == marker:
                        if current is None:
                            current = marker
                            depth += 1
                            line_end = text.find('\n', i)
                            if line_end == -1:
                                i = len(text)
                            else:
                                i = line_end + 1
                            break
                        elif text[i:i+len(current)] == current:
                            depth -= 1
                            if depth == 0:
                                current = None
                            i = text.find('\n', i)
                            if i == -1:
                                i = len(text)
                            else:
                                i += 1
                            break
                else:
                    i += 1
            else:
                i += 1

        self._i = i
        self._depth = depth
        self._current = current
        return depth > 0


# ============================================================================
# 标签扫描
# ============================================================================

def normalize_fullwidth_ascii(text: str, start: int) -> Tuple[str, int]:
    """标准化全角 ASCII 字符"""
    if start >= len(text):
        return "", 0
    
    ch = text[start]
    code = ord(ch)
    
    if 0xFF01 <= code <= 0xFF5E:
        normalized = chr(code - 0xFEE0)
        return normalized, 1
    
    return ch, 1


def has_dsml_prefix_at(text: str, start: int) -> bool:
    """检查位置是否是 DSML 前缀"""
    for variant in DSML_VARIANTS:
        if text[start:start+len(variant)] == variant:
            return True
    
    if start + 8 < len(text):
        normalized = ""
        pos = start
        for _ in range(8):
            ch, length = normalize_fullwidth_ascii(text, pos)
            normalized += ch
            pos += length
        
        if normalized in DSML_VARIANTS:
            return True
    
    return False


def consume_dsml_prefix(text: str, idx: int) -> Tuple[int, bool]:
    """
    消费 DSML 前缀
    返回: (next_position, found)
    """
    for variant in DSML_VARIANTS:
        if text[idx:idx+len(variant)] == variant:
            return idx + len(variant), True
    
    if idx + 8 < len(text):
        normalized = ""
        pos = idx
        for _ in range(8):
            ch, length = normalize_fullwidth_ascii(text, pos)
            normalized += ch
            pos += length
        
        if normalized in DSML_VARIANTS:
            return pos, True
    
    return idx, False


def match_tool_markup_name(text: str, start: int) -> Tuple[str, int, bool]:
    """
    匹配工具标记名称（✅ 修复：支持任意标签名）
    
    返回: (canonical_name, end_position, is_dsml_like)
    """
    dsml_like = False
    idx = start
    
    if has_dsml_prefix_at(text, idx):
        idx, _ = consume_dsml_prefix(text, idx)
        dsml_like = True
        # 模型常在前缀与标签名之间插空格（<｜｜DSML｜｜ calls>）。
        # 不跳过会导致标签名解析为空 → 整段标记当普通文本泄漏给客户端。
        while idx < len(text) and text[idx] in (" ", "\t", "\r", "\n", "　", "\xa0"):
            idx += 1

    name_start = idx
    name_end = idx
    
    while name_end < len(text):
        ch = text[name_end]
        if ch.isalnum() or ch in ('_', '-'):
            name_end += 1
        else:
            break
    
    if name_end == name_start:
        return "", start, False
    
    raw_name = text[name_start:name_end].lower()
    
    # 查找标准化名称（特殊标签）
    for raw, canonical, dsml_only in TOOL_MARKUP_NAMES:
        if raw_name == raw:
            if dsml_only and not dsml_like:
                continue
            return canonical, name_end, dsml_like
    
    # ✅ 关键修复：任意其他标签名也接受（用于参数标签如 <cmd>, <path> 等）
    return raw_name, name_end, dsml_like


def scan_tool_markup_tag_at(text: str, start: int) -> Tuple[Optional[ToolMarkupTag], bool]:
    """在指定位置扫描工具标记标签"""
    if start >= len(text) or text[start] != '<':
        return None, False
    
    idx = start + 1
    
    closing = False
    if idx < len(text) and text[idx] == '/':
        closing = True
        idx += 1
    
    while idx < len(text) and text[idx] in (' ', '\t', '\r', '\n'):
        idx += 1
    
    if idx >= len(text):
        return None, False
    
    name_start = idx
    canonical_name, name_end, dsml_like = match_tool_markup_name(text, idx)
    
    if not canonical_name:
        return None, False
    
    idx = name_end
    attr_start = idx
    self_closing = False
    
    while idx < len(text):
        ch = text[idx]
        
        if ch == '>':
            attrs = text[attr_start:idx].strip()
            return ToolMarkupTag(
                start=start,
                end=idx,
                name_start=name_start,
                name_end=name_end,
                name=canonical_name,
                closing=closing,
                self_closing=self_closing,
                dsml_like=dsml_like,
                canonical=not dsml_like,
                attributes=attrs
            ), True
        
        if ch == '/' and idx + 1 < len(text) and text[idx + 1] == '>':
            self_closing = True
            attrs = text[attr_start:idx].strip()
            return ToolMarkupTag(
                start=start,
                end=idx + 1,
                name_start=name_start,
                name_end=name_end,
                name=canonical_name,
                closing=closing,
                self_closing=self_closing,
                dsml_like=dsml_like,
                canonical=not dsml_like,
                attributes=attrs
            ), True
        
        idx += 1
    
    return None, False


def find_tool_markup_tag_outside_ignored(text: str, start: int) -> Tuple[Optional[ToolMarkupTag], bool]:
    """从指定位置开始查找下一个工具标记标签（跳过忽略区域）"""
    i = max(start, 0)
    # i 在本函数内单调不减，用跟踪器替代逐位置调用 is_inside_markdown_fence
    # （后者每次都从 0 重扫，整体 O(n²)——见 MarkdownFenceTracker 文档）。
    fence = MarkdownFenceTracker(text)

    while i < len(text):
        next_pos, advanced, blocked = skip_xml_ignored_section(text, i)
        if blocked:
            return None, False
        if advanced:
            i = next_pos
            continue
        
        if text[i] == '`':
            end, found = markdown_code_span_end(text, i)
            if found:
                i = end
                continue
            elif end == len(text) and not fence.inside_at(i):
                # 未闭合的单/双反引号 code span（流式下闭合符尚未到达）：
                # 剩余内容视为代码，不再识别工具调用标签，避免把内联代码里的
                # `<tool_calls>` 误判为工具调用开标签而被扣留到流末尾。
                # 注意排除 fence 内的反引号（fence 由 MarkdownFenceTracker 处理）。
                return None, False
            # end == start（三反引号 fence 首字节）或处于 fence 内：交给 fence 跟踪器处理

        if fence.inside_at(i):
            line_end = text.find('\n', i)
            if line_end == -1:
                return None, False
            i = line_end + 1
            continue
        
        tag, found = scan_tool_markup_tag_at(text, i)
        if found:
            return tag, True
        
        i += 1
    
    return None, False


def find_matching_tool_markup_close(text: str, open_tag: ToolMarkupTag) -> Tuple[Optional[ToolMarkupTag], bool]:
    """查找匹配的闭标签"""
    depth = 1
    i = open_tag.end + 1
    
    while i < len(text):
        tag, found = find_tool_markup_tag_outside_ignored(text, i)
        if not found:
            break
        
        if tag.name == open_tag.name:
            if tag.closing:
                depth -= 1
                if depth == 0:
                    return tag, True
            else:
                depth += 1
        
        i = tag.end + 1
    
    return None, False


# ============================================================================
# 属性解析
# ============================================================================

def parse_xml_attributes(attrs_text: str) -> Dict[str, str]:
    """解析 XML 属性"""
    attrs = {}
    # 修复正则表达式以支持 - 和 :
    pattern = r'([a-z0-9_:-]+)\s*=\s*["\']([^"\']*)["\']'
    
    for match in re.finditer(pattern, attrs_text, re.IGNORECASE):
        key = match.group(1)
        value = match.group(2)
        attrs[key] = html.unescape(value)
    
    return attrs

