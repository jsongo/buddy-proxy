"""异常链工具：判断「是不是本机 DNS 故障」与「取出可读的错误描述」。

两者都建立在同一个观察上：**异常的真因常常不在顶层对象里**，而在
``__cause__`` / ``__context__`` 链的下层。两条独立的成因都指向这里：

1. **本机 DNS 故障被误判成上游故障**（2026-10-02 事故）。一次约 1 秒的本机 DNS
   抖动让三个候选通道同时被打上 5 分钟冷却，整个模型锁死 5 分钟——期间客户端
   每几秒重试，全部命中 ``model_order_skip reason=cooldown``，一个上游请求都没
   发出去。而 DNS 是**机器级**故障：对所有通道同时生效、通常秒级自愈，不该按
   「某个上游病了」处理。
2. **错误消息读出来是空的**。httpcore 把底层异常映射成 ``httpx.ReadError()``
   这类**自身 str() 为空**的对象，于是 ``log.warning("... error: %s", exc)``
   打出来只有前缀、没有内容，真因（在下层）完全看不见。

本模块是**叶子**模块：只 import 标准库，从任何地方 import 都不会造成循环导入
（同 ``core/cooldown.py`` 的约定，见 ``docs/architecture.md``）。
"""

from __future__ import annotations

import socket

#: 沿异常链下钻的最大层数。真实链路深度只有 3~4（见下），设上限纯属防御
#: 环状 ``__context__``（理论上异常链不会成环，但这里不值得为它冒险死循环）。
_MAX_CHAIN_DEPTH = 8


def _walk(exc: BaseException | None):
    """逐个产出异常链上的异常（顶层 → ``__cause__``/``__context__`` 下层）。"""
    cur = exc
    for _ in range(_MAX_CHAIN_DEPTH):
        if cur is None:
            return
        yield cur
        cur = cur.__cause__ or cur.__context__


def is_local_dns_failure(exc: BaseException | None) -> bool:
    """该异常是否由**本机域名解析失败**引起（而非某个上游自己的毛病）。

    供换档逻辑决定「要不要打冷却标记」：解析失败时**不打**（见
    :func:`buddy_proxy.codebuddy_provider.forward._forward_with_order`）。

    识别范围刻意窄到**只有解析失败**：连接被拒/超时仍照常冷却，那些确实更可能
    是特定上游的状态。

    异常形态实测（httpx 0.28 / httpcore 1.0.9，无代理直连）::

        httpx.ConnectError            (str: '[Errno 8] nodename nor servname ...')
          └─ httpcore.ConnectError    (同一消息)
               └─ socket.gaierror     (errno 8)

    **关键**：各 provider 会先把这类异常转成 ``HTTPException(502)`` 再抛给换档层，
    所以调用方拿到的往往是 ``HTTPException`` 而非 ``httpx.ConnectError``。好在转换
    保留了 ``__cause__`` 链（``raise ... from exc``，或 Python 的隐式 ``__context__``），
    因此这里**沿链逐层下钻**——只判顶层会被 provider 边界挡掉（实测确认过：
    按顶层类型判断的修法对本次事故完全无效）。
    """
    for cur in _walk(exc):
        if isinstance(cur, socket.gaierror):
            return True
        # 兜底：某些包装层把 gaierror 压成普通 OSError，但 errno 还留着
        if isinstance(cur, OSError) and cur.errno == getattr(socket, "EAI_NONAME", -2):
            return True
    return False


def describe_exception(exc: BaseException | None, *, max_len: int = 300) -> str:
    """把异常渲染成**一定能看出真因**的一行文本。

    顶层 ``str()`` 为空时（``httpx.ReadError()`` 这类），退回类型名并继续沿链
    找第一个有内容的下层异常，形如::

        ReadError (caused by socket.gaierror: [Errno 8] nodename nor servname ...)

    顶层与下层都有内容时只给顶层（避免日志噪音）：最常见的
    ``HTTPException`` ``str()`` 已经带了完整信息，没必要把整条链都铺开。

    截断到 ``max_len``，与各 provider 既有的 ``[:500]`` / ``[:300]`` 口径一致。
    """
    if exc is None:
        return "unknown error"

    chain = list(_walk(exc))
    top = chain[0]
    top_text = str(top).strip()

    if top_text:
        return top_text[:max_len]

    # 顶层 str() 为空：先用类型名占位，再找第一个有内容的下层
    detail = ""
    for cur in chain[1:]:
        text = str(cur).strip()
        if text:
            detail = f"caused by {type(cur).__module__}.{type(cur).__name__}: {text}"
            break

    name = type(top).__name__
    return (f"{name} ({detail})" if detail else name)[:max_len]
