"""Trae 常量与配置：URL、版本头、模型映射、环境开关、调试/心跳工具。"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# ───────────────────────── Trae 常量 ─────────────────────────

BASE_URL_CN = "https://trae-api-cn.mchost.guru"
BASE_URL_SG = "https://a0ai-api-sg.byteintlapi.com"
IDE_VERSION = "3.3.67"
IDE_VERSION_CODE = "20260401"
X_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"

# Trae Work (SOLO) 客户端版本。
# 注意：服务端按版本号 gating 新模型——旧版本（0.1.43/20260716）请求 glm-5.3
# 等新模型会被拒为 4001 "param is invalid"；升级后才返回真实路由（实测）。
_TRAE_APP_VERSION = "0.2.0"
_TRAE_APP_VERSION_CODE = "20260901"

# X-Ide-Version-Code 单独可调（默认 20260906）：2026-09 实测上游按这个头逐模型
# 门控能力——20260401 下新一代模型（glm-5.3 / kimi-k2.7-code / qwen3.8-max 等）
# 在 chat_v3 上整表 4001；glm-5.3 的放行阈值实测落在 20260801~20260815 之间，
# kimi-k2.7-code / Doubao-Seed-2.1-Pro 20260725 即放行。上游会持续按模型发布
# 节奏抬阈值，此值要跟随真实 Trae 客户端版本更新（可用环境变量覆盖）。
_TRAE_IDE_VERSION_CODE = os.environ.get("WB_TRAE_IDE_VERSION_CODE", "20260906")

# 原生 function calling 通道（2026-09 实测全模型可用，glm-5-turbo 除外——不在
# chat_v3 通道，自动走文本协议兜底）。WB_TRAE_NATIVE_TOOLS=0 可整体关闭。
_NATIVE_TOOLS_ENABLED = os.environ.get("WB_TRAE_NATIVE_TOOLS", "1").lower() not in ("0", "false", "off")
_NATIVE_FUNCTION = os.environ.get("WB_TRAE_NATIVE_FUNCTION", "chat_v3")

# 3 级端点回退（与 trae-local-api 一致）
ENDPOINTS = [
    "/api/agent/v3/llm_utils_chat",
    "/api/ide/v1/chat",
    "/api/agent/v3/create_agent_task",
]

# Trae Work 通道同端点最大尝试次数（含首次）：覆盖 IncompleteRead 等瞬态断流
_WORK_CHAT_MAX_ATTEMPTS = 3

# 模型名映射：外部别名 -> Trae 内部 config_name（大小写敏感，须与下方实测白名单一致）
MODEL_MAP: dict[str, str] = {
    # "claude-opus-4-7": "glm-5.3",
    # "claude-opus-4-6": "glm-5.3",
    # "claude-opus-4-5": "glm-5.3",
    # "claude-sonnet-4-6": "glm-5.3",
    # "claude-sonnet-4-5": "glm-5.3",
    # "claude-sonnet-4": "glm-5.3",
    # "claude-3.5-sonnet": "glm-5.3",
    # "claude-3.7-sonnet": "glm-5.3",
    # "claude-haiku-4-5": "DeepSeek-V4-Flash",
    # "mimo-v2.5-pro": "glm-5.3",
    # "mimo-v2.5": "glm-5.3",
    # "gpt-4o": "DeepSeek-V4-Pro",
    # "gpt-4o-mini": "DeepSeek-V4-Flash",
    # "gpt-4.1": "DeepSeek-V4-Pro",
    # "auto": "glm-5.3",
    # 大小写容错：Trae config_name 大小写不统一（DeepSeek-/Doubao- 为大写前缀），
    # 而 OpenAI 生态习惯全小写；小写请求若不在此映射，会落回默认 CodeBuddy 通道
    # （报错表现为 CodeBuddy 上游的安全审核/路由错误，而非 Trae 响应）
    "deepseek-v4-pro": "DeepSeek-V4-Pro",
    "deepseek-v4-flash": "DeepSeek-V4-Flash",
    "doubao-seed-evolving": "Doubao-Seed-Evolving",
    "doubao-seed-2.1-pro": "Doubao-Seed-2.1-Pro",
    "doubao-seed-2.1-turbo": "Doubao-Seed-2.1-Turbo",
    "doubao-seed-code": "Doubao-Seed-Code",
    # qwen 官方命名点号/连字符混用，两种写法都放行
    "qwen-3.8-max": "qwen3.8-max",
    "qwen3.7-plus": "qwen-3.7-plus",
}

# 模型分级（T1 最强 -> T4 最弱）。
# config_name 全部为 2026-09 实测通过值（Work 凭证 + llm_utils_chat 端点）：
# 注意命名大小写不统一——DeepSeek-/Doubao- 为大写前缀，glm/kimi/minimax 小写，
# qwen 两种写法并存（qwen3.8-max 用点号、qwen-3.7-plus 用连字符）。
# kimi-k3 需会员 Pro+/Ultra/Express（免费账号 1005）；付费账号实测可用
# （2026-09-06 chat_v3 出流正常），故收录。
#
# glm-5.3-flashx（2026-09-30 补录）：上游已放行但本表原先漏收——它不在
# MODEL_MAP 里、模型名原样透传，所以「能通却不出现在 /v1/models」。
# 实测判定依据（三条一起看才排除了「别名模糊匹配」的解释）：
#   1. 源码中无任何 flashx 映射（原样透传，上游自己认这个名字）；
#   2. 上游精确匹配：glm-5.3-flashxx / glm-5.3-flashX 均被 4001
#      "param is invalid" 拒绝——若按前缀或大小写不敏感解析，这两个不会死；
#   3. 连续多次 200 且回真实 usage，自述为 Z.ai GLM 系。
# 注意 zcode 通道对同一模型返回 1311「套餐暂未开放」，别被名字相近误导：
# 两个通道是各自独立的授权，这里能通不代表 zcode 也能通。
#
# deepseek-v4.1-flash（2026-09-30 补录）：与 flashx 同一批漏收——源码无映射、
# 原样透传、上游已放行。归 T1 的理由：新一代旗舰系（原生读图 + 1M ctx，
# 与 glm-5.3-flash 在 T1 的定位一致；上一代 DeepSeek-V4-Pro 是 T2）。
# **它确实是 V4.1 权重而不是 V4-Flash**（名字被上游接受 ≠ 服务的真是这个
# 模型，还得靠能力指纹区分——self-report 不可靠，V4-Flash 会自称
# "deepseek-chat"）。判定试验（各 5 张纯色 1x1 PNG 问颜色）：
#   deepseek-v4.1-flash  5/5 全对（红/绿/蓝/黄/青），且能读图答题；
#   deepseek-v4-flash    1/5，唯一"对"的那次是把固定幻觉"蓝色"撞上了蓝图；
#                        去掉图片它也答"蓝色"，直接问则承认「无图」。
# 若这个名字背后真是 V4-Flash 权重，视觉表现应与对照组一样烂——它没有。
# 另：上游对该名**精确匹配**（deepseek-v4.1 / v41 / 大写变体 / -flashx
# 全部 4001），说明走的是独立路由而非别名模糊匹配。
# Work 通道（solo_work_lite，即文本协议回落时经 ~/.ethan/trae_work.json
# 转投的那条）未实测——native chat_v3 已通，用不上；**不要**凭猜测往
# _WORK_FUNCTION_OVERRIDE 加条目，真失败时让它诚实地 4001 冒出来。
MODEL_TIERS: dict[str, list[str]] = {
    "T1": ["glm-5.3", "glm-5.3-flash", "glm-5.3-flashx",
           "deepseek-v4.1-flash", "Doubao-Seed-Evolving", "kimi-k3"],
    "T2": ["glm-5.2", "Doubao-Seed-2.1-Pro", "DeepSeek-V4-Pro",
           "kimi-k2.7-code", "qwen3.8-max"],
    "T3": ["Doubao-Seed-2.1-Turbo", "DeepSeek-V4-Flash", "minimax-m3",
           "kimi-k2.6", "glm-5.1"],
    "T4": ["Doubao-Seed-Code", "glm-5", "glm-5-turbo", "qwen-3.7-plus"],
}

# 模型积分倍率：整理自知识库《模型及成本整理-workbuddy-trae-含选用建议》
# （2026-09-05 版，WorkBuddy 定价截图）。别名（如 deepseek-v4-flash）由
# models() 按 MODEL_MAP 解析到内部名后取同一倍率。
# glm-5 / glm-5-turbo / glm-5.1 / kimi-k2.6 官方最新价目已下架，未收录。
# glm-5.3-flashx / deepseek-v4.1-flash：2026-09-30 由用户从 WorkBuddy 客户端
# 「模型倍率」面板截图取得（x0.31 / x0.08）。同图里 glm-5.3-flash x0.06 /
# kimi-k3 x1.83 / minimax-m3 x0.26 / qwen3.8-max x1.50 与本表完全吻合，口径一致。
# 注意同图部分旧模型与本表有小差（glm-5.3 / glm-5.2 显示 0.39、DeepSeek-V4-Flash
# 显示 0.10，均带「会员5折」徽章；V4-Pro 带「闲时折扣」徽章）——疑似促销期浮动，
# 本表维持知识库原值未动；若要跟价需定期截图更新，静态表追不动动态折扣。
# 图中另有 Step-5-Preview(0.48) / Kimi-K2.8-Preview(0.98) / Qwen3.8-Flash(0.08)
# 未接入 trae 目录——可用性未实测，**勿只凭价目表收录**（deepseek-v4.1-pro 就
# 是反例：价目之外的「名字被上游接受」才是收录依据）。
MODEL_CREDITS: dict[str, str] = {
    "Doubao-Seed-Evolving": "x0.77",
    "Doubao-Seed-2.1-Pro": "x0.77",
    "Doubao-Seed-2.1-Turbo": "x0.10",
    "Doubao-Seed-Code": "x0.03",
    "glm-5.3-flash": "x0.06",
    "glm-5.3-flashx": "x0.31",
    "glm-5.3": "x0.40",
    "glm-5.2": "x0.40",
    "deepseek-v4.1-flash": "x0.08",
    "kimi-k3": "x1.83",
    "DeepSeek-V4-Flash": "x0.08",
    "DeepSeek-V4-Pro": "x0.72",
    "kimi-k2.7-code": "x0.83",
    "minimax-m3": "x0.26",
    "qwen3.8-max": "x1.50",
    "qwen-3.7-plus": "x0.25",
}

# 支持图片输入的模型（内部 config_name 口径）。
# 这些模型上游 llm_utils_chat 接受 OpenAI 风格 image_url（data URL）block。
#
# 注意：Trae 目录里的 DeepSeek-V4-Flash / V4-Pro **不支持**图片输入；
# 带图片能力的是新一代 deepseek-v4.1-flash（2026-09-30 实测收录：5 张纯色
# 1x1 PNG 问颜色 5/5 全对，且对照组 deepseek-v4-flash 确认读不了图——
# 判定过程见 MODEL_TIERS 处注释）。注意内部名是**全小写**
# ``deepseek-v4.1-flash``，大写 DeepSeek-V4.1-Flash 会被上游 4001。
# 若声明与实际不符，会导致 /v1/models 把纯文本模型报成可读图，客户端盲发
# 图片 → 上游 4001。
#
# 反向的漏报同样有害：未声明时 /v1/models 报 input_modalities=["text"]，
# 外部客户端（agent/IDE）据此自行判断（典型做法是按模型名匹配关键词）会
# 误剥图片，表现为「不支持读图」——即使模型本身支持。
#
# 未在此列表中的模型一律按不支持处理（保守）。
MODEL_SUPPORTS_IMAGES: set[str] = {"deepseek-v4.1-flash"}

# 实测计价（2026-09-30，官方「使用记录」对账法）：模型 -> (输入单价, 输出单价, 校准倍率)。
# 单价单位：积分/token，是**校准时刻的绝对值**（已含当时倍率与折扣）。
#
# 估算式：credits ≈ (in_rate×P + out_rate×C) × 当前倍率 / 校准倍率
# 「当前倍率」取 MODEL_CREDITS（人工跟官方倍率面板的那张表）——面板调价时
# 只改 MODEL_CREDITS，估算自动等比跟进，**不用重跑对账、不用改代码**。
# 元组第三项「校准倍率」是解单价当日 MODEL_CREDITS 的值（现为 x0.31/x0.08），
# 必须与校准时的面板值一致；哪天重跑对账，单价与校准倍率要**成对**更新。
# 注意：单看倍率比值无法区分「上游调倍率」与「上游调基价/折扣起止」——前者
# 估算自动跟对，后者会跟错（按比例缩放），偏差大到对不上账单时重跑对账即可。
#
# 背景：MODEL_CREDITS 的「100×倍率/1M 总 tokens」公式对这两个模型**不成立**——
# 官方真实计费对输入/输出分开计价（out ≈ 4×in），按面板倍率线性折算会差约 9 倍
# （flashx 247 tokens 官方收 0.07，公式只算出 0.008）。上表 x0.31/x0.08 保留作
# 目录展示（官方面板相对倍率），**积分估算走本表**。
#
# 测法：构造「大输入小输出」+「小输入大输出」各一条，与官方账单积分构成二元
# 方程组解出单价；再用其余官方记录回代验证——9 条命中 8 条（唯一偏差 0.0753
# vs 0.07，骑在舍入边界上，上游疑似 floor 而非 round）。
# 附带发现：
# - deepseek-v4.1-flash 在官方账单挂在「DeepSeek-V4-Flash 正式版」名下（计费
#   产品族归属，不代表权重是 V4——能力指纹已证是 V4.1）；
# - 面板 x0.08 带「会员5折」徽章；若按未折价 0.16 归一，两模型隐含基价接近
#   （in ≈2.1~2.6e-4、out ≈8.7~9.1e-4 /token/倍率）——猜测 5 折在账单外补偿，
#   未证实，勿当结论引用。若某天徽章消失、面板翻回 0.16，等比缩放会把估算
#   翻倍：真打折结束则跟对，若只是徽章口径变化则跟错（届时对账见真章）；
# - 断连的请求上游照常计费（实测一次 RemoteDisconnected 仍出账 2.16），对账时
#   注意本侧 metrics 没有它的 token 记录。
# 局限：in 价未细分缓存命中折扣（Claude Code 类大缓存流量会被高估），待有缓存
# 账单样本再拆。其余 trae 模型仍走倍率公式（其校准为 2026-09-05，若有偏差可用
# 同法复测）。
MEASURED_CREDIT_RATES: dict[str, tuple[float, float, float]] = {
    "glm-5.3-flashx": (7.98e-5, 2.81e-4, 0.31),
    "deepseek-v4.1-flash": (3.41e-5, 1.39e-4, 0.08),
}

# 部分模型在 solo_work_lite function 下不可用（服务端 4001），需改用 chat_v3。
# 实测（2026-09-03）：glm-5.1 / Doubao-Seed-Code 仅在 chat_v3 下可路由；
# glm-5.3-flash 同理——solo_work_lite 报 4001 "param is invalid"，
# chat_v3 正常出流（会话 s_20260903_2108_4190 即踩此坑）。
_WORK_FUNCTION_OVERRIDE: dict[str, str] = {
    "glm-5.1": "chat_v3",
    "Doubao-Seed-Code": "chat_v3",
    "glm-5.3-flash": "chat_v3",
}

def _map_model(requested: str) -> str:
    return MODEL_MAP.get(requested, requested)


_DEBUG_SENSITIVE_KEYS = {"raw", "body", "content", "messages", "token", "uid", "tool_calls", "arguments"}


def _debug_dump(event: str, **kwargs: Any) -> None:
    """WB_DEBUG_DUMP=1 时记录安全诊断摘要，绝不落请求或响应原文。

    惰性 import state 避免模块级循环依赖；失败静默（调试设施不能影响主流程）。
    保留敏感文本的长度和短哈希，供关联同一故障使用。
    """
    if not os.environ.get("WB_DEBUG_DUMP"):
        return
    safe: dict[str, Any] = {}
    for key, value in kwargs.items():
        if key in _DEBUG_SENSITIVE_KEYS:
            raw = str(value).encode("utf-8", errors="replace")
            safe[f"{key}_bytes"] = len(raw)
            safe[f"{key}_sha256"] = hashlib.sha256(raw).hexdigest()[:16]
        else:
            safe[key] = value
    try:
        from ..core.state import get_state
        get_state().write_log(event, **safe)
    except Exception:
        pass


# 等待上游期间的心跳间隔（秒）；0 = 关闭心跳。
# 背景：Trae 上游是「整段缓冲」模式——send_trae_chat 同步读完整个 SSE 才返回，
# 生成期间客户端收不到任何数据。长生成（深度 review 大报告等）会触发下游
# 单 chunk 超时（实测 Ethan _CHUNK_TIMEOUT=120s → 回合中止、落库空回复）。
# _stream 在等待时按此间隔发一条 reasoning_content 心跳提示，既喂饱下游
# 超时计时器（续命），又让下游 UI 知道中转还在等。45s 意味着 120s 超时窗口
# 内至少有 2 次心跳，单次 SSE 分包延迟也不会误杀；每条 ~30 字节，开销可忽略。
TRAE_HEARTBEAT_INTERVAL = max(0, int(os.environ.get("WB_TRAE_HEARTBEAT_INTERVAL", "45")))
# 首个或相邻两个真实语义事件之间的最长等待；协议注释/保活不算模型进展。
TRAE_SEMANTIC_TIMEOUT = max(1, int(os.environ.get("WB_TRAE_SEMANTIC_TIMEOUT", "180")))
# 非流式请求整读的总时长上限。urllib 的 timeout 只约束单次 socket 阻塞，
# 上游慢速滴字（keepalive/分块）时 read() 会被无限拖延（实测 PAT 挂过 17min
# 才 502、零输出）。到点主动断开回 504。取值权衡：traepat 非流式健康请求
# p99≈310s、max≈622s（thinking 模型长生成），默认 300s 会牺牲极少数超长
# 成功换取挂死下界；批量场景可调大。
TRAE_NONSTREAM_MAX_S = max(60, int(os.environ.get("WB_TRAE_NONSTREAM_MAX_S", "300")))


def _heartbeat_text(waited: int) -> str:
    return f"⏳ [trae 中转] 上游模型仍在生成，已等待 {waited}s（流保持存活）…"

