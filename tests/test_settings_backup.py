"""``save_settings`` 覆盖前留备份的回归测试（完全离线，不触碰真实 ~/.buddy-proxy）。

管理页的写操作（改默认模型、停用、时段、候选顺序）都是**整体覆盖**
``settings.json``；每次覆盖前把上一版存一份 ``settings.json.bak``，让手工配置可回滚。

要守住的性质：

1. 覆盖前**上一版**确实落到了 ``.bak``（不是新版、不是空文件）。
2. 首次保存（还没有文件）不产生 ``.bak``——没有「上一版」可留。
3. 备份权限与主文件一致（0o600），不因为多了份副本就把配置暴露出去。
4. 备份失败**绝不阻断**主写入：设置该存还得存（护栏不能变成新故障点）。
"""
from __future__ import annotations

import json
import pathlib
import stat

import pytest

from buddy_proxy.core import settings as settings_mod


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("BUDDY_PROXY_SETTINGS", str(tmp_path / "settings.json"))
    return tmp_path


def _bak() -> pathlib.Path:
    p = settings_mod.settings_path()
    return p.with_name(p.name + ".bak")


def test_first_save_makes_no_backup():
    """还没有设置文件时没有「上一版」，不该凭空造一个 .bak。"""
    settings_mod.save_settings({"default_provider": "codebuddy"})
    assert settings_mod.settings_path().exists()
    assert not _bak().exists()


def test_backup_holds_previous_version_not_the_new_one():
    """第二次保存时，.bak 必须是**上一版**——否则备份毫无意义。"""
    settings_mod.save_settings({"default_provider": "codebuddy"})
    settings_mod.save_settings({"model_order": {"a/m": ["a/m", "b/m"]}})

    assert _bak().exists()
    previous = json.loads(_bak().read_text(encoding="utf-8"))
    assert previous["default_provider"] == "codebuddy"
    # 本次写入的内容不该出现在备份里
    assert "model_order" not in previous

    # 且再次保存会把 .bak 推进到中间那一版
    settings_mod.save_settings({"default_provider": "trae"})
    mid = json.loads(_bak().read_text(encoding="utf-8"))
    assert "model_order" in mid
    assert mid["default_provider"] == "codebuddy"


def test_backup_is_written_atomically_and_not_half_written():
    """备份走临时文件 + os.replace：不该在 .bak 旁边留残渣。"""
    settings_mod.save_settings({"default_provider": "codebuddy"})
    settings_mod.save_settings({"default_model": "codebuddy/auto"})
    leftovers = [p.name for p in _bak().parent.iterdir() if p.name.startswith(".")
                 and p.name.endswith(".tmp")]
    assert leftovers == []


def test_backup_permissions_match_main_file():
    """配置里有停用列表/时段等信息，备份不该比主文件更宽松。"""
    settings_mod.save_settings({"default_provider": "codebuddy"})
    settings_mod.save_settings({"default_model": "codebuddy/auto"})
    mode = stat.S_IMODE(_bak().stat().st_mode)
    assert mode == 0o600


def test_backup_failure_does_not_block_the_save(monkeypatch):
    """护栏不能变成新故障点：备份写不出来时，设置照样存下去。

    这里只让**备份路径**炸（替换 ``_backup_settings`` 内部用的 ``Path.write_text``
    对 ``.bak.tmp`` 的调用会连带影响主写入，所以直接在 helper 上打桩，精确命中
    「备份这一步失败」而主流程完好）。
    """
    settings_mod.save_settings({"default_provider": "codebuddy"})

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(settings_mod, "_backup_settings", boom)
    saved = settings_mod.save_settings({"default_provider": "trae"})

    assert saved["default_provider"] == "trae"
    assert json.loads(
        settings_mod.settings_path().read_text(encoding="utf-8")
    )["default_provider"] == "trae"


def test_backup_swallows_unserializable_previous(monkeypatch):
    """备份步骤抛**非 OSError**（如 json.dumps 的 TypeError）同样不能阻断保存。

    护栏要收得够宽：真出这种事时正确行为是「这次没备份，但设置照存」，而不是让
    管理页操作 500。这里让 ``_backup_settings`` 抛 TypeError 来精确模拟。
    """
    settings_mod.save_settings({"default_provider": "codebuddy"})

    def boom(*_args, **_kwargs):
        raise TypeError("Object of type set is not JSON serializable")

    monkeypatch.setattr(settings_mod, "_backup_settings", boom)
    saved = settings_mod.save_settings({"default_provider": "trae"})
    assert saved["default_provider"] == "trae"
    assert json.loads(
        settings_mod.settings_path().read_text(encoding="utf-8")
    )["default_provider"] == "trae"


def test_backup_cleans_up_its_own_partial_temp_file(monkeypatch):
    """备份自己失败时要清掉写了一半的临时文件，别在目录里留残渣。"""
    settings_mod.save_settings({"default_provider": "codebuddy"})
    tmp_name = settings_mod.settings_path().name + ".bak.tmp"

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(settings_mod.pathlib.Path, "write_text", boom)
    with pytest.raises(OSError):
        settings_mod._backup_settings(settings_mod.settings_path(),
                                      {"default_provider": "codebuddy"})
    assert not (settings_mod.settings_path().parent / ("." + tmp_name)).exists()


def test_backup_does_not_replace_a_readable_settings_file_on_bad_json():
    """设置文件损坏（读不出来）时按「无上一版」处理，不拿空 dict 覆盖备份。

    否则一次 json 解析失败就会把好备份抹成 ``{}``，护栏反而成了数据丢失源。
    """
    settings_mod.save_settings({"default_provider": "codebuddy"})
    settings_mod.save_settings({"default_model": "codebuddy/auto"})
    good = _bak().read_text(encoding="utf-8")

    settings_mod.settings_path().write_text("{ not json", encoding="utf-8")
    settings_mod.save_settings({"default_provider": "trae"})

    assert _bak().read_text(encoding="utf-8") == good, "坏 JSON 不该污染已有的好备份"
