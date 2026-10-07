"""trae 通道 step-5-preview 接入回归测试（离线，不访问上游）。

背景（2026-10-07 实测）：step-5-preview 是 2026-10-06 从客户端「模型倍率」
面板同批收录的（x0.48），当时只进了倍率表、**聊天未实测**——首次转发即
4001 ``param is invalid``。probe 定性（对照组 glm-5.3 正常，探针本身有效）：
**上游只认全小写 step-5-preview**，与 deepseek-v4.1-flash 同款精确匹配：

- ``Step-5-Preview``（旧 MODEL_MAP 的大小写转换产物）/ ``Step5-Preview`` /
  ``Step-5.5-Preview``：全部 4001；
- 小写原样透传：metadata + 真实 output 流 + token_usage，self-report 为
  阶跃星辰（Step）系，模型身份吻合。

故 MODEL_MAP **不得**加映射：旧映射把能用的名字静默换成上游不认的大写变体，
用户拿不到任何提示。倍率面板显示的 "Step-5-Preview" 只是展示名。
"""
from __future__ import annotations

from buddy_proxy.trae.config import (
    MODEL_CREDITS,
    MODEL_IDS,
    MODEL_MAP,
    _map_model,
    map_model_for,
    _WORK_FUNCTION_OVERRIDE,
)
from buddy_proxy.trae.provider import TraeProvider


def test_step5_listed_by_trae_catalog():
    """目录里要有 step-5-preview（/v1/models 与前端模型选择器都读它）。"""
    ids = [m["id"] for m in TraeProvider().models()]
    assert "step-5-preview" in ids
    assert MODEL_IDS.index("step-5-preview") < MODEL_IDS.index("doubao-seed-code")


def test_step5_is_passed_through_verbatim():
    """**关键断言**：模型名原样透传，不做任何大小写转换。

    旧映射 ``"step-5-preview": "Step-5-Preview"`` 是 4001 的直接原因——上游
    精确匹配小写字面量，任何映射都会把请求静默换成上游不认的名字。宁可红。
    """
    assert "step-5-preview" not in MODEL_MAP
    assert map_model_for("cn", "step-5-preview") == "step-5-preview"
    assert _map_model("step-5-preview") == "step-5-preview"


def test_step5_credit_multiplier():
    """目录展示倍率 x0.48（2026-10-06 倍率面板截图，同批模型口径互验）。"""
    assert MODEL_CREDITS["step-5-preview"] == "x0.48"
    entry = next(m for m in TraeProvider().models() if m["id"] == "step-5-preview")
    assert entry["credits"] == "x0.48"


def test_step5_needs_no_function_override():
    """不要给 step-5-preview 加 ``_WORK_FUNCTION_OVERRIDE``。

    native 主通道固定 chat_v3 已实测可用；solo_work_lite 文本兜底从未被触发、
    也未实测——没有依据支持加覆盖项。别顺手抄一个进去：真在 solo_work_lite
    下失败时应该让 4001 诚实地冒出来，而不是被覆盖项悄悄绕过。
    """
    assert "step-5-preview" not in _WORK_FUNCTION_OVERRIDE
    assert "Step-5-Preview" not in _WORK_FUNCTION_OVERRIDE
