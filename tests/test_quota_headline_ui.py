"""额度卡标题行「剩 X / Y」汇总的前端测试。

Qoder 的额度面板有三份**并存**的额度（订阅额度 / 加油包 / 专属积分），加起来
才是账号总量，但标题行原先一律取 ``items[0]``，于是只显示 1621/2000，把另
外两份（100 + 1030）漏掉了——用户看到的数比实际少一截。

修法是后端用 ``sum_items`` 显式声明「这些条目可合计」，前端只认这个标记：
各家 items 语义不同（Trae 的权益包是总额度的明细，加了就重复计算；ZCode /
MiMo 各项量纲不同），一律相加会把它们全算错，所以必须由数据源声明而不是猜。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

INDEX = pathlib.Path(__file__).resolve().parents[1] / "src/buddy_proxy/web/static/index.html"
FUNC_RE = re.compile(r"function quotaHeadSum\(q\) \{(.*?)\n\}\n", re.S)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="需要 node 跑内联 JS（CI 有，本机可选）"
)


def _run_js(body: str) -> str:
    """抽出 quotaHeadSum 跑一遍（带最小 DOM 无关的桩）。"""
    text = INDEX.read_text(encoding="utf-8")
    match = FUNC_RE.search(text)
    assert match, "index.html 里找不到 quotaHeadSum（被改名/挪走了？）"
    stub = """
