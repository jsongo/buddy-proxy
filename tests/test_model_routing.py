"""模型路由回归：带前缀 id 的通道（Qoder 形态）与别名兜底。

本文件锁定三处**静默失效**类 Bug 的修复（2026-09 联调中发现，均非新增功能
引入，而是 Qoder 的带前缀 id 第一次让它们显形）：

1. **停用键双前缀**：管理页把带前缀的 ``id``（``qoder/qfmodel``）当 ``model``
   回传，与 ``provider`` 再拼一次得到 ``qoder/qoder/qfmodel``，而转发侧用的是
   剥前缀后的裸名 —— 写入的键永远命中不了，「停用」显示成功但完全失效。
2. **别名请求掉进兜底通道**：自动路由只比 ``m["id"] == model``；目录带前缀、
   客户端发裸名时漏配，请求掉进兜底通道被上游拒为 11102。
3. **别名抢走别的通道按 id 命中的模型**：别名若与精确匹配同轮参与、按注册序
   先到先得，注册靠前的通道会把别人的模型抢走（``glm-5.3`` 曾被 qoder 从
   zcode 抢走）。故路由分两轮：先全部按 id 精确匹配，再退回别名兜底。

运行：
    .venv/bin/python -m pytest tests/test_model_routing.py -v
"""
from __future__ import annotations

import inspect
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from buddy_proxy import __main__ as m
from buddy_proxy.core import settings as settings_mod
from buddy_proxy.core import state as st
from buddy_proxy.core.metrics import MetricsCollector
from buddy_proxy.providers.base import BaseProvider


def _make_state(providers, tmp_path):
    client = mock.MagicMock()
    client.endpoint = "https://fake.endpoint.invalid"
    client.auth_headers.return_value = {}
    client.session = {"auth": {"accessToken": "t",
                               "expiresAt": int((time.time() + 3600) * 1000)}}
    return SimpleNamespace(
        client=client,
        providers=providers,
        mock_dir=None,
        started_at=time.time(),
        enable_desensitize=False,
        enable_optimize_context=False,
        verbose_llm=False,
        default_provider="codebuddy",
        default_model=None,
        disabled_models=set(),
        model_schedules={},
        metrics=MetricsCollector(tmp_path / "metrics.jsonl"),
        write_log=mock.MagicMock(),
        ensure_auth=mock.MagicMock(),
        logger=mock.MagicMock(),
        json_logger=mock.MagicMock(),
        runtime_info={"app_version": "test", "system_version": "test",
                      "python_version": "test", "machine": "test"},
    )


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """隔离设置文件，绝不读写真实 ~/.buddy-proxy/settings.json。"""
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))


class PrefixedAliasProvider(BaseProvider):
    """模拟 Qoder：目录 id 带前缀，且接受显示名/大小写别名。"""

    id = "prefixed"
    name = "Prefixed Alias Provider"

    #: 归一键 -> 内部 key（与真实 catalog 的别名表同构：显示名与 key 都指向 key）
    _ALIASES = {
        "flashx": "flashx", "qwen38flash": "flashx",
        "glm53": "glm53", "fakeglm99": "glm53",
    }

    def __init__(self):
        self.calls = []

    def models(self):
        return [
            {"id": "prefixed/flashx", "description": "Qwen3.8-Flash"},
            {"id": "prefixed/glm53", "description": "FakeGlm-9.9"},
        ]

    def ensure_auth(self):
        pass

    def resolve_model(self, model: str) -> str:
        norm = "".join(ch for ch in str(model).lower() if ch.isalnum())
        return self._ALIASES.get(norm, model)

    def accepts_model(self, model: str, aliases: bool = True) -> bool:
        if super().accepts_model(model):
            return True
        if not aliases:
            return False
        want = (model or "").strip()
        return bool(want) and self.resolve_model(want) != want

    async def forward(self, body, protocol, original=None):
        self.calls.append(dict(body))
        return JSONResponse({
            "id": "chatcmpl-prefixed", "model": body.get("model"),
            "choices": [{"message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })


@pytest.fixture()
def prefixed_env(tmp_path, monkeypatch):
    prov = PrefixedAliasProvider()
    state = _make_state({"prefixed": prov}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    return SimpleNamespace(state=state, provider=prov, client=TestClient(m.app))


# --- 停用/时段键的口径 ------------------------------------------------------


def test_prefixed_toggle_key_has_single_prefix(prefixed_env):
    """管理页回传带前缀 id 时，落键必须只有一层前缀。

    回归：曾写出 ``prefixed/prefixed/flashx``，与转发侧 ``prefixed/flashx``
    对不上，停用静默失效。
    """
    r = prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "prefixed/flashx", "disabled": True})
    assert r.status_code == 200
    assert r.json()["model"] == "prefixed/flashx"
    assert prefixed_env.state.disabled_models == {"prefixed/flashx"}
    assert settings_mod.load_settings()["disabled_models"] == ["prefixed/flashx"]


def test_prefixed_toggle_accepts_bare_name(prefixed_env):
    """裸名同样落同一把键（客户端实际发的形态）。"""
    r = prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "flashx", "disabled": True})
    assert r.status_code == 200
    assert r.json()["model"] == "prefixed/flashx"


@pytest.mark.parametrize("model", ["flashx", "prefixed/flashx", "Qwen3.8-Flash",
                                   "qwen3.8-flash", "QWEN3.8-FLASH"])
def test_prefixed_aliases_auto_route(prefixed_env, model):
    """别名不该掉进兜底通道：必须命中 prefixed 通道。"""
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, (model, r.text)
    assert prefixed_env.provider.calls, f"{model} 未被路由到 prefixed 通道"


@pytest.mark.parametrize("model", ["flashx", "prefixed/flashx", "Qwen3.8-Flash",
                                   "qwen3.8-flash"])
def test_prefixed_disable_blocks_all_aliases(prefixed_env, model):
    """停用后所有别名写法一律 403（别名绕过会让人以为停用失效）。"""
    prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "prefixed/flashx", "disabled": True})
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403, (model, r.status_code, r.text)


