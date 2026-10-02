"""Code Assist onboarding：loadCodeAssist → （必要时）onboardUser → 项目 ID。

与真 CLI（code_assist/setup.js）行为逐项对齐：

1. ``loadCodeAssist``，metadata 固定 ``IDE_UNSPECIFIED / PLATFORM_UNSPECIFIED /
   GEMINI``；已有 project 时带 ``cloudaicompanionProject`` 与 ``duetProject``。
2. 响应有 ``currentTier`` → 已开通：项目取 ``cloudaicompanionProject``
   （没有且没传 project 才报错，真 CLI 此时提示要 GOOGLE_CLOUD_PROJECT）。
3. 没有 ``currentTier`` → 走 onboarding：tier 取 ``allowedTiers`` 里
   ``isDefault`` 的那个（都没有则 legacy-tier）。**free-tier 特殊**：用托管
   项目，onboardUser 请求里**不能带** cloudaicompanionProject（带了报
   Precondition Failed——setup.js 113-120 行注释明说）。其它 tier 带上。
4. ``onboardUser`` 是 LRO：``done=false`` 且有 ``name`` 时轮询
   ``GET /v1internal/{name}``（真 CLI 每 5 秒一次），直到 done，
   从 ``response.cloudaicompanionProject.id`` 拿项目。

与插件 (cpa-plugin-gemini-cli) 的差异：插件把「无 currentTier」一律先盲发
一次不带项目的 onboardUser 来「发现项目」，这在非 free tier 是多余请求
（多一次无意义调用也是指纹差异）；这里按真 CLI 逻辑走。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any


log = logging.getLogger(__name__)

CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com"
API_VERSION = "v1internal"

#: 真 CLI 的 metadata（setup.js coreClientMetadata），IDE 类型固定这三个。
CORE_METADATA = {
    "ideType": "IDE_UNSPECIFIED",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}

TIER_FREE = "free-tier"
TIER_LEGACY = "legacy-tier"
TIER_STANDARD = "standard-tier"

#: LRO 轮询节奏：真 CLI 5 秒一次；总预算 60 秒（6 轮）足够 onboarding 完成。
_POLL_INTERVAL_S = 5.0
_POLL_BUDGET_S = 60.0


class SetupError(RuntimeError):
    """onboarding 失败（loadCodeAssist/onboardUser 被拒、拿不到项目）。"""


def _code_assist_post(
    access_token: str, method: str, body: dict[str, Any], timeout: float = 30.0
) -> dict[str, Any]:
    """POST /v1internal:<method>。请求头走 fingerprint（与真 CLI 一致）。"""
    from .fingerprint import user_agent

    url = f"{CODE_ASSIST_BASE}/{API_VERSION}:{method}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": user_agent(""),
            "x-goog-api-client": _goog_api_client(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:500]
        except Exception:  # noqa: BLE001
            pass
        raise SetupError(f"{method} HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SetupError(f"{method} 网络失败: {exc.reason}") from exc


def _goog_api_client() -> str:
    from .fingerprint import GOOG_API_CLIENT

    return GOOG_API_CLIENT


def _code_assist_get_operation(
    access_token: str, name: str, timeout: float = 30.0
) -> dict[str, Any]:
    """GET /v1internal/{name}（LRO 轮询，真 CLI server.requestGetOperation）。"""
    from .fingerprint import user_agent

    url = f"{CODE_ASSIST_BASE}/{API_VERSION}/{name}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "User-Agent": user_agent(""),
            "x-goog-api-client": _goog_api_client(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:500]
        except Exception:  # noqa: BLE001
            pass
        raise SetupError(f"getOperation HTTP {exc.code}: {detail}") from exc


def _default_tier(load_res: dict[str, Any]) -> dict[str, Any]:
    """allowedTiers 里 isDefault 的 tier；没有则 legacy-tier（setup.js:170）。"""
    for tier in load_res.get("allowedTiers") or []:
        if isinstance(tier, dict) and tier.get("isDefault"):
            return tier
    return {"id": TIER_LEGACY, "name": "", "description": ""}


def _project_from_value(value: Any) -> str:
    """从 cloudaicompanionProject 字段提取项目 ID（上游出现过多种形态）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("id", "projectId", "project"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
            if isinstance(inner, dict):
                nested = inner.get("id")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _ineligible_reasons(load_res: dict[str, Any]) -> str:
    """ineligibleTiers 里的真实拒绝原因（tierId: reasonCode: reasonMessage）。"""
    parts: list[str] = []
    for tier in load_res.get("ineligibleTiers") or []:
        if not isinstance(tier, dict):
            continue
        parts.append(
            "{}: {}: {}".format(
                tier.get("tierId") or "?",
                tier.get("reasonCode") or "?",
                str(tier.get("reasonMessage") or "").strip(),
            ).strip(": ")
        )
    return "；".join(parts)


def _load_diag(load_res: dict[str, Any]) -> str:
    """loadCodeAssist 响应摘要（失败时给人看的诊断）。"""
    parts: list[str] = []
    cur = load_res.get("currentTier")
    if isinstance(cur, dict) and cur.get("id"):
        parts.append(f"currentTier={cur['id']}")
    allowed = [
        str(t.get("id"))
        for t in load_res.get("allowedTiers") or []
        if isinstance(t, dict) and t.get("id")
    ]
    if allowed:
        parts.append("allowedTiers=" + ",".join(allowed))
    reasons = _ineligible_reasons(load_res)
    if reasons:
        parts.append(f"ineligibleTiers=[{reasons}]")
    return "; ".join(parts)


def _missing_project_error(
    load_res: dict[str, Any],
    onboard_resp: dict[str, Any] | None = None,
    *,
    base: str = "onboarding 完成但未返回项目 ID",
) -> SetupError:
    """走完流程却没项目 ID 时的报错。

    真 CLI 此时会看 ineligibleTiers 把真实拒绝原因抛出来
    （throwIneligibleOrProjectIdError，setup.js:163）；照做——只说
    「未返回项目 ID」等于把上游给的原因吞掉，用户没法自查。
    """
    reasons = _ineligible_reasons(load_res)
    msg = f"账号不符合 Code Assist 资格：{reasons}" if reasons else base
    diag = _load_diag(load_res)
    if diag:
        msg += f"\n    loadCodeAssist: {diag}"
    if onboard_resp is not None:
        msg += f"\n    onboardUser: {json.dumps(onboard_resp, ensure_ascii=False)[:600]}"
    return SetupError(msg)


def _wait_lro(access_token: str, op: dict[str, Any]) -> dict[str, Any]:
    """onboardUser 的 LRO 等待（done=false 且有 name 时轮询）。"""
    deadline = time.monotonic() + _POLL_BUDGET_S
    while not op.get("done") and op.get("name"):
        if time.monotonic() > deadline:
            raise SetupError("onboardUser 超时（LRO 未在预算内完成）")
        time.sleep(_POLL_INTERVAL_S)
        op = _code_assist_get_operation(access_token, op["name"])
    return op


def setup_code_assist(
    access_token: str, project_id: str = ""
) -> dict[str, Any]:
    """执行 setup，返回 ``{"project_id", "tier", "tier_name"}``。

    ``project_id``：已知的托管项目（重登/换号时可以带，一般留空）。
    失败抛 :class:`SetupError` / :class:`AuthError`。
    """
    metadata = dict(CORE_METADATA)
    load_body: dict[str, Any] = {"metadata": metadata}
    if project_id:
        load_body["cloudaicompanionProject"] = project_id
        load_body["metadata"] = {**metadata, "duetProject": project_id}

    load_res = _code_assist_post(access_token, "loadCodeAssist", load_body)
    if not isinstance(load_res, dict):
        raise SetupError("loadCodeAssist 返回异常")

    # 已开通：直接取项目
    if load_res.get("currentTier"):
        tier = load_res.get("paidTier") or load_res.get("currentTier") or {}
        proj = _project_from_value(load_res.get("cloudaicompanionProject"))
        if not proj and project_id:
            proj = project_id
        if not proj:
            raise _missing_project_error(
                load_res,
                base=(
                    "账号已开通但未返回 cloudaicompanionProject；"
                    "请设置 GOOGLE_CLOUD_PROJECT 后重试（工作区账号）"
                ),
            )
        return {
            "project_id": proj,
            "tier": str(tier.get("id") or TIER_STANDARD),
            "tier_name": str(tier.get("name") or ""),
        }

    # 未开通：onboarding。free-tier 用托管项目，请求不能带 project。
    tier = _default_tier(load_res)
    tier_id = str(tier.get("id") or TIER_LEGACY)
    if tier_id == TIER_FREE:
        onboard_body = {"tierId": tier_id, "metadata": metadata}
    else:
        onboard_body = {
            "tierId": tier_id,
            "metadata": {**metadata, "duetProject": project_id} if project_id else metadata,
        }
        if project_id:
            onboard_body["cloudaicompanionProject"] = project_id

    lro = _code_assist_post(access_token, "onboardUser", onboard_body)
    lro = _wait_lro(access_token, lro)

    # LRO 以 error 收尾时（done=true + error，Google LRO 经典形态）：
    # 之前会掉进「未返回项目 ID」的兜底分支，把上游给的真实原因吞掉。
    lro_err = lro.get("error")
    if isinstance(lro_err, dict) and lro_err:
        raise SetupError(
            "onboardUser 失败: [{}] {}".format(
                lro_err.get("code") or "?",
                str(lro_err.get("message") or "").strip() or json.dumps(lro_err, ensure_ascii=False)[:400],
            )
        )

    resp = lro.get("response") if isinstance(lro.get("response"), dict) else {}
    proj = _project_from_value(resp.get("cloudaicompanionProject"))
    if not proj:
        # 防御：个别响应把项目放在 LRO 顶层而非 response 里
        proj = _project_from_value(lro.get("cloudaicompanionProject"))
    if not proj and project_id:
        proj = project_id
    if not proj:
        raise _missing_project_error(load_res, lro)
    return {"project_id": proj, "tier": tier_id, "tier_name": str(tier.get("name") or "")}
