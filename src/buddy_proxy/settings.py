"""管理页设置持久化：默认启用模型、兜底通道覆盖等。

存放在 ``~/.buddy-proxy/settings.json``（可用 ``BUDDY_PROXY_SETTINGS`` 覆盖），
机器本地配置，不进仓库。启动时由 ``__main__`` 加载进 :class:`ProxyState`，
管理页 POST /ui/api/settings 修改后立即写回并热更新到运行态。
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_DEFAULT_PATH = "~/.buddy-proxy/settings.json"

# 时段判定统一用上海时区，与 trae/pat/cooldown._next_day_timestamp 的日界一致。
_SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# 单模型最多允许的时间窗数量；防止 UI/配置写入超长列表。
_MAX_WINDOWS = 8


def settings_path() -> pathlib.Path:
    return pathlib.Path(
        os.getenv("BUDDY_PROXY_SETTINGS", _DEFAULT_PATH)
    ).expanduser()


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(update: dict[str, Any]) -> dict[str, Any]:
    """合并写入并返回合并后的完整设置。"""
    path = settings_path()
    merged = {**load_settings(), **update, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except Exception:
        pass
    return merged


def normalize_default_model(raw: str) -> str:
    """归一化默认模型字符串：去空白、provider 别名归一。"""
    value = (raw or "").strip()
    if "/" in value:
        prefix, model = value.split("/", 1)
        # workbuddy 是 codebuddy 的旧称
        prefix = {"workbuddy": "codebuddy"}.get(prefix, prefix)
        value = f"{prefix}/{model}"
    return value


def model_key(provider: str, model: str) -> str:
    """停用集合的规范键：``<provider>/<model>``，provider 缺省视为 codebuddy。"""
    provider = (provider or "codebuddy").strip()
    provider = {"workbuddy": "codebuddy"}.get(provider, provider)
    return f"{provider}/{(model or '').strip()}"


def _to_minutes(hhmm: str) -> int | None:
    """``HH:MM`` → 当日分钟数（0..1439）；非法返回 None。"""
    m = _HHMM_RE.match((hhmm or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def normalize_windows(raw: Any) -> list[list[str]]:
    """校验并归一化时间窗列表 ``[["HH:MM","HH:MM"], ...]``。

    - 每个窗口两个合法 ``HH:MM``；起==止视为无效（零长度）丢弃。
    - 起>止允许（跨零点，如 22:00~08:00）。
    - 最多保留 ``_MAX_WINDOWS`` 个，超量截断。
    - 输入不是 list、项非法一律跳过，绝不抛异常（配置层容错）。
    """
    if not isinstance(raw, list):
        return []
    out: list[list[str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = _to_minutes(str(item[0])), _to_minutes(str(item[1]))
        if start is None or end is None or start == end:
            continue
        out.append([f"{start // 60:02d}:{start % 60:02d}",
                    f"{end // 60:02d}:{end % 60:02d}"])
        if len(out) >= _MAX_WINDOWS:
            break
    return out


def model_schedule_open(windows: Any, now: float | None = None) -> bool:
    """当前时刻是否落入任一允许窗口（上海时区）。

    ``windows`` 为 ``normalize_windows`` 归一后的列表。空列表 → 恒 False
    （无任何开放时段 = 始终挡）。跨零点窗口（起>止）判定为 ``t>=start or t<end``。
    """
    wins = windows if isinstance(windows, list) else []
    if not wins:
        return False
    ts = now if now is not None else time.time()
    current = datetime.fromtimestamp(ts, _SCHEDULE_TZ)
    minute = current.hour * 60 + current.minute
    for item in wins:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        start, end = _to_minutes(str(item[0])), _to_minutes(str(item[1]))
        if start is None or end is None or start == end:
            continue
        if start < end:
            if start <= minute < end:
                return True
        else:  # 跨零点
            if minute >= start or minute < end:
                return True
    return False


def format_windows(windows: Any) -> str:
    """人类可读窗口摘要，如 ``22:00~08:00, 12:00~14:00``；空 → ``无开放时段``。"""
    wins = windows if isinstance(windows, list) else []
    parts = [f"{item[0]}~{item[1]}" for item in wins
             if isinstance(item, (list, tuple)) and len(item) == 2]
    return ", ".join(parts) if parts else "无开放时段"
