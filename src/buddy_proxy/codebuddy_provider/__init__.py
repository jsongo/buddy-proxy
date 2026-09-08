"""CodeBuddy 默认上游 provider 及核心转发逻辑。

包含：
- ``CodeBuddyProvider``：默认上游，实现 ``BaseProvider`` 接口。
- ``forward_chat``：多 provider 路由入口。
- ``stream_upstream`` / ``collect_upstream`` / ``convert_nonstream``：
  流式/非流式转发与协议转换（SSE 解析、DSML、工具调用）。

子模块（依赖方向自上而下，禁止反向）：

- ``observability``  指标埋点（_instrument/_metrics_stream）、客户端来源标签与请求/响应日志
- ``pipeline``       SSE 流式/聚合转发与协议转换 + 可选协议适配器（脱敏/投影/双协议）
- ``provider``       CodeBuddyProvider（认证/签到/额度/计费流水）与默认单例
- ``forward``        多 provider 路由入口 forward_chat

兼容约定：历史 ``buddy_proxy.codebuddy_provider.<名字>`` 导入路径全部保持
有效（显式再导出 + ``__getattr__`` 兜底）；``stream_upstream`` /
``collect_upstream`` 等打在包命名空间上的测试 monkeypatch 依然生效
（provider.forward 经包命名空间延迟解析，见 provider.py）。
"""

from __future__ import annotations

from .observability import (  # noqa: F401
    CLIENT_TAG,
    _instrument,
    _metrics_stream,
    body_summary,
    log_client_request,
    log_upstream_request,
    log_upstream_response,
    resolve_client_tag,
)
from .pipeline import (  # noqa: F401
    HAS_ANTHROPIC_ADAPTER,
    HAS_DESENSITIZE,
    HAS_PROJECTION,
    HAS_RESPONSES_ADAPTER,
    AnthropicStreamConverter,
    ResponsesStreamConverter,
    anthropic_to_chat,
    chat_completion_to_anthropic_message,
    collect_upstream,
    convert_nonstream,
    desensitize_body,
    project_responses_chat_body,
    responses_request_to_chat,
    stream_upstream,
)
from .provider import (  # noqa: F401
    CodeBuddyProvider,
    _default_codebuddy,
    _normalize_tool_choice,
)
from .forward import forward_chat  # noqa: F401

_SUBMODULES = ("observability", "pipeline", "provider", "forward")


def __getattr__(name: str):
    """兜底再导出：显式清单之外的历史名字（私有辅助/常量/第三方导入）
    从子模块解析。与 ``buddy_proxy.trae_provider`` / ``trae.text_toolcall``
    的兼容 shim 同一套约定：旧代码对本包任意历史顶层名字的读取不断链。
    """
    import importlib

    for sub in _SUBMODULES:
        mod = importlib.import_module(f"{__package__}.{sub}")
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
