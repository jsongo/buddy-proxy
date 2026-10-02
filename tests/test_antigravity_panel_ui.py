"""Antigravity 面板「邮箱上牌 + 账号状态一行」的前端测试。

额度接口（/ui/api/benefits）的 label 只带「AG #N · 」前缀、不含邮箱，账号
状态接口（/ui/api/antigravity/accounts）才有 email——渲染时序上额度先出、
账号后到，组名替换只能发生在 accounts 回来之后。这里用最小 DOM 桩真跑一遍
``loadAntigravityAccounts``，盯三件事：

- **组名=邮箱**：多账号按 ``data-ag-idx`` 对应替换；单账号组名没有前缀，
  整组直接替换——别退回显示「AG #1」/「Antigravity」这种内部代号
- **状态一行**：邮箱已在组名上，账号状态区不再逐账号竖排、不重复 email，
  只剩 #序号 / 缺 project / token 剩余 / 冷却的一行简报
- **未登录**：enabled=false 显示提示，不渲染空壳

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

BENEFITS_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/benefits.js"
)
FUNC_RE = re.compile(r"async function loadAntigravityAccounts\(\) \{(.*?)\n\}\n", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)


def _run_js(body: str) -> str:
    """抽出 loadAntigravityAccounts，带最小 DOM 桩跑一遍，返回 stdout。"""
    text = BENEFITS_JS.read_text(encoding="utf-8")
    match = FUNC_RE.search(text)
    assert match, "benefits.js 里找不到 loadAntigravityAccounts（被改名/挪走了？）"
    stub = """
let QS = {};                        // 选择器 -> 桩元素（没设的选择器返回 null）
let BOX = {innerHTML: ''};          // #antigravity-accounts
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.api = async () => globalThis.__RESPONSE;
globalThis.document = {
  querySelector: sel => (Object.prototype.hasOwnProperty.call(QS, sel) ? QS[sel] : null),
  querySelectorAll: () => [],
  getElementById: id => (id === 'antigravity-accounts' ? BOX : null),
};
"""
    script = (
        stub + match.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_multi_account_group_names_become_emails():
    """多账号：组名 AG #N 按序号替换成对应邮箱；状态行不重复 email。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'a@x.com', project_id: 'p', hours_left: 3.2, cooling: []},
  {index: 2, email: 'b@x.com', project_id: 'p', hours_left: null, cooling: []},
]};
QS['#antigravity-panel [data-ag-idx="1"]'] = {textContent: 'AG #1'};
QS['#antigravity-panel [data-ag-idx="2"]'] = {textContent: 'AG #2'};
await loadAntigravityAccounts();
console.log(JSON.stringify({
  name1: QS['#antigravity-panel [data-ag-idx="1"]'].textContent,
  name2: QS['#antigravity-panel [data-ag-idx="2"]'].textContent,
  box: BOX.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name1"] == "a@x.com", "组名 1 应替换成账号 1 的邮箱"
    assert data["name2"] == "b@x.com", "组名 2 应替换成账号 2 的邮箱"
    assert "a@x.com" not in data["box"] and "b@x.com" not in data["box"], \
        "邮箱已在组名上，账号状态区不该重复"
    assert "#1" in data["box"] and "#2" in data["box"]
    # 一行：pat-acct 只出现一次（不再每账号一行竖排）
    assert data["box"].count('class="pat-acct"') == 1, data["box"]


def test_single_account_group_name_replaced_without_index():
    """单账号组名没有 AG #N 前缀（后端 label 不加）→ 唯一组名直接换邮箱。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'solo@x.com', project_id: 'p', hours_left: 0.7, cooling: []},
]};
QS['#antigravity-panel .pat-pkg-name'] = {textContent: 'Antigravity'};
await loadAntigravityAccounts();
console.log(JSON.stringify({
  name: QS['#antigravity-panel .pat-pkg-name'].textContent,
  box: BOX.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name"] == "solo@x.com", "单账号组名应替换成邮箱，而不是停在 'Antigravity'"
    assert "solo@x.com" not in data["box"]
    assert "token 剩 0.7h" in data["box"]


def test_status_line_carries_warnings_and_cooldown():
    """缺 project / token 剩余 / 冷却都进状态行；email 不出现。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'a@x.com', project_id: '', hours_left: null,
   cooling: [{kind: 'quota', minutes_left: 4}, {kind: 'account', minutes_left: 1}]},
]};
await loadAntigravityAccounts();
console.log(JSON.stringify({box: BOX.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "缺 project" in data["box"]
    assert "额度冷却 4min" in data["box"] and "账号冷却 1min" in data["box"]
    assert "a@x.com" not in data["box"], "邮箱只在组名上，状态行不重复"


def test_disabled_shows_hint_not_empty():
    """未登录任何账号：给一句提示，别渲染空壳让人以为面板坏了。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: false};
await loadAntigravityAccounts();
console.log(JSON.stringify({box: BOX.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "未登录" in data["box"]
