"""打卡轮换时刻：把各上游的「下次什么时候能再打」统一成一个 epoch 秒。

三个支持打卡的通道，轮换语义**各不相同**，而且只有 Qoder 的上游明确给出
时间窗（均为 2026-09-30 实测）：

- **qoder**：活动自带 ``startAt`` / ``endAt``，实测 ``09-29 10:00`` →
  ``09-30 09:59``（UTC+8 的 10 点轮换），下一轮开始就是 ``endAt + 60``。
  绝对时刻由上游给，本模块不参与。
- **codebuddy**：上游只给**整个活动的档期**（``start_time`` /
  ``end_time``，如 ``2026-09-30 00:00:00`` ~ ``2026-10-15 23:59:59``），
  **没有**每日轮换字段。
- **trae**：``/ug/checkin_credits/status`` 返回里连档期都没有，只有
  ``checked_in`` / ``enable``。

后两者的每日轮换时刻只能从 ``logs/checkin.jsonl`` 的真实领取记录反推：
连续多天在 ``00:01``、``00:04``、``00:05``、``00:09`` 领取成功并被记为
新的一天（``09-27 01:56``、``02:20`` 这种凌晨时刻同样成功），说明是
**本地零点**轮换。这是推断而非上游契约，故 :func:`next_daily_reset` 的
调用方要把它标成 ``inferred``，界面上不宣称是上游给的。

统一口径：``next_ts`` 是**当前状态翻转的时刻**——已签到时它是下一轮开始，
未签到时它正好也是本轮截止（零点轮换下两者同一时刻），所以一个字段就够。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

#: ``next_ts`` 的来源，供界面标注可信度。
#:
#: - ``upstream``：上游明确给了时间窗（目前只有 qoder）
#: - ``inferred``：上游没有每日轮换字段，按实测的零点轮换推断
#: - ``none``：算不出来（无活动/档期已结束），界面不显示
SOURCE_UPSTREAM = "upstream"
SOURCE_INFERRED = "inferred"


def next_daily_reset(now: float | None = None) -> int:
    """下一个**本地零点**的 epoch 秒。

    用本地时区而不是 UTC：打卡记录的 ``date`` 走 :func:`time.strftime`
    （本地时区），轮换既然与它一致，就必须用同一口径算，否则跨时区部署时
    界面显示的「下次」会与日历上的格子差一天。
    """
    moment = datetime.fromtimestamp(time.time() if now is None else now)
    tomorrow = (moment + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int(tomorrow.timestamp())


def parse_upstream_datetime(text: object) -> int | None:
    """上游的 ``"YYYY-MM-DD HH:MM:SS"`` -> epoch 秒；解析不出返回 ``None``。

    CodeBuddy 的档期是**没有时区标注**的本地时间字符串（实测
    ``start_time = "2026-09-30 00:00:00"`` 正是当天零点），按本地时区解析。
    宁可返回 ``None`` 让界面不显示，也不要抛异常把整个打卡面板打挂——
    这是展示用的辅助信息，不该有让 500 的能力。
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    return None


def next_from_window(start_at: int, end_at: int, now: float | None = None) -> int | None:
    """上游给了时间窗（qoder）时，算下一轮开始的 epoch 秒。

    实测窗口是 ``startAt 10:00:00`` → ``endAt 次日 09:59:00``（整 23h59m），
    所以 ``endAt + 60`` 正好落在下一轮的 ``10:00:00``。

    ``endAt + 60`` 已经过去时（列表是旧的 / 时钟漂移）退回 ``startAt + 24h``
    ——它同样是 10:00，且不依赖窗口长度。两个都落在过去就返回 ``None``，
    界面不显示，好过显示一个已经过期的时刻。
    """
    moment = time.time() if now is None else now
    if end_at > 0:
        candidate = end_at + 60
        if candidate > moment:
            return int(candidate)
    if start_at > 0:
        candidate = start_at + 86400
        if candidate > moment:
            return int(candidate)
    return None


def daily_reset_within_season(season_end: object, now: float | None = None) -> int | None:
    """零点轮换的下次时刻，但受活动档期约束。

    档期已过（``season_end`` 早于下一次零点）就没有「下次」了——继续显示
    明天零点会骗用户：到点确实翻篇，但活动本身已经结束、领不出东西。
    """
    reset = next_daily_reset(now)
    end = parse_upstream_datetime(season_end)
    if end is not None and end < reset:
        return None
    return reset