def test_prefixed_disable_does_not_leak_to_other_models(prefixed_env):
    prefixed_env.client.post(
        "/ui/api/model-toggle",
        json={"provider": "prefixed", "model": "prefixed/flashx", "disabled": True})
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": "FakeGlm-9.9", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200


@pytest.mark.parametrize("model", ["glm53", "FakeGlm-9.9", "prefixed/glm53"])
def test_prefixed_schedule_blocks_all_aliases(prefixed_env, monkeypatch, model):
    """限时窗口同样要按归一后的键命中所有别名写法。"""
    prefixed_env.state.model_schedules = {"prefixed/glm53": [["00:00", "00:01"]]}
    monkeypatch.setattr(settings_mod, "model_schedule_open", lambda w: False)
    r = prefixed_env.client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403, (model, r.status_code, r.text)


# --- accepts_model 契约 -----------------------------------------------------


def test_accepts_model_default_rejects_unknown(prefixed_env):
    """accepts_model 不能把无关模型也认领走。"""
    assert prefixed_env.provider.accepts_model("totally-other-model") is False
    assert prefixed_env.provider.accepts_model("") is False


def test_alias_does_not_steal_exact_match_from_other_channel():
    """别名只做兜底，绝不能抢走别的通道按 id 精确匹配就能认领的模型。

    回归（真实场景）：qoder 目录里 GLM-5.3 的显示名与 zcode 的模型名
    ``glm-5.3`` 撞车，且 qoder 注册更靠前。若别名与精确匹配同轮参与，
    ``glm-5.3`` 会被 qoder 抢走、路由归属静默改变。
    """

    class AliasProvider(BaseProvider):
        """只通过别名认识 glm-5.3（自己的 id 是别的名字）。"""

        id = "aliasprov"
        name = "Alias Provider"

        def models(self):
            return [{"id": "aliasprov/internal-glm"}]

        def ensure_auth(self):
            pass

        def resolve_model(self, model):
            return "internal-glm" if model.strip().lower() == "glm-5.3" else model

        def accepts_model(self, model, aliases=True):
            if super().accepts_model(model):
                return True
            return aliases and self.resolve_model(model) != model

        async def forward(self, body, protocol, original=None):  # pragma: no cover
            raise NotImplementedError

    class ExactProvider(BaseProvider):
        """按 id 精确提供 glm-5.3 的通道。"""

        id = "exactprov"
        name = "Exact Provider"

        def models(self):
            return [{"id": "glm-5.3"}]

        def ensure_auth(self):
            pass

        async def forward(self, body, protocol, original=None):  # pragma: no cover
            raise NotImplementedError

    alias, exact = AliasProvider(), ExactProvider()
    assert alias.accepts_model("glm-5.3", aliases=False) is False
    assert exact.accepts_model("glm-5.3", aliases=False) is True
    assert alias.accepts_model("glm-5.3", aliases=True) is True


