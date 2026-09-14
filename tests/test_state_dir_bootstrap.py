"""首次运行的状态目录初始化回归（完全离线，不触碰真实 ~/.buddy-proxy）。

历史缺陷：``state_dir()`` 只解析路径不建目录，``state_file()`` 仅在遗留迁移
分支里 mkdir，``load_settings()`` 读不到文件时静默返回 {}。于是新用户装完
跑起来后 ``~/.buddy-proxy/`` 根本不存在（只有带了 --default-model 或改过
管理页设置才会被 ``save_settings()`` 顺带创建），看起来像没装好。
"""
from __future__ import annotations

import os
import stat

import pytest

from buddy_proxy.core import paths


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """状态目录整体指向 tmp，避免动到开发者真实的 ~/.buddy-proxy。"""
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def test_state_dir_does_not_create_by_itself():
    """纯解析不该有副作用（保持原语义：只给路径）。"""
    d = paths.state_dir()
    assert not d.exists()


def test_ensure_state_dir_creates_missing_directory():
    d = paths.ensure_state_dir()
    assert d == paths.state_dir()
    assert d.is_dir()


def test_ensure_state_dir_is_idempotent_and_keeps_contents(tmp_path):
    d = paths.ensure_state_dir()
    (d / "settings.json").write_text("{}", encoding="utf-8")
    assert paths.ensure_state_dir() == d
    assert (d / "settings.json").read_text(encoding="utf-8") == "{}"


def test_ensure_state_dir_creates_nested_paths(monkeypatch, tmp_path):
    nested = tmp_path / "a" / "b" / "state"
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(nested))
    assert paths.ensure_state_dir() == nested
    assert nested.is_dir()


def test_ensure_state_dir_permissions_are_private():
    """目录里会落凭证类文件，权限收 0700。"""
    d = paths.ensure_state_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


def test_ensure_state_dir_expands_user(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", "~/buddy-proxy-test-dir")
    d = paths.ensure_state_dir()
    assert "~" not in str(d)
    assert d.is_dir()
    d.rmdir()


def test_ensure_state_dir_survives_unwritable_parent(monkeypatch, tmp_path):
    """建不出来时不抛异常，交给真正需要写盘的调用去报错。"""
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(ro / "state"))
    try:
        d = paths.ensure_state_dir()  # 不应抛
        assert d == ro / "state"
        assert not d.exists()
    finally:
        os.chmod(ro, 0o700)


def test_ensure_state_dir_matches_settings_file_location(monkeypatch, tmp_path):
    """state_file() 与 ensure_state_dir() 必须落在同一目录。"""
    d = paths.ensure_state_dir()
    f = paths.state_file("settings.json")
    assert f.parent == d
    assert f.parent.is_dir()
