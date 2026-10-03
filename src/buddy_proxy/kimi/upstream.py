"""Kimi Code 上游的请求/响应纯函数（便于离线单测）。

上游形态（2026-10-03 实测 + kimi cli 源码核对）：

- OpenAI chat-completions 兼容：``{base_url}/v1/chat/completions``，流式
  标准 SSE，delta 带 ``reasoning_content``（思考）+ ``content`` + ``tool_calls``；
- 4 个模型全部 ``supports_thinking_type: "only"``（思考关不掉），力度经
  extra body ``{"thinking": {"type": "enabled", "effort": "low|high|max"}}``
  控制，不传时上游默认 max；
- ``GET {base_url}/v1/models`` / ``/v1/usages`` / ``/v1/me``，Bearer 鉴权；
- 请求要带 kimi cli 的设备头（``X-Msh-*`` + ``User-Agent``），上游按它
  识别客户端身份。
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any

#: 设备头固定值（kimi cli 的 ``KIMI_CODE_PLATFORM``；伪装成官方 CLI 身份）。
X_MSH_PLATFORM = "kimi_code_cli"
#: X-Msh-Version / User-Agent 里的版本号；KIMI_CLI_VERSION 环境变量可覆盖。
FALLBACK_VERSION = "1.0.0"


def cli_version() -> str:
    return os.environ.get("KIMI_CLI_VERSION", "").strip() or FALLBACK_VERSION


def normalize_base_url(raw: str) -> str:
    """账号 JSON 的 ``base_url`` → 可直接拼 ``/chat/completions`` 的 base。

    kimi cli 导出的是 ``https://api.kimi.com/coding``（不带 /v1），但用户
    手工导入的值可能已带 ``/v1``——必须幂等，否则拼出 ``/v1/v1/chat/completions``。
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return base


def device_flow_headers(device_id: str = "") -> dict[str, str]:
    """OAuth 端点（device_authorization / token）用的设备头。

    kimi cli 的 OAuthManager 会把同一组 ``X-Msh-*`` 打到 OAuth 与业务端点上；
    ``device_id`` 缺省时（登录前还没有稳定 id）留空即可，上游不强制。
    """
    headers = {
        "X-Msh-Platform": X_MSH_PLATFORM,
        "X-Msh-Version": cli_version(),
        "User-Agent": f"kimi-code-cli/{cli_version()}",
    }
    if device_id:
        headers["X-Msh-Device-Id"] = device_id
    return headers


def device_headers(cred: dict[str, Any], *, access_token: str | None = None) -> dict[str, str]:
    """业务端点请求头：设备头 + ``Authorization: Bearer``。

    ``device_id`` 每账号固定（cred 里存着），上游按它绑定设备指纹。
    """
    headers = device_flow_headers(str(cred.get("device_id") or ""))
    token = access_token if access_token is not None else str(cred.get("access_token") or "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


#: 入参 reasoning_effort → 上游 thinking.effort 的映射。
#: kimi 只认 low/high/max（4 个模型实测），medium/high 档折到 high、
#: xhigh/max 折到 max；不认识/没给就返回 None（不注入，上游默认 max——
#: 这些模型 thinking only，不存在「不思考」的选项）。
_THINKING_EFFORT_MAP = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "max",
    "max": "max",
}


def map_thinking(reasoning_effort: Any) -> dict[str, Any] | None:
    """入参 effort → extra body ``thinking`` 节点；不需要注入时返回 None。"""
    key = str(reasoning_effort or "").strip().lower()
    effort = _THINKING_EFFORT_MAP.get(key)
    if effort is None:
        return None
    return {"thinking": {"type": "enabled", "effort": effort}}


#: 透传给上游的请求体字段（qoder 同款白名单口径）；``reasoning_effort``
#: 不在表里——它被 ``map_thinking`` 消费成 ``thinking`` 节点。
_PASSTHROUGH_FIELDS = (
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "response_format",
    "seed",
    "user",
    "parallel_tool_calls",
    "system",
)


