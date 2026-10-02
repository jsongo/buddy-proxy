"""全局测试夹具。

gemini 凭证相关的两个写盘路径必须**默认隔离**：`GEMINI_OAUTH_JSON`（buddy
侧凭据文件）和 `GEMINI_CLI_HOME`（本机 gemini CLI 配置目录，cli_bridge 的
读写都认它）。曾经吃过亏——test_resume_onboarding_uses_saved_token 只隔离
了前者，互通功能给 resume_onboarding 挂上「回写 ~/.gemini」后，跑一轮全量
测试就把假登录态（token "tok"、邮箱 a@b.c）写进了真实 ~/.gemini，
`buddy login gemini` 差点把假 token 当真用。

个别测试想自定路径时在测试体内再 setenv 覆盖即可（autouse fixture 先跑，
测试体里的 monkeypatch.setenv 后生效）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_gemini_state_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_OAUTH_JSON", str(tmp_path / "gemini_oauth.json"))
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini-cli-home"))
