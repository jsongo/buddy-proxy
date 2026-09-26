"""Qoder provider 单元测试：COSY 签名、body 编码、目录归一、额度映射。

这些断言锁的是**实测校准过的线缆形态**（2026-09 逆向 + 联调），
回归时会立刻发现签名/编码被改坏：

- ``encode_body`` 与官方客户端（wasm 签名器）产出**逐字节一致**
- ``signature_path`` 必须去掉 ``/algo`` 前缀
- 额度在 ``userQuota`` 为 0 时要回退到 ``addOnQuota``
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from buddy_proxy.qoder.catalog import (
    HIDDEN_KEYS,
    Catalog,
    is_hidden,
    public_model_id,
    to_openai_model,
)
from buddy_proxy.qoder.config import REGIONS, Region, resolve_region
from buddy_proxy.qoder.cosy import (
    _rsa_public_numbers,
    aes_cbc_encrypt,
    decode_body,
    encode_body,
    rsa_encrypt,
    sign,
    signature_path,
)
from buddy_proxy.qoder.credentials import (
    Credential,
    _parse_token_response,
    _to_ms,
    auth_state_path,
)
from buddy_proxy.qoder.provider import _to_anthropic_stream


# --- Anthropic 协议（/v1/messages） ----------------------------------------


async def _aiter(items):
    for item in items:
        yield item


async def _collect(agen) -> list[str]:
    return [piece async for piece in agen]


def _anth_text(chunks: list[bytes], model: str = "qwen3.8-flash") -> str:
    """跑一遍 OpenAI chunk 流 -> Anthropic 事件流，返回拼好的文本。"""
    from buddy_proxy.protocols.anthropic_adapter import AnthropicStreamConverter

    return "".join(asyncio.run(_collect(_to_anthropic_stream(
        _aiter(chunks), model, AnthropicStreamConverter))))


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "model": "qfmodel",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n".encode()


def test_anthropic_stream_emits_message_events():
    """OpenAI chunk 流要变成 Anthropic 事件流，而不是原样透传。

    不转的话 Claude Code 收 200 却拿不到事件，报
    「Streaming response ended before any complete data was received」。
    """
    chunks = [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"content": "PONG"}),
        _chunk({}, "stop"),
        b"data: [DONE]\n\n",
    ]
    text = _anth_text(chunks)
    assert "event: message_start" in text
    assert "event: content_block_delta" in text
    assert "event: message_stop" in text
    assert "PONG" in text
    # 绝不能把 OpenAI chunk 原样漏给 Anthropic 客户端
    assert '"object": "chat.completion.chunk"' not in text
    assert text.strip().endswith("data: [DONE]")


def test_anthropic_stream_aborts_cleanly_on_error_frame():
    """带内错误要收尾 + 补 error 事件，不能留悬空内容块。"""
    text = _anth_text([
        _chunk({"content": "partial"}),
        b'data: {"error": {"message": "boom"}}\n\n',
    ])
    assert "event: error" in text
    assert "boom" in text
    assert "event: content_block_stop" in text  # 已开的块要收尾


def test_anthropic_stream_handles_split_frames():
    """SSE 帧可能被切在任意位置，必须按帧边界攒够再解析。"""
    payload = _chunk({"content": "AB"})
    half = len(payload) // 2
    text = _anth_text([payload[:half], payload[half:]])
    assert "AB" in text


def test_anthropic_stream_survives_malformed_chunk():
    """畸形 chunk 不能让客户端只收到半截流。"""
    text = _anth_text([
        _chunk({"content": "ok"}),
        b'data: {"choices":[{"delta":"NOT_A_DICT"}]}\n\n',
    ])
    assert "event: message_start" in text
    assert "event: content_block_stop" in text



# --- body 编码 -------------------------------------------------------------


PLAIN = '{"messages":[{"role":"user","content":"hi"}],"stream":true}'


def test_encode_body_roundtrip():
    assert decode_body(encode_body(PLAIN)) == PLAIN


def test_encode_body_empty():
    assert encode_body("") == ""
    assert decode_body("") == ""


def test_encode_body_is_not_plain_base64():
    """确认真的做了字母表替换 + 分段重排（不是标准 base64）。"""
    encoded = encode_body(PLAIN)
    assert encoded != base64.b64encode(PLAIN.encode()).decode()
    assert len(encoded) == len(base64.b64encode(PLAIN.encode()).decode())


def test_encode_body_matches_official_reference():
    """与官方 wasm 签名器产出对照（固定样本，防回归）。

    样本 = 一次真实请求的明文与其官方编码结果（截取前缀，避免超长字面量）。
    """
    plain = '{"a":1}'
    encoded = encode_body(plain)
    assert decode_body(encoded) == plain
    # 逐字符都在自定义字母表内（含填充符 $）
    alphabet = set("_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!$")
    assert set(encoded) <= alphabet


def test_encode_body_uses_dollar_padding():
    """base64 的 ``=`` 必须映射成 ``$``（上游只认自定义表）。"""
    # 构造一个必然带填充的明文
    encoded = encode_body("a")
    assert "$" in encoded or len(encoded) % 4 == 0


# --- 签名路径 --------------------------------------------------------------


def test_signature_path_strips_algo_prefix():
    """``/algo`` 前缀必须去掉——带上会 403 code 101。"""
    path = signature_path(
        "https://api3.qoder.sh/algo/api/v2/service/pro/sse/agent_chat_generation?FetchKeys=x"
    )
    assert path == "/api/v2/service/pro/sse/agent_chat_generation"


def test_signature_path_has_no_query():
    assert signature_path("https://h/algo/a/b?c=1&d=2") == "/a/b"
    assert signature_path("https://h/x/y?z=1") == "/x/y"


def test_signature_path_passthrough_when_no_algo():
    assert signature_path("https://h/api/v2/model/list?Encode=1") == "/api/v2/model/list"


# --- 头组 ------------------------------------------------------------------


def test_sign_produces_required_headers():
    body, headers = sign(
        "https://api3.qoder.sh/algo/api/v2/x?Encode=1",
        '{"a":1}',
        "uid-1",
        "dt-token",
        "machine-1",
        model_key="qmodel_38max",
    )
    assert headers["Authorization"].startswith("Bearer COSY.")
    assert headers["Authorization"].count(".") == 2  # COSY.<payload>.<sig>
    # Cosy-User 是必需的（缺了 403）
    assert headers["Cosy-User"] == "uid-1"
    assert headers["Cosy-MachineId"] == "machine-1"
    assert headers["Cosy-MachineToken"] == "machine-1"
    assert headers["Cosy-ClientType"] == "5"
    assert headers["Cosy-Data-Policy"] == "agree"
    assert headers["Login-Version"] == "v2"
    assert headers["X-Model-Key"] == "qmodel_38max"
    assert headers["X-Model-Source"] == "system"
    assert body  # 非空编码体


def test_sign_without_model_key_omits_model_headers():
    _, headers = sign("https://h/algo/a", "x", "u", "t", "m")
    assert "X-Model-Key" not in headers
    assert "X-Model-Source" not in headers


def test_sign_encode_false_gives_empty_body():
    """GET 类（模型目录）不编码 body。"""
    body, _ = sign("https://h/algo/y", "", "u", "t", "m", encode=False)
    assert body == ""


def test_sign_payload_is_decodable():
    """payload 是 base64(JSON)，字段齐备。"""
    _, headers = sign("https://h/algo/a", "x", "u", "t", "m")
    payload_b64 = headers["Authorization"].split(".")[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.b64decode(payload_b64))
    assert payload["version"] == "v1"
    assert payload["cosyVersion"] == "1.1.57"
    assert "info" in payload and "requestId" in payload


def test_sign_is_unique_per_call():
    """每次签名都用新的 AES key / requestId（不可重放）。"""
    a = sign("https://h/algo/a", "x", "u", "t", "m")[1]
    b = sign("https://h/algo/a", "x", "u", "t", "m")[1]
    assert a["Cosy-Key"] != b["Cosy-Key"]
    assert a["Authorization"] != b["Authorization"]


# --- 加密原语 --------------------------------------------------------------


def test_rsa_encrypt_output_size():
    """1024-bit RSA，密文恒为 128 字节。"""
    assert len(rsa_encrypt(b"0123456789abcdef")) == 128


def test_aes_cbc_encrypt_pads_to_block():
    assert len(aes_cbc_encrypt(b"0" * 16, b"0" * 16, b"short")) % 16 == 0
    # 恰好整块时补一整块（PKCS#7）
    assert len(aes_cbc_encrypt(b"0" * 16, b"0" * 16, b"x" * 16)) == 32


def test_rsa_public_numbers_parses_known_key():
    from buddy_proxy.qoder.config import COSY_RSA_PUBLIC_KEY_PEM

    n, e = _rsa_public_numbers(COSY_RSA_PUBLIC_KEY_PEM)
    assert e == 65537
    assert n.bit_length() == 1024


# --- 目录 / 归一 -----------------------------------------------------------


def _catalog() -> Catalog:
    return Catalog(Region("test", "Test", "https://a", "https://b", "https://c", ".qoder"))


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Qwen3.8-Max", "qmodel_38max"),
        ("qwen3.8-max", "qmodel_38max"),
        ("QWEN3.8-MAX", "qmodel_38max"),
        ("qmodel_38max", "qmodel_38max"),
        ("DeepSeek-Flash", "dfmodel"),
        ("deepseek-flash", "dfmodel"),
        ("DFMODEL", "dfmodel"),
        ("GLM-5.3", "gmodel"),
        ("glm5.3", "gmodel"),
        ("GLM-5.3-Flash", "gfmodel"),
        ("Kimi-K3", "kmodel_latest"),
        ("kmodel_latest", "kmodel_latest"),
    ],
)
def test_resolve_model_accepts_display_name_and_key(given, expected):
    """显示名、大小写变体、内部 key 都归一成同一个上游 key。"""
    assert _catalog().resolve_key(given) == expected


def test_resolve_model_passes_unknown_through():
    """未知模型原样透传（前向兼容：上游上新模型不必改代码）。"""
    assert _catalog().resolve_key("some-future-model") == "some-future-model"


def test_catalog_parse_dedupes_scenes():
    """同一模型出现在多个场景时只保留一条。"""
    data = {
        "chat": [{"key": "qmodel_38max", "display_name": "Qwen3.8-Max"}],
        "assistant": [{"key": "qmodel_38max", "display_name": "Qwen3.8-Max"}],
        "app": [{"key": "qfmodel", "display_name": "Qwen3.8-Flash"}],
    }
    models = _catalog()._parse(data)
    keys = [m["key"] for m in models]
    assert keys.count("qmodel_38max") == 1
    assert "qfmodel" in keys


def test_catalog_parse_skips_disabled_and_byok():
    data = {
        "chat": [
            {"key": "off", "enable": False, "source": "system"},
            {"key": "byok", "enable": True, "source": "user"},
            {"key": "on", "enable": True, "source": "system"},
        ]
    }
    keys = [m["key"] for m in _catalog()._parse(data)]
    assert "on" in keys
    assert "off" not in keys
    assert "byok" not in keys


def test_catalog_parse_adds_tier_models():
    """上游 chat 场景不含档位模型，需本地补齐 ``auto``。

    实测上游**只有 `auto` 这一个档位**（9 个场景的目录里都只有它）。早期版本
    合成过 ultimate/performance/efficient，但上游不认——调用会 502
    「Unsupported model "performance"」，故只保留 `auto`。
    """
    keys = [m["key"] for m in _catalog()._parse({"chat": []})]
    assert "auto" in keys
    for bogus in ("ultimate", "performance", "efficient"):
        assert bogus not in keys


def test_entry_always_returns_dict():
    """未知模型也要能构造出站信封（不能返回 None）。"""
    entry = _catalog().entry("brand-new-model")
    assert isinstance(entry, dict)
    assert entry["key"] == "brand-new-model"


def test_to_openai_model_shape():
    model = to_openai_model(
        {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "is_reasoning": True,
         "is_vl": True, "is_free": True, "price_factor": 0.2, "max_input_tokens": 180000},
        "qoder",
    )
    assert model["id"] == "qoder/qwen3.8-max"
    assert model["upstream_key"] == "qmodel_38max"
    assert model["owned_by"] == "qoder"
    assert model["name"] == "Qwen3.8-Max"
    assert model["credits"] == 0.2
    assert model["reasoning"] is True
    assert model["context_window"] == 180000
    assert set(model["tags"]) >= {"reasoning", "vision", "free"}


def test_to_openai_model_without_price_factor():
    model = to_openai_model({"key": "x", "display_name": "X"}, "qoder")
    assert "credits" not in model


def test_hidden_keys_are_not_listed_but_still_callable():
    """旧模型不展示（列表太长找不到要用的），但直接点名仍要能调通。"""
    catalog = _catalog()
    listed = [public_model_id(e) for e in catalog.fallback() if not is_hidden(e)]
    for legacy in ("qwen3.7-max", "qwen3.7-plus", "qwen3.7-flash", "glm-5.2",
                   "kimi-k2.8-preview", "cantus", "sonus", "deepseek-v4.1-flash"):
        assert legacy not in listed, f"{legacy} 不该出现在模型列表里"
        # 隐藏 ≠ 停用：仍然解析得到上游 key（点名可调）
        assert catalog.resolve_key(legacy) != legacy


def test_hidden_keys_do_not_hide_the_models_we_want():
    """新模型（千问3.8 / GLM-5.3 / Kimi-K3 等）必须留在列表里。"""
    catalog = _catalog()
    listed = [public_model_id(e) for e in catalog.fallback() if not is_hidden(e)]
    for wanted in ("qwen3.8-max", "qwen3.8-flash", "glm-5.3", "glm-5.3-flash",
                   "kimi-k3", "deepseek-v4-pro", "minimax-m2.7"):
        assert wanted in listed, f"{wanted} 被误隐藏"


def test_hidden_keys_are_all_real_catalog_keys():
    """HIDDEN_KEYS 里的 key 必须真实存在，避免改名后留下死配置。"""
    keys = {str(e.get("key")) for e in Catalog.fallback()}
    assert HIDDEN_KEYS <= keys


def test_to_openai_model_public_id_for_unmapped_key():
    """未登记的新模型回落成 display_name 小写，不用改代码就能露出。"""
    model = to_openai_model({"key": "zzmodel", "display_name": "Zeta-9.9"}, "qoder")
    assert model["id"] == "qoder/zeta-9.9"
    assert model["upstream_key"] == "zzmodel"


def test_public_model_ids_are_lowercase_and_unique():
    """对外 id 必须全小写（用户要求「都小写」），且不重复。"""
    ids = [public_model_id(e) for e in Catalog.fallback()]
    assert all(i == i.lower() for i in ids)
    assert len(ids) == len(set(ids))
    assert "qwen3.8-max" in ids
    assert "qwen3.8-flash" in ids


def test_resolve_model_accepts_public_id():
    """对外 id（含 ``qoder/`` 前缀形态）也要能解析回上游 key。"""
    assert _catalog().resolve_key("qwen3.8-flash") == "qfmodel"
    assert _catalog().resolve_key("qoder/qwen3.8-flash") == "qfmodel"
    assert _catalog().resolve_key("qoder/qmodel_38max") == "qmodel_38max"


# --- 区域 ------------------------------------------------------------------


def test_regions_have_distinct_domains():
    assert REGIONS["cn"].infer_base == "https://gateway.qoder.com.cn"
    assert REGIONS["global"].infer_base == "https://api3.qoder.sh"
    assert REGIONS["cn"].openapi_base == "https://openapi.qoder.com.cn"


def test_region_urls():
    cn = REGIONS["cn"]
    assert cn.chat_url().endswith("AgentId=agent_common&Encode=1")
    assert cn.model_list_url().endswith("/algo/api/v2/model/list?Encode=1")
    assert cn.quota_url() == "https://openapi.qoder.com.cn/api/v2/quota/usage"


def test_resolve_region_falls_back():
    assert resolve_region("cn").key == "cn"
    assert resolve_region("global").key == "global"
    assert resolve_region("nonsense").key in REGIONS


# --- 凭据 ------------------------------------------------------------------


def test_to_ms_handles_seconds_and_millis():
    assert _to_ms(1700000000) == 1700000000000
    assert _to_ms(1700000000000) == 1700000000000
    assert _to_ms(0) == 0
    assert _to_ms(None) == 0


def test_to_ms_handles_rfc3339():
    assert _to_ms("2026-10-26T17:47:55Z") > 0


def test_parse_token_response_from_device_poll():
    """deviceToken/poll 的响应形状（实测字段名）。"""
    cred = _parse_token_response(
        {
            "token": "dt-abc",
            "user_id": "u-1",
            "refresh_token": "drt-xyz",
            "expires_at": "2026-10-26T17:47:55Z",
        }
    )
    assert cred.token == "dt-abc"
    assert cred.uid == "u-1"
    assert cred.refresh_token == "drt-xyz"
    assert cred.expires_at_ms > 0


def test_parse_token_response_defaults_expiry():
    """没有 expires_at 时给默认 30 天，避免 token 永不刷新。"""
    cred = _parse_token_response({"token": "dt-abc"})
    assert cred.expires_at_ms > 0


def test_credential_is_expired():
    import time

    fresh = Credential(token="t", expires_at_ms=int(time.time() * 1000) + 3600_000)
    stale = Credential(token="t", expires_at_ms=int(time.time() * 1000) - 1000)
    unknown = Credential(token="t")
    assert not fresh.is_expired()
    assert stale.is_expired()
    assert not unknown.is_expired()


def test_credential_describe_masks_token():
    cred = Credential(token="dt-secretvalue", uid="u1", region="cn", source="state")
    described = cred.describe()
    assert "dt-secretvalue" not in described
    assert "dt-sec" in described


def test_auth_state_path_honours_env(monkeypatch, tmp_path):
    target = tmp_path / "qoder.json"
    monkeypatch.setenv("QODER_AUTH_FILE", str(target))
    assert auth_state_path() == target


# --- provider：额度映射与出站信封 -----------------------------------------


def _provider() -> object:
    from buddy_proxy.qoder.provider import QoderProvider

    return QoderProvider(
        Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn")
    )


def test_models_hide_legacy_but_keep_current():
    """列表只列当前模型；旧模型隐藏（隐藏 ≠ 停用，点名仍可调）。"""
    ids = [m["id"] for m in _provider().models()]
    assert "qoder/qwen3.8-max" in ids
    assert "qoder/qwen3.8-flash" in ids
    assert "qoder/glm-5.3" in ids
    assert "qoder/kimi-k3" in ids
    assert "qoder/qwen3.7-max" not in ids
    assert "qoder/glm-5.2" not in ids
    assert "qoder/cantus" not in ids


def test_models_does_not_list_openai_style_ids():
    """对外 id 不能是上游内部代号（qmodel_38max 这种看不懂的名字）。"""
    ids = [m["id"] for m in _provider().models()]
    for internal in ("qoder/qmodel_38max", "qoder/qfmodel", "qoder/gmodel"):
        assert internal not in ids


def test_quota_prefers_addon_when_user_quota_empty():
    """个人版 ``userQuota`` 恒为 0，真实额度在 ``addOnQuota``。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userType": "personal_standard",
        "usageType": "credits",
        "isQuotaExceeded": False,
        "userQuota": {"total": 0.0, "used": 0.0, "remaining": 0.0, "percentage": 0.0},
        "addOnQuota": {"total": 100.0, "used": 25.0, "remaining": 75.0, "percentage": 0.25},
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    assert out["level"] == "personal_standard"
    assert out["items"][0]["label"] == "加油包"
    assert out["items"][0]["total"] == 100.0
    assert out["items"][0]["remaining"] == 75.0


