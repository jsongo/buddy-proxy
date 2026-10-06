"""glm 官方渠道（BigModel Coding Plan）接入回归测试（离线，不访问上游）。

背景（2026-10-06 实测，lite 档）：``glm-5.3`` / ``glm-5.3-flash`` /
``glm-5-turbo`` 均 200；``glm-5.3-flashx`` 仍 ``429 code 1311「当前订阅套餐
暂未开放GLM-5.3-FlashX权限」``（与 zcode 的 2026-09-19 实测一致）——模型表
继承 zcode 的「预备接入」策略，套餐升级后无需改代码。

核心契约：glm 与 zcode **同上游、凭据链独立**——glm 只认 ``GLM_API_KEY`` /
``~/.buddy-proxy/glm_api_key``，绝不读 ``~/.zcode`` CLI 配置（两条 key 是
独立购买的两个套餐，串了额度就对不上）。
"""
from __future__ import annotations

from buddy_proxy.providers.glm import GlmProvider, resolve_credentials, secret_file_path
from buddy_proxy.providers.zcode import BIGMODEL_ANTHROPIC_BASE, ZcodeProvider


# ---------------------------------------------------------------------------
# 子类关系与注册
# ---------------------------------------------------------------------------


def test_glm_subclasses_zcode_sharing_upstream():
    """glm 是 zcode 的子类：同 base、同模型表（转发/直通/额度全继承）。"""
    assert issubclass(GlmProvider, ZcodeProvider)
    p = GlmProvider()
    assert p.id == "glm"
    assert "BigModel" in p.name
    assert p.health()["base_url"] == BIGMODEL_ANTHROPIC_BASE


def test_glm_models_inherited_including_flashx_reserve():
    """模型表继承 zcode：lite 实测可用的三个 + flashx 预备接入。"""
    ids = {m["id"] for m in GlmProvider().models()}
    assert {"glm-5.3", "glm-5.3-flash", "glm-5-turbo", "glm-5.3-flashx"} <= ids


def test_glm_registered_in_known_provider_ids():
    """``glm/<模型>`` 未启用时要报「通道未启用」而非漏给兜底通道。"""
    from buddy_proxy.core.settings import KNOWN_PROVIDER_IDS, PROVIDER_ENABLE_HINTS

    assert "glm" in KNOWN_PROVIDER_IDS
    assert "--glm" in PROVIDER_ENABLE_HINTS["glm"]


# ---------------------------------------------------------------------------
# 凭据链：env > 自有 key 文件；绝不读 ~/.zcode
# ---------------------------------------------------------------------------


