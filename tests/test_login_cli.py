"""``buddy login <provider>`` 的 CLI 入口测试（离线，不访问上游）。

锁住两件容易回退的事：

1. **zcode 的指引要能照着做**。zcode 没有可自动化的浏览器登录（凭据是智谱
   官网签发的 coding-plan API key，签发入口得人工点），所以这个子命令的全部
   价值就是「把怎么拿到 key 说清楚」。早期版本只打印一个文件路径——用户根本
   不知道该去哪儿取 key，等于没说。
2. **``memo`` 是 mimo 的别名**。``login memo`` 是照着日常发音敲出来的，
   打错了才报「未知 provider」很别扭。

运行：
    .venv/bin/python -m pytest tests/test_login_cli.py -v
"""
from __future__ import annotations

import pathlib

import pytest

from buddy_proxy.auth import login as auth_login
from buddy_proxy.providers import zcode


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """隔离状态目录与相关环境变量，绝不碰真实的 key 文件。"""
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path))
    for name in ("ZCODE_API_KEY", "MIMO_API_KEY", "MIMO_ACCOUNT_JSON", "MIMO_COOKIE_DB"):
        monkeypatch.delenv(name, raising=False)
    # 别真去读用户主目录下的 ZCode CLI 配置（config.json 那条回退链）
    monkeypatch.setattr(
        zcode.Path, "home", classmethod(lambda cls: pathlib.Path(tmp_path))
    )


# ---------------------------------------------------------------------------
# zcode 指引
# ---------------------------------------------------------------------------


def test_zcode_guide_tells_where_to_get_key(capsys):
    """未配置时：必须给出控制台地址 + 购买入口 + 具体怎么配。"""
    rc = auth_login._login_zcode()
    out = capsys.readouterr().out

    assert rc == 1, "没配好应当返回非零，脚本才能感知"
    assert auth_login.ZCODE_CONSOLE_URL in out, "必须告诉用户去哪领 key"
    assert auth_login.ZCODE_PLAN_URL in out, "没套餐的话也要知道去哪开通"
    assert "ZCODE_API_KEY" in out, "要给出环境变量这种最快的方式"
    assert "buddy restart" in out, "配完得知道要重启才生效"


def test_zcode_guide_shows_secret_path_when_configured(capsys, monkeypatch):
    """已配置时：回显打码 key，并说明换 key 改哪里。"""
    monkeypatch.setenv("ZCODE_API_KEY", "1234567890abcdef.ABCDEFGHIJKLMNOP")
    rc = auth_login._login_zcode()
    out = capsys.readouterr().out

    assert rc == 0
    assert "1234567890abcdef.ABCDEFGHIJKLMNOP" not in out, "完整 key 绝不能回显"
    assert "123456***MNOP" in out, "打码形式便于对照是哪把 key"
    assert auth_login.ZCODE_CONSOLE_URL in out


# ---------------------------------------------------------------------------
# 别名
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("memo", "mimo"),
        ("mimocode", "mimo"),
        ("MIMO", "mimo"),
        ("MEMO", "mimo"),  # 大小写归一后照样命中 memo 别名
        ("workbuddy", "codebuddy"),
        ("cb", "codebuddy"),
        ("quoder", "qoder"),
    ],
)
def test_aliases_resolve(typed, expected):
    """别名归一：大小写不敏感，敲错音近的名字也有救。"""
    got = auth_login.PROVIDER_ALIASES.get(typed.strip().lower(), typed.strip().lower())
    assert got == expected


def test_every_alias_target_is_dispatchable():
    """别名只能指向真实存在的 provider，否则等于把用户引到 error 分支。"""
    for alias, target in auth_login.PROVIDER_ALIASES.items():
        assert target in auth_login._DISPATCH, f"别名 {alias} 指向了不存在的 {target}"
        assert target in auth_login.KNOWN_PROVIDERS
