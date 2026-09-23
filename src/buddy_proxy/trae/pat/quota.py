"""PAT 额度查询：按账号拉取 entitlement 包并归一为 UI 额度项。"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import socket
import time
import urllib.parse
import urllib.request
from typing import Any

from fastapi import HTTPException

# 接缝约定：函数体内对「测试可注入接缝」（monkeypatch 打在本包命名空间上的
# 名字，见包 __init__ 兼容约定）及包内共享状态经 _ns 调用期解析。
import buddy_proxy.trae.pat as _ns

from .config import _PLUS_GATEWAY, PatProfile, ensure_pat_config
from .credentials import _pat_headers
from .store import _account_state, _mutate_account

log = logging.getLogger(__name__)

# ───────────────────────── 额度查询 ─────────────────────────
#
# 本函数服务于 /ui 额度页，**必须在秒级返回**：页面 30s 轮询一次，而
# benefits 快照是常驻数据集，慢在这里＝整页白屏。
#
# 2026-09-24 复盘：网关 DNS 不可达时曾经慢到 245s（10 账号 × 20s 超时 +
# 每账号一次 ~5s 的 http.client 连接重试退避），前端表现为「额度页一直不
# 出来、点刷新也没用」，而纯本地的 overview/stats/models 毫秒级正常。
# 因此这里两道闸：进循环前探测网关可达性（不可达直接走缓存），以及每账号
# 请求用短超时；并发跑账号，最坏耗时不再随账号数线性增长。

# 单账号额度请求超时：展示用途，6s 足够（实测正常响应亚秒级）。
_QUOTA_TIMEOUT_S = 6.0
# 并发账号数上限：够快，又不至于一次打太多连接。
_QUOTA_MAX_WORKERS = 6
# 网关可达性探测总预算（DNS + 全部 TCP 尝试，不含 TLS）。
# 注意是**总预算**而非每地址超时：解析出多地址时逐个尝试，若各自跑满超时，
# 探测本身就会变成新的慢点（实测 2 地址 × 1.5s = 3s）。这里按整体 deadline 收口。
_QUOTA_PROBE_BUDGET_S = 1.2
# 可达性探测结果缓存秒数：避免 30s 一轮的轮询每次都做探测。
_GATEWAY_PROBE_TTL_S = 30.0
# 探测结论缓存 {"key": gateway_url, "checked_at": float, "reachable": bool}
_gateway_probe_cache: dict[str, Any] = {}


def _gateway_reachable(plus: str, budget: float = _QUOTA_PROBE_BUDGET_S) -> bool:
    """探测 plus 网关 DNS+TCP 是否可达。

    ``urllib`` 对连接类失败会带一次隐式重试且退避约 5s（实测），串行 10 个
    账号就是近 1 分钟起步——这类「网络整体不可达」必须提前短路，而不是让
    每个账号各自去撞一遍。与 ``keeper._exchange_env_ready`` 同思路：**只做
    DNS + TCP 预检，不建 TLS、不发请求**，探测失败只意味着「本轮跳过主动
    查询」，已缓存的额度照常展示。

    ``budget`` 是整轮探测的总预算（不是每地址超时）：地址逐个试，但剩余
    时间不够就立刻放弃，避免「多地址 × 每地址超时」把探测本身变成慢点。
    """
    host, port = _gateway_host_port(plus)
    if not host:
        return True  # 解析不出主机名：交给真正的请求去报错，不在这里下结论
    deadline = time.monotonic() + budget
    for info in _resolve(host, port):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            with socket.create_connection(info[4][:2], timeout=remaining):
                return True
        except OSError:
            continue
    return False


def _gateway_host_port(gateway: str) -> tuple[str, int]:
    try:
        parts = urllib.parse.urlsplit(gateway)
    except ValueError:
        return "", 0
    return parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)


def _resolve(host: str, port: int) -> list[Any]:
    try:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return []


def _gateway_ok(plus: str) -> bool:
    """带短 TTL 缓存的网关可达性判断（一次轮询/刷新只探测一次）。"""
    now = time.time()
    cached = _gateway_probe_cache
    if cached.get("key") == plus and now - float(cached.get("checked_at") or 0) < _GATEWAY_PROBE_TTL_S:
        return bool(cached.get("reachable"))
    # 经 _ns 调用期解析：与包内其它接缝一致，测试可在本包命名空间打桩
    reachable = _ns._gateway_reachable(plus)
    _gateway_probe_cache.update({"key": plus, "checked_at": now, "reachable": reachable})
    return reachable


def _cached_quota(profile: PatProfile, quota_class: str) -> list[dict[str, Any]] | None:
    state = _account_state(profile.cache_key)
    quotas = state.get("quota") if isinstance(state.get("quota"), dict) else {}
    value = quotas.get(quota_class) if isinstance(quotas, dict) else None
    items = value.get("items") if isinstance(value, dict) else None
    return items if isinstance(items, list) else None


def _save_quota(profile: PatProfile, quota_class: str, items: list[dict[str, Any]]) -> None:
    def store(state: dict[str, Any]) -> None:
        quotas = state.setdefault("quota", {})
        quotas[quota_class] = {"items": items, "fetched_at": time.time()}
    _mutate_account(profile.cache_key, store)


def _quota_items(data: dict[str, Any], profile: PatProfile, multi: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for pack in data.get("user_entitlement_pack_list") or []:
        base = pack.get("entitlement_base_info") or {}
        quota = base.get("quota") or {}
        usage = pack.get("usage") or {}
        limit = quota.get("basic_usage_limit")
        used = usage.get("basic_usage_amount") or 0
        if not isinstance(limit, (int, float)) or limit <= 0:
            continue
        entitlement_id = str(base.get("entitlement_id") or "pack")
        if "weekly" in entitlement_id:
            label = "PAT 周包（通用额度）"
        elif "daily" in entitlement_id:
            # 同一账号会同时返回多个日包（例如 GPT-5.6 与 GPT-6），不能都叫
            # “高级模型共享”，否则 UI 看起来像重复额度。只从 entitlement_id 的
            # 已知后缀提取公开模型系列，不展示账号标识或其它原始字段。
            family = ""
            if entitlement_id.endswith("_gpt_56_sol"):
                family = "GPT-5.6 Sol"
            elif entitlement_id.endswith("_gpt_6"):
                family = "GPT-6"
            label = f"PAT 日包（{family or '高级模型'}）"
        else:
            label = "PAT 额度包"
        if multi:
            label = f"PAT #{profile.index + 1} · {label[4:]}"
        end_time = base.get("end_time") or 0
        items.append({"label": label, "used": round(used, 2), "total": limit,
                      "remaining": round(limit - used, 2),
                      "percent": round(used / limit * 100),
                      "reset_ts": int(end_time) if end_time else None})
    return items


def _failure_notice(failed: int, total: int, gateway_ok: bool) -> dict[str, Any]:
    """额度查询失败时给 UI 的说明条目。

    区分「网关整体不可达」和「网关可达但账号查询失败」：前者是网络问题（常见于
    离开内网），用户无需反复点刷新；后者才需要看具体账号。绝不把上游地址、
    token 或账号标识带进 UI。
    """
    if not gateway_ok:
        return {
            "label": "PAT 额度网关不可达",
            "used": None, "total": None, "percent": None, "reset_ts": None,
            "remaining": (f"{failed}/{total} 个账号本轮未取到新数据（网络不可达）；"
                          f"下方为上次成功查询的缓存，稍后会自动重试"),
            "unreachable": True, "query_failed": True,
        }
    return {
        "label": "PAT 额度查询失败",
        "used": None, "total": None, "percent": None, "reset_ts": None,
        "remaining": f"{failed}/{total} 个账号查询失败（下方可能为缓存数据）",
        "unreachable": False, "query_failed": True,
    }


def _fetch_one_account(
    profile: PatProfile, plus: str, multi: bool, gateway_ok: bool
) -> tuple[list[dict[str, Any]], bool]:
    """单账号额度：成功返回 (items, True)，失败回退该账号缓存并返回 (items, False)。

    ``gateway_ok=False`` 时跳过请求直接走缓存：网络整体不可达时不必让每个账号
    各自去撞一遍超时（那正是额度页卡死的来源）。
    """
    if not gateway_ok:
        cached = _cached_quota(profile, "advanced")
        return (
            [dict(item, label=f"{item['label']}·缓存") for item in cached] if cached else []
        ), False
    try:
        credentials = _ns._get_profile_credentials(profile)
        request = urllib.request.Request(
            f"{plus}/trae/api/v1/pay/ide_user_ent_usage", data=b"{}", method="POST",
            headers=_pat_headers(credentials, "application/json"))
        with urllib.request.urlopen(request, timeout=_QUOTA_TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        items = _quota_items(data, profile, multi)
        _save_quota(profile, "advanced", items)
        return items, True
    except Exception as exc:
        # 只记异常类型：额度查询失败多为网络层问题，具体消息可能带上游地址，
        # 不进日志也不进 UI（UI 用 _failure_notice 给出脱敏文案）。
        cached = _cached_quota(profile, "advanced")
        log.warning("PAT 额度查询失败，账号序号=%d（%s）", profile.index, type(exc).__name__)
        return (
            [dict(item, label=f"{item['label']}·缓存") for item in cached] if cached else []
        ), False


def fetch_pat_ent_usage() -> list[dict[str, Any]]:
    """按账号**并发**查询 advanced 额度；失败只使用该账号自己的缓存。

    网关不可达时整轮跳过主动查询、直接用缓存（见模块头注释的 245s 复盘）。
    各账号互不影响：单账号请求/缓存/日志都在 ``_fetch_one_account`` 内，
    结果按 ``profiles`` 原顺序拼接，保证 UI 展示顺序稳定。

    末尾附带被动采集的 standard 池数据（若有）：standard 走外网中继、无主动
    查询接口，唯一信号源是撞 4031 时错误体 ``extra`` 携带的 used/quota，由
    ``_record_standard_pool_4031`` 在撞码时写入**撞码账号自己**的缓存（4031
    实测为账号级分桶，非租户共享）。
    """
    plus = os.environ.get(_PLUS_GATEWAY, "").strip().rstrip("/")
    if not plus:
        raise HTTPException(status_code=503, detail="PAT 通道未配置 TRAE_PAT_PLUS_GATEWAY")
    profiles = list(ensure_pat_config())
    if not profiles:
        return list(_ns._standard_pool_items())
    multi = len(profiles) > 1

    gateway_ok = _gateway_ok(plus)
    if not gateway_ok:
        log.warning("PAT 额度网关不可达，本轮跳过主动查询，仅展示各账号缓存")

    def run(profile: PatProfile) -> tuple[list[dict[str, Any]], bool]:
        return _fetch_one_account(profile, plus, multi, gateway_ok)

    if len(profiles) == 1:
        results = [run(profiles[0])]
    else:
        # 并发账号数封顶，避免一次打开过多连接；结果顺序由 profiles 决定
        workers = min(_QUOTA_MAX_WORKERS, len(profiles))
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="pat-quota"
        ) as pool:
            results = list(pool.map(run, profiles))

    all_items: list[dict[str, Any]] = []
    failures = 0
    for items, ok in results:
        if not ok:
            failures += 1
        all_items.extend(items)
    standard_pool = _ns._standard_pool_items()
    if standard_pool:
        all_items.extend(standard_pool)
    if failures:
        # 有账号失败：说明条与被查到的条目并存，页面照常渲染可用数据，顶部能
        # 直接看到「几个账号没取到」。这里刻意**不抛 502**——额度页是常驻数据集，
        # 抛错会让整页（含其它 provider 的额度）一起失败，而失败原因往往只是
        # 本机网络不通，缓存里的数据依然有意义。说明条已带 unreachable/
        # query_failed 标记，前端用警告色区分。
        all_items.insert(0, _failure_notice(failures, len(profiles), gateway_ok))
    return all_items
