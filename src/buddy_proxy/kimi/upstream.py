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


def build_upstream_body(body: dict[str, Any], *, model: str,
                        stream: bool = False) -> dict[str, Any]:
    """入站 chat 请求体 → 上游请求体（白名单透传 + thinking 注入）。

    ``stream`` 由调用方（provider.forward 已算好的本地判定）显式传入，**不**
    走白名单：OpenAI 兼容上游按**请求体**这个字段决定返回 SSE 还是整块 JSON，
    漏了它上游会回非流式 JSON，而本地按 SSE 逐行解析——解析不出 ``data:``
    事件直接判成「首事件前空流」换号，客户端拿到的是 502（真机/契约实证）。
    """
    upstream: dict[str, Any] = {"model": model, "stream": stream}
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


def _get_json(
    url: str,
    access_token: str,
    timeout: float,
    cred: dict[str, Any] | None = None,
) -> dict[str, Any]:
    req = urllib.request.Request(
        url, headers=device_headers(cred or {}, access_token=access_token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception as exc:  # noqa: BLE001 - 统一转 OSError 语义，调用方按失败处理
        raise OSError(f"Kimi 上游请求失败（{url}）: {exc}") from exc
    return data if isinstance(data, dict) else {}


def fetch_models(
    base_url: str,
    access_token: str,
    cred: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """``GET /v1/models`` 并归一成运行时目录条目。

    官方端点使用 OpenAI 常见的 ``{"data": [...]}`` 形状。每项优先用 ``id``
    作路由 id；少数兼容实现只有 ``name`` / ``display_name`` 时依次回退。新增
    模型保留这三个可读字段，并生成 provider 使用的 ``description``。
    """
    payload = _get_json(
        f"{normalize_base_url(base_url)}/models", access_token, timeout, cred)
    raw_models = payload.get("data")
    if not isinstance(raw_models, list):
        raise OSError("Kimi /models 响应缺少 data 数组")

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_models:
        if isinstance(raw, str):
            raw = {"id": raw}
        if not isinstance(raw, dict):
            continue
        model_id = str(
            raw.get("id") or raw.get("name") or raw.get("display_name") or ""
        ).strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        entry: dict[str, Any] = {"id": model_id}
        for key in ("name", "display_name"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value.strip()
        entry["description"] = str(
            entry.get("display_name") or entry.get("name") or model_id)
        models.append(entry)
    if not models:
        raise OSError("Kimi /models 未返回有效模型")
    return models


def fetch_me(base_url: str, access_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """``GET /v1/me``：user_id / nickname / phone / user_level_name（面板展示用）。"""
    return _get_json(f"{normalize_base_url(base_url)}/me", access_token, timeout)


def fetch_usages(base_url: str, access_token: str, timeout: float = 15.0) -> dict[str, Any]:
    """``GET /v1/usages``：5h / 7d 双池用量。"""
    return _get_json(f"{normalize_base_url(base_url)}/usages", access_token, timeout)


def _num(value: Any) -> float | None:
    """上游数字（可能是字符串 "100" / 数字 / 缺失）→ float，缺失/非法返回 None。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def usages_to_items(data: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
    """``/v1/usages`` 响应 → 额度条目（``percent`` 是**已用**，前端进度条语义）。

    **绝对积分数优先**（真机 2026-10-05 实测：上游一直都在发，只是这里早先
    只读了 ratio 把它丢了——用户看到的「剩 93.31 / 100」其实是百分比伪装成
    积分）。逐窗口独立取数、互为回退：

    - 5 小时窗口：``limits[0].detail``（``{limit, used, remaining, resetTime}``，
      window.duration=300 分钟即 5h）→ 没有则 ``usages.limit_5h.used_ratio``
    - 7 天池：顶层 ``usage``（``{limit, used, remaining, resetTime}``，实测是
      7d 池的合成值）→ 没有则 ``usages.limit_7d.used_ratio``

    5 小时窗口在前（周期由小到大，与 zcode 顺序契约一致）。ratio 兜底发的是
    percent（total=100、unit=percent），绝对数发 credit——前端按数字直接渲染，
    空响应（Free 层实测 ``{}``）返回 ``[]``。
    """
    buckets = data.get("usages") if isinstance(data.get("usages"), dict) else {}
    items: list[dict[str, Any]] = []

    # -- 5 小时窗口：limits[] 的绝对数优先 -------------------------------
    detail = None
    for lim in data.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        win = lim.get("window") or {}
        try:
            minutes = float(win.get("duration") or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        if str(win.get("timeUnit") or "").endswith("MINUTE") and minutes == 300:
            detail = lim.get("detail")
            break
    detail = detail if isinstance(detail, dict) else {}
    d_total, d_used = _num(detail.get("limit")), _num(detail.get("used"))
    if d_total is not None and d_total > 0 and d_used is not None:
        remaining = _num(detail.get("remaining"))
        if remaining is None:
            remaining = max(d_total - d_used, 0.0)
        used_pct = round(d_used / d_total * 100, 2)
        items.append({
            "label": f"{prefix}5 小时窗口",
            "used": round(d_used, 4),
            "total": round(d_total, 4),
            "remaining": round(remaining, 4),
            "percent": used_pct,
            "reset_ts": iso_to_epoch(detail.get("resetTime")) or None,
            "expire_ts": None,
            "unit": "credit",
        })
    else:
        bucket = buckets.get("limit_5h")
        ratio = bucket.get("used_ratio") if isinstance(bucket, dict) else None
        if isinstance(ratio, (int, float)):
            used = round(float(ratio) * 100, 2)
            items.append({
                "label": f"{prefix}5 小时窗口",
                "used": used,
                "total": 100,
                "remaining": round(100 - used, 2),
                "percent": used,
                "reset_ts": iso_to_epoch(bucket.get("reset_time")) or None,
                "expire_ts": None,
                "unit": "percent",
            })

    # -- 7 天池：顶层 usage 的绝对数优先，ratio 兜底 ----------------------
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    u_total, u_used = _num(usage.get("limit")), _num(usage.get("used"))
    if u_total is not None and u_total > 0 and u_used is not None:
        remaining = _num(usage.get("remaining"))
        if remaining is None:
            remaining = max(u_total - u_used, 0.0)
        used_pct = round(u_used / u_total * 100, 2)
        items.append({
            "label": f"{prefix}7 天池",
            "used": round(u_used, 4),
            "total": round(u_total, 4),
            "remaining": round(remaining, 4),
            "percent": used_pct,
            "reset_ts": iso_to_epoch(usage.get("resetTime")) or None,
            "expire_ts": None,
            "unit": "credit",
        })
    else:
        bucket = buckets.get("limit_7d")
        ratio = bucket.get("used_ratio") if isinstance(bucket, dict) else None
        if isinstance(ratio, (int, float)):
            used = round(float(ratio) * 100, 2)
            items.append({
                "label": f"{prefix}7 天池",
                "used": used,
                "total": 100,
                "remaining": round(100 - used, 2),
                "percent": used,
                # 5h/7d 都是周期重置（不是权益到期），不给 expire_ts 防「快到期」误报
                "reset_ts": iso_to_epoch(bucket.get("reset_time")) or None,
                "expire_ts": None,
                "unit": "percent",
            })
    return items


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