globalThis.esc = s => String(s);
globalThis.fmtNum = n => (Math.round(n * 100) / 100).toLocaleString('en-US');
"""
    proc = subprocess.run(
        ["node", "-e", stub + match.group(0) + "\n" + body],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"node 执行失败:\n{proc.stderr}"
    return proc.stdout


QODER = """{
  sum_items: true,
  items: [
    {label: '订阅额度', remaining: 1621, total: 2000},
    {label: '加油包', remaining: 100, total: 400},
    {label: 'Qwen 专属积分', remaining: 1030, total: 2000},
  ],
}"""


def test_sum_items_adds_every_bucket_up():
    """置了 sum_items 就要把三份额度**全加起来**，这才是账号真实剩余。

    数字取自真实返回值（2026-09-29 抓取）：三份合计 2751/4400，已用 37.48%；
    上游自己的 ``totalUsagePercentage`` 是 0.38，正好对得上——这是「相加才
    是正确口径」的独立佐证。
    """
    out = _run_js(f"console.log(quotaHeadSum({QODER}));")
    assert "2,751" in out, f"应显示三份合计 2751，实际: {out}"
    assert "4,400" in out, f"应显示合计总额 4400，实际: {out}"
    # 旧 bug 的特征值：只显示第一份
    assert "1,621" not in out, f"不该只显示订阅额度那一份: {out}"
    assert "3 项合计" in out, "多于一项时要说清这是几项的合计"


def test_without_the_flag_it_keeps_the_old_single_item_behaviour():
    """没声明 sum_items 的通道必须维持旧行为（取第一条）。

    否则就会把 Trae 这类「总额度 + 权益包明细」的通道算重复。这条是防回归的
    关键：修复不能变成「所有通道都相加」。
    """
    out = _run_js(f"""console.log(quotaHeadSum({{
      items: [{{label: '总额度', remaining: 500, total: 1000}},
              {{label: '权益包', remaining: 300, total: 300}}],
    }}));""")
    assert "500" in out and "1,000" in out, f"应取第一条: {out}"
    assert "800" not in out, f"不该把权益包加进来（那是明细，会重复计算）: {out}"
    assert "合计" not in out, "单条不该标「合计」"


def test_single_item_sum_does_not_say_jihe():
    """只有一条时不必标「1 项合计」——读着啰嗦。"""
    out = _run_js("console.log(quotaHeadSum({sum_items: true, items: [{remaining: 5, total: 9}]}));")
    assert "5" in out and "9" in out
    assert "合计" not in out, f"单项不用标合计: {out}"


def test_items_without_numbers_are_skipped_and_empty_renders_nothing():
    """没有 remaining/total 的条目不参与合计；一条可用都没有就别渲染。

    漏掉「全不可用」这步的话会渲染成「剩 0 / 0」，看着像额度真的用光了。
    """
    out = _run_js("""console.log(JSON.stringify({
      partial: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: 10, total: 20},
        {label: '查询失败', remaining: '网络不可达'},      // 文案，不是数
        {label: 'B', remaining: 5, total: 10},
      ]}),
      bridge: quotaHeadSum({sum_items: true, items: [
        {label: '只给 total', total: 300},
      ]}),
      none: quotaHeadSum({sum_items: true, items: []}),
      noitems: quotaHeadSum({sum_items: true}),
    }));""")
    data = json.loads(out.strip().splitlines()[-1])
    assert "15" in data["partial"] and "30" in data["partial"], data["partial"]
    assert "网络不可达" not in data["partial"]
    assert data["bridge"] == "", f"只有 total 不算有数: {data['bridge']}"
    assert data["none"] == "", "空列表不该渲染"
    assert data["noitems"] == "", "没有 items 字段也不该炸"


def test_failure_notices_never_get_summed_into_the_headline():
    """「查询失败 / 网关不可达」说明条不能被当数字求和。

    ``trae.pat.quota._failure_notice`` 把说明条插在 items 首位，``remaining``
    是**文案**（「2/9 个账号本轮未取到新数据」）而不是数。只要过滤条件写成
    ``remaining != null``，文案就会过筛，``Number(...)`` 得 NaN，标题行渲染成
    「剩 NaN / NaN」——比不显示还糟。

    同时要**保住数字字符串**（``"2000"``）：上游发 zcode 的 unit/number 就
    用过字符串，额度字段也可能这么来；用 ``Number.isFinite`` 直接筛会把这种
    能算的值误杀。
    """
    out = _run_js("""console.log(JSON.stringify({
      notice: quotaHeadSum({sum_items: true, items: [
        {label: 'PAT 额度网关不可达', remaining: '2/9 个账号本轮未取到新数据（网络不可达）',
         total: null, unreachable: true, query_failed: true},
        {label: '账号 A', remaining: 100, total: 200}]}),
      allnotice: quotaHeadSum({sum_items: true, items: [
        {label: '查询失败', remaining: '9/9 个账号查询失败', total: null}]}),
      numeric_string: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: '2000', total: '4000'}]}),
      bools: quotaHeadSum({sum_items: true, items: [
        {label: 'A', remaining: true, total: true}]}),
    }));""")
    data = json.loads(out.strip().splitlines()[-1])

    assert "NaN" not in data["notice"], f"说明条被算进去了: {data['notice']}"
    assert "100" in data["notice"] and "200" in data["notice"], data["notice"]
    assert "个账号" not in data["notice"], "文案不该出现在标题行"
    assert data["allnotice"] == "", f"全是说明条就该不显示: {data['allnotice']}"
    assert "2,000" in data["numeric_string"] and "4,000" in data["numeric_string"], \
        f"数字字符串是可算的，不该被滤掉: {data['numeric_string']}"
    assert data["bools"] == "", f"true/false 不是额度: {data['bools']}"


def test_the_render_path_uses_the_helper_not_a_bare_find():
    """渲染处必须调 ``quotaHeadSum``，不能再自己 ``find`` 第一条。

    这条盯的是「改了个函数但没接上去」——helper 写得再对，渲染处若仍留着
    ``.find(it => ...)``，界面就还是老样子（这个 bug 的本质就是渲染处的取值）。
    """
    text = INDEX.read_text(encoding="utf-8")
    assert "quotaHeadSum(q)" in text, "渲染处没有调用 quotaHeadSum"
    # 只扫渲染那段（quota-list 的赋值处），不能全文件扫：helper 自己内部
    # 就写着 ``const head = usable[0]``（未声明可合计时的兜底），全局扫会误报
    render = text[text.index("getElementById('quota-list')"):]
    render = render[:render.index("renderTraepatPanel")]
    assert "quotaHeadSum(q)" in render, "渲染处没有调用 quotaHeadSum"
    assert ".find(it => it.remaining" not in render, \
        "渲染处仍留着直接 find 第一条的写法（helper 没接上）"
