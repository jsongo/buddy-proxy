"""文本协议工具调用解析器：标签/属性/裸 JSON/散装 KV/冒号连写全形态解析。

上游不支持（历史版本头下的实测误判，现作为 glm-5-turbo 等非原生通道模型的
兜底）结构化 tool_calls 时，模型按提示词教学把调用语法输出在正文里；本包
把这些文本形态还原成 OpenAI tool_calls。含 JSON 修复、流式跨 chunk 分片
（_StreamToolCallSplitter）与泄漏闸门。

子模块（依赖方向自上而下，禁止反向）：

- ``exprs``     调用形态提取：函数表达式 / XML 属性标签 / 冒号连写 / 裸 JSON 校验
- ``repair``    坏 JSON 修复管线：非法转义 / 值内未转义引号 / 尾部截断
- ``parse``     统一解析入口 _parse_tool_calls 与泄漏闸门（教学格式 + 全形态兜底）
- ``splitter``  流式扣留切分器 _StreamToolCallSplitter（正文透传、调用块整段扣留）
"""

from __future__ import annotations

from .exprs import _tool_names  # noqa: F401
from .parse import _TC_CLOSE, _TC_OPEN  # noqa: F401
from .parse import _parse_tool_calls  # noqa: F401
from .splitter import _StreamToolCallSplitter  # noqa: F401

_SUBMODULES = ("exprs", "repair", "parse", "splitter")


def __getattr__(name: str):
    """兜底再导出：显式清单之外的历史私有名（常量/辅助函数）从子模块解析。

    与 ``buddy_proxy.trae_provider`` 的兼容 shim 同一套约定：旧代码对
    ``text_toolcall.<任意历史顶层名字>`` 的读取/patch 不断链。
    """
    import importlib

    for sub in _SUBMODULES:
        mod = importlib.import_module(f"{__package__}.{sub}")
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
