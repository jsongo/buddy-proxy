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
// 快照已有旧值（回填路径；null 会走「首份快照整卡重渲」分支）
AG_ACCTS = [
  {index: 1, email: 'old@x.com', project_id: 'p', hours_left: 9, cooling: []},
  {index: 2, email: 'old2@x.com', project_id: '', hours_left: null, cooling: []},
];
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
AG_ACCTS = [{index: 1, email: 'stale@x.com', project_id: '', hours_left: null, cooling: []}];
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
AG_ACCTS = [{index: 1, email: 'a@x.com', project_id: 'p', hours_left: 9, cooling: []}];
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


def test_move_buttons_multi_account_with_boundary_disables():
    """多账号：每块有 ▲▼，首位 ▲ 禁用、末位 ▼ 禁用；单账号不渲染按钮。"""
    multi = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
  {label: 'AG #2 · Gemini 组', remaining: 500, total: 1000, percent: 50, used: null, reset_ts: null},
]}}]};
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(multi.strip().splitlines()[-1])["html"]
    assert html.count("agMoveAccount(") == 4, "两个账号各 ▲▼ 共 4 个按钮"
    assert "agMoveAccount(1,-1)" in html and "agMoveAccount(1,1)" in html
    assert "agMoveAccount(2,-1)" in html and "agMoveAccount(2,1)" in html
    # 首位 ▲、末位 ▼ 禁用（看按钮前后的 disabled）
    first_up = html.split("agMoveAccount(1,-1)")[0].rsplit("<button", 1)[-1]
    last_down = html.split("agMoveAccount(2,1)")[0].rsplit("<button", 1)[-1]
    assert "disabled" in first_up and "disabled" in last_down

    single = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    assert "agMoveAccount(" not in json.loads(single.strip().splitlines()[-1])["html"], \
        "单账号没有顺位可调，不应渲染按钮"


