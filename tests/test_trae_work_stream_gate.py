"""trae work 流式首事件闸门单测：心跳丢弃 / 账号级错误帧换号 / 正常帧放行。

覆盖 ``_gate_first_event`` 与 ``_account_error_from_frame``——这是多账号 failover
防「假成功」的关键：``_stream`` 把 HTTPException yield 成 error chunk 再 return，
闸门必须把这种账号级错误帧（401/429）还原成异常让 forward 换号，而不是当语义
事件放行。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

from buddy_proxy.trae.provider import (
    _account_error_from_frame,
    _gate_first_event,
    _gate_first_event_async,
    _sync_to_async_iter,
)


def _sync_gen(pieces: list[str]):
    yield from pieces


def test_gate_skips_heartbeat_and_empty():
    gen = _sync_gen([": heartbeat\n\n", "\n", ": another\n\n", "data: {\"x\":1}\n\n"])
    out = _gate_first_event(gen)
    assert not isinstance(out, BaseException)
    assert out.buffered == ["data: {\"x\":1}\n\n"]


def test_gate_empty_stream_returns_502():
    out = _gate_first_event(_sync_gen([]))
    assert isinstance(out, HTTPException)
    assert out.status_code == 502


def test_gate_account_error_401_frame_raises():
    frame = 'data: {"error": {"message": "auth", "type": "upstream_error", "code": 401}}\n\n'
    out = _gate_first_event(_sync_gen([frame]))
    assert isinstance(out, HTTPException)
    assert out.status_code == 401


def test_gate_account_error_429_frame_raises():
    frame = 'data: {"error": {"message": "quota", "type": "upstream_error", "code": 429}}\n\n'
    out = _gate_first_event(_sync_gen([frame]))
    assert isinstance(out, HTTPException)
    assert out.status_code == 429


def test_gate_non_account_error_frame_passes_through():
    # 502/400 不是账号级错误：放行（不缓冲为异常），让下游按原样处理
    frame = 'data: {"error": {"message": "bad", "type": "upstream_error", "code": 502}}\n\n'
    out = _gate_first_event(_sync_gen([frame]))
    assert not isinstance(out, BaseException)
    assert out.buffered == [frame]


def test_gate_normal_frame_committed_then_replays_rest():
    gen = _sync_gen([
        ": heartbeat\n\n",
        "data: {\"a\":1}\n\n",
        "data: {\"a\":2}\n\n",
    ])
    out = _gate_first_event(gen)
    assert not isinstance(out, BaseException)
    assert out.buffered == ["data: {\"a\":1}\n\n"]
    # 续跑同一迭代器：剩下一帧被消费，不重开（防二次计费）
    rest = list(out.gen)
    assert rest == ["data: {\"a\":2}\n\n"]


def test_gate_exception_before_first_event_propagates():
    def _raising():
        yield ": heartbeat\n\n"
        raise HTTPException(status_code=401, detail="boom")

    out = _gate_first_event(_raising())
    assert isinstance(out, HTTPException)
    assert out.status_code == 401


def test_async_gate_keeps_event_loop_responsive_while_waiting():
    def _slow():
        time.sleep(0.2)
        yield 'data: {"x":1}\n\n'

    async def run():
        task = asyncio.create_task(_gate_first_event_async(_slow()))
        started = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.12
        result = await task
        assert result.buffered == ['data: {"x":1}\n\n']

    asyncio.run(run())


def test_async_gate_propagates_request_context_to_generator():
    """PAT 在闸门工作线程里选账号；ACCOUNT_META 必须随请求上下文传播。"""
    from buddy_proxy.core.metrics import ACCOUNT_META

    async def run():
        meta: dict[str, str] = {}
        token = ACCOUNT_META.set(meta)
        try:
            def source():
                holder = ACCOUNT_META.get()
                assert holder is meta
                holder["account"] = "primary"
                yield 'data: {"x":1}\n\n'

            result = await _gate_first_event_async(source())
            assert not isinstance(result, BaseException)
            assert meta == {"account": "primary"}
        finally:
            ACCOUNT_META.reset(token)

    asyncio.run(run())


def test_sync_to_async_iter_keeps_event_loop_responsive_while_waiting():
    def _slow():
        time.sleep(0.2)
        yield "first"

    async def run():
        iterator = _sync_to_async_iter(_slow())
        task = asyncio.create_task(iterator.__anext__())
        started = time.monotonic()
        await asyncio.sleep(0.01)
        assert time.monotonic() - started < 0.12
        assert await task == "first"
        await iterator.aclose()

    asyncio.run(run())


def test_slow_gates_do_not_starve_default_executor(monkeypatch):
    import threading

    from buddy_proxy.trae import provider

    release = threading.Event()
    entered = threading.Event()
    started = 0
    lock = threading.Lock()
    original_gate = provider._gate_first_event

    def slow_gate(_gen):
        nonlocal started
        with lock:
            started += 1
            if started >= provider._STREAM_GATE_WORKERS:
                entered.set()
        release.wait(timeout=2)
        return original_gate(_sync_gen(['data: {"x":1}\n\n']))

    monkeypatch.setattr(provider, "_gate_first_event", slow_gate)

    async def run():
        tasks = [
            asyncio.create_task(_gate_first_event_async(_sync_gen([])))
            for _ in range(provider._STREAM_GATE_WORKERS * 4)
        ]
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            # Even with every gate worker occupied, unrelated to_thread work uses
            # the default executor and should not queue behind these slow streams.
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "ok"), 0.2) == "ok"
        finally:
            release.set()
            await asyncio.gather(*tasks)

    asyncio.run(run())


# -- _account_error_from_frame 边界 ------------------------------------------


@pytest.mark.parametrize("frame,expect", [
    # 账号级错误帧 → 对应 HTTPException
    ('data: {"error": {"code": 401, "message": "x"}}', 401),
    ('data: {"error": {"code": 429, "message": "y"}}', 429),
    ('data: {"error": {"code": "401"}}', 401),  # 字符串码
])
def test_account_error_from_frame_positive(frame, expect):
    err = _account_error_from_frame(frame)
    assert isinstance(err, HTTPException)
    assert err.status_code == expect


@pytest.mark.parametrize("frame", [
    'data: {"error": {"code": 502}}',      # 非账号级
    'data: {"error": {"code": 400}}',      # 业务错误
    'data: {"choices": []}',                # 正常 chunk 无 error 字段
    'data: not-json',                       # 非法 JSON
    'event: output\ndata: {}',              # 非 data: 起始
    ': heartbeat',                          # 心跳
    'data: {"error": "plain-string"}',      # error 非 dict
    'data: {"error": {"code": null}}',      # 码缺失
])
def test_account_error_from_frame_negative(frame):
    assert _account_error_from_frame(frame) is None
