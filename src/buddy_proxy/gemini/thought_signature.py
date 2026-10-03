"""thoughtSignature 进程内回环（call id → 签名 + 函数名）。

**背景**：Gemini 3 系对 functionCall part 强制校验 ``thoughtSignature``——
上游返回的签名必须在后续轮次原样带回，缺了直接 400：

    Function call is missing a thought_signature in functionCall parts...

**问题**：签名只有 OpenAI 协议的 ``tool_calls[i].gemini_thought_signature``
字段能藏，而 Anthropic 协议（Claude Code 等客户端）转换 tool_use 块时只保留
id/name/input——未知字段被丢，客户端拿到什么回什么，签名根本到不了回程。

**方案**：两条协议都忠实回传 tool id（Anthropic 的 ``tool_use.id`` ↔
``tool_result.tool_use_id`` ↔ OpenAI ``tool_calls[].id``），所以把 id 当回环
通道：响应方向自造唯一 id 时把签名/函数名记进本模块的进程内 LRU，请求方向
凭 id 取回。2026-10-03 真机实证：上游不校验 id 是否等于它当时发的值
（自造 id 配对也 200），因此自造 id 安全——反而避免了上游 ``call_850216``
式短计数器 id 跨响应撞车串缓存。

**各模型族的完整要求**（同批实证，测试与 README 同口径）：

====================  ==================  ====================  ==========
模型族                 fc/fr id             thoughtSignature      哨兵兼容
====================  ==================  ====================  ==========
gemini-3 系           带上要成对           必带（缺 → 400）       ✓ 200
claude 系             必须成对            不需要（上游也不回）   ✓ 200
gpt-oss               必须成对            不需要                 ✓ 200
====================  ==================  ====================  ==========

**兜底**：签名缺失时（缓存未命中——进程重启、id 被客户端改写——或命中但
为空——历史轮次是 claude/gpt-oss 服务的不带签名）gemini 系注哨兵值
``skip_thought_signature_validator``（实测 200；代价是跳过校验，可能轻微
降质）。注意「缓存命中空签名」也要注：实测 fc 无签名 + gemini-3 → 400，
和未命中一样致命（claude 开局、用户切到 gemini 的对话即此形态）。缓存刻意
不做跨进程持久化——签名是上游加密的思维态，落盘既不安全也没必要。

**附加实测**（2026-10-03，review 自查）：签名不绑定模型（3.1-pro 的签名
在 3.6-flash 上 200）、不绑定账号（账号 A 的签名在账号 B 上 200，failover
期间安全）；但垃圾签名（非哨兵、非有效密文）会 400 ``Corrupted thought
signature``（explicit 透传即此风险，缓存还原的都是上游真签名）。
antigravity 的 2.5 系同样返回签名（140 字节）且容忍哨兵——这也是
``needs_signature`` 匹配全部 gemini 系（而非仅 gemini-3）的实证依据。

缓存条目也存函数名：Anthropic 的 tool_result 不带函数名，过去 functionResponse
的 name 只能拿 tool_call_id 顶替（sanitize 后是个错名字），现在可以还原真名。
"""

from __future__ import annotations

from collections import OrderedDict

#: 哨兵：告知上游跳过签名校验（官方给无状态客户端的逃生门，实测 200）。
SENTINEL = "skip_thought_signature_validator"

#: LRU 上限：实测签名 140B~4KB（gemini-3.1-pro-low 见过 3976B），
#: 2048 条最坏 ~8MB 封顶；对话工具调用数远低于此。
MAX_ENTRIES = 2048

_CACHE: OrderedDict[str, tuple[str, str]] = OrderedDict()


def remember(call_id: str, *, signature: str = "", name: str = "") -> None:
    """记下 ``call_id`` 对应的签名/函数名（响应方向调用）。"""
    if not call_id:
        return
    _CACHE[call_id] = (signature or "", name or "")
    _CACHE.move_to_end(call_id)
    while len(_CACHE) > MAX_ENTRIES:
        _CACHE.popitem(last=False)


def lookup(call_id: str) -> tuple[str, str]:
    """取 ``call_id`` 的 (签名, 函数名)；未命中返回 ("", "")。"""
    if not call_id:
        return "", ""
    entry = _CACHE.get(call_id)
    if entry is None:
        return "", ""
    _CACHE.move_to_end(call_id)
    return entry


def needs_signature(model: str) -> bool:
    """该模型是否可能校验 thoughtSignature。

    覆盖全部 gemini 系：antigravity 通道实测了 3.x（强制校验）；2.5 系
    按 Google 文档（2.5 起引入 thought signatures）保守纳入——本机
    gemini-cli 通道 403 不可用没法实测 2.5，但哨兵兜底只在「返回过
    functionCall 但签名丢了」时生效，而哨兵已被 gemini-3/claude/gpt-oss
    三族实测容忍（最坏是多一个被忽略的字段）。claude/gpt-oss 不校验，
    缓存 miss 时保持请求体干净。
    """
    return "gemini" in (model or "").lower()


def resolve_signature(
    call_id: str, explicit: str = "", *, model: str
) -> str:
    """请求方向定签名：客户端显式回传 > 缓存还原 > gemini-3 系哨兵兜底。

    非 gemini-3 系不需要签名（上游不校验，注了反而多一个未知字段），
    只有显式/缓存命中时才带。
    """
    sig = explicit or (lookup(call_id)[0] if call_id else "")
    if not sig and needs_signature(model):
        return SENTINEL
    return sig
