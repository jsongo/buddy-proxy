"""Qoder 模型目录：从上游拉取 + 本地兜底 + 别名归一。

上游目录（``/algo/api/v2/model/list?Encode=1``，需 COSY 签名）返回按场景
分组的结构：``chat`` / ``assistant`` / ``inline`` / ``quest`` / ``qwork`` /
``experts`` / ``qwake`` / ``app`` / ``byok_*``。同一模型在多个场景里重复
出现，本模块按 ``key`` 去重（取 ``chat`` 优先）。

目录里的 ``key`` 是**内部代号**（如 ``qmodel_38max``），``display_name``
才是用户看到的名称（``Qwen3.8-Max``）。两者都接受：``resolve_key`` 做归一。

上游不可用（未登录/超时）时回落到 ``FALLBACK_MODELS``——里面只放实测确认
可用的条目，避免把不存在的模型报给客户端。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from .config import Region
from .cosy import sign
from .credentials import Credential

log = logging.getLogger(__name__)

#: 目录缓存时长（秒）——目录变化很慢，避免每次 /v1/models 都签名请求。
CACHE_TTL_S = 600

#: 场景优先级：上游同一模型在多个场景重复出现，按此顺序取第一个。
#: ``chat`` 是面向对话的主场景；``developer``/``assistant``/``app`` 与之
#: 基本同集，作为兜底（不同账号可见场景略有差异）。
CHAT_SCENES = ("chat", "developer", "assistant", "app")

#: 上游不可用时的兜底目录（全部为实测可用的 key）。
FALLBACK_MODELS: tuple[dict[str, Any], ...] = (
    {"key": "qmodel_38max", "display_name": "Qwen3.8-Max", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.2, "is_free": True, "max_input_tokens": 180000},
    {"key": "qfmodel", "display_name": "Qwen3.8-Flash", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.0, "is_free": True, "max_input_tokens": 180000},
    {"key": "qmodel_latest", "display_name": "Qwen3.7-Max", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.1, "max_input_tokens": 1000000},
    {"key": "qmodel", "display_name": "Qwen3.7-Plus", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.04, "max_input_tokens": 1000000},
    {"key": "dfmodel", "display_name": "DeepSeek-Flash", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.1, "max_input_tokens": 1000000},
    {"key": "dmodel", "display_name": "DeepSeek-V4-Pro", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.5, "max_input_tokens": 1000000},
    {"key": "gmodel", "display_name": "GLM-5.3", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.8, "max_input_tokens": 180000},
    {"key": "gfmodel", "display_name": "GLM-5.3-Flash", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.1, "max_input_tokens": 1000000},
    {"key": "kmodel_latest", "display_name": "Kimi-K3", "is_reasoning": True,
     "is_vl": True, "price_factor": 1.4, "max_input_tokens": 180000},
    {"key": "kmodel", "display_name": "Kimi-K2.8-Preview", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.8},
    {"key": "q37fmodel", "display_name": "Qwen3.7-Flash", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.02, "max_input_tokens": 1000000},
    {"key": "gm51model", "display_name": "GLM-5.2", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.6, "max_input_tokens": 180000},
    {"key": "mmodel", "display_name": "MiniMax-M2.7", "is_reasoning": True,
     "is_vl": True, "price_factor": 0.2, "max_input_tokens": 180000},
    {"key": "cmodel", "display_name": "Cantus", "is_reasoning": True,
     "is_vl": True, "price_factor": 4.0, "max_input_tokens": 180000},
    {"key": "smodel", "display_name": "Sonus", "is_reasoning": True,
     "is_vl": True, "price_factor": 8.0, "max_input_tokens": 180000},
)

#: 档位模型（由平台路由，不绑定具体底层模型）。
#:
#: 实测（2026-09）：上游**只有 `auto` 这一个档位**——9 个场景（chat/developer/
#: assistant/inline/quest/qwork/experts/qwake/app）的目录里都只有 `auto`。
#: 早期版本按客户端 UI 的印象合成过 ultimate/performance/efficient，但上游
#: 不认这三个名字，调用会 502「Unsupported model "performance"」。故只保留
#: `auto`；将来上游若真给出别的档位，会在目录里自然出现，不必在这里补。
TIER_MODELS: tuple[dict[str, Any], ...] = (
    {"key": "auto", "display_name": "Auto", "is_reasoning": True, "is_vl": True,
     "price_factor": 0.5, "max_input_tokens": 200000},
)

#: 旧模型：仅用于兼容调用，**不在模型列表里展示**（列表太长反而找不到要用的）。
#:
#: 判定按**上游 key**——对外 id 与 key 一一对应，用 key 更稳（不会因改名漏掉）。
#: 隐藏 ≠ 停用：客户端直接点名仍可调用，只是 /v1/models 与管理页不列出来。
#: 想恢复展示：把 key 从本集合里删掉即可。
HIDDEN_KEYS: frozenset[str] = frozenset({
    "qmodel_latest",   # Qwen3.7-Max
    "qmodel",          # Qwen3.7-Plus
    "q37fmodel",       # Qwen3.7-Flash
    "gm51model",       # GLM-5.2
    "kmodel",          # Kimi-K2.8-Preview
    "cmodel",          # Cantus
    "smodel",          # Sonus
})
#: 注意：``dfmodel``（上游显示名 ``DeepSeek-Flash``，即用户口中的 4.1-Flash）
#: **不隐藏**——它是最常用的模型之一，名字看着旧不代表模型旧。


def is_hidden(entry: dict[str, Any]) -> bool:
    """该条目是否属于「旧模型，不展示」。"""
    return str(entry.get("key") or "").strip() in HIDDEN_KEYS


#: 上游 key -> 对外模型 id（小写真实名）。
#:
#: 上游目录只给 ``key``（内部代号，如 ``qmodel_38max``）与 ``display_name``
#: （``Qwen3.8-Max``）——前者看不懂、后者大小写混杂难记。对外统一用小写真实名，
#: 由本表映射；未登记的新模型回落到 ``display_name`` 的小写形态（见
#: :func:`public_model_id`），因此上游上新模型不必改这里。
#:
#: 冲突说明：其中若干名字（``qwen3.8-max``/``glm-5.3``/``kimi-k3``/…）与
#: CodeBuddy 静态表同名——同一批模型两边都有，裸名调用由 ``forward_chat``
#: 的路由决定归属；要强制走本通道请写 ``qoder/<id>``。
MODEL_IDS: dict[str, str] = {
    "qmodel_38max": "qwen3.8-max",
    "qfmodel": "qwen3.8-flash",
    "qmodel_latest": "qwen3.7-max",
    "qmodel": "qwen3.7-plus",
    "q37fmodel": "qwen3.7-flash",
    "dmodel": "deepseek-v4-pro",
    # 上游把 4.1-Flash 这份权重叫 ``DeepSeek-Flash``（不带版本号）；用户按版本
    # 称呼它，故对外 id 用带版本号的名字，和 CodeBuddy 通道的
    # ``deepseek-v4.1-flash`` 对齐，两边叫法一致。
    "dfmodel": "deepseek-v4.1-flash",
    "gmodel": "glm-5.3",
    "gfmodel": "glm-5.3-flash",
    "gm51model": "glm-5.2",
    "kmodel_latest": "kimi-k3",
    "kmodel": "kimi-k2.8-preview",
    "mmodel": "minimax-m2.7",
    "cmodel": "cantus",
    "smodel": "sonus",
}


def public_model_id(entry: dict[str, Any]) -> str:
    """目录条目 -> 对外模型 id（小写真实名）。

    优先查 :data:`MODEL_IDS`；未登记的新模型把 ``display_name`` 小写化后使用
    （上游 display_name 本来就是人读的名字，比内部代号友好）。
    """
    key = str(entry.get("key") or "").strip()
    if key in MODEL_IDS:
        return MODEL_IDS[key]
    display = str(entry.get("display_name") or "").strip()
    return display.lower() if display else key


def _canonical_id_slug(name: str) -> str:
    """对外 id 的归一键：转小写、点号保留、空格转连字符。

    与 ``_normalize``（去掉全部非字母数字）不同——这里要保留 ``qwen3.8-max``
    里的点与连字符，因为对外 id 本身就是给人看的。
    """
    return str(name).strip().lower().replace("_", "-").replace(" ", "-")


class Catalog:
    """模型目录（带 TTL 缓存 + 上游失败兜底）。"""

    def __init__(self, region: Region) -> None:
        self.region = region
        self._models: list[dict[str, Any]] = []
        self._fetched_at: float = 0.0
        self._aliases: dict[str, str] = {}
        self._built_aliases()

    # -- 别名 ---------------------------------------------------------------

    def _built_aliases(self) -> None:
        """用兜底目录先把别名表建起来（拉取成功后重建）。"""
        self._rebuild_aliases(list(FALLBACK_MODELS) + list(TIER_MODELS))

    def _rebuild_aliases(self, models: list[dict[str, Any]]) -> None:
        """上游 key / display_name / 对外 id 三者都指向同一个 key。

        三种写法都要能解析：对外 id（``qwen3.8-flash``，推荐给用户）、
        display_name（``Qwen3.8-Flash``，官方文档里的写法）、内部 key
        （``qfmodel``，老客户端/脚本里可能已在用）。
        """
        self._aliases = {}
        for m in models:
            key = str(m.get("key") or "").strip()
            if not key:
                continue
            self._aliases[_normalize(key)] = key
            display = str(m.get("display_name") or "").strip()
            if display:
                self._aliases[_normalize(display)] = key
            # 对外 id：既按 _normalize 口径（去符号）登记，也按 slug 口径
            # （保留 ``.``/``-``）登记，两种输入都能命中。
            public = public_model_id(m)
            if public:
                self._aliases[_normalize(public)] = key
                self._aliases.setdefault(_canonical_id_slug(public), key)

    def entry(self, model: str) -> dict[str, Any]:
        """按 key/显示名取目录原始条目；找不到时返回最小可用条目。

        ``provider`` 构造出站信封（``model_config``/``chat_context``）要读
        ``is_reasoning`` / ``is_vl`` / ``max_input_tokens``，因此这里必须
        永远返回一个 dict，而不是 ``None``。
        """
        key = self.resolve_key(model)
        for entry in self._models or list(FALLBACK_MODELS) + list(TIER_MODELS):
            if str(entry.get("key") or "") == key:
                return entry
        return {"key": key, "display_name": key}

    def resolve_key(self, model: str) -> str:
        """把用户给的名字归一成上游 key。

        接受四种写法（大小写均不敏感）：对外 id（``qwen3.8-flash``）、
        display_name（``Qwen3.8-Flash``）、内部 key（``qfmodel``）、以及
        ``qoder/`` 前缀形态。上游对大小写不敏感，但 ``X-Model-Key`` 与 body 里的
        ``model`` 必须自洽，故这里统一成 catalog 的规范 key；未知名字原样返回，
        交给上游报错（保留前向兼容：新模型不用改代码就能用）。
        """
        raw = (model or "").strip()
        if not raw:
            return raw
        # 允许带 "qoder/" 前缀调用（客户端从 /v1/models 复制粘贴的形态）
        if "/" in raw:
            prefix, _, rest = raw.partition("/")
            if prefix == "qoder" and rest:
                raw = rest
        if raw in self._aliases.values():
            return raw  # 已经是上游 key
        hit = self._aliases.get(raw.lower())
        if hit is None:
            hit = self._aliases.get(_canonical_id_slug(raw))
        if hit is None:
            hit = self._aliases.get(_normalize(raw))
        return hit if hit is not None else raw

    # -- 拉取 ---------------------------------------------------------------

    async def fetch(self, cred: Credential, *, force: bool = False) -> list[dict[str, Any]]:
        """拉取目录（带缓存）；失败时回落到兜底目录。"""
        now = time.monotonic()
        if not force and self._models and now - self._fetched_at < CACHE_TTL_S:
            return self._models
        try:
            models = await self._fetch_remote(cred)
        except Exception as exc:  # noqa: BLE001 - 目录失败不该阻断转发
            log.warning("qoder 模型目录拉取失败，使用兜底目录: %s", exc)
            models = []
        if models:
            self._models = models
            self._fetched_at = now
            self._rebuild_aliases(models)
        elif not self._models:
            self._models = self.fallback()
        return self._models

    async def _fetch_remote(self, cred: Credential) -> list[dict[str, Any]]:
        url = self.region.model_list_url()
        _, headers = sign(
            url,
            "",
            cred.uid,
            cred.token,
            cred.machine_id,
            encode=False,
        )
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        return self._parse(data)

    def _parse(self, data: object) -> list[dict[str, Any]]:
        """按场景分组解析，按 key 去重（场景顺序即优先级）。"""
        if not isinstance(data, dict):
            return []
        seen: dict[str, dict[str, Any]] = {}
        for scene in CHAT_SCENES:
            entries = data.get(scene)
            if not isinstance(entries, list):
                continue
            for item in entries:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "").strip()
                if not key or key in seen:
                    continue
                if item.get("enable") is False:
                    continue
                if str(item.get("source") or "system") != "system":
                    # BYOK / 自定义模型不在个人通道能力范围内
                    continue
                seen[key] = item
        # 档位模型上游目录里也有，但按 chat 场景已经覆盖；补齐缺失的档位。
        for tier in TIER_MODELS:
            seen.setdefault(str(tier["key"]), dict(tier))
        return list(seen.values())

    @staticmethod
    def fallback() -> list[dict[str, Any]]:
        """无凭证/上游不可用时的本地目录。"""
        out = [dict(m) for m in TIER_MODELS]
        out += [dict(m) for m in FALLBACK_MODELS]
        return out


def _normalize(name: str) -> str:
    """归一键：转小写并去掉 ``-`` / ``_`` / 空格 / ``.``。

    ``DeepSeek-V4.1-Flash``、``deepseek-v4.1-flash``、``deepseek v41 flash``
    因此指向同一个模型。
    """
    return "".join(ch for ch in name.lower() if ch.isalnum())


def to_openai_model(entry: dict[str, Any], provider_id: str) -> dict[str, Any]:
    """目录条目 -> OpenAI ``/v1/models`` 元素（带 buddy-proxy 扩展字段）。

    ``id`` 用**对外小写真实名**（``qoder/qwen3.8-flash``）而不是上游内部代号
    （``qoder/qfmodel``）——后者看不出是什么模型。上游 key 仍放在 ``upstream_key``
    里供排障对照，调用时两种写法都接受（见 :meth:`Catalog.resolve_key`）。
    """
    key = str(entry.get("key") or "")
    display = str(entry.get("display_name") or key)
    public = public_model_id(entry) or key
    price = entry.get("price_factor")
    tags: list[str] = []
    if entry.get("is_reasoning"):
        tags.append("reasoning")
    if entry.get("is_vl"):
        tags.append("vision")
    if entry.get("is_free"):
        tags.append("free")
    model = {
        "id": f"{provider_id}/{public}",
        "object": "model",
        "owned_by": provider_id,
        "name": display,
        "description": display,
        "provider": provider_id,
        "vendor": provider_id,
        "upstream_key": key,
        "max_input": entry.get("max_input_tokens"),
        "context_window": entry.get("max_input_tokens"),
        "reasoning": bool(entry.get("is_reasoning")),
        "tags": tags,
    }
    if isinstance(price, (int, float)):
        # 上游 price_factor 就是「倍率」（0.2× = 0.2）；保留两位便于展示。
        model["credits"] = round(float(price), 2)
        model["price_factor"] = round(float(price), 2)
    return model
