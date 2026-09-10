"""PAT 账号/额度冷却：撞码分级冷却、通道级 4031 快速失败与可用账号序。"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _ACCOUNT_COOLDOWN_S, PatProfile, ensure_pat_config
from .models import pat_gateway_is_plus
from .store import _account_state, _mutate_account

log = logging.getLogger(__name__)

# ───────────────────────── 账号/额度冷却 ─────────────────────────


def _quota_class(model: str) -> str:
    return "advanced" if pat_gateway_is_plus(model) else "standard"


def _cooldown_until(profile: PatProfile, quota_class: str) -> float:
    state = _account_state(profile.cache_key)
    cooldowns = state.get("cooldowns") if isinstance(state.get("cooldowns"), dict) else {}
    try:
        return max(float(cooldowns.get("account") or 0),
                   float(cooldowns.get(quota_class) or 0))
    except (TypeError, ValueError):
        return 0


def _next_day_timestamp(now: float) -> float:
    current = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai"))
    return (current.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)).timestamp()


def _retry_after_seconds(value: str | None, now: float) -> int | None:
    if not value:
        return None
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                return None
            seconds = int(target.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None
    return min(max(seconds, 1), 7 * 86400)


def _quota_reset_timestamp(now: float, quota_class: str) -> float:
    # 两类模型池都按已确认的日界自动恢复；池仍独立记录，互不影响。
    del quota_class
    return _next_day_timestamp(now)


# 白名单额度码的分级冷却：首次撞码只做短冷却并立即换号（防上游偶发误报
# 毁掉整个通道一整天）；短窗口内同一账号同类反复撞码才升级为「冷却到次日」
#（真额度耗尽）。内存计数即可——重启后重新探测一次的代价远小于误冷却。
_QUOTA_CODE_HIT_WINDOW_S = 300
_QUOTA_CODE_ESCALATE_HITS = 3
_quota_code_hits: dict[tuple[str, str], list[float]] = {}
_quota_code_hits_lock = threading.Lock()

# 通道级额度限制标记：4031（日额度包耗尽）实测会在同一窗口内拦截全部账号
# —— 判定维度不是单账号余额（余额 0/58 的账号同样 4031，疑似按 IP/租户维度）。
# 此时逐个探测 10 个账号毫无意义，改为快速失败 + 短 TTL 定期重探（默认 5 分钟）：
# 上游限制解除后自动恢复；若真有个别账号仍有余量，最多 5 分钟后也会被发现。
_channel_exhausted: dict[str, float] = {}
_channel_exhausted_lock = threading.Lock()
_CHANNEL_EXHAUSTED_TTL_S = 300


def _channel_exhausted_until(quota_class: str) -> float | None:
    with _channel_exhausted_lock:
        until = float(_ns._channel_exhausted.get(quota_class) or 0)
    return until if until > time.time() else None


def _mark_channel_exhausted(quota_class: str) -> None:
    until = time.time() + _CHANNEL_EXHAUSTED_TTL_S
    with _channel_exhausted_lock:
            _ns._channel_exhausted[quota_class] = until


def _raise_channel_exhausted(quota_class: str) -> None:
    until = _channel_exhausted_until(quota_class)
    if not until:
        return
    retry = max(0, int(until - time.time()))
    raise HTTPException(
        status_code=429,
        detail=(f"PAT {quota_class} 本轮所有可用账号均返回 4031（额度耗尽），"
                f"已暂停探测，{retry}s 后自动重试；"
                f"期间可改用 trae/ 或 codebuddy/ 通道"))


def _record_quota_code_hit(cache_key: str, quota_class: str) -> int:
    """记录一次额度码撞码，返回窗口内累计次数。"""
    now = time.monotonic()
    key = (cache_key, quota_class)
    with _quota_code_hits_lock:
        hits = [t for t in _ns._quota_code_hits.get(key, []) if now - t < _QUOTA_CODE_HIT_WINDOW_S]
        hits.append(now)
        _ns._quota_code_hits[key] = hits
        return len(hits)


def _mark_cooldown(
    profile: PatProfile, quota_class: str, *, code: int | None = None,
) -> None:
    """额度码冷却：首次短冷却（5分钟）换号，反复撞码升级为冷却到次日。"""
    hits = _record_quota_code_hit(profile.cache_key, quota_class)
    if hits >= _QUOTA_CODE_ESCALATE_HITS:
        until = _quota_reset_timestamp(time.time(), quota_class)
        until_desc = "至次日"
    else:
        until = time.time() + _ACCOUNT_COOLDOWN_S * 5
        until_desc = "5分钟（首次，未升级）"

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get(quota_class) or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns[quota_class] = max(old, until)

    _mutate_account(profile.cache_key, store)
    log.warning(
        "PAT 账号序号=%d %s 类冷却%s（触发码=%s，窗口内第%d次）",
        profile.index, quota_class, until_desc,
        code if code is not None else "unknown", hits)


def _mark_quota_exhausted(profile: PatProfile, quota_class: str) -> None:
    """4031（该账号该池日额度耗尽）：给该账号该池打短冷却（5分钟，不升级次日），
    以便 failover 跳过它去试下一个额度独立的账号。日包按 00:00 重置，短冷却只是
    避免同一请求/短窗口内反复撞同一个耗尽账号——真正恢复靠日界或次日额度。"""
    until = time.time() + _ACCOUNT_COOLDOWN_S * 5

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get(quota_class) or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns[quota_class] = max(old, until)

    _mutate_account(profile.cache_key, store)
    log.warning("PAT 账号序号=%d %s 类日额度耗尽（4031），短冷却5分钟并换号",
                profile.index, quota_class)


def _record_standard_pool_4031(profile: PatProfile, extra: Any) -> None:
    """从 4031 extra 被动采集 standard 池用量（无主动查询接口，唯一信号源）。

    2026-09-10 实测：4031 是**账号级**分桶（同租户 #0 撞 4031 weekly 160/160、
    #1 同秒成功出流），且账本有 daily/weekly 两个池（extra.dimension 区分）。
    成功流不带任何账单事件——只有撞码才回 extra，因此被动采集挂在 4031
    路径上。数据写入**撞码账号自己**的 ``quota["standard"]``，供 /ui 额度页
    按账号展示；只解析公共计费字段（used/quota/next_flash/dimension），绝不
    落盘 token/uid/body。extra 可能是 dict 或 JSON 字符串，解析失败静默放弃
    （诊断采集不能影响 failover 主流程）。
    """
    try:
        if isinstance(extra, str):
            extra = json.loads(extra)
        if not isinstance(extra, dict):
            return
        used = extra.get("used")
        quota = extra.get("quota")
        if not isinstance(used, (int, float)) or not isinstance(quota, (int, float)) or quota <= 0:
            return
        dimension = extra.get("dimension") if isinstance(extra.get("dimension"), str) else ""
        reset_ts = extra.get("next_flash")
        reset = reset_ts / 1000 if isinstance(reset_ts, (int, float)) else None
        # 撞码时 extra 报的是已满的那个池；另一个池余量未知，只有撞到才有数。
        label = "standard 池"
        if dimension:
            label = f"standard {dimension} 池"
        item = {
            "label": label,
            "used": round(float(used), 2),
            "total": round(float(quota), 2),
            "remaining": round(quota - used, 2),
            "percent": round(used / quota * 100),
            "reset_ts": int(reset) if reset else None,
            "source": "4031",
        }

        def store(state: dict[str, Any]) -> None:
            quotas = state.setdefault("quota", {})
            existing = quotas.get("standard") if isinstance(quotas.get("standard"), dict) else {}
            items = existing.get("items") if isinstance(existing.get("items"), list) else []
            # 同维度覆盖更新，不同维度各存一条（daily + weekly 并存）
            items = [i for i in items if i.get("label") != label] + [item]
            quotas["standard"] = {"items": items, "fetched_at": time.time()}

        _mutate_account(profile.cache_key, store)
    except Exception:
        pass


def _standard_pool_items() -> list[dict[str, Any]]:
    """汇总各账号被动采集的 standard 池数据（无数据的账号不产出条目）。

    14 天未刷新的条目视为陈旧丢弃（账号早已删配/池结构变化时不展示旧账）。
    """
    out: list[dict[str, Any]] = []
    profiles = ensure_pat_config()
    multi = len(profiles) > 1
    for profile in profiles:
        state = _account_state(profile.cache_key)
        quotas = state.get("quota") if isinstance(state.get("quota"), dict) else {}
        value = quotas.get("standard") if isinstance(quotas, dict) else None
        items = value.get("items") if isinstance(value, dict) else None
        if not (isinstance(items, list) and items):
            continue
        fetched_at = value.get("fetched_at") or 0
        stale = time.time() - fetched_at > 14 * 86400
        for item in items:
            if stale or not isinstance(item, dict):
                continue
            label = str(item.get("label") or "standard 池")
            out.append(dict(item, label=f"PAT #{profile.index + 1} · {label}"
                           if multi else label))
    return out


def _mark_account_cooldown(profile: PatProfile, *, retry_after: str | None = None) -> None:
    now = time.time()
    until = now + (_retry_after_seconds(retry_after, now) or _ACCOUNT_COOLDOWN_S)

    def store(state: dict[str, Any]) -> None:
        cooldowns = state.setdefault("cooldowns", {})
        try:
            old = float(cooldowns.get("account") or 0)
        except (TypeError, ValueError):
            old = 0
        cooldowns["account"] = max(old, until)

    _mutate_account(profile.cache_key, store)


def _ordered_available_profiles(quota_class: str) -> tuple[PatProfile, ...]:
    now = time.time()
    return tuple(profile for profile in ensure_pat_config()
                 if _cooldown_until(profile, quota_class) <= now)


def _clear_standard_cooldowns() -> int:
    """定向清理旧 4031 逻辑误写的账号级 ``standard`` 次日级冷却。

    历史上 4031（通道/租户级日额度耗尽）会同时触发账号级 ``_mark_cooldown``，
    短窗口内一次 failover 扫描就把多个账号的 ``standard`` 冷却升级到次日。改为
    仅通道级快速失败后，这些残留冷却需要清掉，否则用户要等到次日或手改状态文件。

    只清 ``until`` 距今超过 1 小时的条目：旧误写全是「冷却到次日」形态，而现行
    ``_mark_quota_exhausted``/``_mark_cooldown`` 首档只写 5 分钟短冷却——不能把
    刚生效的正常限流冷却一并清空（否则 keeper 启动即把仍被限流的账号放出去）。
    难以与现行升级冷却（同为次日级、罕见且撞码会重新冷却）区分，接受极小误伤。

    幂等：无可清条目时不写盘。返回被清理的账号数。跨进程一次性由 keeper 的
    迁移标记文件控制（见 ``keeper._migrate_standard_cooldowns_once``）。
    """
    now = time.time()
    cleared = 0
    for profile in ensure_pat_config():
        state = _account_state(profile.cache_key)
        cooldowns = state.get("cooldowns")
        if not isinstance(cooldowns, dict):
            continue
        try:
            until = float(cooldowns.get("standard") or 0)
        except (TypeError, ValueError):
            continue
        if until - now <= 3600:  # 短冷却（≤5min 档）是现行正常状态，不清
            continue

        def store(current: dict[str, Any]) -> None:
            cds = current.get("cooldowns")
            if isinstance(cds, dict):
                cds.pop("standard", None)

        _mutate_account(profile.cache_key, store)
        cleared += 1
    if cleared:
        log.info("PAT 清理误写的账号级 standard 次日级冷却：%d 个账号", cleared)
    return cleared

