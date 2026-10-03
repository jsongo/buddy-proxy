"""打卡 / 额度：打卡历史落盘 + 自动打卡后台任务（/ui 管理页消费）。

能力约定见 ``providers.BaseProvider``：provider 以 ``supports_checkin = True``
声明支持打卡，``checkin_status()/checkin_claim()/quota()`` 返回 None 表示
不支持对应能力。所有上游调用都是同步的，经 ``asyncio.to_thread`` 执行。

打卡历史逐行落盘 ``logs/checkin.jsonl``（手动 / 自动都走同一条路），管理页
的「打卡日历」由此渲染，自动打卡任务以此判断当天是否已完成。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from buddy_proxy.core import settings as settings_mod

# 自动打卡巡检周期；失败重试间隔
CHECK_INTERVAL_S = 600
RETRY_THROTTLE_S = 1800
# 状态/额度缓存 TTL：管理页 30s 自动刷新，不能每次都打上游
SNAPSHOT_TTL_S = 300
# 失败结果缓存 TTL：短得多，让网络抖动几秒恢复后下一轮就能自愈
FAILURE_TTL_S = 30
DEFAULT_CHECKIN_TIME = "09:30"
# 「翻转点已过 → 提前作废快照」只在这个宽限窗口内生效。正常轮换最多让缓存
# 早退这么多；超出说明上游给的 next_ts 已经不可信，退回按 TTL 过期。
FLIP_GRACE_S = 3600
# 到期告警：权益剩下的日子少于此值就上横幅；积分类还要剩得比它多才值得提醒
# （用户 2026-10-03 要求「大于 300 积分再提醒」——每天签到送的 200 分小包
# 一到 7 天内就刷屏，真正值钱的会员包反而被淹）。判定见 :func:`_expiring`。
EXPIRY_WARN_DAYS = 7
EXPIRY_WARN_MIN_CREDITS = 300


def _state_flipped(data: Any, now: float) -> bool:
    """缓存里的快照是否「描述的状态已经翻篇」。

    ``checkin_status`` 会给 ``next_ts``——按 ``providers/base.py`` 的契约，它
    正是这个状态**翻转**的时刻（已签到 = 下一轮开始，未签到 = 本轮截止）。
    一旦它成为过去，这份快照描述的就不再是现状：界面刚把倒计时数到「即将
    刷新」，紧接着那次轮询却仍拿到翻篇前的旧状态，于是显示「已签到」+ 按钮
    禁用，要等满 TTL（5 分钟）才自愈。而 qoder 的窗口错过即失效，这 5 分钟
    足够错过一整轮——倒计时承诺的「即将刷新」也就成了空话。

    所以把 ``next_ts`` 本身当作这份缓存的到期时刻：它比任何固定 TTL 都准，
    且天然只在轮换点触发一次重查，不会带来额外轮询。

    ``next_ts`` 缺失或不是数字（额度结果、失败结构、上游给了怪值）时一律
    判否——拿不准就照旧走 TTL，别因为一个坏字段把缓存整个废掉。

    只认「刚过点」的 ``next_ts``：正常路径下越过翻转点后重查一次，上游就会
    给下一个（未来的）时刻，缓存自然恢复。若重查回来的**仍是**很久以前的值
    （活动已停但上游没更新字段之类），那说明这个字段已经不可信，再拿它当
    到期条件就会让缓存永久失效、每次轮询都打上游。故超过 ``FLIP_GRACE_S``
    就退回 TTL——反正 TTL 到了还会再查一次，不会漏掉恢复。
    """
    if not isinstance(data, dict):
        return False
    nxt = data.get("next_ts")
    if isinstance(nxt, bool) or not isinstance(nxt, (int, float)):
        return False
    return nxt <= now and now - nxt <= FLIP_GRACE_S


def _expiring(provider_entries: list[dict[str, Any]], now: float | None = None) -> list[dict[str, Any]]:
    """挑出「快到期」的权益条目，供管理页顶部横幅展示（2026-10-03）。

    到期告警是**权益**的到期（``expire_ts``：套餐、加油包、签到积分、资源包
    这些领了就有保质期的东西），不是周期重置（``reset_ts``：5 小时窗口、
    weekly 池这类到点回满的）。两种时刻在前端是两个字段、两套文案，这里只认
    前者——把重充当到期会天天误报「你的额度快没了」（其实只是快刷新了）。

    两条过滤，缺一不可：

    1. **天数**：``days_left < EXPIRY_WARN_DAYS``（含已过期——最该提醒的正是
       这个）。
    2. **量**：``unit == "credit"`` 的条目还要 ``remaining >
       EXPIRY_WARN_MIN_CREDITS``。用户 2026-10-03 要求「大于 300 积分再提醒」：
       否则每天签到的 200 分小包一到 7 天内就刷一排横幅，真正值钱的（会员包、
       4000 分加油包）反而被淹没。非积分类（天数 / 次数 / 千分制）量纲不同、
       无法与 300 比较，只看天数——它们的 ``remaining`` 是「还剩几天」这类量，
       本来就该按天预警。

    ``unit`` 缺失或为 ``None`` 时**不做量过滤**（宽松放行）：宁可多提醒一个，
    也别因为新通道忘了填 ``unit`` 而漏掉真到期。这是有意的取舍。

    纯函数（``now`` 可注入），便于测试；不触网、不改状态。
    """
    now = time.time() if now is None else now
    out: list[dict[str, Any]] = []
    for entry in provider_entries:
        quota = entry.get("quota") or {}
        if not quota.get("supported"):
            continue
        for it in quota.get("items") or []:
            if not isinstance(it, dict):
                continue
            expire_ts = it.get("expire_ts")
            if isinstance(expire_ts, bool) or not isinstance(expire_ts, (int, float)):
                continue
            if expire_ts <= 0:
                continue
            days_left = (expire_ts - now) / 86400.0
            if days_left >= EXPIRY_WARN_DAYS:
                continue
            if it.get("unit") == "credit":
                remaining = it.get("remaining")
                if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
                    continue  # 拿不到余量就不报：无法判断值不值得提醒
                if remaining <= EXPIRY_WARN_MIN_CREDITS:
                    continue
            out.append({
                "provider": entry.get("id"),
                "provider_name": entry.get("name") or entry.get("id"),
                "label": it.get("label"),
                "expire_ts": int(expire_ts),
                "days_left": round(days_left, 1),
                "remaining": it.get("remaining"),
                "unit": it.get("unit"),
            })
    out.sort(key=lambda e: e["expire_ts"])
    return out


def _is_failure(data: Any) -> bool:
    """判断 provider 返回/兜错的结果是否算「查询失败」，决定用不用短 TTL。

    认三种形态：兜错字典（含 ``error``）、provider 直接在结果上标了失败
    （``query_failed``/``unreachable``）、以及 items 里**混进了**失败说明条
    （``trae.pat.quota._failure_notice`` 会把说明条插在首位，后面跟着各账号
    缓存数据——这种「部分失败」同样要短缓存，好让网络恢复后尽快自愈）。
    """
    if not isinstance(data, dict):
        return False
    if data.get("error") or data.get("query_failed") or data.get("unreachable"):
        return True
    items = data.get("items")
    if isinstance(items, list):
        return any(isinstance(i, dict) and i.get("query_failed") for i in items)
    return False


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def claimable_now(status: Any) -> bool:
    """上游此刻是否**有可领的签到**。

    用来盖过「今天已打过」的本地记录：历史按**日历日**去重，而轮换单位不一定
    是日历日。qoder 的活动窗口实测是「10:00 → 次日 09:59」，于是窗口内凌晨的
    巡检会把上一轮的 ``checked_in`` 补记成今天已打，等 10:00 新活动开出、上游
    报 ``claimable`` 时，光看本地记录就会整天跳过领取，界面也显示「已签到」+
    按钮禁用。（实测 ``logs/checkin.jsonl`` 里 qoder 至今两条记录的 message
    都是「今日已领取」，即都由「上游已签到 → 补记」写入、**代理自己没领过**，
    所以是「会漏」而非「已漏」——但界面确实已经在说「已签到」。）

    ``checked_in`` 与 ``claimable`` 同时为真时**以 checked_in 为准**：三家真实
    provider 都保证互斥（qoder 的 ``checked_in`` 就是 ``claimed and not
    claimable``），同时为真只能是上游自相矛盾或桩数据写错，此时宁可按「已打」
    处理——重复 claim 虽幂等，但每轮都会多打一次上游。
    """
    if not isinstance(status, dict):
        return False
    if status.get("error"):
        return False  # 查询失败：拿不到实况，不要据此推翻本地记录
    return bool(status.get("claimable")) and not status.get("checked_in")


async def _call(fn: Callable, *args):
    """调用 provider 的打卡/额度方法，同步异步都支持。

    provider 多为同步实现（urllib/httpx 同步），经 ``asyncio.to_thread`` 跑，
    避免阻塞事件循环；但 Qoder 这类通道只有异步 HTTP 客户端，``to_thread``
    拿到的是**协程对象**而不是结果（既不 await 就丢弃，返回值也不对），
    故按函数类型分派。
    """
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await asyncio.to_thread(fn, *args)


def read_checkin_settings() -> dict[str, Any]:
    saved = settings_mod.load_settings()
    raw_time = str(saved.get("checkin_time") or DEFAULT_CHECKIN_TIME)
    return {
        "auto_checkin": bool(saved.get("auto_checkin", True)),
        "checkin_time": raw_time if re.fullmatch(r"\d{1,2}:\d{2}", raw_time) else DEFAULT_CHECKIN_TIME,
    }


class CheckinHistory:
    """append-only 打卡历史（JSONL）。"""

    def __init__(self, path):
        self.path = path

    def append(self, provider: str, ok: bool, message: str = "",
               credits: Any = None, date: str | None = None) -> None:
        rec = {
            "ts": round(time.time(), 3),
            "date": date or _today(),
            "provider": provider,
            "ok": bool(ok),
            "message": (message or "")[:200],
            "credits": credits,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 历史落盘失败不影响打卡本身

    def entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def ok_dates_by_provider(self) -> dict[str, set[str]]:
        """provider -> 打卡成功的日期集合（含上游返回"已签到"的记录）。"""
        dates: dict[str, set[str]] = {}
        for rec in self.entries():
            if rec.get("ok"):
                dates.setdefault(rec.get("provider", ""), set()).add(rec.get("date", ""))
        return dates

    def calendar(self, days: int = 35) -> list[dict[str, Any]]:
        """最近 N 天的打卡日历，旧日期在前。

        每天一项：``{"date", "providers": [id...], "credits": {id: 积分}}``，
        credits 是当天实际领取的签到积分（上游签到历史合并进来的日子没有
        本地领取记录，只有 provider 名）。
        """
        by_date: dict[str, dict[str, Any]] = {}
        for rec in self.entries():
            if not (rec.get("ok") and rec.get("date")):
                continue
            info = by_date.setdefault(rec["date"], {"providers": [], "credits": {}})
            pid = rec.get("provider", "")
            if pid not in info["providers"]:
                info["providers"].append(pid)
            if rec.get("credits") is not None:
                info["credits"][pid] = rec["credits"]
        today = datetime.now().date()
        out = []
        for offset in range(days - 1, -1, -1):
            date = (today - timedelta(days=offset)).isoformat()
            info = by_date.get(date, {"providers": [], "credits": {}})
            out.append({
                "date": date,
                "providers": sorted(info["providers"]),
                "credits": info["credits"],
            })
        return out


class BenefitsManager:
    """打卡状态聚合 + 自动打卡后台循环。挂在 ``ProxyState.benefits`` 上。"""

    def __init__(self, history_path, state: Any):
        self.history = CheckinHistory(history_path)
        self._state = state
        self._task: asyncio.Task | None = None
        self._last_attempt: dict[str, float] = {}
        self._startup_seen: set[str] = set()
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------
    # provider 能力
    # ------------------------------------------------------------------

    def _providers(self) -> dict[str, Any]:
        """默认 codebuddy 通道 + 已启用 provider（UI 面板按此顺序渲染，
        codebuddy 恒排首位，其余按注册序）。"""
        try:
            from buddy_proxy.codebuddy_provider import _default_codebuddy
            providers = {"codebuddy": _default_codebuddy}
        except Exception:
            providers = {}
        providers.update(getattr(self._state, "providers", {}) or {})
        return providers

    def checkin_providers(self) -> dict[str, Any]:
        return {
            pid: p for pid, p in self._providers().items()
            if getattr(p, "supports_checkin", False)
        }

    # ------------------------------------------------------------------
    # 快照（GET /ui/api/benefits）
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        checkin_cfg = read_checkin_settings()
        done = self.history.ok_dates_by_provider()
        today = _today()

        provider_entries = []
        for pid, p in self._providers().items():
            supports_checkin = bool(getattr(p, "supports_checkin", False))
            entry: dict[str, Any] = {
                "id": pid,
                "name": getattr(p, "name", pid),
                "checkin": {"supported": supports_checkin},
                "quota": {"supported": False},
            }
            if supports_checkin:
                status = await self._cached(f"checkin:{pid}", p.checkin_status)
                done_today = not claimable_now(status) and (
                    today in done.get(pid, set())
                    or bool(status and status.get("checked_in"))
                )
                entry["checkin"].update({
                    "done_today": done_today,
                    **(status or {}),
                })
            # 缓存键可带 provider 的「缓存代」：账号列表会变的通道（antigravity）
            # 声明 quota_epoch()，账号一变键就变，旧快照不再顶满 TTL
            epoch_fn = getattr(p, "quota_epoch", None)
            qkey = f"quota:{pid}" + (f":{epoch_fn()}" if callable(epoch_fn) else "")
            quota = await self._cached(qkey, p.quota)
            if quota is not None:
                entry["quota"] = {"supported": True, **quota}
            provider_entries.append(entry)

        # 上游直接给出的签到历史（如 CodeBuddy checkin_dates）合并进日历，
        # 这样代理没记录过的历史打卡天也能展示（无本地领取积分记录）
        day_map: dict[str, dict[str, Any]] = {}
        day_order: list[str] = []
        for d in self.history.calendar(35):
            day_map[d["date"]] = {
                "providers": set(d["providers"]),
                "credits": dict(d.get("credits") or {}),
            }
            day_order.append(d["date"])
        for entry in provider_entries:
            pid = entry["id"]
            for ds in entry["checkin"].get("checkin_dates") or []:
                if ds in day_map and pid not in day_map[ds]["providers"]:
                    day_map[ds]["providers"].add(pid)
        calendar_out = [
            {"date": d,
             "providers": sorted(day_map[d]["providers"]),
             "credits": day_map[d]["credits"]}
            for d in day_order
        ]

        return {
            "providers": provider_entries,
            "calendar": calendar_out,
            "auto_checkin": checkin_cfg["auto_checkin"],
            "checkin_time": checkin_cfg["checkin_time"],
            "checkin_enabled_providers": sorted(self.checkin_providers()),
            # 到期告警明细（见 _expiring）。放后端算而不是前端：阈值与「积分类
            # 才做量过滤」的规则集中一处，pytest 直接覆盖；前端只渲染不判规则，
            # 免得两边各写一份慢慢走偏。空列表＝无告警，前端据此隐藏横幅。
            "expiring": _expiring(provider_entries),
        }

    async def _cached(self, key: str, fn: Callable, *args):
        """TTL 缓存的 to_thread 调用；上游抛错时返回错误结构而不是 500。

        失败结果只短缓存（``FAILURE_TTL_S``）：额度/打卡这类查询失败往往是网络
        抖动（切 WiFi、VPN 重连），若按正常的 5 分钟缓存，用户会盯着一条不准确
        的「网关不可达 / 查询失败」警告好几分钟——哪怕几秒后网就恢复了。成功
        结果照常缓存 ``SNAPSHOT_TTL_S``。

        还有一条比 TTL 更早的失效条件：快照自己说的「翻转时刻」已过
        （见 :func:`_state_flipped`），此时状态必然已经变了，再拿它渲染就是
        在说谎。
        """
        now = time.time()
        cached = self._cache.get(key)
        if cached:
            ttl = FAILURE_TTL_S if _is_failure(cached[1]) else SNAPSHOT_TTL_S
            if now - cached[0] < ttl and not _state_flipped(cached[1], now):
                return cached[1]
        try:
            data = await _call(fn, *args)
        except Exception as exc:
            data = {"error": str(exc)[:300]}
        self._cache[key] = (now, data)
        return data

    # ------------------------------------------------------------------
    # 手动 / 自动打卡
    # ------------------------------------------------------------------

    async def claim_now(self, provider_id: str) -> dict[str, Any]:
        provider = self.checkin_providers().get(provider_id)
        if provider is None:
            return {"ok": False, "error": f"provider {provider_id} 不支持打卡"}
        return await self._claim_and_record(provider_id, provider)

    async def _claim_and_record(self, provider_id: str, provider: Any) -> dict[str, Any]:
        # 领完就作废状态缓存：_tick 现在也走这份缓存，不清掉的话界面要等满
        # TTL（300s）才会从「立即打卡」翻成「已签到」。手动与自动共用这条路径。
        self._cache.pop(f"checkin:{provider_id}", None)
        try:
            status = await _call(provider.checkin_claim)
        except Exception as exc:
            message = str(exc)[:300]
            # 上游返回"已签到"类提示视为成功（幂等补记录）
            already = ("已签" in message) or ("already" in message.lower())
            self.history.append(provider_id, ok=already, message=message)
            return {"ok": already, "already": already, "message": message}
        self.history.append(provider_id, ok=True,
                            message=status.get("message", ""),
                            credits=status.get("extra_credits", status.get("credits")))
        return {"ok": True, **(status or {})}

    # ------------------------------------------------------------------
    # 后台自动打卡循环
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # 自动打卡异常不影响代理转发
            await asyncio.sleep(CHECK_INTERVAL_S)

    async def _tick(self) -> None:
        cfg = read_checkin_settings()
        if not cfg["auto_checkin"]:
            return
        hh, mm = (int(x) for x in cfg["checkin_time"].split(":"))
        now = datetime.now()
        due_today = (now.hour, now.minute) >= (hh, mm)
        done = self.history.ok_dates_by_provider()
        for pid, provider in self.checkin_providers().items():
            first_run = pid not in self._startup_seen
            self._startup_seen.add(pid)
            # 启动后首轮视为补签窗口（不管是否到点）；之后只在到达设定时刻后打
            if not due_today and not first_run:
                continue
            if time.time() - self._last_attempt.get(pid, 0.0) < RETRY_THROTTLE_S:
                continue
            # 先查状态再决定是否领：避免对「已签到/当天无活动」的上游反复打 claim。
            # 走 _cached（与 snapshot 同一份，TTL 300s），管理页开着时这里几乎
            # 总是命中缓存，不会因本次调整而多打上游。
            status = await self._cached(f"checkin:{pid}", provider.checkin_status)
            if not isinstance(status, dict):
                continue
            # 查询失败（``_cached`` 把异常兜成 ``{"error": ...}``，不会抛出）时
            # 必须停在这里：拿不到实况就既不知道「打没打过」也不知道「有没有
            # 活动」，继续往下会对着一个查不通的通道反复 claim，把失败记录
            # 写进 checkin.jsonl 污染日历。失败结果只短缓存 30s，网络恢复后
            # 下一轮自然重试。
            if status.get("error"):
                continue
            # 「今天已打过」要以上游实况为准（见 :func:`claimable_now`）：
            # 本地历史按日历日去重，而 qoder 这类通道按「10:00 窗口」轮换，
            # 光看本地记录会整天跳过领取。与 snapshot 共用同一个判据，避免
            # 两处口径漂移（一处认 claimable、另一处不认，就会界面说没打、
            # 后台也不领，或反过来）。
            if not claimable_now(status) and _today() in done.get(pid, set()):
                continue
            self._last_attempt[pid] = time.time()
            if status.get("checked_in"):
                # 上游已签到（如网页/客户端手动签过）→ 补记历史，当天不再重试
                self.history.append(pid, ok=True,
                                    message=status.get("message") or "已签到（上游记录）")
                continue
            if status.get("claimable") is False:
                continue  # 当天无签到活动（如 CodeBuddy 档期未开），保持轻量轮询
            await self._claim_and_record(pid, provider)