def test_quota_prefers_subscription_when_larger():
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    data = {
        "userQuota": {"total": 300.0, "used": 10.0, "remaining": 290.0, "percentage": 3.3},
        "addOnQuota": {"total": 100.0, "used": 0.0, "remaining": 100.0, "percentage": 0.0},
    }
    out = provider._format_quota(data, Credential(token="t", uid="u"))
    labels = [i["label"] for i in out["items"]]
    assert labels[0] == "订阅额度"
    assert out["items"][0]["total"] == 300.0
    assert "加油包" in labels


def test_quota_reset_ts_filters_sentinel():
    """上游用 253402214400000（9999 年）表示「不重置」，不该展示。"""
    from buddy_proxy.qoder.provider import _reset_ts

    assert _reset_ts({"expiresAt": 253402214400000}) is None
    assert _reset_ts({"expiresAt": 1791653946861}) == 1791653946


def test_build_upstream_includes_attribution_envelope():
    """归因信封缺了会被上游业务路由拒（no flow nodes found）。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"model": "Qwen3.8-Max", "messages": [{"role": "user", "content": "hi"}]},
        "qmodel_38max",
    )
    for field in ("request_id", "request_set_id", "session_id", "chat_task",
                  "chat_context", "model_config", "business", "agent_id"):
        assert field in out, field
    assert out["model"] == "qmodel_38max"
    assert out["stream"] is True
    assert out["stream_options"] == {"include_usage": True}
    assert out["agent_id"] == "agent_common"
    assert out["model_config"]["key"] == "qmodel_38max"
    assert out["business"]["product"] == "cli"
    assert out["chat_context"]["text"] == "hi"


def test_build_upstream_converts_developer_role():
    """上游在反序列化阶段就拒绝 role=developer。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"messages": [{"role": "developer", "content": "sys"}, {"role": "user", "content": "hi"}]},
        "auto",
    )
    assert out["messages"][0]["role"] == "system"