def build_upstream_body(body: dict[str, Any], *, model: str) -> dict[str, Any]:
    """入站 chat 请求体 → 上游请求体（白名单透传 + thinking 注入）。"""
    upstream: dict[str, Any] = {"model": model}
    for field in _PASSTHROUGH_FIELDS:
        value = body.get(field)
        if value is not None:
            upstream[field] = value
    thinking = map_thinking(body.get("reasoning_effort"))
    if thinking is not None:
        upstream.update(thinking)
    return upstream


def iso_to_epoch(value: Any) -> float:
    """上游 ISO 时间（``2026-10-03T07:11:53Z`` 等）→ epoch 秒；解析失败返回 0。"""
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _get_json(url: str, access_token: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=device_headers({}, access_token=access_token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - 统一转 OSError 语义，调用方按失败处理
        raise OSError(f"Kimi 上游请求失败（{url}）: {exc}") from exc
    return data if isinstance(data, dict) else {}


def fetch_me(base_url: str, access_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """``GET /v1/me``：user_id / nickname / phone / user_level_name（面板展示用）。"""
    return _get_json(f"{normalize_base_url(base_url)}/me", access_token, timeout)


def fetch_usages(base_url: str, access_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """``GET /v1/usages``：5h / 7d 双池用量。"""
    return _get_json(f"{normalize_base_url(base_url)}/usages", access_token, timeout)


def usages_to_items(data: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    """``/v1/usages`` 响应 → 额度条目（``percent`` 是**已用**，前端进度条语义）。

    优先用 ``usages.limit_5h`` / ``limit_7d``（``used_ratio`` 0~1 + ``reset_time``），
    5 小时窗口在前（周期由小到大，与 zcode 顺序契约一致）；老响应没有
    ``usages`` 节点时回退顶层 ``usage: {limit, used, remaining, resetTime}``
    （实测那是 7d 池的合成值）。
    """
    buckets = data.get("usages") if isinstance(data.get("usages"), dict) else {}
    items: list[dict[str, Any]] = []
    for key, label in (("limit_5h", "5 小时窗口"), ("limit_7d", "7 天池")):
        bucket = buckets.get(key)
        if not isinstance(bucket, dict):
            continue
        ratio = bucket.get("used_ratio")
        if not isinstance(ratio, (int, float)):
            continue
        used = round(float(ratio) * 100, 2)
        items.append({
            "label": f"{prefix}{label}",
            "used": used,
            "total": 100,
            "remaining": round(100 - used, 2),
            "percent": used,
            # 5h/7d 都是周期重置（不是权益到期），不给 expire_ts 防「快到期」误报
            "reset_ts": iso_to_epoch(bucket.get("reset_time")) or None,
            "expire_ts": None,
            "unit": "percent",
        })
    if items:
        return items

    # 回退：顶层 usage（limit/used/remaining 是字符串数字）
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    try:
        total = float(usage.get("limit"))
        used = float(usage.get("used"))
    except (TypeError, ValueError):
        return []
    if total <= 0:
        return []
    used_pct = round(used / total * 100, 2)
    return [{
        "label": f"{prefix}7 天池",
        "used": used_pct,
        "total": 100,
        "remaining": round(100 - used_pct, 2),
        "percent": used_pct,
        "reset_ts": iso_to_epoch(usage.get("resetTime")) or None,
        "expire_ts": None,
        "unit": "percent",
    }]


def cred_expired_epoch(cred: dict[str, Any]) -> float:
    """cred 的 ``expired``（ISO；kimi cli 导出格式）→ epoch 秒；解析失败返回 0。

    导出值可能带 Z 后缀（``2026-10-03T06:06:17Z``），统一按 UTC 解析。
    """
    raw = str(cred.get("expired") or cred.get("expiry") or "").strip()
    if not raw:
        return 0.0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
