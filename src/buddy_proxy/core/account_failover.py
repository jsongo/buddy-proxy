"""账号级 failover 冷却状态机：稳定主备 + 内存冷却（多通道共用）。

antigravity / kimi / qoder / trae 的「坏账号冷却、按顺位主备降级」是同一套逻辑：
内存 TTL 字典 + ``threading.Lock``，到期惰性恢复。本模块把这套状态机参数化成
:class:`CooldownTracker`，各通道的 ``failover.py`` 持有一个实例并保留自己的
``accounts_status``（字段不同）与 ``available_accounts``（qoder 带 region）。

**为什么收拢**：此前 antigravity/kimi/qoder 各 copy-paste 一份（~400 行），核心
``mark_cooldown / clear_cooldown / cooldown_left / cooldown_report`` 逐字一致，
只有冷却档位（blacklist 仅 antigravity）、Retry-After 区间等常量差异。收敛后修
一处（如 Retry-After 钳制区间）不必三个通道同步改。

与 ``core/cooldown.py``（**模型级**换档冷却）区分：本模块管**账号**健康状态，
那个管 **provider/model 目标**的换档失败。

刻意**不落盘**：同 ``core/cooldown.py`` 的理由——冷却只是「此刻的健康状态」，
重启后重探一次的代价远小于带着不信任启动。

本模块是**叶子**：只 import 标准库，任何地方 import 都不会循环导入。
"""

from __future__ import annotations

import threading
import time

#: 403 / 401 强刷后仍被拒：账号级问题，短冷却快速重探。
DEFAULT_ACCOUNT_COOLDOWN_S = 60.0
#: 429（额度耗尽）默认冷却：对齐各通道首档 5min；Retry-After 可覆盖。
DEFAULT_QUOTA_COOLDOWN_S = 300.0
#: Retry-After 的合理区间（钳到 1s~7d 防御离谱值）。
RETRY_AFTER_MIN_S = 1.0
RETRY_AFTER_MAX_S = 7 * 86400.0


class CooldownTracker:
    """一组账号的冷却状态。

    每个多账号通道持有一个实例（``failover.py`` 模块级单例）。冷却键是
    account_id（不耦合具体通道的 AccountRef 类型——各通道字段不同）。
    """

    def __init__(
        self,
        *,
        account_cooldown_s: float = DEFAULT_ACCOUNT_COOLDOWN_S,
        quota_cooldown_s: float = DEFAULT_QUOTA_COOLDOWN_S,
        extra_kinds: dict[str, float] | None = None,
    ) -> None:
        """``extra_kinds``：通道特有档位（如 antigravity 的 blacklist→6h），
        键是类别名，值是默认时长（秒）。``mark_cooldown`` 的 ``kind=`` 可用。"""
        self._durations: dict[str, float] = {
            "account": account_cooldown_s,
            "quota": quota_cooldown_s,
        }
        if extra_kinds:
            self._durations.update(extra_kinds)
        self._cooldowns: dict[str, tuple[float, str]] = {}  # id -> (until, kind)
        self._lock = threading.Lock()

    # -- 基本操作 -----------------------------------------------------------

    def mark(
        self,
        account_id: str,
        *,
        retry_after: str | None = None,
        kind: str = "account",
        reason: str = "",
        log: object = None,
    ) -> float:
        """把账号冷却一段时间，返回实际冷却秒数。

        ``retry_after``（Retry-After 头）优先于该类别默认时长，钳到
        ``[RETRY_AFTER_MIN_S, RETRY_AFTER_MAX_S]`` 防御离谱值。
        """
        default = self._durations.get(kind, self._durations["account"])
        seconds = default
        if retry_after:
            try:
                seconds = max(RETRY_AFTER_MIN_S,
                              min(float(retry_after), RETRY_AFTER_MAX_S))
            except (TypeError, ValueError):
                pass
        with self._lock:
            self._cooldowns[account_id] = (time.time() + seconds, kind)
        if reason and log is not None:
            log.warning("账号 %s 冷却 %s（%s）", account_id, self.fmt_left(seconds), reason)
        return seconds

    def clear(self, account_id: str) -> None:
        """清掉该账号的冷却标记（删除账号后调用，防内存残留）。"""
        with self._lock:
            self._cooldowns.pop(account_id, None)

    def clear_all(self) -> None:
        """清掉全部冷却标记（测试隔离用）。"""
        with self._lock:
            self._cooldowns.clear()

    def left(self, account_id: str) -> tuple[float, str]:
        """剩余冷却秒数与类别（0, "" 表示没在冷却）。"""
        with self._lock:
            entry = self._cooldowns.get(account_id)
        if not entry:
            return 0.0, ""
        until, kind = entry
        left = until - time.time()
        if left <= 0:
            return 0.0, ""
        return left, kind

    def is_cooling(self, account_id: str) -> bool:
        return self.left(account_id)[0] > 0

    # -- 聚合视图 -----------------------------------------------------------

    def report(self, ids: list[str], labels: dict[str, str] | None = None) -> str:
        """给定账号 id 列表的冷却状态一句话画像（通道耗尽报错用）。

        ``labels``：类别 → 展示名（如 antigravity 的 ``blacklist→疑似拉黑``）；
        缺省 ``quota→额度冷却``、其余 ``账号冷却``。
        """
        default_labels = {"quota": "额度冷却"}
        if labels:
            default_labels.update(labels)
        parts = []
        for aid in ids:
            left, kind = self.left(aid)
            if left <= 0:
                continue
            label = default_labels.get(kind, "账号冷却")
            parts.append(f"{aid} {label}剩 {self.fmt_left(left)}")
        return "、".join(parts)

    def snapshot(self) -> dict[str, tuple[float, str]]:
        """当前生效中的冷却 ``{id: (剩余秒, 类别)}``（未过期的）。"""
        now = time.time()
        out: dict[str, tuple[float, str]] = {}
        with self._lock:
            items = list(self._cooldowns.items())
        for aid, (until, kind) in items:
            left = until - now
            if left > 0:
                out[aid] = (left, kind)
        return out

    @staticmethod
    def fmt_left(seconds: float) -> str:
        if seconds >= 90 * 60:
            return f"{seconds / 3600:.1f}h"
        return f"{seconds / 60:.1f}min"
