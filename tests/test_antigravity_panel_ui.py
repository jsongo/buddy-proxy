"""Antigravity 面板「标题=邮箱、副标题=账号状态」的前端测试。

额度接口（/ui/api/benefits）的 label 只带「AG #N · 」前缀、不含邮箱，账号
状态接口（/ui/api/antigravity/accounts）才有 email——渲染时序上额度先出、
账号后到，标题/副标题的回填只能发生在 accounts 回来之后。这里把整段面板
代码抽出来用最小 DOM 桩真跑，盯四件事：

- **标题=邮箱**：多账号按 ``data-ag-idx`` 对应替换；单账号组名没有前缀，
  唯一标题直接替换——别退回显示「AG #1」/「Antigravity」这种内部代号
- **副标题=状态**：标题下一行 muted 小字（缺 project / token 剩余 / 冷却），
  且数据变化时重写（冷却结束要消失）、不重复堆叠
- **不闪内部代号**：有 AG_ACCTS 快照时 render 直接渲染邮箱，30s 轮询重建
  面板不再闪回「AG #N」
- **底部状态区已删**：状态全部上移为各账号副标题，不再有独立区块

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
# 整段 ANTIGRAVITY 区块（renderAntigravityPanel + loadAntigravityAccounts +
# AG_ACCTS/_ag_* 辅助），下一段是 trae 模型负载的 force=false 注释
SEG_RE = re.compile(r"// ---- ANTIGRAVITY 面板.*?(?=\n// force=false)", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let QS = {};                        // 选择器 -> 桩元素（没设的选择器返回 null）
let PANEL = {innerHTML: ''};        // #antigravity-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.api = async () => globalThis.__RESPONSE;
globalThis.quotaItemHtml = it => '<div class="qitem">' + esc(it.label) + '</div>';
globalThis.BENEFITS = {};
globalThis.document = {
  querySelector: sel => (Object.prototype.hasOwnProperty.call(QS, sel) ? QS[sel] : null),
  querySelectorAll: () => [],
  getElementById: id => (id === 'antigravity-panel' ? PANEL : null),
};
function el(text) {
  return {
    textContent: text,
    nextElementSibling: null,
    inserted: [],
    insertAdjacentHTML(pos, html) { this.inserted.push(html); },
  };
}
"""