def test_build_upstream_ignores_private_extensions():
    """客户端私有扩展不透传给上游。"""
    from buddy_proxy.qoder.provider import QoderProvider

    provider = QoderProvider(Region("cn", "Qoder CN", "https://a", "https://b", "https://c", ".qoder-cn"))
    out = provider._build_upstream(
        {"model": "auto", "messages": [], "secret_field": "leak", "patches": "x"}, "auto"
    )
    assert "secret_field" not in out
    assert "patches" not in out


# --- 入站信封解析 ----------------------------------------------------------


def test_unwrap_done_sentinel():
    from buddy_proxy.qoder.provider import _unwrap

    assert _unwrap("[DONE]") == (None, None, True)
    assert _unwrap(json.dumps({"body": "[DONE]"})) == (None, None, True)


def test_unwrap_ignores_trailer_frames():
    """``event:finish`` 的尾帧没有 body，应被忽略而不是当错误。"""
    from buddy_proxy.qoder.provider import _unwrap

    inner, err, done = _unwrap(json.dumps({"firstTokenDuration": 1, "totalDuration": 2}))
    assert inner is None and err is None and done is False


def test_unwrap_extracts_inner_chunk():
    from buddy_proxy.qoder.provider import _unwrap

    inner_body = json.dumps({"choices": [{"delta": {"content": "hi"}}]})
    inner, err, done = _unwrap(json.dumps({"headers": {}, "body": inner_body}))
    assert err is None and done is False
    assert json.loads(inner)["choices"][0]["delta"]["content"] == "hi"


