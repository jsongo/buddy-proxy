"""日志配置与运行时信息工具函数。"""

from __future__ import annotations

import importlib.metadata
import logging
import logging.handlers
import pathlib
import platform
import time


class AsyncioNoiseFilter(logging.Filter):
    """压掉 asyncio 往半关连接写数据时刷的「socket.send() raised exception」。

    浏览器管理页轮询时 abort 旧请求是常态：连接断了 asyncio 还在往里写响应，
    每次一条 error——2026-10-06 实测单日志文件刷了 5271 条，把启动日志全淹了。
    同类消息按时间窗限频：窗口内第一条照常放行（留排查线索），其余静默计数，
    下一条放行时补报「已抑制 N 条」。其他消息不受影响。
    """

    _NOISE = "socket.send() raised exception"

    def __init__(self, window_s: float = 300.0):
        super().__init__()
        self._window_s = window_s
        self._last_seen = 0.0
        self._suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != self._NOISE:
            return True
        now = time.monotonic()
        if now - self._last_seen < self._window_s:
            self._suppressed += 1
            return False
        if self._suppressed:
            # 窗口刚结束，借这条的放行把抑制数带出去（噪音计量，不求精确）
            record.msg = "%s (%d suppressed in last %.0fs)"
            record.args = (self._NOISE, self._suppressed, self._window_s)
            self._suppressed = 0
        self._last_seen = now
        return True


def setup_logging(log_dir: pathlib.Path) -> logging.Logger:
    """配置滚动日志：按天分片，保留30天。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "proxy.log"

    logger = logging.getLogger("buddy_proxy")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    handler.suffix = "%Y-%m-%d"
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # asyncio 的报错不经 buddy_proxy logger（无 handler 时走 lastResort 到
    # stderr，launchd 一并收进 service.log），filter 挂在它的 logger 上
    # （Logger.handle 在分发 handler 前先过 filter，lastResort 也拦得住）。
    noise_logger = logging.getLogger("asyncio")
    if not any(isinstance(f, AsyncioNoiseFilter) for f in noise_logger.filters):
        noise_logger.addFilter(AsyncioNoiseFilter())

    return logger


def setup_json_logging(log_file: pathlib.Path) -> logging.Logger:
    """配置 JSONL 滚动日志：按天分片，保留30天。"""
    logger = logging.getLogger("buddy_proxy.jsonl")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()

    handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    handler.suffix = "%Y-%m-%d"
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)

    return logger


def now_s() -> int:
    return int(time.time())


def get_runtime_info() -> dict[str, str]:
    try:
        app_version = importlib.metadata.version("buddy-proxy")
    except importlib.metadata.PackageNotFoundError:
        app_version = "unknown"
    return {
        "app_version": app_version,
        "system_version": platform.platform(),
        "python_version": platform.python_version(),
        "machine": platform.machine(),
    }
