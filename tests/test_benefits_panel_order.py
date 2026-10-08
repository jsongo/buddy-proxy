"""额度页特殊通道卡片的视觉顺序契约。"""

from pathlib import Path


_INDEX = Path(__file__).parents[1] / "src/buddy_proxy/web/static/index.html"


def test_kimi_is_immediately_above_dumate_and_traepat_is_last():
    """Kimi 紧贴百度搭子上方；运维性质的 Trae PAT 永远收在末尾。"""
    html = _INDEX.read_text("utf-8")
    kimi = html.index('id="kimi-panel"')
    dumate = html.index('id="dumate-panel"')
    traepat = html.index('id="traepat-panel"')

    assert kimi < dumate < traepat
    between = html[kimi:dumate]
    assert "-panel" not in between.replace('id="kimi-panel"', "")
    assert "-panel" not in html[traepat:].replace('id="traepat-panel"', "")