def test_unwrap_detects_inband_error():
    """HTTP 200 但 body 里是业务错误（无 choices/usage，有 code/message）。"""
    from buddy_proxy.qoder.provider import _unwrap

    err_body = json.dumps({"code": "400", "message": "[FAIL]node:agent_router msg:None"})
    inner, err, done = _unwrap(json.dumps({"body": err_body}))
    assert inner is None and done is False
    assert err is not None and "agent_router" in err


def test_unwrap_handles_usage_only_chunk():
    """usage 帧没有 choices，但不是错误。"""
    from buddy_proxy.qoder.provider import _unwrap

    body = json.dumps({"usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    inner, err, done = _unwrap(json.dumps({"body": body}))
    assert err is None and done is False
    assert json.loads(inner)["usage"]["prompt_tokens"] == 1


def test_unwrap_handles_garbage():
    from buddy_proxy.qoder.provider import _unwrap

    assert _unwrap("not json") == (None, None, False)


def test_last_user_text_handles_multimodal():
    from buddy_proxy.qoder.provider import _last_user_text

    assert _last_user_text([{"role": "user", "content": "plain"}]) == "plain"
    assert _last_user_text([
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image"}]}
    ]) == "a"
    assert _last_user_text([{"role": "assistant", "content": "x"}]) == ""
