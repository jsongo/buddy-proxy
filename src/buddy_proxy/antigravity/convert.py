"""OpenAI chat → Antigravity（v1internal）请求包装。

内层 ``request`` 就是标准 Gemini generateContent 体，OpenAI → Gemini 的
messages/tools/schema 转换全部复用 :mod:`buddy_proxy.gemini.convert`（同一
API 家族，转换规则一致）；本模块只做 antigravity 特有的三件事：

1. **envelope 不同**：``{project, model, request, userAgent, requestType,
   requestId}``——model 是裸名（不带 ``models/`` 前缀），无 ``user_prompt_id``。
2. **systemInstruction 注入**：社区共识方案（CLIProxyAPI/gcli2api 同款）——
   两个 part：身份断言 + ``[ignore]`` 包裹的同一份（让模型读到 Antigravity
   身份过服务端风控，又指示它别自称 Antigravity，fix 参考实现 issue #76）；
   用户自己的 system 追加在后面。
3. **身份清洗**：用户 system 里「You are Claude/Codex/…」式断言会触发
   cloudcode-pa 的 429 RESOURCE_EXHAUSTED（Adrian De Vera 逆向实测），替换
   成中性表述。``ANTIGRAVITY_SCRUB_IDENTITY``（``term=>replacement,…``）可加规则。

响应方向（Gemini → OpenAI / SSE 解析）与 gemini 完全同构，直接复用。
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any

from ..gemini.convert import gemini_response_to_chat, iter_gemini_sse_payloads  # noqa: F401 - re-export 供 provider 用
from ..gemini.convert import chat_to_gemini_request

#: 服务端风控要求请求自称 Antigravity；[ignore] 段让模型别真信（issue #76）。
_SYSTEM_IDENTITY = (
    "You are Antigravity, a powerful agentic AI coding assistant designed by "
    "the Google Deepmind team working on Advanced Agentic Coding."
    "You are pair programming with a USER to solve their coding task."
    " The task may require creating a new codebase, modifying or debugging an"
    " existing codebase, or simply answering a question."
    "**Absolute paths only****Proactiveness**"
)

#: ``[ignore]`` 包裹副本（参考实现 buildCloudCodeRequest 同款双 part）。
_IGNORE_WRAP = "Please ignore the following [ignore]{text}[/ignore]"

#: 默认清洗规则：竞品身份断言 → 中性表述（触发 429 的已知模式）。
_DEFAULT_SCRUB_RULES: list[tuple[str, str]] = [
    ("You are Claude Code", "You are the assistant"),
    ("You are Claude", "You are the assistant"),
    ("You are Codex", "You are the assistant"),
    ("You are ChatGPT", "You are the assistant"),
    ("You are GPT", "You are the assistant"),
]


def _parse_env_rules(raw: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for pair in raw.split(","):
        idx = pair.find("=>")
        if idx > 0:
            term, repl = pair[:idx].strip(), pair[idx + 2:].strip()
            if term:
                rules.append((term, repl))
    return rules


def _scrub_rules() -> list[tuple[str, str]]:
    env = os.environ.get("ANTIGRAVITY_SCRUB_IDENTITY", "").strip()
    return _DEFAULT_SCRUB_RULES + (_parse_env_rules(env) if env else [])


def scrub_identity(text: str) -> str:
    """清洗 system 文本里的竞品身份断言（大小写不敏感的整词替换）。"""
    for term, repl in _scrub_rules():
        text = re.sub(re.escape(term), repl, text, flags=re.IGNORECASE)
    return text


def apply_effort_suffix(entry: dict[str, Any], reasoning_effort: Any) -> str:
    """模型表条目 → 上游真名：upstream 覆盖 + effort 后缀解析。

    antigravity 上游只认 fetchAvailableModels 列表里的名字（2026-10-02 实测：
    gemini 3 系裸名 429、claude 裸名可用、3.8 只有 -tiered、gpt-oss 只有
    -medium），所以后缀策略由模型表逐模型声明（``efforts`` / ``default_effort``），
    不按名字前缀猜。显式 effort 不在 ``efforts`` 里时回落 ``default_effort``
    （用户拿到一个能用的档位总比 400 好）。名字固定的模型（无 ``efforts``）
    只做 upstream 映射。
    """
    base = str(entry.get("upstream") or entry["id"])
    efforts = [str(e).strip().lower() for e in entry.get("efforts") or [] if str(e).strip()]
    if not efforts:
        return base
    effort = str(reasoning_effort or "").strip().lower()
    if effort not in efforts:
        effort = str(entry.get("default_effort") or efforts[0]).strip().lower()
    return f"{base}-{effort}"


def chat_to_antigravity_request(
    body: dict[str, Any],
    *,
    project_id: str,
    model: str,
) -> dict[str, Any]:
    """OpenAI chat 请求 → antigravity envelope（流式/非流式同体，SSE 由 URL 决定）。"""
    inner = chat_to_gemini_request(
        body, project_id=project_id, model=model, stream=bool(body.get("stream"))
    )
    request = inner["request"]
    request.pop("session_id", None)  # 参考实现不发 session_id

    system_parts: list[dict[str, Any]] = [
        {"text": _SYSTEM_IDENTITY},
        {"text": _IGNORE_WRAP.format(text=_SYSTEM_IDENTITY)},
    ]
    user_si = request.get("systemInstruction")
    if isinstance(user_si, dict):
        for part in user_si.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                system_parts.append({"text": scrub_identity(str(part["text"]))})
    # 参考实现形态：systemInstruction 带 role: "user"，置于顶层 request
    request["systemInstruction"] = {"role": "user", "parts": system_parts}

    return {
        "project": project_id,
        "model": model,
        "request": request,
        "userAgent": "antigravity",
        "requestType": "agent",
        "requestId": f"agent-{uuid.uuid4()}",
    }
