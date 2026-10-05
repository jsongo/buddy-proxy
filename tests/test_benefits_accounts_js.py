"""benefits_accounts.js 公共多账号 helper 的 node 桩测试。

2026-10 用户要求把 antigravity/kimi/qoder 已稳定的多账号卡片模板抽象成公共件、
未来接入直接复用。该文件把可复用件参数化：confirmAccountDelete（删除确认弹窗，
跨通道单例锁）/ acctSubHtml（账号状态副标题）/ acctMoveButtons / acctDeleteButton /
acctRowButtons / groupAccountsByPrefix / accountGroupBound / acctLoad（账号快照回填）/
acctMove/acctDelete（全局分发）。dumate 面板是第一个复用方（见 benefits.js）。

这里直接跑公共件本身（dumate 之外的消费方面板在 test_antigravity_panel_ui.py /
test_kimi_panel_ui.py 里已各自覆盖）。盯六件事：

- **分组**：按「<Prefix> #N · 」切分组；query_failed / 纯静态说明条不进分组
- **界标**：accountGroupBound 取「<Prefix> #N」序号最大值（不依赖账号快照时序）
- **副标题**：region / token 剩余 / 冷却 bits，通道特有 bit 经 subExtra 注入
- **按钮**：▲▼ 带 id 内联 + 首尾禁用；✕ 带 pid+id；↻ 无条件在按钮组最前
- **确认弹窗**：Promise 化；取消/遮罩汇入 closeModal resolve(false)；确认时锁
  不立即清（交给删除方的 finally），取消立即清
- **回填**：首份快照整卡重渲 + syncQuotaFold；之后就地回填（换名 + 重写副标题，
  不是堆叠）；数据没变不动 DOM
- **分发**：acctMove/acctDelete 按 pid 路由到通道处理函数

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node。
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

_STATIC = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static"
HELPERS_JS = _STATIC / "benefits_accounts.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑面板 JS（CI 有，本机可选）"
)

_STUB = """
let PANEL = {innerHTML: ''};        // #dumate-panel
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.__SYNC_CALLS = 0;
globalThis.syncQuotaFold = () => { globalThis.__SYNC_CALLS++; };
globalThis.BENEFITS = {};
const MOD = {};
globalThis.MOD = MOD;
// 懒建兜底：元素首次被读时若还没建，先补一个带 classList 的桩再返回。
// classList 方法必须是 function 语法（方法调用 this=接收者）——箭头函数会把
// this 词法绑到模块级（CommonJS 下是 undefined），contains 永远炸
// （test_antigravity_panel_ui 的桩只调 add 恰好没踩到）。
function mkClassList() {
  return {_s: new Set(),
          add(c) { this._s.add(c); },
          remove(c) { this._s.delete(c); },
          contains(c) { return this._s.has(c); }};
}
function modalEl(id) {
  if (!MOD[id] || !MOD[id].classList) {
    MOD[id] = Object.assign(MOD[id] || {}, {textContent: '', innerHTML: '',
      classList: mkClassList()});
  }
  return MOD[id];
}
globalThis.modalEl = modalEl;
globalThis.__FOOT = '';
globalThis.__CLOSED = 0;
globalThis.setModalFoot = html => { globalThis.__FOOT = html; };
globalThis.closeModal = () => { globalThis.__CLOSED++; };
const OVERLAY = {className: '', classList: mkClassList()};
let QS = {};                        // 选择器 -> 桩元素（没设的选择器返回 null）
globalThis.__QS = QS;
globalThis.document = {
  querySelector: sel => (Object.prototype.hasOwnProperty.call(QS, sel) ? QS[sel] : null),
  querySelectorAll: () => [],
  getElementById: id => {
    if (id === 'overlay') { MOD[id] = OVERLAY; return OVERLAY; }
    if (id === 'modal-title' || id === 'modal-body') {
      if (!MOD[id]) MOD[id] = {textContent: '', innerHTML: ''};
      return MOD[id];
    }
    return null;
  },
  // 兜底读路径：classList 缺失就补桩（防 undefined.classList）
  __ensureCls: e => e,
};
// 桩账号元素：textContent 可写、insertAdjacentHTML 可观察（acctLoad 就地回填用）
function el(text) {
  return {
    textContent: text,
    nextElementSibling: null,
    inserted: [],
    insertAdjacentHTML(pos, html) { this.inserted.push(html); },
  };
}
globalThis.el = el;
"""


def _run_js(body: str) -> str:
    script = (
        _STUB + HELPERS_JS.read_text(encoding="utf-8")
        + "\n(async () => {\n" + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_group_accounts_by_prefix_splits_and_notices():
    """按「Prefix #N · 」切分组；query_failed 与纯静态条（percent/used 全空）不进组。"""
    out = _run_js("""
const {groups, notices} = groupAccountsByPrefix([
  {label: 'DuMate #1 · 可用额度', used: 0, total: 100, percent: 0},
  {label: 'DuMate #1 ·  another', used: 1, total: 100, percent: 1},
  {label: '单账号无前缀条目', used: 2, total: 100, percent: 2},
  {label: '查询失败说明', query_failed: true},
  {label: '未登录说明条', remaining: '请 buddy login'},
], 'DuMate', 'DuMate');
console.log(JSON.stringify({
  keys: [...groups.keys()],
  sub0: groups.get('DuMate #1')[0].label,
  fallback: groups.get('DuMate').map(it => it.label),
  notices: notices.length,
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["keys"] == ["DuMate #1", "DuMate"], "无前缀条目落到 defaultName 组"
    assert data["sub0"] == "可用额度", "组内条目标签剥掉「Prefix #N · 」前缀"
    assert data["fallback"] == ["单账号无前缀条目"], "defaultName 组里是剥前缀后的条目"
    assert data["notices"] == 2, "query_failed + 纯静态条（percent/used 全空）都进 notices"


def test_account_group_bound_uses_max_index():
    """界标 = 「Prefix #N」序号最大值（自包含，不依赖账号快照到达时序）。"""
    out = _run_js("""
const g = new Map([['DuMate #1', []], ['DuMate #3', []]]);
console.log(JSON.stringify({
  n: accountGroupBound(g, 'DuMate'),
  empty: accountGroupBound(new Map(), 'DuMate'),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["n"] == 3 and data["empty"] == 1


def test_acct_sub_html_bits_and_subextra():
    """通用 bits（region/token 剩余/冷却/疑似拉黑）+ 通道特有 bit 经 subExtra 注入。"""
    out = _run_js("""
const a = {region: 'cn', hours_left: 3.2,
           cooling: [{kind: 'quota', minutes_left: 4}, {kind: 'blacklist', minutes_left: 360}]};
const html = acctSubHtml(a, (x, bits) => bits.push('累计签到 1000 分'), 'data-dumate-sub');
const none = acctSubHtml({cooling: []}, null, 'data-dumate-sub');
console.log(JSON.stringify({html, none}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "CN 区" in data["html"] and "token 剩 3.2h" in data["html"]
    assert "额度冷却 4min" in data["html"] and "疑似拉黑 剩6.0h" in data["html"]
    assert "累计签到 1000 分" in data["html"], "通道特有 bit 注入"
    assert "data-dumate-sub" in data["html"]
    assert data["none"] == "", "没有任何 bit 时不渲染空副标题行"


def test_move_delete_row_buttons_markup():
    """▲▼ 带 id 内联 + 首尾禁用；✕ 带 pid+id + 通道 hint；↻ 无条件排在按钮组最前。"""
    out = _run_js("""
const acct = {id: 'u1'};
console.log(JSON.stringify({
  mid: acctMoveButtons('dumate', 2, 3, 'u1'),
  first: acctMoveButtons('dumate', 1, 3, 'u1'),
  last: acctMoveButtons('dumate', 3, 3, 'u1'),
  del: acctDeleteButton('dumate', acct, '清除本地缓存'),
  delNoAcct: acctDeleteButton('dumate', null, 'x'),
  row: acctRowButtons('dumate', acctMoveButtons('dumate', 1, 1, 'u1'), acctDeleteButton('dumate', acct, 'h')),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "acctMove('dumate',2,-1,'u1')" in data["mid"]
    assert "acctMove('dumate',2,1,'u1')" in data["mid"]
    assert "disabled" in data["first"] and "acctMove('dumate',1,-1" in data["first"]
    assert "disabled" in data["last"]
    assert "acctDelete('dumate','u1')" in data["del"] and "清除本地缓存" in data["del"]
    assert "ghost danger" in data["del"]
    assert data["delNoAcct"] == "", "没有账号（快照未到）时不渲染 ✕"
    assert data["row"].startswith('<span class="ag-move"><button class="ghost" title="刷新')
    assert "refreshProviderQuota('dumate', this)" in data["row"], "↻ 无条件在组内"
    assert data["row"].index("refreshProviderQuota") < data["row"].index("acctMove"), "↻ 在最前"


def test_confirm_account_delete_modal_flow():
    """确认弹窗：标题/正文/danger 键；取消走 closeModal resolve(false) 并立即清锁；
    确认 resolve(true) 但锁不清（交给删除方 finally）。"""
    out = _run_js("""
let TOASTS = [];
globalThis.toast = m => TOASTS.push(m);
const pCancel = confirmAccountDelete({title: '删除 X 账号', name: 'a@x.com', extra: '注'});
await new Promise(r => setTimeout(r, 5));
const shown = {
  title: MOD['modal-title'].textContent,
  body: MOD['modal-body'].innerHTML,
  foot: globalThis.__FOOT,
  overlayShown: modalEl('overlay').classList.contains('show'),
};
globalThis.closeModal();                       // 取消路径
const canceled = await pCancel;
const lockAfterCancel = ACCT_CONFIRM_OPEN;
const pYes = confirmAccountDelete({title: 'T', name: 'n', extra: 'e'});
await new Promise(r => setTimeout(r, 5));
globalThis.__acctDelYes();                     // 确认路径
const yes = await pYes;
console.log(JSON.stringify({shown, canceled, lockAfterCancel, yes,
  lockAfterYes: ACCT_CONFIRM_OPEN, closed: globalThis.__CLOSED}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["shown"]["title"] == "删除 X 账号"
    assert "a@x.com" in data["shown"]["body"] and "注" in data["shown"]["body"]
    assert 'class="danger"' in data["shown"]["foot"]
    assert data["shown"]["overlayShown"]
    assert data["canceled"] is False and data["lockAfterCancel"] is False, "取消立即清锁"
    assert data["yes"] is True
    assert data["lockAfterYes"] is True, "确认后锁仍在（删除方 finally 才放，防连点叠框）"
    assert data["closed"] >= 2, "取消 + 确认（__acctDelYes 内部走 closeModal）各关一次"


def test_confirm_account_delete_is_singleton():
    """跨通道单例：已有一个确认框开着时，再开直接 resolve(false) 不叠层。"""
    out = _run_js("""
const p1 = confirmAccountDelete({title: '一', name: 'a', extra: ''});
await new Promise(r => setTimeout(r, 5));
const p2 = confirmAccountDelete({title: '二', name: 'b', extra: ''});
const second = await p2;
globalThis.__acctDelYes();
const first = await p1;
console.log(JSON.stringify({first, second, lockHeld: ACCT_CONFIRM_OPEN}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["first"] is True and data["second"] is False, "第二个被锁挡掉"
    assert data["lockHeld"], "确认后锁仍在——公共件只负责挡叠层，释放是删除方 finally 的契约"


def test_acct_load_first_snapshot_rerenders_then_in_place():
    """acctLoad 回填：首份快照整卡重渲（render+syncQuotaFold）；之后就地回填
    （换名 + 重写副标题，先删旧的）；数据没变不动 DOM。"""
    out = _run_js("""
let RENDERED = 0;
const state = {accts: null};
// render 桩按真实消费方（renderDumatePanel）的契约：首渲前把快照填进 state，
// 这样 acctLoad 才能区分「首份快照」与「后续变化」
const cfg = {pid: 'dumate', nameOf: a => a.displayName || a.id,
             subExtra: (a, bits) => bits.push('累计签到 ' + a.pts + ' 分'),
             render: () => { RENDERED++; state.accts = state.accts || [{index: 1, id: 'u1', displayName: '旧名', pts: 100}]; }};
const load = acctLoad(cfg, state);
let RESP = {enabled: true, accounts: [
  {index: 1, id: 'u1', displayName: '旧名', pts: 100}]};
globalThis.api = async () => RESP;
await load();                                   // 首份快照 → 整卡重渲
const afterFirst = {rendered: RENDERED, sync: globalThis.__SYNC_CALLS,
                    accts: state.accts.map(a => a.displayName)};
// 第二拍：数据变了 → 就地回填（快照已在，不整卡重渲）
RESP = {enabled: true, accounts: [{index: 1, id: 'u1', displayName: '新名', pts: 200}]};
const nameEl = el('旧名');
nameEl.nextElementSibling = {hasAttribute: t => t === 'data-dumate-sub', remove() { this.removed = true; }};
// 单账号（list.length===1）走 .pat-pkg-name 分支（与真实 dumate 面板一致）；
// 多账号才按 [data-<pid>-idx="N"] 对应
QS['#dumate-panel .pat-pkg-name'] = nameEl;
globalThis.__SYNC_CALLS = 0;
await load();
const afterUpdate = {rendered: RENDERED, sync: globalThis.__SYNC_CALLS,
                     name: nameEl.textContent, sub: nameEl.inserted.join(''),
                     removedOld: !!nameEl.nextElementSibling.removed};
// 第三拍：数据没变 → 不动 DOM
globalThis.__SYNC_CALLS = 0;
await load();
const afterSame = {rendered: RENDERED, sync: globalThis.__SYNC_CALLS,
                   name: nameEl.textContent};
console.log(JSON.stringify({afterFirst, afterUpdate, afterSame}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["afterFirst"]["rendered"] == 1, "首份快照触发整卡重渲"
    assert data["afterFirst"]["sync"] == 1, "重渲后校准折叠"
    assert data["afterUpdate"]["name"] == "新名", "就地回填换组名"
    assert "累计签到 200 分" in data["afterUpdate"]["sub"], "通道特有 bit 进副标题"
    assert data["afterUpdate"]["removedOld"], "旧副标题先移除再插新的（不堆叠）"
    assert data["afterUpdate"]["rendered"] == 1, "快照已在就不整卡重渲（就地回填）"
    assert data["afterSame"]["rendered"] == 1 and data["afterSame"]["sync"] == 0, \
        "数据没变不动 DOM、不再校准"
    assert data["afterSame"]["name"] == "新名", "没变就不重写名字（重写会闪旧代号）"


def test_acct_load_disabled_clears_snapshot():
    """enabled=false 清快照（面板渲染侧据此隐藏账号位）。"""
    out = _run_js("""
const state = {accts: [{index: 1, id: 'u1'}]};
const load = acctLoad({pid: 'dumate', nameOf: a => a.id, render: () => {}}, state);
globalThis.api = async () => ({enabled: false});
await load();
console.log(JSON.stringify({accts: state.accts}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["accts"] == []


def test_acct_move_delete_dispatch_by_pid():
    """acctMove/acctDelete 按 pid 路由：未知 pid 不炸；已登记 pid 调到对应函数。"""
    out = _run_js("""
const calls = [];
globalThis.dumateMove = (...a) => calls.push(['dumateMove', ...a]);
globalThis.dumateDelete = (...a) => calls.push(['dumateDelete', ...a]);
globalThis.agMoveAccount = (...a) => calls.push(['agMove', ...a]);
globalThis.agDeleteAccount = (...a) => calls.push(['agDel', ...a]);
acctMove('dumate', 1, -1, 'u1');
acctDelete('dumate', 'u1');
acctMove('antigravity', 2, 1, 'a@x.com');
acctDelete('antigravity', 'a@x.com');
acctMove('unknown', 1, 1);                      // 未登记：静默不炸
acctDelete('unknown', 'x');
console.log(JSON.stringify(calls));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert ["dumateMove", 1, -1, "u1"] in data
    assert ["dumateDelete", "u1"] in data
    assert ["agMove", 2, 1, "a@x.com"] in data
    assert ["agDel", "a@x.com"] in data
    assert len(data) == 4, "未知 pid 不调用任何处理（也不报错中断后续）"
