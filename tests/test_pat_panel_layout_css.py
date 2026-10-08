"""TRAE PAT 面板两栏布局的静态断言：列轨道必须能收缩到内容以下。

真实的 bug（用户截图）：额度网关不可达时，面板第一块是那条长说明条
（「Trae PAT (服务账号直连) · PAT 额度网关不可达 剩 10/10 个账号本轮未取到
新数据（网络不可达）；下方为上次成功查询的缓存…」）。``.pat-quota-grid`` 当时
写的是 ``grid-template-columns: 1fr 1fr``——``1fr`` 的隐含下限是内容的
``min-content``（``minmax(auto, 1fr)``），长说明条把**左列**撑到 900px+，
右列（PAT #1/#3/#5 账号卡）被挤到 400px 并整体顶出卡片右缘，视觉上就是
「账号卡片超出了父容器」。

修法是把轨道下限压到 0：``repeat(2, minmax(0, 1fr))``，两列严格等宽，长文本
交给 ``.qlabel`` 的省略号收起。这个断言盯住「别改回 1fr」——改回去不报错、
只是又溢出了，所以必须静态盯着。

同一个面板的 ``.pat-load-grid``（下方模型负载两栏）是同一失效模式，一并
盯住。``.pat-quota-grid`` 还被多个通道面板共用（antigravity / kimi / qoder /
trae / codebuddy / dumate），所以这里查的是**类定义本身**，修好一次对全部面板
生效，也保证不会有哪个面板被改回会溢出的写法。
"""

from __future__ import annotations

import pathlib
import re

import pytest

STYLE_CSS = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static/style.css"


def _rule(selector: str) -> str:
    """取 style.css 里某个选择器的声明块（剥掉注释）。"""
    text = STYLE_CSS.read_text(encoding="utf-8")
    # 注释里为了说明来龙去脉会写「反例」写法（如 `1fr 1fr`），不剥会先命中它
    css = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"style.css 里找不到 {selector} 的声明块（选择器改名了？）"
    return m.group(1)


@pytest.mark.parametrize("selector", [".pat-quota-grid", ".pat-load-grid"])
def test_pat_grid_tracks_can_shrink_below_content(selector):
    """两栏轨道必须是 minmax(0, 1fr)：1fr 的 min-content 下限会让长说明条
    把一列撑爆、另一列被顶出父容器（用户截图报的账号卡溢出）。"""
    decl = _rule(selector)
    m = re.search(r"grid-template-columns\s*:\s*([^;]+);", decl)
    assert m, f"{selector} 没写 grid-template-columns"
    tracks = m.group(1).strip()

    # 两列等宽：允许 「repeat(2, minmax(0, 1fr))」 或显式写两遍 minmax(0, 1fr)
    assert "minmax(0, 1fr)" in tracks, (
        f"{selector} 的列轨道必须是 minmax(0, 1fr)，实际: {tracks!r}——"
        "写 1fr 会在长说明条下溢出父容器"
    )
    # 「1fr」单独出现（不带 minmax 下限）就是回归：1fr 不能在内容以下收缩
    assert not re.search(r"(?<!minmax\(0,\s)1fr", tracks.replace("minmax(0, 1fr)", "")), (
        f"{selector} 里出现了裸 1fr 轨道，实际: {tracks!r}"
    )


@pytest.mark.parametrize("selector", [".pat-quota-grid", ".pat-load-grid"])
def test_pat_grid_narrow_fallback_is_single_column(selector):
    """窄屏回退到单列时，也必须是 minmax(0, 1fr)——同一条溢出规则。"""
    text = STYLE_CSS.read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    m = re.search(
        r"@media \(max-width: 720px\)\s*\{\s*" + re.escape(selector) +
        r"\s*\{\s*grid-template-columns\s*:\s*([^;]+);", css)
    assert m, f"找不到 {selector} 的 720px 单列回退"
    tracks = m.group(1).strip()
    assert tracks.startswith("minmax(0,"), (
        f"{selector} 单列回退应为 minmax(0, 1fr)，实际: {tracks!r}"
    )
