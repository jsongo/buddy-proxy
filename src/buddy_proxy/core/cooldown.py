"""跨 provider 的目标级冷却标记：失败换档后短时间内跳过该 (provider, model)。

与 ``trae/pat/cooldown.py`` 的 ``_channel_exhausted`` 同构：**内存** TTL 字典 +
``threading.Lock``，到期惰性自动恢复；短窗口内反复失败升级为长冷却。

刻意**不落盘**（不写 ``~/.buddy-proxy/``）：

1. 要照抄的先例 ``_channel_exhausted`` 就是内存态，PAT 那边把理由写明了——「重启后
   重新探测一次的代价远小于误冷却」；
2. 进程重启常常正是因为用户刚修好上游（换 key、改订阅），启动就带着对某个目标的不信任
   比多探测一次更糟；
3. 标记是高 churn 写入，持久化意味着每次失败都 fsync，与 settings.json 争同一条原子写
   路径；
4. ``settings.py`` 拥有的是「用户意图」（停用/时段/顺序），冷却只是**此刻的健康状态**，
   不是用户意图。

本模块是**叶子**模块：只 import 标准库，因此从任何地方 import 都不会造成循环导入
（见 ``docs/architecture.md`` 与 ``core/state.py`` 里 TYPE_CHECKING 的警告）。
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

#: 首档冷却时长（秒）。与 PAT 的 ``_CHANNEL_EXHAUSTED_TTL_S`` 同量级：上游抖动
#: 通常在分钟级自愈，5 分钟足够换开、又不会把偶尔抖一下的通道长期拉黑。
_TARGET_COOLDOWN_S = 300
#: 升级计数的滑动窗口（秒）。
_ESCALATE_WINDOW_S = 300
#: 窗口内第 N 次失败 → 升级为长冷却。
_ESCALATE_HITS = 3
#: 升级档上限（秒）= 1 小时。刻意**不**照搬 PAT 的「冷却到次日」：模型级失败多为上游
#: 抖动而非当日额度耗尽，1 小时足够且误伤小。
_ESCALATE_COOLDOWN_S = 3600

#: ``(provider_id, model_id)`` → 解禁时刻（epoch 秒）。
_marks: dict[tuple[str, str], float] = {}
#: ``(provider_id, model_id)`` → 窗口内失败时间戳列表。
_hits: dict[tuple[str, str], list[float]] = {}
_lock = threading.Lock()


def _prune_hits(key: tuple[str, str], now: float) -> list[float]:
    """返回窗口内的失败时间戳（顺带丢弃过期的）。调用方须持有 ``_lock``。"""
    hits = [t for t in _hits.get(key, []) if now - t < _ESCALATE_WINDOW_S]
    _hits[key] = hits
    return hits


def mark_failed(provider_id: str, model_id: str, *, status: int | None = None) -> float:
    """标记一次失败，返回解禁时刻（epoch 秒）。

    短窗口内累计失败达到 ``_ESCALATE_HITS`` 次则升级为 ``_ESCALATE_COOLDOWN_S``，
    否则用 ``_TARGET_COOLDOWN_S``。始终取 ``max``，短冷却不得缩短已有的长冷却。

    只记 provider / model / status，**绝不记录上游响应体**（隐私红线，见
    docs/architecture.md）。
    """
    key = (provider_id, model_id)
    now = time.time()
    with _lock:
        hits = _prune_hits(key, now)
        hits.append(now)
        escalated = len(hits) >= _ESCALATE_HITS
        span = _ESCALATE_COOLDOWN_S if escalated else _TARGET_COOLDOWN_S
        until = max(_marks.get(key, 0.0), now + span)
        _marks[key] = until
    log.warning(
        "目标 %s/%s 换档失败，冷却 %d 分钟%s（触发状态=%s，窗口内第%d次）",
        provider_id, model_id, span // 60,
        "（已升级）" if escalated else "（首次，未升级）",
        status if status is not None else "unknown", len(hits),
    )
    return until


def is_marked(provider_id: str, model_id: str) -> bool:
    """该目标当前是否处于冷却中（过期条目顺带清掉，保持 dict 有界）。"""
    return remaining(provider_id, model_id) > 0


def remaining(provider_id: str, model_id: str) -> int:
    """剩余冷却秒数；未标记或已过期返回 0。"""
    key = (provider_id, model_id)
    now = time.time()
    with _lock:
        until = _marks.get(key, 0.0)
        if until <= now:
            _marks.pop(key, None)
            return 0
        return max(0, int(round(until - now)))


def clear(provider_id: str | None = None, model_id: str | None = None) -> int:
    """清除标记，返回被清除的目标数。

    - 两者都给 → 只清该目标；
    - 只给 ``provider_id`` → 清该通道下所有目标（含其升级计数）；
    - 都不给 → 全清（供 UI「全部重试」与测试隔离用）。
    """
    with _lock:
        if provider_id is None:
            removed = len(_marks)
            _marks.clear()
            _hits.clear()
            return removed
        if model_id is not None:
            removed = 1 if _marks.pop((provider_id, model_id), None) is not None else 0
            _hits.pop((provider_id, model_id), None)
            return removed
        keys = [k for k in _marks if k[0] == provider_id]
        for k in keys:
            _marks.pop(k, None)
            _hits.pop(k, None)
        return len(keys)


def snapshot() -> dict[str, int]:
    """当前所有生效中的标记：``"provider/model"`` → 剩余秒数（供 /ui 展示）。"""
    now = time.time()
    out: dict[str, int] = {}
    with _lock:
        expired = [k for k, until in _marks.items() if until <= now]
        for k in expired:
            _marks.pop(k, None)
        for (provider_id, model_id), until in _marks.items():
            out[f"{provider_id}/{model_id}"] = max(0, int(round(until - now)))
    return out


def _reset_for_tests() -> None:
    """清空全部模块级状态。

    标记是**模块级单例**（刻意不挂在 ProxyState 上，免得四个 SimpleNamespace 假 state
    都要跟着扩），因此打路由的测试之间必须显式清空——同
    ``test_trae_pat_failover`` 里 monkeypatch ``_channel_exhausted`` 的做法。
    """
    clear()