def test_base_accepts_model_handles_bare_and_prefixed():
    """基类默认实现：id 精确匹配 + 剥前缀裸名，不认别名（两个 aliases 取值等价）。"""

    class P(BaseProvider):
        id = "p"
        name = "P"

        def models(self):
            return [{"id": "p/m1"}, {"id": "bare2"}]

        def ensure_auth(self):
            pass

        async def forward(self, body, protocol, original=None):  # pragma: no cover
            raise NotImplementedError

    p = P()
    assert p.accepts_model("p/m1") is True
    assert p.accepts_model("m1") is True
    assert p.accepts_model("bare2") is True
    assert p.accepts_model("nope") is False
    # 基类没有别名概念：aliases 不影响结果
    assert p.accepts_model("nope", aliases=False) is False
    assert p.accepts_model("bare2", aliases=False) is True


def test_two_round_routing_prefers_exact_match_over_alias(tmp_path, monkeypatch):
    """整链验证：精确匹配轮先跑完，别名轮才兜底。

    用两个通道复刻 ``glm-5.3`` 撞车场景：别名通道注册在前，精确通道在后。
    注册靠前不该让别名抢走结果。
    """

    class AliasOnly(BaseProvider):
        id = "aliasonly"
        name = "Alias Only"

        def models(self):
            return [{"id": "aliasonly/other"}]

        def ensure_auth(self):
            pass

        def resolve_model(self, model):
            return "other" if model.strip().lower() == "glm-5.3" else model

        def accepts_model(self, model, aliases=True):
            if super().accepts_model(model):
                return True
            return aliases and self.resolve_model(model) != model

        async def forward(self, body, protocol, original=None):
            return JSONResponse({
                "id": "chatcmpl-alias", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "ALIAS"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    class Exact(BaseProvider):
        id = "exact"
        name = "Exact"

        def models(self):
            return [{"id": "glm-5.3"}]

        def ensure_auth(self):
            pass

        async def forward(self, body, protocol, original=None):
            return JSONResponse({
                "id": "chatcmpl-exact", "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": "EXACT"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    # 别名通道注册在前，模拟 qoder 早于 zcode
    state = _make_state({"aliasonly": AliasOnly(), "exact": Exact()}, tmp_path)
    monkeypatch.setattr(st, "proxy_state", state)
    client = TestClient(m.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "EXACT"


# --- 别名不得抢走 CodeBuddy 静态表的模型名 ---------------------------------


def test_codebuddy_static_name_not_claimed_by_alias():
    """``auto`` 是 CodeBuddy 的默认模型，别名通道不得认领。

    回归：Qoder 的档位模型（本地合成的 TIER_MODELS）也叫 ``auto``，别名表因此
    命中。``auto`` 又正是 CodeBuddy 的默认模型名（models_config.json 的
    isDefault），没带 model 的请求会被 forward 用 default_model=auto 补齐——
    一旦被别名截走，这些请求会在 Qoder 通道上静默改道。
    """
    from buddy_proxy.codebuddy_provider.forward import _is_codebuddy_model

    assert _is_codebuddy_model("auto") is True
    assert _is_codebuddy_model("not-a-codebuddy-model") is False
    assert _is_codebuddy_model("") is False


@pytest.mark.parametrize("name", ["Auto", "AUTO", "Qwen3.8-Max", "GLM-5.3", "Kimi-K3"])
def test_codebuddy_guard_does_not_block_display_names(name):
    """守卫只按**原样**比对，不挡通道目录里的官方显示名。

    回归：守卫曾做小写化比对，于是 ``Qwen3.8-Max``/``Kimi-K3`` 这些小写化后与
    静态表撞车的**官方 display_name** 被一并挡掉，别名轮不再认领 → 请求掉进
    CodeBuddy 兜底通道 → 上游 502「model [Qwen3.8-Max] service info not found」。
    按官方文档写模型名是正常用法，不该失败。

    需要防的是裸名 ``auto`` 被别名改道，而那种输入本来就没有大小写变体。
    """
    from buddy_proxy.codebuddy_provider.forward import _is_codebuddy_model

    assert _is_codebuddy_model(name) is False


def test_codebuddy_guard_applies_only_to_alias_round():
    """保护只挡别名轮，不挡精确轮——否则会改变 trae/zcode 等既有路由行为。

    精确轮是各通道按自己发布的 id 认领，模型名与静态表重合是正常的（两边都真
    有这个模型），此时让该通道照常先认领。
    """
    from buddy_proxy.codebuddy_provider import forward as fwd

    src = inspect.getsource(fwd.forward_chat)
    # 守卫必须在 allow_aliases 为真的分支里
    assert "if allow_aliases and _is_codebuddy_model(requested_model):" in src