def test_move_account_posts_full_order_and_refreshes():
    """点 ▼（把 #1 下移）：POST 完整的重排后 id 列表，拿到响应后触发刷新。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', project_id: 'p', hours_left: 1, cooling: []},
  {index: 3, id: 'c@x.com', email: 'c@x.com', project_id: 'p', hours_left: 1, cooling: []},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  if (path.endsWith('/order')) {
    return {enabled: true, accounts: [
      {index: 1, id: 'b@x.com', email: 'b@x.com'}, {index: 2, id: 'a@x.com', email: 'a@x.com'},
      {index: 3, id: 'c@x.com', email: 'c@x.com'}]};
  }
  return {enabled: true, accounts: []};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = (m) => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
await agMoveAccount(1, 1);   // #1 下移 → 期望 b,a,c
console.log(JSON.stringify({
  calls: CALLS, toasts: TOASTS, refreshed: REFRESHED,
  snapshot: AG_ACCTS.map(a => a.id)}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    order_call = [c for c in data["calls"] if c["path"].endswith("/order")]
    assert len(order_call) == 1
    assert order_call[0]["body"]["ids"] == ["b@x.com", "a@x.com", "c@x.com"], \
        "提交的是完整顺序（后端校验完整性）"
    assert data["refreshed"] == 1, "重排后要刷新额度（quota_epoch 变了，重查换新顺位）"
    assert data["snapshot"] == ["b@x.com", "a@x.com", "c@x.com"], "本地快照同步成响应"
    assert any("a@x.com" in t and "#2" in t for t in data["toasts"]), \
        f"toast 报被移动的账号与其新顺位: {data['toasts']}"


def test_move_buttons_carry_account_id_when_snapshot_present():
    """有快照时按钮内联账号 id：面板重绘滞后于快照时靠它定位（见 agMoveAccount）。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
  {label: 'AG #2 · Gemini 组', remaining: 500, total: 1000, percent: 50, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "agMoveAccount(1,-1,'a@x.com')" in html and "agMoveAccount(1,1,'a@x.com')" in html
    assert "agMoveAccount(2,1,'b@x.com')" in html, "每个按钮都带自己账号的 id"


def test_move_account_uses_id_position_when_dom_is_stale():
    """按钮 idx 过期（重排响应已回、面板未重绘）：按 id 校正，挪的是账号自己。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com'},
  {index: 2, id: 'b@x.com', email: 'b@x.com'},
  {index: 3, id: 'c@x.com', email: 'c@x.com'},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(opts ? JSON.parse(opts.body).ids : null);
  if (path.endsWith('/order')) {
    const ids = JSON.parse(opts.body).ids;
    return {enabled: true, accounts: ids.map((id, i) => ({index: i+1, id, email: id}))};
  }
  return {enabled: true, accounts: []};
};
let TOASTS = [];
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => {};
await agMoveAccount(1, 1, 'a@x.com');   // [a,b,c] → [b,a,c]
// 面板还没重绘，屏幕上的按钮仍是 idx=1；但 a 的实际位次已是 #2。
// 不按 id 校正的话这次会挪到 b（提交 [a,b,c] 把整轮点回去）。
await agMoveAccount(1, 1, 'a@x.com');
console.log(JSON.stringify({calls: CALLS, final: AG_ACCTS.map(a => a.id), toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"][1] == ["b@x.com", "c@x.com", "a@x.com"], \
        f"第二次点按应按 id 定位到 a 的当前位次: {data['calls']}"
    assert data["final"] == ["b@x.com", "c@x.com", "a@x.com"]
    assert any("a@x.com" in t and "#3" in t for t in data["toasts"])


def test_move_account_fetches_snapshot_when_missing():
    """按钮随额度先到、快照还没回：点按时先补拉 accounts 再提交。"""
    out = _run_js("""
AG_ACCTS = null;
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  if (path.endsWith('/order')) return {enabled: true, accounts: []};
  return {enabled: true, accounts: [
    {index: 1, id: 'a@x.com', email: 'a@x.com'},
    {index: 2, id: 'b@x.com', email: 'b@x.com'}]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
await agMoveAccount(2, -1);  // #2 上移 → 期望 b,a
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert len(data["calls"]) == 2, "先 GET 补快照，再 POST order"
    assert data["calls"][0]["path"] == "/ui/api/antigravity/accounts"
    assert data["calls"][1]["body"]["ids"] == ["b@x.com", "a@x.com"]


def test_move_account_inflight_clicks_ignored():
    """重排在途（POST + 额度重取还没完）：第二次点按直接忽略，不重复提交。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com'},
  {index: 2, id: 'b@x.com', email: 'b@x.com'},
  {index: 3, id: 'c@x.com', email: 'c@x.com'},
];
let CALLS = [], TOASTS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(opts ? JSON.parse(opts.body).ids : null);
  if (path.endsWith('/order')) {
    const ids = JSON.parse(opts.body).ids;
    return {enabled: true, accounts: ids.map((id, i) => ({index: i+1, id, email: id}))};
  }
  return {enabled: true, accounts: []};
};
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => {};
const p1 = agMoveAccount(1, 1, 'a@x.com');
const p2 = agMoveAccount(1, 1, 'a@x.com');   // 在飞，应被忽略
await Promise.all([p1, p2]);
console.log(JSON.stringify({calls: CALLS, final: AG_ACCTS.map(a => a.id), toasts: TOASTS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [["b@x.com", "a@x.com", "c@x.com"]], \
        f"在途点按不应产生第二次提交: {data['calls']}"
    assert data["final"] == ["b@x.com", "a@x.com", "c@x.com"]
    assert len(data["toasts"]) == 1, "只报一次"


def test_move_account_missing_id_is_noop():
    """按钮带的 id 不在快照里（账号刚被删）：宁可不动，也不按过期 idx 挪别人。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com'},
  {index: 2, id: 'b@x.com', email: 'b@x.com'},
];
let CALLS = [];
globalThis.api = async (path, opts) => {
  CALLS.push(path);
  return {enabled: true, accounts: []};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
await agMoveAccount(1, 1, 'gone@x.com');   // 不在快照里的 id
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == [], f"不应发任何请求: {data['calls']}"


# ---------------------------------------------------------------------------
# 删除账号（✕ 按钮 + agDeleteAccount）
# ---------------------------------------------------------------------------

def test_delete_button_renders_even_for_single_account():
    """✕ 不依赖多账号（单个被拉黑的账号恰恰最需要删）；要等快照给出 id。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
AG_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "agDeleteAccount('a@x.com')" in html, "单账号也要有删除入口"
    assert "agMoveAccount(" not in html, "单账号没有顺位可调（与既有行为一致）"

    nosnap = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
AG_ACCTS = null;
globalThis.__RESPONSE = {enabled: false};
renderAntigravityPanel();
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    assert "agDeleteAccount(" not in json.loads(nosnap.strip().splitlines()[-1])["html"], \
        "快照没到（拿不到 id）时先不渲染删除按钮"


def test_delete_account_confirms_posts_and_updates_snapshot():
    """确认后 POST /delete（带 id），快照同步响应、toast 报邮箱、触发刷新。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
let CALLS = [];
globalThis.confirm = () => true;
globalThis.api = async (path, opts) => {
  CALLS.push({path, body: opts ? JSON.parse(opts.body) : null});
  return {enabled: true, accounts: [{index: 1, id: 'b@x.com', email: 'b@x.com'}]};
};
let TOASTS = [], REFRESHED = 0;
globalThis.toast = m => TOASTS.push(m);
globalThis.refreshAll = () => REFRESHED++;
await agDeleteAccount('a@x.com');
console.log(JSON.stringify({
  calls: CALLS, toasts: TOASTS, refreshed: REFRESHED,
  snapshot: AG_ACCTS.map(a => a.id)}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert len(data["calls"]) == 1 and data["calls"][0]["path"].endswith("/delete")
    assert data["calls"][0]["body"] == {"id": "a@x.com"}
    assert data["snapshot"] == ["b@x.com"], "快照同步成删除后响应"
    assert data["refreshed"] == 1, "删除改变轮换组成，要刷新额度面板"
    assert any("a@x.com" in t for t in data["toasts"])


def test_first_snapshot_rerenders_panel_with_buttons():
    """首份快照到位（首屏 AG_ACCTS=null）：整卡重渲，✕ 不干等 30s 才出现。"""
    out = _run_js("""
globalThis.BENEFITS = {providers: [{id: 'antigravity', quota: {supported: true, items: [
  {label: 'AG #1 · Gemini 组', remaining: 900, total: 1000, percent: 10, used: null, reset_ts: null},
]}}]};
AG_ACCTS = null;
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, id: 'a@x.com', email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []},
]};
renderAntigravityPanel();   // 首渲（无快照占位）尾部拉账号 → 快照到位触发重渲
await new Promise(r => setTimeout(r, 10));   // render 内部 load 是 fire-and-forget，等它落地
console.log(JSON.stringify({html: PANEL.innerHTML}));
""")
    html = json.loads(out.strip().splitlines()[-1])["html"]
    assert "agDeleteAccount('a@x.com')" in html, "首份快照就该带出 ✕，不等下一轮 30s"
    assert "a@x.com" in html and ">AG #1<" not in html, "重渲走快照直出，不闪内部代号"


def test_delete_account_cancelled_sends_nothing():
    """confirm 取消：不发请求、快照不动。"""
    out = _run_js("""
AG_ACCTS = [{index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []}];
let CALLS = [];
globalThis.confirm = () => false;
globalThis.api = async (path) => { CALLS.push(path); return {enabled: true, accounts: []}; };
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
await agDeleteAccount('a@x.com');
console.log(JSON.stringify({calls: CALLS, snapshot: AG_ACCTS.map(a => a.id)}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"] == []
    assert data["snapshot"] == ["a@x.com"]


def test_delete_account_inflight_clicks_ignored():
    """与顺位调整共用 AG_MOVING 在飞锁：确认框关掉后的连点只发一个请求。"""
    out = _run_js("""
AG_ACCTS = [
  {index: 1, id: 'a@x.com', email: 'a@x.com', cooling: []},
  {index: 2, id: 'b@x.com', email: 'b@x.com', cooling: []},
];
let CALLS = [];
globalThis.confirm = () => true;
globalThis.api = async (path, opts) => {
  CALLS.push(path);
  await new Promise(r => setTimeout(r, 20));   // 拉长在飞窗口
  return {enabled: true, accounts: [{index: 1, id: 'b@x.com', email: 'b@x.com'}]};
};
globalThis.toast = () => {};
globalThis.refreshAll = () => {};
const p1 = agDeleteAccount('a@x.com');
const p2 = agDeleteAccount('a@x.com');   // 在飞，应被忽略
await Promise.all([p1, p2]);
console.log(JSON.stringify({calls: CALLS}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["calls"].count("/ui/api/antigravity/accounts/delete") == 1, \
        f"在飞点按不应产生第二次删除请求: {data['calls']}"


def test_blacklist_subtitle_label_and_hours():
    """拉黑冷却的副标题标注「疑似拉黑」；2h 以上按小时显示（不是 360min）。"""
    out = _run_js("""
globalThis.__RESPONSE = {enabled: true, accounts: [
  {index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1,
   cooling: [{kind: 'blacklist', minutes_left: 359.5}]},
  {index: 2, email: 'b@x.com', project_id: 'p', hours_left: 1, cooling: []},
]};
QS['#antigravity-panel [data-ag-idx="1"]'] = el('AG #1');
QS['#antigravity-panel [data-ag-idx="2"]'] = el('AG #2');
// 快照已有旧值，走就地回填分支（null 会触发整卡重渲、不经过回填）
AG_ACCTS = [
  {index: 1, email: 'a@x.com', project_id: 'p', hours_left: 1, cooling: []},
  {index: 2, email: 'b@x.com', project_id: 'p', hours_left: 1, cooling: []},
];
await loadAntigravityAccounts();
console.log(JSON.stringify({sub: QS['#antigravity-panel [data-ag-idx="1"]'].inserted.join('')}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "疑似拉黑" in data["sub"], data["sub"]
    assert "6.0h" in data["sub"], f"6h 档要按小时展示: {data['sub']}"
    assert "360min" not in data["sub"]
