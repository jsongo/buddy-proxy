"""打卡「下次时间」前端逻辑（index.html 内联 JS）的离网测试。

后端算得再对，前端渲染/定时器错了也是白搭，而这类 bug 在 Python 测试里
看不见。这里把 index.html 里那段 JS 原样抽出来，用最小 DOM 桩跑：

- **渲染**：``nextTimeHtml`` 的措辞（下次 / 截止）、「≈」只在推断值上出现、
  所有上游字段都过 ``esc``（``next_ts_source`` 是上游字符串，会进 title）
- **定时器生命周期**：只在有元素且页面可见时跑，离开即停且不重复创建

JS 由本机 ``node`` 执行；没有 node 时整文件跳过（不是失败）——CI 有 node，
本地开发也不该因为缺个可选工具就红。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

INDEX = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static/index.html"
SECTION_RE = re.compile(r'// ---- 打卡「下次时间」----(.*?)\n// ---- 打卡日历', re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)


def _extract_section() -> str:
    text = INDEX.read_text(encoding="utf-8")
    match = SECTION_RE.search(text)
    assert match, "index.html 里找不到「下次时间」那段 JS（函数被改名/挪走了？）"
    return match.group(1)


def _run_js(body: str) -> str:
    """跑一段 JS，返回 stdout。带最小 DOM 桩（只为跑纯函数/定时器逻辑）。"""
    stub = """
let INTERVALS = [], CLEARED = [];
globalThis.setInterval = (fn, ms) => { INTERVALS.push({id: INTERVALS.length + 1, ms}); return INTERVALS.length; };
globalThis.clearInterval = id => { CLEARED.push(id); };
let ELS = [];
const PAGE = { classList: { contains: () => true } };
globalThis.document = {
  addEventListener() {}, visibilityState: 'visible',
  getElementById: id => (id === 'page-benefits' ? PAGE : null),
  querySelector: sel => (sel === '[data-next-ts]' ? (ELS[0] || null) : null),
  querySelectorAll: sel => (sel === '[data-next-ts]' ? ELS : []),
};
globalThis.esc = s => String(s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
globalThis.fmtCredit = v => String(v);
globalThis.__els = ELS;
// 冻结时钟：倒计时/日期措辞的断言必须确定性。不冻的话两次 Date.now()
// 之间过去几毫秒，``now+60`` 就会算成 59 秒，测试随机器负载飘。
globalThis.__freeze = ms => {
  const RealDate = Date;
  globalThis.Date = class extends RealDate {
    constructor(...a) { a.length === 0 ? super(ms) : super(...a); }
    static now() { return ms; }
  };
};
"""
    script = stub + _extract_section() + "\n" + body
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


# --- 渲染 -------------------------------------------------------------------


def test_renders_both_next_and_deadline_wording():
    """已领取说「下次」，还没领说「截止」——此刻该做的事不同。"""
    out = _run_js("""
const now = Math.floor(Date.now() / 1000);
console.log(JSON.stringify({
  claimed: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'upstream',
                         checked_in: true, claimable: false, done_today: true}),
  pending: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'upstream',
                         claimable: true, done_today: false}),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "下次" in data["claimed"], data["claimed"]
    assert "截止" in data["pending"], data["pending"]


