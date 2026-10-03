"""app.js 刷新机制（ensureData）的写后刷新竞态。

写操作（如 antigravity 顺位调整）后的 force 刷新若撞上「写之前发起、还在飞」
的同键请求，旧实现直接返回那发旧 promise——它的结果被世代守卫作废成 null，
本次刷新白跑，界面停留旧值直到下轮 30s 轮询。修复：等在飞落定后补一发
真正的重取。用 node 加载 app.js 的刷新机制切片（api → currentTab）做行为
断言（仿 test_antigravity_panel_ui 的 node 桩法）。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

APP_JS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src/buddy_proxy/web/static/app.js"
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑前端 JS（CI 有，本机可选）"
)

# 刷新机制切片：GET_INFLIGHT/api() → currentTab()（render/renderAlert 之后是
# DOM 重活，不切片内——render 用桩覆盖）。切片内 loadAll 引用 RECENT_QS
# 但不被调用。
SLICE_RE = re.compile(
    r"const GET_INFLIGHT = new Map\(\);.*?(?=\n// 设置文件告警条幅)", re.S
)


_STUB = """
globalThis.toast = () => {};
// 万能元素桩：切片里有顶层的 DOM wiring（rb-quick 等），摸不到真 DOM
const _EL = {
  addEventListener() {}, removeEventListener() {}, insertAdjacentHTML() {},
  classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
  textContent: '', innerHTML: '', value: '', checked: false,
  dataset: {}, style: {},
  querySelector: () => null, querySelectorAll: () => [],
};
globalThis.document = {
  getElementById: () => _EL,
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
"""


def _run_js(body: str) -> str:
    text = APP_JS.read_text(encoding="utf-8")
    m = SLICE_RE.search(text)
    assert m, "app.js 里找不到刷新机制切片（边界注释被改了？）"
    script = (
        _STUB
        + "let RECENT_QS = null;\n"  # loadAll 引用但本测试不调用
        + m.group(0)
        + """
// render() 在切片里是真函数，引用一堆 DOM 重活——桩掉，调用可观察
globalThis.__RENDERS = 0;
render = () => { globalThis.__RENDERS++; };
(async () => {
"""
        + body
        + "\n})().catch(e => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


def test_force_refresh_after_write_wins_over_inflight():
    """force 刷新撞上「写前在飞」请求：等它落定后补发真重取，读到写后状态。"""
    out = _run_js("""
const B_OLD = {providers: [], tag: 'old'};
const B_NEW = {providers: [], tag: 'new'};
let MOVED = false;
let SLOW_RESOLVE = null;
let benefitsFetches = 0;
globalThis.fetch = async (path) => {
  if (String(path).includes('benefits')) {
    benefitsFetches++;
    if (benefitsFetches === 1)   // 第一发：模拟点按钮时还在飞的轮询
      return new Promise(res => { SLOW_RESOLVE = () => res({
        ok: true, json: async () => B_OLD }); });
    return {ok: true, json: async () => (MOVED ? B_NEW : B_OLD)};
  }
  return {ok: true, json: async () => ({})};
};
// T0：轮询在飞（写前发起）
const poll = ensureData('benefits', true);
await new Promise(r => setTimeout(r, 5));
// T1：写操作落地后 force 刷新（refreshAll 的路径）——不能吃旧在飞的结果
MOVED = true;
const refresh = loadData(['benefits'], true);
SLOW_RESOLVE();          // 写前的慢请求带着旧数据返回
await poll; await refresh;
console.log(JSON.stringify({
  fetches: benefitsFetches,
  benefitsTag: BENEFITS && BENEFITS.tag,
  rendered: globalThis.__RENDERS,
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["fetches"] == 2, \
        f"force 要在在飞请求落定后补发真重取: {data}"
    assert data["benefitsTag"] == "new", \
        f"写后刷新必须读到写后状态，不能被写前旧数据顶掉: {data}"
    assert data["rendered"] >= 1, "刷新完成后要真的渲染"


def test_non_force_dedup_unchanged():
    """非 force 的在飞去重保持原样（同键并发只发一发）。"""
    out = _run_js("""
let statsFetches = 0;
globalThis.fetch = async () => {
  statsFetches++;
  return {ok: true, json: async () => ({daily: []})};
};
const a = ensureData('stats', false);
const b = ensureData('stats', false);
await Promise.all([a, b]);
console.log(JSON.stringify({fetches: statsFetches}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["fetches"] == 1, f"非 force 并发要去重成单发: {data}"