def _run_js(body: str) -> str:
    text = BENEFITS_JS.read_text(encoding="utf-8")
    match = SEG_RE.search(text)
    assert match, "benefits.js 里找不到 ANTIGRAVITY 面板段（边界注释被改了？）"
    script = (
        _STUB + match.group(0)
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_accounts_fill_names_and_subtitles():
    """多账号：标题按 data-ag-idx 换成邮箱，副标题带状态且不含 email。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'a@x.com', project_id: 'p', hours_left: 3.2, cooling: []},
  {index: 2, email: 'b@x.com', project_id: '', hours_left: null,
   cooling: [{kind: 'quota', minutes_left: 4}]},
]};
QS['#antigravity-panel [data-ag-idx="1"]'] = el('AG #1');
QS['#antigravity-panel [data-ag-idx="2"]'] = el('AG #2');
await loadAntigravityAccounts();
console.log(JSON.stringify({
  n1: QS['#antigravity-panel [data-ag-idx="1"]'].textContent,
  n2: QS['#antigravity-panel [data-ag-idx="2"]'].textContent,
  sub1: QS['#antigravity-panel [data-ag-idx="1"]'].inserted.join(''),
  sub2: QS['#antigravity-panel [data-ag-idx="2"]'].inserted.join('')}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["n1"] == "a@x.com", "标题 1 应替换成账号 1 的邮箱"
    assert data["n2"] == "b@x.com", "标题 2 应替换成账号 2 的邮箱"
    assert "token 剩 3.2h" in data["sub1"], data["sub1"]
    assert "缺 project" in data["sub2"] and "额度冷却 4min" in data["sub2"], data["sub2"]
    assert "a@x.com" not in data["sub1"] and "b@x.com" not in data["sub2"], \
        "邮箱在标题上，副标题只放状态"


def test_single_account_name_and_subtitle_without_index():
    """单账号标题没有 AG #N 前缀（后端 label 不加）→ 唯一标题直接换。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'solo@x.com', project_id: 'p', hours_left: 0.7, cooling: []},
]};
QS['#antigravity-panel .pat-pkg-name'] = el('Antigravity');
await loadAntigravityAccounts();
console.log(JSON.stringify({
  name: QS['#antigravity-panel .pat-pkg-name'].textContent,
  sub: QS['#antigravity-panel .pat-pkg-name'].inserted.join('')}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name"] == "solo@x.com", "单账号标题应替换成邮箱，而不是停在 'Antigravity'"
    assert "token 剩 0.7h" in data["sub"]


def test_changed_data_rewrites_subtitle_without_duplicating():
    """数据变化：副标题**重写**（旧内容消失），而不是在旁边再插一份。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'a@x.com', project_id: 'p', hours_left: 2, cooling: []},
]};
const nameEl = el('AG #1');
nameEl.nextElementSibling = {
  hasAttribute: t => t === 'data-ag-sub',
  remove() { this.removed = true; },
};
// 单账号（accts.length===1）走 .pat-pkg-name 分支，不走 data-ag-idx
QS['#antigravity-panel .pat-pkg-name'] = nameEl;
await loadAntigravityAccounts();
console.log(JSON.stringify({
  removed: !!nameEl.nextElementSibling.removed,
  sub: nameEl.inserted.join('')}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["removed"], "旧副标题要先移除，否则 30s 一轮会越堆越多"
    assert "token 剩 2h" in data["sub"], "新副标题反映最新状态"


def test_unchanged_data_leaves_dom_alone():
    """accounts 没变就不动 DOM——render 已用同一份快照渲染过，别把邮箱打回代号。"""
    accts = "[{index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []}]"
    out = _run_js(f"""
AG_ACCTS = {accts};
globalThis.__RESPONSE = {{enabled: true, accounts: {accts}}};
QS['#antigravity-panel [data-ag-idx="1"]'] = el('a@x.com');   // render 渲染好的样子
await loadAntigravityAccounts();
console.log(JSON.stringify({{
  name: QS['#antigravity-panel [data-ag-idx="1"]'].textContent,
  touched: QS['#antigravity-panel [data-ag-idx="1"]'].inserted.length}}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["name"] == "a@x.com", "没变就不该重写（重写会闪一下内部代号）"
    assert data["touched"] == 0


def test_render_uses_snapshot_and_drops_status_block():
    """有快照时 render 直接出邮箱+副标题；底部「账号状态」区不再渲染。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组（组内共享 weekly + 5h 双池）',
   remaining: 999, total: 1000, percent: 0.1, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [{index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1.5, cooling: []}];
globalThis.__RESPONSE = {enabled: false};   // render 尾部会再拉一次，别让它改 DOM
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    html = data["html"]
    assert "a@x.com" in html, "有快照就该直接渲染邮箱（不闪 AG #1）"
    assert ">AG #1<" not in html, "内部代号不该露出来"
    assert "data-ag-sub" in html and "token 剩 1.5h" in html, "副标题（状态）在标题下"
    assert 'id="antigravity-accounts"' not in html and "账号状态" not in html, \
        "底部独立状态区已删，状态全部上移为副标题"


def test_render_without_snapshot_keeps_placeholder():
    """没快照（首屏）：先按 AG #N 占位渲染，等 accounts 回来再回填。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #2 · Claude/GPT 组', remaining: 500, total: 1000, percent: 50,
   used: null, reset_ts: null},
]}}]};
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert 'data-ag-idx="2"' in data["html"], "占位要挂序号供回填定位"
    assert ">AG #2<" in data["html"]


def test_disabled_accounts_clear_snapshot():
    """enabled=false（未登录）→ 快照清空；面板本来就隐藏，不该留旧账号残影。"""
    out = _run_js("""
AG_ACCTS = [{index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []}];
globalThis.__RESPONSE = {enabled: false};
await loadAntigravityAccounts();
console.log(JSON.stringify({snapshot: AG_ACCTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["snapshot"] == []
