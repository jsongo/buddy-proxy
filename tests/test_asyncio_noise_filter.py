"""AsyncioNoiseFilter：压「socket.send() raised exception」风暴（2026-10-06）。

浏览器管理页轮询 abort 旧请求，asyncio 每次往半关连接写都报一条 error——
实测单日志文件 5271 条，把启动日志全淹了。窗口内只放第一条、其余计数，
下一条放行时补报抑制数；其他消息不拦。
"""

from __future__ import annotations

import logging

from buddy_proxy.core.logging_setup import AsyncioNoiseFilter

_NOISE = "socket.send() raised exception"


def _noise_record() -> logging.LogRecord:
    return logging.LogRecord("asyncio", logging.ERROR, "x", 1, _NOISE, None, None)


def _other_record() -> logging.LogRecord:
    return logging.LogRecord("asyncio", logging.ERROR, "x", 1, "boom: real error",
                             None, None)


def test_first_pass_then_suppressed_then_reported():
    f = AsyncioNoiseFilter(window_s=300.0)
    clock = {"t": 1000.0}

    import buddy_proxy.core.logging_setup as mod
    real_monotonic = mod.time.monotonic
    mod.time.monotonic = lambda: clock["t"]
    try:
        # 第一条放行
        assert f.filter(_noise_record()) is True
        # 窗口内连环刷：全部静默
        for _ in range(9):
            clock["t"] += 30.0
            assert f.filter(_noise_record()) is False
        # 窗口结束后的下一条：放行，并把抑制数补报进文案
        clock["t"] += 300.0
        rec = _noise_record()
        assert f.filter(rec) is True
        assert rec.getMessage() == f"{_NOISE} (9 suppressed in last 300s)"
    finally:
        mod.time.monotonic = real_monotonic


def test_other_messages_untouched():
    f = AsyncioNoiseFilter(window_s=300.0)
    rec = _other_record()
    assert f.filter(rec) is True
    assert rec.getMessage() == "boom: real error", "非噪音消息原文放行、不改写"


def test_setup_logging_installs_filter_once(tmp_path):
    """setup_logging 挂 filter 且幂等——重复配置（测试里多跑几次）不叠多层。"""
    logging.getLogger("asyncio").filters.clear()
    try:
        from buddy_proxy.core.logging_setup import setup_logging
        setup_logging(tmp_path)
        setup_logging(tmp_path)
        flts = [f for f in logging.getLogger("asyncio").filters
                if isinstance(f, AsyncioNoiseFilter)]
        assert len(flts) == 1
    finally:
        logging.getLogger("asyncio").filters.clear()