def _isolate(monkeypatch, tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BUDDY_PROXY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("GLM_API_KEY", raising=False)
    monkeypatch.delenv("ZCODE_API_KEY", raising=False)
    # HOME 指到空目录：zcode 的第三级兜底（~/.zcode/v2/config.json）即使被
    # 误调也读不到东西——配合下面的断言锁死「glm 不走那条链」。
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_secret_file_lives_in_project_state_dir(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    path = secret_file_path()
    assert path.parent == tmp_path / "state"
    assert path.name == "glm_api_key"


def test_credentials_env_beats_file(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    secret_file_path().write_text("file.key.value\n", encoding="utf-8")
    monkeypatch.setenv("GLM_API_KEY", "env.key.value")
    key, base = resolve_credentials()
    assert key == "env.key.value"
    assert base == BIGMODEL_ANTHROPIC_BASE


def test_credentials_from_file_without_zcode_config(tmp_path, monkeypatch):
    """key 文件生效；且 HOME 下放一个 zcode CLI 配置也**不许**被读到。"""
    _isolate(monkeypatch, tmp_path)
    # 伪造 ~/.zcode/v2/config.json（若 glm 误走 zcode 链，这里会被读走）
    zcfg = tmp_path / "home" / ".zcode" / "v2"
    zcfg.mkdir(parents=True)
    (zcfg / "config.json").write_text(
        '{"provider":{"p":{"kind":"anthropic","enabled":true,'
        '"options":{"apiKey":"zcode-cli-key","baseURL":"https://x.example"}}}}',
        encoding="utf-8",
    )
    secret_file_path().write_text("glm.own.key\n", encoding="utf-8")
    key, _base = resolve_credentials()
    assert key == "glm.own.key"


def test_credentials_empty_when_nothing_configured(tmp_path, monkeypatch):
    """无 env、无文件、HOME 空 → key 为空（绝不拿到 zcode CLI 的 key）。"""
    _isolate(monkeypatch, tmp_path)
    zcfg = tmp_path / "home" / ".zcode" / "v2"
    zcfg.mkdir(parents=True)
    (zcfg / "config.json").write_text(
        '{"provider":{"p":{"kind":"anthropic","enabled":true,'
        '"options":{"apiKey":"zcode-cli-key"}}}}',
        encoding="utf-8",
    )
    key, _base = resolve_credentials()
    assert key == ""


def test_load_secret_file_impl_shapes(tmp_path, monkeypatch):
    """读法与 zcode 同源：裸 key / name=value / 多行取首个非空行。"""
    _isolate(monkeypatch, tmp_path)
    from buddy_proxy.providers import glm

    glm.secret_file_path().write_text("GLM_API_KEY=abc.def\n", encoding="utf-8")
    assert glm._load_secret_file_impl(glm.secret_file_path()) == "abc.def"
    glm.secret_file_path().write_text("\n\nbare.key\nsecond.key\n", encoding="utf-8")
    assert glm._load_secret_file_impl(glm.secret_file_path()) == "bare.key"
    glm.secret_file_path().write_text("", encoding="utf-8")
    assert glm._load_secret_file_impl(glm.secret_file_path()) == ""


# ---------------------------------------------------------------------------
# 认证与额度 guard
# ---------------------------------------------------------------------------


def test_ensure_auth_message_points_to_glm_sources(tmp_path, monkeypatch):
    import fastapi

    _isolate(monkeypatch, tmp_path)
    p = GlmProvider()
    try:
        p.ensure_auth()
        raise AssertionError("无凭据时 ensure_auth 必须 401")
    except fastapi.HTTPException as exc:
        msg = str(exc.detail)
        assert "GLM_API_KEY" in msg and "glm_api_key" in msg
        # 文案必须指向 glm 自己的凭据源，不许让用户去配 zcode
        assert "~/.zcode" not in msg


def test_quota_refuses_to_borrow_zcode_key(tmp_path, monkeypatch):
    """glm 凭据未就绪时，额度查询必须报 glm 文案——不许借 zcode 的 key。

    父类 ZcodeProvider.quota() 在 self._api_key 为空时会 fallback 到 zcode
    的三级凭据链；对 glm 渠道那意味着拿 zcode 的 key 查出 zcode 套餐的
    额度、渲染进 glm 的额度卡。guard 必须在网络请求之前就拦下。
    """
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ZCODE_API_KEY", "zcode.key.should-not-be-borrowed")
    p = GlmProvider()
    try:
        p.quota()
        raise AssertionError("无凭据时 quota 必须 raise")
    except RuntimeError as exc:
        assert "glm 未配置 API key" in str(exc)


def test_zcode_quota_fallback_still_intact(tmp_path, monkeypatch):
    """反向回归：glm 的 guard 不得改变 zcode 自己的凭据兜底行为。"""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("ZCODE_API_KEY", "zc.ey")
    from buddy_proxy.providers import zcode

    p = ZcodeProvider()
    assert p._api_key == "zc.ey"


def test_credit_estimate_reuses_official_coeffs():
    """glm 请求的 Credit 列必须复用 zcode 的官方抵扣系数。

    estimate_credit 按 provider id 分派——漏掉 glm 会掉进倍率粗估并因
    glm 无倍率表返回 None（Credit 列整列空白）。两条通道同上游同计价，
    同一输入应给出同一个数。
    """
    from buddy_proxy.core.credit_estimate import estimate_credit

    args = ("glm-5.3-flash", 100_000, 10_000, 0)
    assert estimate_credit("glm", *args) == estimate_credit("zcode", *args)
    assert estimate_credit("glm", *args) is not None
