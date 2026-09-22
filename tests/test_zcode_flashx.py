"""zcode 通道 glm-5.3-flashx 接入回归测试（离线，不访问上游）。

背景（2026-09-19 实测）：``glm-5.3-flashx`` 出现在智谱 coding 端点
``/models`` 列表里，但本机订阅调用被拒为 ``429 code 1311
「当前订阅套餐暂未开放GLM-5.3-FlashX权限」``——同一把 key 打 ``glm-5.3`` /
``glm-5.3-flash`` / ``glm-5-turbo`` 均 200，属套餐授权缺口而非 key 失效。
故按「预备接入」登记模型表：订阅开通后无需改代码即可用。

同时锁死两条实测结论：
1. 该端点 model 名**大小写不敏感**（四种写法都解析到 GLM-5.3-FlashX，乱名才
   报 1211 模型不存在）——故大小写不能作为校验手段，只能靠断言锁住字面量。
2. ``codebuddy`` / ``traepat`` 通道**没有**这个模型：前者 11102
   ``service info not found``，后者不在 PAT 目录（目录由 models_config.json
   的 provider=="traepat" 条目生成）。因此不要把它写进 models_config.json——
   那会把模型标成 codebuddy 通道，与实测矛盾。
"""
from __future__ import annotations

import json

from buddy_proxy.providers.zcode import (
    DEFAULT_MODELS,
    MODEL_NAME_CANONICAL,
    ZcodeProvider,
)
from buddy_proxy.web.model_list import normalize_model_format


def test_flashx_registered_in_provider_tables():
    """模型表与上游名映射都要有 flashx，且字面量与上游一致。"""
    assert "glm-5.3-flashx" in DEFAULT_MODELS
    assert MODEL_NAME_CANONICAL["glm-5.3-flashx"] == "GLM-5.3-FlashX"


def test_flashx_listed_by_provider_models():
    """ZcodeProvider.models() 要包含 flashx（/v1/models 与自动匹配都靠它）。"""
    ids = [m["id"] for m in ZcodeProvider().models()]
    assert "glm-5.3-flashx" in ids
    # 既有模型不受影响
    assert {"glm-5.3", "glm-5.3-flash", "glm-5-turbo"} <= set(ids)


def test_flashx_in_health_report():
    """health() 的 models 列表是排障入口，必须能反映 flashx。"""
    assert "glm-5.3-flashx" in ZcodeProvider().health()["models"]


def test_bare_name_auto_matches_zcode():
    """裸名 glm-5.3-flashx 必须能被 zcode 命中。

    forward_chat 的第 2 优先级路由是「模型 id 命中某个非默认 provider 的
    models()」。zcode 是唯一声明 glm-* 的通道（codebuddy 走静态表兜底），
    若这里漏登记，裸名请求会落回 codebuddy 兜底并报 11102 而非 zcode 的
    1311，排障时极具误导性。
    """
    provider = ZcodeProvider()
    assert any(m["id"] == "glm-5.3-flashx" for m in provider.models())


def test_canonical_rewrite_applied_on_anthropic_passthrough():
    """anthropic 直通路径要把小写 id 改写为上游正式名。

    断言的是 ``forward`` 里 ``MODEL_NAME_CANONICAL.get(body["model"], ...)``
    的取值口径，避免有人误删映射后静默透传小写名。
    """
    assert MODEL_NAME_CANONICAL.get("glm-5.3-flashx", "glm-5.3-flashx") == "GLM-5.3-FlashX"


def test_codebuddy_channel_does_not_claim_flashx():
    """models_config.json 不得把 flashx 标进 codebuddy 静态表。

    实测 codebuddy 通道对该模型返回 11102 service info not found；若写进
    静态表（无 provider 字段 → 默认 codebuddy），/v1/models 会对外宣称
    「codebuddy 通道可用」，客户端按目录选择后必炸。
    """
    from buddy_proxy.web import model_list

    config_file = model_list.pathlib.Path(model_list.__file__).parent / "models_config.json"
    data = json.loads(config_file.read_text("utf-8"))
    for m in data.get("models", []):
        assert m.get("id") != "glm-5.3-flashx", (
            "flashx 不应写进 models_config.json：它只在 zcode 通道注册，"
            "写进静态表会被标成 codebuddy 通道（实测该通道无此模型）"
        )


def test_normalize_model_format_keeps_flashx_fields():
    """配置解析口径不因新模型变形（防御性：字段名拼错会静默丢能力）。"""
    entry = normalize_model_format({
        "id": "glm-5.3-flashx",
        "name": "GLM-5.3-FlashX",
        "vendor": "zcode",
        "max_input": 200000,
        "max_output": 48000,
        "tool_call": True,
        "images": False,
        "reasoning": True,
    })
    assert entry["id"] == "glm-5.3-flashx"
    assert entry["tool_call"] is True
    assert entry["reasoning"] is True
