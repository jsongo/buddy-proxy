"""Start Plan 上游 200 假成功不能变成客户端 200。"""
import asyncio
import json
from unittest.mock import patch

import httpx
from fastapi.responses import JSONResponse, StreamingResponse


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

from buddy_proxy.providers import zcode_start as zs


class FakeClient:
    def __init__(self, payload, stream=False, content_type="application/json"):
        self.payload = payload
        self.stream = stream
        self.content_type = content_type

    def build_request(self, method, url, json=None, headers=None):
        return httpx.Request(method, url)

    async def send(self, req, stream=False):
        if isinstance(self.payload, list):
            return httpx.Response(200, headers={"content-type": self.content_type},
                                  stream=ChunkStream(self.payload), request=req)
        body = self.payload if isinstance(self.payload, bytes) else json.dumps(self.payload).encode()
        return httpx.Response(200, headers={"content-type": self.content_type},
                              content=body, request=req)


def forward(payload, stream=False, protocol="anthropic", content_type="application/json"):
    async def run():
        provider = zs.ZCodeStartPlanProvider(base_url="http://upstream.test", api_key="k")
        fake = FakeClient(payload, stream, content_type)
        async def get_client():
            return fake
        provider._get_client = get_client
        body = {"model": "glm-5.3-flash", "max_tokens": 16, "stream": stream,
                "messages": [{"role": "user", "content": "hi"}]}
        with patch.object(zs, "_load_device_mid", return_value="test-device"):
            resp = await provider.forward(body, protocol, body if protocol == "anthropic" else None)
        data = b""
        if isinstance(resp, StreamingResponse):
            async for chunk in resp.body_iterator:
                data += chunk
        return resp, data
    return asyncio.run(run())


def test_quota_json_with_http_200_is_rate_limit_for_both_protocols():
    for protocol in ("anthropic", "openai"):
        resp, _ = forward({"code": 1308, "msg": "已达到使用限额"}, protocol=protocol)
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 429
        assert json.loads(resp.body)["error"]["code"] == 1308


def test_unknown_json_with_http_200_is_bad_gateway():
    resp, _ = forward({"error": {"message": "unavailable"}})
    assert resp.status_code == 502


def test_string_error_with_http_200_is_quota():
    resp, _ = forward({"error": "quota exceeded"})
    assert resp.status_code == 429
    assert json.loads(resp.body)["error"]["message"] == "quota exceeded"


def test_valid_message_still_passes():
    resp, _ = forward({"id": "msg_1", "type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": "ok"}]})
    assert resp.status_code == 200


def test_stream_json_error_before_commit():
    resp, _ = forward({"code": 1308, "msg": "quota exceeded"}, stream=True)
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429


def test_sse_error_frame_before_commit():
    frame = b'event: error\ndata: {"type":"error","error":{"message":"quota exceeded","code":1308}}\n\n'
    resp, _ = forward(frame, stream=True, content_type="text/event-stream")
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429


def test_empty_and_invalid_sse_are_bad_gateway():
    for data in (b"", b": keepalive\n\n", b"data: bad json\n\n"):
        resp, _ = forward(data, stream=True, content_type="text/event-stream")
        assert isinstance(resp, JSONResponse)
        assert resp.status_code == 502


def test_valid_sse_passes_original_bytes():
    frame = (b'event: message_start\ndata: {"type":"message_start","message":'
             b'{"type":"message","role":"assistant","content":[]}}\n\n'
             b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    resp, data = forward(frame, stream=True, content_type="text/event-stream")
    assert isinstance(resp, StreamingResponse)
    assert data == frame


def test_sse_keepalive_before_message_preserved():
    frame = (b': keepalive\n\n' * 20000 +
             b'event: message_start\ndata: {"type":"message_start","message":'
             b'{"type":"message","role":"assistant","content":[]}}\n\n')
    resp, _ = forward(frame, stream=True, content_type="text/event-stream")
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 502  # 首事件前累计字节有限，不能无限缓存心跳


def test_crlf_split_across_chunks_passes():
    frame = (b': keepalive\r\n\r\nevent: message_start\r\ndata: '
             b'{"type":"message_start","message":{"type":"message",'
             b'"role":"assistant","content":[]}}\r\n\r\n')
    chunks = [frame[i:i + 1] for i in range(len(frame))]
    resp, data = forward(chunks, stream=True, content_type="text/event-stream")
    assert isinstance(resp, StreamingResponse)
    assert data == frame


def test_sse_error_event_without_data_type_is_quota():
    frame = b'event: error\ndata: {"code":1308,"msg":"quota exceeded"}\n\n'
    resp, _ = forward([frame[:7], frame[7:]], stream=True, content_type="text/event-stream")
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429


def test_openai_stream_error_not_fake_done():
    resp, _ = forward({"code": 1308, "msg": "quota exceeded"},
                      stream=True, protocol="openai")
    assert isinstance(resp, JSONResponse)
    assert resp.status_code == 429


def test_openai_stream_crlf_delivers_text():
    frames = (
        b'event: message_start\r\ndata: {"type":"message_start","message":'
        b'{"type":"message","role":"assistant","content":[]}}\r\n\r\n'
        b'event: content_block_delta\r\ndata: {"type":"content_block_delta",'
        b'"delta":{"type":"text_delta","text":"hello"}}\r\n\r\n'
        b'event: message_stop\r\ndata: {"type":"message_stop"}\r\n\r\n'
    )
    resp, data = forward([frames[i:i + 1] for i in range(len(frames))], stream=True,
                         protocol="openai", content_type="text/event-stream")
    assert isinstance(resp, StreamingResponse)
    events = [json.loads(line[6:]) for line in data.splitlines() if line.startswith(b"data: {")]
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert any(event["choices"][0]["delta"].get("content") == "hello" for event in events)
    assert data.endswith(b"data: [DONE]\n\n")