def test_inferred_source_is_marked_but_upstream_is_not():
    """推断值必须加「≈」——那是我们从打卡记录反推的，不是上游契约。"""
    out = _run_js("""
const now = Math.floor(Date.now() / 1000);
console.log(JSON.stringify({
  inferred: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'inferred', done_today: true}),
  upstream: nextTimeHtml({next_ts: now + 3600, next_ts_source: 'upstream', done_today: true}),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "≈" in data["inferred"], "推断值必须标出来"
    assert "≈" not in data["upstream"], "上游给的不该标推测"
    assert "本地零点" in data["inferred"], "推断值的 tooltip 要说明依据"


def test_missing_next_ts_renders_nothing():
    """算不出下次（活动结束/档期未开）→ 不渲染任何东西，别给个空壳 tag。"""
    out = _run_js("""
console.log(JSON.stringify({
  none: nextTimeHtml({checked_in: true, done_today: true}),
  nullish: nextTimeHtml({next_ts: null, done_today: true}),
  empty: nextTimeHtml(null),
}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["none"] == ""
    assert data["nullish"] == ""
    assert data["empty"] == ""


def test_upstream_source_never_reaches_the_dom():
    """``next_ts_source`` 只被当**判据**用，绝不整串拼进 HTML。

    曾经的写法容易是 ``title="${esc(c.next_ts_source)}"``——那样一旦上游给个
    奇怪的值就多一处注入面。这里断言它根本没进输出：无论传什么，
    ``title`` 都只能是代码里写死的那两句。
    """
    out = _run_js("""
console.log(nextTimeHtml({next_ts: 1, next_ts_source: '"><img src=x onerror=alert(1)>',
                          done_today: true}));
""")
    assert "onerror" not in out and "<img" not in out, f"上游字符串漏进 DOM: {out}"
    assert 'title="时刻来自上游返回的时间窗"' in out, f"未知 source 应退回上游措辞: {out}"

    inferred = _run_js("""
console.log(nextTimeHtml({next_ts: 1, next_ts_source: 'inferred', done_today: true}));
""")
    assert 'title="上游未提供每日轮换时刻，按打卡记录推断为本地零点"' in inferred


def test_countdown_boundaries_are_sane():
    """倒计时在边界上不能出现「0 分钟后」或负数这种读起来像坏了的文案。"""
    out = _run_js("""
__freeze(1_800_000_000_000);                  // 冻住时钟，边界才可比
const now = Math.floor(Date.now() / 1000);
const r = {};
for (const d of [-100, 0, 1, 59, 60, 3600, 86400]) r[d] = fmtCountdown(now + d);
console.log(JSON.stringify(r));
""")
    data = json.loads(out.strip().splitlines()[-1])
    for key, text in data.items():
        assert not text.startswith("-"), f"{key} -> {text}（负数文案）"
        assert "0 分钟后" not in text, f"{key} -> {text}（应进位成「秒后」）"
        assert "0 秒后" not in text, f"{key} -> {text}（应说「即将刷新」）"
    assert data["-100"] == "即将刷新"
    assert data["0"] == "即将刷新"
    assert data["1"] == "1 秒后"            # 边界是 s<=0，还剩 1 秒就照实说
    assert data["59"] == "59 秒后"
    assert data["60"] == "1 分钟后"
    assert data["3600"] == "1 小时 0 分后"
    assert data["86400"] == "1 天后"


# --- 定时器生命周期 ---------------------------------------------------------


def test_timer_starts_once_and_stops_when_no_elements():
    """有元素才起、不重复创建；元素没了就停（不空转）。"""
    out = _run_js("""
ELS.push({dataset: {nextTs: String(Math.floor(Date.now()/1000) + 3600)},
          querySelector: () => null});
syncNextTimeAuto(); syncNextTimeAuto(); syncNextTimeAuto();
const started = INTERVALS.length, ms = INTERVALS[0] && INTERVALS[0].ms;
ELS.length = 0;
syncNextTimeAuto();
console.log(JSON.stringify({started, ms, cleared: CLEARED.length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["started"] == 1, "重复调用不该创建多个定时器"
    assert data["ms"] == 1000, "倒计时要每秒走一格"
    assert data["cleared"] == 1, "元素消失后必须停掉"


def test_timer_stops_when_page_hidden():
    """标签页切走即停——后台每秒钟重排 DOM 纯属浪费。"""
    out = _run_js("""
ELS.push({dataset: {nextTs: '1'}, querySelector: () => null});
syncNextTimeAuto();
document.visibilityState = 'hidden';
syncNextTimeAuto();
console.log(JSON.stringify({started: INTERVALS.length, cleared: CLEARED.length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["started"] == 1
    assert data["cleared"] == 1, "页面隐藏后必须停掉"


def test_timer_restarts_after_returning():
    """切回来要能重新起（停掉后不留「已死」的全局状态）。"""
    out = _run_js("""
ELS.push({dataset: {nextTs: '1'}, querySelector: () => null});
syncNextTimeAuto();
ELS.length = 0; syncNextTimeAuto();          // 停
ELS.push({dataset: {nextTs: '1'}, querySelector: () => null});
syncNextTimeAuto();                          // 再起
console.log(JSON.stringify({total: INTERVALS.length, cleared: CLEARED.length}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["total"] == 2, "回来后应重新创建定时器"
    assert data["cleared"] == 1


def test_render_updates_text_without_touching_dataset():
    """tick 只改文案，不改 ``data-next-ts``——否则倒计时会自己把自己算歪。"""
    out = _run_js("""
const ts = Math.floor(Date.now()/1000) + 3600;
const cd = {textContent: ''}, clock = {textContent: ''};
ELS.push({dataset: {nextTs: String(ts)},
          querySelector: sel => sel === '[data-next-cd]' ? cd : clock});
renderNextTimes();
console.log(JSON.stringify({tsAfter: ELS[0].dataset.nextTs, cd: cd.textContent,
                            clock: clock.textContent, orig: String(ts)}));
""")
    data = json.loads(out.strip().splitlines()[-1])
    assert data["tsAfter"] == data["orig"], "不该改写 data-next-ts"
    assert data["cd"].startswith(" · "), f"倒计时前缀不对: {data['cd']}"
    assert data["clock"], "时钟文案要被刷新"
