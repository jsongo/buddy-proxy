"""按 token 数粗估单次请求积分消耗（上游不给单次计费时的兜底口径）。

Trae 从 2026-09 起改为积分制按 token 计费，但 chat 流只回 token 数、不带
单次积分；IDE JWT 能打的 UG/pay 接口里也没有按请求粒度的消耗流水（候选
endpoint 全部 404，网页「使用记录」走另一套 web 会话体系）。因此按官方
「模型倍率」口径粗估，并用真实账单校准。

实测校准（2026-09-06，10 组代理 token 记录 ↔ Trae 网页「使用记录」真实
积分的分钟级对齐样本，glm-5.3-flash x0.06，输入 2.8 万~4.3 万 token）：

- 公式 ``(输入 + 输出) / 1M × 100 × 倍率``：稳态请求误差 ±0.04 积分；
- 会话首请求实测 0.45 vs 公式 0.17（约 2.7 倍）——疑似上游把隐藏注入
  （guard/教学 prompt）计入计费但未体现在 token_usage 里，代理侧无法
  识别，此类请求按公式值低估处理；
- 重复 ~32k 输入的请求积分不变（0.16~0.21 随输入线性微动），说明该通道
  缓存即便命中也按全价计（或根本未命中），故缓存 token 不做折扣加权。

zcode（GLM Coding Plan）的 /api/monitor/usage/quota/limit 计数器批量聚合
更新（0~90s 抖动）且无按请求流水，官方口径也没有单次积分概念，不做估算。
"""

from __future__ import annotations

# 积分 / 1M token / 倍率单位。校准：glm-5.3-flash（x0.06）实测 ≈6.0~6.2
# 积分/M，即基准 100×倍率；别家倍率体系（MODEL_CREDITS）同源换算。
CREDITS_PER_M_PER_MULT = 100.0


def _load_trae_tables() -> tuple[dict[str, str], dict[str, str]]:
    """惰性加载 Trae 的倍率表与别名映射（避免模块级循环导入）。"""
    try:
        from ..trae.config import MODEL_CREDITS, MODEL_MAP
        return MODEL_CREDITS, MODEL_MAP
    except Exception:
        return {}, {}


def _multiplier(provider: str, model: str) -> float | None:
    """解析模型倍率（"x0.06" → 0.06）；无表/无该模型返回 None。"""
    if provider != "trae":
        return None
    credits, model_map = _load_trae_tables()
    internal = model_map.get(model, model)
    raw = credits.get(internal) or credits.get(internal.lower())
    if not raw:
        return None
    try:
        return float(str(raw).lower().lstrip("x×"))
    except ValueError:
        return None


def estimate_credit(
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None = 0,
) -> float | None:
    """按 token 粗估单次积分消耗；无倍率表或无 token 数据时返回 None。

    返回值统一 round 到 2 位（与 Trae 网页使用记录的展示粒度一致）。
    """
    mult = _multiplier(provider, model)
    if mult is None:
        return None
    total = max(0, int(prompt_tokens or 0)) + max(0, int(completion_tokens or 0))
    if total <= 0:
        return None
    return round(total / 1e6 * CREDITS_PER_M_PER_MULT * mult, 2)
