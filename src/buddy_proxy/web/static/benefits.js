// ---- 打卡「下次时间」----
// 后端给 next_ts（epoch 秒）+ next_ts_source（upstream=上游时间窗 /
// inferred=上游没给、按实测零点轮换推断）。推断的要标出来：那是我们从打卡
// 记录反推的，不是上游契约，说成准点会误导。
function fmtNextClock(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const hhmm = `${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return `今天 ${hhmm}`;
  const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
  if (d.toDateString() === tmr.toDateString()) return `明天 ${hhmm}`;
  return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${hhmm}`;
}
function fmtCountdown(ts) {
  let s = Math.floor(ts - Date.now() / 1000);
  if (s <= 0) return '即将刷新';
  if (s < 60) return `${s} 秒后`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} 分钟后`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} 小时 ${m % 60} 分后`;
  return `${Math.floor(h / 24)} 天后`;
}
function nextTimeHtml(c) {
  if (!c || c.next_ts == null) return '';
  const inferred = c.next_ts_source === 'inferred';
  // 未签到时 next_ts 是本轮**截止**（qoder 的窗口此刻过期即失效），措辞要区分：
  // 「可打卡中，X 后截止」比「下次 X」更贴合用户当下该做的事。
  const verb = (c.claimable && !c.done_today) ? '截止' : '下次';
  const tip = inferred
    ? '上游未提供每日轮换时刻，按打卡记录推断为本地零点'
    : '时刻来自上游返回的时间窗';
  // 推断值用虚线框 + 斜体表示「不确定」，替掉原先贴在「分后」后面的「≈」：
  // 那个位置读着像错字，而且它只区分了来源，没说清「哪里不确定」。
  // data-next-* 三件套保持不变（renderNextTimes 与测试都依赖）。
  return `<span class="br-next${inferred ? ' inferred' : ''}" title="${esc(tip)}" data-next-ts="${c.next_ts}">` +
    `<span class="lbl">${verb}</span>` +
    `<b class="at" data-next-clock>${esc(fmtNextClock(c.next_ts))}</b>` +
    `<span class="cd" data-next-cd>${esc(fmtCountdown(c.next_ts))}</span></span>`;
}
// 倒计时每秒走一格（纯本地计算，不打上游）；只在打卡页可见且真有元素时跑。
let NEXT_TIME_TIMER = null;
function nextTimeWanted() {
  const page = document.getElementById('page-benefits');
  return !!page && page.classList.contains('active')
    && document.visibilityState === 'visible'
    && !!document.querySelector('[data-next-ts]');
}
function renderNextTimes() {
  document.querySelectorAll('[data-next-ts]').forEach(el => {
    const ts = Number(el.dataset.nextTs);
    const cd = el.querySelector('[data-next-cd]');
    // 分隔符由 CSS 的 gap 承担，这里只写文案（原先手写 ' · ' 是给平铺 tag 用的）
    if (cd) cd.textContent = fmtCountdown(ts);
    const clock = el.querySelector('[data-next-clock]');
    // 过点后本地时钟文案也要跟上（数据要等下一轮刷新才换）
    if (clock) clock.textContent = fmtNextClock(ts);
  });
}
function syncNextTimeAuto() {
  if (nextTimeWanted()) {
    renderNextTimes();                       // 立刻算一次，别等第一秒
    if (!NEXT_TIME_TIMER) NEXT_TIME_TIMER = setInterval(renderNextTimes, 1000);
  } else if (NEXT_TIME_TIMER) {
    clearInterval(NEXT_TIME_TIMER);
    NEXT_TIME_TIMER = null;
  }
}
document.addEventListener('visibilitychange', syncNextTimeAuto);

// ---- 打卡日历 + 自动打卡 + 各通道额度 ----
function renderBenefits() {
  if (!BENEFITS) return;
  document.getElementById('auto-checkin').checked = !!BENEFITS.auto_checkin;
  document.getElementById('checkin-time').value = BENEFITS.checkin_time || '09:30';
  const enabled = BENEFITS.checkin_enabled_providers || [];
  document.getElementById('checkin-hint').textContent =
    enabled.length ? '每天自动为: ' + enabled.join(', ') : '暂无支持打卡的通道';

  const cal = document.getElementById('cal');
  const days = BENEFITS.calendar || [];
  window._calTips = {};
  const DOW = ['一', '二', '三', '四', '五', '六', '日'];
  const DOW_FULL = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  const now = new Date();
  const pad2 = n => String(n).padStart(2, '0');
  const todayStr = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
  // 起点补齐到周一，让列与星期对齐
  const first = new Date(now); first.setDate(now.getDate() - (days.length - 1 || 34));
  const padN = (first.getDay() + 6) % 7;
  const cellCount = Math.ceil((days.length + padN) / 7) * 7;
  let html = DOW.map(w => `<div class="dow">${w}</div>`).join('');
  for (let i = 0; i < cellCount; i++) {
    if (i < padN) { html += '<div class="day blank"></div>'; continue; }
    const d = days[i - padN];
    if (!d) { html += '<div class="day blank"></div>'; continue; }
    const hit = (d.providers || []).length > 0;
    html += `<div class="day${hit ? ' hit' : ''}${d.date === todayStr ? ' today' : ''}" data-date="${d.date}">${Number(d.date.slice(8))}</div>`;
  }
  cal.innerHTML = html;
  // 悬停 tooltip 数据：日期 + 星期 + 各通道当日领取
  const dowOf = ds => DOW_FULL[(new Date(ds + 'T12:00:00').getDay() + 6) % 7];
  days.forEach(d => {
    const lines = (d.providers || []).map(pid => {
      const credit = fmtCredit((d.credits || {})[pid]);
      return `<div class="t-p"><i style="background:${pcolor(pid)}"></i>${esc(pid)}${credit != null ? ` <b style="color:var(--ok)">+${esc(credit)}</b>` : ''}</div>`;
    });
    window._calTips[d.date] =
      `<div class="t-date">${d.date} ${dowOf(d.date)}${d.date === todayStr ? ' · 今天' : ''}</div>` +
      (lines.length ? lines.join('') : '<div class="t-p muted">当天未打卡</div>');
  });

  const rows = (BENEFITS.providers || []).filter(p => p.checkin.supported);
  document.getElementById('checkin-rows').innerHTML = rows.length ? rows.map(p => {
    const c = p.checkin;
    // 签到态做成带状态点的徽标；「连续 N 天 / 每日 +X」降级成旁边的小 chip，
    // 别和状态挤在同一个 tag 里（那行本来就长，挤一起更难扫）
    const st = c.error ? `<span class="br-state bad" title="${esc(c.error)}">状态未知</span>`
      : c.inactive ? '<span class="br-state">今日无签到活动</span>'
      : c.done_today ? '<span class="br-state ok">已签到</span>'
      : '<span class="br-state pending">未签到</span>';
    const chips = [];
    if (c.daily_credit > 0) chips.push(`每日 +${fmtCredit(c.daily_credit)}`);
    if (c.streak_days >= 2) chips.push(`连续 ${c.streak_days} 天`);
    if (c.activity_name) chips.push(c.activity_name);
    const meta = chips.map(t => `<span class="br-chip">${esc(t)}</span>`).join('');
    return `<div class="benefit-row">
      <div class="br-id">
        <i class="br-dot" style="background:${pcolor(p.id)}"></i>
        <span class="br-name">${esc(p.id)}</span>
      </div>
      <div class="br-meta">${st}${meta}${nextTimeHtml(c)}</div>
      <div class="br-act">
        <button class="primary" ${(c.done_today || c.inactive) ? 'disabled' : ''} onclick="claimNow('${esc(p.id)}')">立即打卡</button>
      </div>
    </div>`;
  }).join('') : '<div class="empty" style="padding:14px 0">当前没有支持打卡的通道</div>';
  // 数据换了新的一批 data-next-ts，重算一次并决定要不要起 ticker
  syncNextTimeAuto();

  // traepat 的日包/周包挪到底部 PAT 面板内展示，这里排除，避免重复且缩短页面
  // traepat/antigravity 的多账号额度挪到底部专属面板内展示，这里排除，避免重复且缩短页面
  const qps = (BENEFITS.providers || []).filter(p => p.quota.supported && p.id !== 'traepat' && p.id !== 'antigravity');
  document.getElementById('quota-list').innerHTML = qps.length ? qps.map(p => {
    const q = p.quota;
    const items = (q.items || []).map(quotaItemHtml).join('');
    const headSum = quotaHeadSum(q);
    return `<div class="chart-card" style="margin-bottom:12px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
        <span style="font-weight:600">${esc(p.name)}</span>
        ${q.level ? `<span class="tag">${esc(q.level)}</span>` : ''}
        ${headSum}
      </div>${items || '<div class="empty" style="padding:12px 0">无额度数据</div>'}</div>`;
  }).join('') : '<div class="chart-card"><div class="empty" style="padding:14px 0">当前通道均不支持额度查询</div></div>';
  renderTraepatPanel();
  renderAntigravityPanel();
}

// 单条额度条目 → HTML（周包/日包/积分窗口通用；PAT 面板与额度列表共用）
// 标题行的「剩 X / Y」汇总。
//
// 不能一律把 items 加起来——各家 items 的语义不同：
// - Qoder：订阅额度 + 加油包 + 专属积分是**并存的三份额度**，加起来才是账号
//   总量（上游自己的 totalUsagePercentage 就是这么算的）。只取第一条会显示
//   剩 1621/2000，而实际还有 100 + 1030 没算进去。
// - Trae：``总额度`` 已经是账号总量，下面的权益包只是它的**明细**（且
//   used/total 为空），加起来就是重复计算。
// - ZCode / MiMo：各条是不同口径（5 小时窗口 vs 月窗口；百分比 vs 天数），
//   量纲都不同，相加没有意义。
//
// 所以由后端用 ``sum_items: true`` 显式声明「这些条目可以合计」，前端只认
// 这个标记，不靠猜——靠猜的话，下一个新通道就会被默默算错。
function quotaHeadSum(q) {
  const items = q.items || [];
  // 只认「能当数字用」的值。两道剔除各有来由：
  // - null / '' / 布尔：``Number(null)`` 和 ``Number('')`` 都是 0，不收的话
  //   会把「没有数据」当 0 求进合计；``Number(true)`` 是 1，更不能要。
  // - 收不进数字的字符串：那是**说明条**不是额度——``trae.pat.quota.
  //   _failure_notice`` 的 remaining 发的是「2/9 个账号本轮未取到新数据」
  //   这类文案，``quotaItemHtml`` 也正是按 ``typeof === 'string'`` 认它的。
  //   不加这道判断，说明条会被当数字求和，标题行渲染成「剩 NaN / NaN」，
  //   比不显示还糟。
  // 注意**数字字符串（"2000"）要照收**：上游给 zcode 的 unit/number 就发过
  // 字符串，额度字段同样可能这么来，直接拿 Number.isFinite 会把它误杀。
  const num = v => v !== null && v !== '' && typeof v !== 'boolean'
                && Number.isFinite(Number(v));
  const usable = items.filter(it => num(it.remaining) && num(it.total));
  if (q.sum_items) {
    // 一条都没有就别显示（否则会渲染成「剩 0 / 0」）
    if (!usable.length) return '';
    const rem = usable.reduce((a, it) => a + Number(it.remaining), 0);
    const total = usable.reduce((a, it) => a + Number(it.total), 0);
    const label = usable.length > 1 ? `${usable.length} 项合计` : '';
    return `<span class="mono" style="margin-left:auto" title="${esc(label || '剩余 / 总额')}">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(rem)}</span>
      <span class="muted">/ ${fmtNum(total)}</span>
      ${label ? `<span class="muted" style="font-size:11px"> · ${esc(label)}</span>` : ''}</span>`;
  }
  // 未声明可合计：取第一条有数的条目。此时**列表顺序就是语义**——各家自己
  // 决定先展示哪一档（ZCode 是「窗口由小到大」，5 小时档在前；Trae 是总额度
  // 在前；CodeBuddy 是它自己算好的合计在前），前端不再自作主张挑「最紧的」。
  const head = usable[0];
  return head ? `<span class="mono" style="margin-left:auto">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(head.remaining)}</span>
      <span class="muted">/ ${fmtNum(head.total)}</span></span>` : '';
}

function quotaItemHtml(it) {
  let pct = it.percent;
  if (pct == null && it.used != null && it.total) pct = Math.round(it.used / it.total * 100);
  const hasBar = pct != null;
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  const color = pct >= 85 ? 'var(--err)' : pct >= 60 ? 'var(--warn)' : 'var(--ok)';
  let rem = it.remaining;
  if (rem == null && it.used != null && it.total != null) rem = it.total - it.used;
  // 只有「分子分母都拿到」才算有数：只见 total 时（例如额度网关只回包容量、
  // 还没回用量）下面的 ``percent`` 实际是 0，报出来就是「剩 300 / 300 · 已用
  // 0%」这种把未知当零的假象。有 rem 走正常文案，没 rem 就说「已用未知」。
  const hasNums = rem != null;
  const hasVolume = hasNums && it.total != null;
  const fmtReset = it.reset_ts ? new Date(it.reset_ts * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '';
  // 已过重置时刻的 standard 池：后端不再给用量（那份快照是撞码时采集的，必然接近
  // 满额，池重置后不会有新数据来覆盖），这里明确说明「待下一次撞码确认」，
  // 免得看起来像池没被重置或卡在 100%。
  const pending = !hasNums && it.reset_pending;
  const reset = fmtReset && !pending ? ` · ${fmtReset} 重置` : '';
  // 查询失败说明条：后端给出 remaining 文案 + unreachable/query_failed 标记，
  // 用警告色区分于正常额度，并说明「是网络问题、已缓存、无需反复刷新」——
  // 以前这种情况页面只是转圈没有任何解释，用户不知道卡在哪。
  const noticeText = (!hasNums && !pending && typeof it.remaining === 'string') ? it.remaining : '';
  const notice = it.unreachable === true || it.query_failed === true;
  const nums = hasNums
    ? (hasVolume
        ? `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> / ${fmtNum(it.total)} · 已用 ${pct}%</span>`
        : `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> · 已用未知</span>`)
    : (pending
        ? `<span class="mono muted" title="standard 池无主动查询接口，用量仅在账号撞 4031 时被动采集；重置后成功请求不带账单事件，故用量待下次撞码确认">已于 ${fmtReset || '—'} 重置 · 用量待确认</span>`
        : (notice
            ? `<span class="mono" style="color:${it.unreachable ? 'var(--err)' : 'var(--warn)'}">${esc(noticeText)}</span>`
            : '<span class="mono"></span>'));
  // 用量为 0 时留空条（不画那撮绿点，避免「0% 却有进度」的观感）；
  // >0 时至少给 2% 让细条可见
  const bar = hasVolume && hasBar ? `<div class="qbar"><div style="width:${pct > 0 ? Math.max(2, pct) : 0}%;background:${color}"></div></div>` : '';
  return `<div class="qitem"><div class="qhead"><span>${esc(it.label)}${reset}</span>${nums}</div>${bar}</div>`;
}

// ---- TRAE PAT 面板（底部整宽卡片）----
// 结构：上方 日包/周包 按 PAT 单双数左右两栏；下方 账号状态（左）/ 模型负载（右）左右两栏。
// 模型负载默认读缓存（GET，不触网），点「更新」再强制查询（POST）。
function renderTraepatPanel() {
  const panel = document.getElementById('traepat-panel');
  if (!panel) return;
  const pat = (BENEFITS.providers || []).find(p => p.id === 'traepat' && p.quota && p.quota.supported);
  if (!pat) { panel.innerHTML = ''; return; }

  // 日包/周包按「PAT #N」分组（label 形如「PAT #1 · 周包（通用额度）」）
  const groups = new Map();
  for (const it of pat.quota.items || []) {
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : (pat.name || 'PAT');
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const quotaHtml = [...groups.entries()].map(([grp, its]) => `
    <div class="pat-pkg">
      <div class="pat-pkg-name">${esc(grp)}</div>
      ${its.map(quotaItemHtml).join('')}
    </div>`).join('') || '<div class="empty" style="padding:12px 0">无额度数据</div>';

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Trae PAT</span>
        <span class="tag">多账号 · 自愈</span>
        <span class="grow"></span>
        <span class="pat-keeper" id="pat-keeper"></span>
      </div>
      <div class="pat-quota-grid">${quotaHtml}</div>
      <div class="pat-cols">
        <div>
          <div class="pat-col-head">
            <span>账号状态</span><span class="grow"></span>
            <button class="primary" onclick="refreshTraepatTokens(this)">补签 Token</button>
          </div>
          <div id="traepat-accounts"><div class="empty" style="padding:8px 0">账号状态加载中…</div></div>
        </div>
        <div>
          <div class="pat-col-head">
            <span>模型负载</span><span class="grow"></span>
            <button class="primary" onclick="loadTraepatStatus(this, true)">更新</button>
          </div>
          <div id="traepat-status"><div class="empty" style="padding:8px 0">加载中…</div></div>
        </div>
      </div>
    </div>`;
  loadTraepatAccounts();
  loadTraepatStatus(null, false);
  syncPatStatusAuto();  // 面板每次重建（含 30s loadAll）后按当前页是否可见起停自动刷新
}

async function loadTraepatAccounts() {
  const box = document.getElementById('traepat-accounts');
  const keeperBox = document.getElementById('pat-keeper');
  if (!box) return;
  try {
    const r = await api('/ui/api/traepat/accounts');
    if (!r.enabled) { document.getElementById('traepat-panel').innerHTML = ''; return; }
    const tag = (t) => t === 'ok' ? '<span class="tag ok">token 正常</span>'
      : t === 'expiring' ? '<span class="tag" style="color:var(--warn)">临期</span>'
      : '<span class="tag bad">无 token</span>';
    // 每账号一行：#N sa_… Pxx token正常 剩Xh（+ 冷却），逐行竖排在左栏。
    // #N 顺序与上方日包/周包的「PAT #N」分组一一对应，便于对照。
    const rows = (r.accounts || []).map((a, i) => {
      const cool = (a.cooling || []).map(c => `${c.kind} 冷却 ${c.minutes_left}min`).join(' · ');
      const left = a.hours_left != null ? `剩 ${a.hours_left}h` : '';
      return `<div class="pat-acct">
        <span class="muted" style="min-width:26px">#${i + 1}</span>
        <span class="mono">${esc(a.id)}</span>
        <span class="muted">P${a.priority}</span>
        ${tag(a.token)}${left ? `<span class="muted">${left}</span>` : ''}
        ${cool ? `<span class="muted" style="color:var(--warn)">${cool}</span>` : ''}</div>`;
    }).join('');
    box.innerHTML = rows || '<div class="empty" style="padding:8px 0">无账号</div>';
    // keeper 自愈状态 → 头部
    const k = r.keeper || {};
    const kAt = k.at ? new Date(k.at * 1000).toLocaleTimeString('zh-CN') : '—';
    const kState = k.env_ready === false ? '<span style="color:var(--warn)">换 token 端点不可达，等网络恢复</span>'
      : k.env_ready === true ? `上轮补签 ${((k.refreshed || []).length)} 个${(k.waiting || []).length ? `，待补 ${(k.waiting).length} 个` : ''}`
      : '尚未运行';
    if (keeperBox) keeperBox.innerHTML =
      `每 ${Math.round((r.keepalive_s || 0) / 60)} 分钟自愈 · ${kState}（${kAt}）`;
  } catch (e) {
    box.innerHTML = `<div class="empty" style="padding:8px 0">账号状态加载失败：${esc(e.message)}</div>`;
  }
}

async function refreshTraepatTokens(btn) {
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '补签中…'; }
  try {
    const r = await api('/ui/api/traepat/refresh-tokens', {method: 'POST'});
    const n = (r.refreshed || []).length;
    const waiting = r.waiting || [];
    if (r.env_ready === false) {
      toast(`补签端点暂不可达，${waiting.length} 个账号等待中`, true);
    } else if (waiting.length) {
      toast(`已补签 ${n} 个，仍有 ${waiting.length} 个待补`, true);
    } else {
      toast(`✓ Token 补签完成${n ? `（更新 ${n} 个）` : '（无需更新）'}`);
    }
    await loadTraepatAccounts();
  } catch (e) { toast('Token 补签失败：' + e.message, true); }
  if (btn) { btn.disabled = false; btn.textContent = old || '补签 Token'; }
}

// ---- ANTIGRAVITY 面板（trae PAT 同款布局：额度按账号左右两栏 + 账号状态一行）----
// 数据两路：额度走 /ui/api/benefits 里 antigravity 条目（label 带「AG #N · 」前缀，
// 组名渲染后替换成账号邮箱），账号状态走 /ui/api/antigravity/accounts（纯本地不触网，
// 压成一行简报）。未登录时整个面板隐藏。
function renderAntigravityPanel() {
  const panel = document.getElementById('antigravity-panel');
  if (!panel) return;
  const ag = (BENEFITS.providers || []).find(p => p.id === 'antigravity' && p.quota && p.quota.supported);
  if (!ag) { panel.innerHTML = ''; return; }

  // 按「AG #N」分组（label 形如「AG #1 · Gemini 组（…）」），与 trae 的 PAT #N 同款切法；
  // 单账号时 label 无前缀，整体落进一个组。组名最终显示为账号邮箱（下方 accounts 回填）。
  // query_failed 说明条不进分组，横贯全宽展示。
  const groups = new Map();
  const notices = [];
  for (const it of ag.quota.items || []) {
    if (it.query_failed) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Antigravity';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  // 组名先按「AG #N」渲染并挂 data-ag-idx 占位——额度接口（benefits）不含邮箱，
  // 等 loadAntigravityAccounts 拿到账号后再把组名替换成邮箱（分组与账号直接对上）。
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^AG #(\d+)$/);
    return `
    <div class="pat-pkg">
      <div class="pat-pkg-name"${m ? ` data-ag-idx="${m[1]}"` : ''}>${esc(grp)}</div>
      ${its.map(quotaItemHtml).join('')}
    </div>`;
  }).join('') || '<div class="empty" style="padding:12px 0">无额度数据</div>';
  const noticeHtml = notices.map(quotaItemHtml).join('');

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Antigravity</span>
        <span class="tag">多账号 · 自动切换</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">429/403 自动冷却换号（按登录顺位）</span>
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      <div class="pat-quota-grid">${quotaHtml}</div>
      <div class="pat-col-head" style="margin-top:10px"><span>账号状态</span></div>
      <div id="antigravity-accounts"><div class="empty" style="padding:8px 0">账号状态加载中…</div></div>
    </div>`;
  loadAntigravityAccounts();
}

async function loadAntigravityAccounts() {
  const box = document.getElementById('antigravity-accounts');
  if (!box) return;
  try {
    const r = await api('/ui/api/antigravity/accounts');
    if (!r.enabled) { box.innerHTML = '<div class="empty" style="padding:8px 0">未登录任何账号</div>'; return; }
    const accts = r.accounts || [];
    // 邮箱上牌：额度分组名 AG #N 换成对应账号邮箱。单账号时组名没有 AG #N
    // 前缀（后端 label 不加），整组只有一个名字，直接替换。
    if (accts.length === 1 && !document.querySelector('#antigravity-panel [data-ag-idx]')) {
      const name = document.querySelector('#antigravity-panel .pat-pkg-name');
      if (name) name.textContent = accts[0].email;
    } else {
      for (const a of accts) {
        const el = document.querySelector(`#antigravity-panel [data-ag-idx="${a.index}"]`);
        if (el) el.textContent = a.email;
      }
    }
    // 账号状态压成一行：邮箱已在各组名上，这里只剩每号的 token/冷却简报
    const line = accts.map((a) => {
      const bits = [];
      if (!a.project_id) bits.push('缺 project');
      if (a.hours_left != null) bits.push(`token 剩 ${a.hours_left}h`);
      for (const c of a.cooling || []) bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${c.minutes_left}min`);
      return `#${a.index} ${bits.join(' · ')}`.trim();
    }).join(' ｜ ');
    box.innerHTML = line
      ? `<div class="pat-acct"><span class="muted">${esc(line)}</span></div>`
      : '<div class="empty" style="padding:8px 0">无账号</div>';
  } catch (e) {
    box.innerHTML = `<div class="empty" style="padding:8px 0">账号状态加载失败：${esc(e.message)}</div>`;
  }
}


// force=false：读缓存（GET，不触网），页面默认展示；force=true：强制查询（POST）。
// silent=true：后台自动刷新用，不清空面板、不占用按钮（避免每轮闪烁）。
async function loadTraepatStatus(btn, force, silent) {
  const box = document.getElementById('traepat-status');
  if (!box) return;
  if (btn) { btn.disabled = true; btn.textContent = '更新中…'; }
  if (force && !silent) box.innerHTML = '<div class="empty" style="padding:8px 0">正在查询模型负载…</div>';
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = '更新'; } };
  try {
    const r = force
      ? await api('/ui/api/traepat/model-status', {method: 'POST'})
      : await api('/ui/api/traepat/model-status');
    const rows = (r.models || []).map(m => {
      const w = m.workload;
      const hasW = w != null;
      const pct = hasW ? Math.max(0, Math.min(100, Number(w))) : 0;
      const color = pct >= 80 ? 'var(--err)' : pct >= 50 ? 'var(--warn)' : 'var(--ok)';
      const label = hasW ? `${Math.round(pct)}%` : '—';
      const bar = hasW ? `<div class="qbar"><div style="width:${pct > 0 ? Math.max(2, pct) : 0}%;background:${color}"></div></div>` : '';
      const extra = [m.credits ? `积分 ${esc(m.credits)}` : '', m.max_input ? `输入上限 ${fmtNum(m.max_input)}` : ''].filter(Boolean).join(' · ');
      return `<div class="qitem"><div class="qhead"><span>${esc(m.name || m.id)}${extra ? ` <span class="muted">${extra}</span>` : ''}</span><span class="mono">${label}</span></div>${bar}</div>`;
    }).join('');
    const ts = r.fetched_at ? new Date(r.fetched_at * 1000).toLocaleTimeString('zh-CN') : '';
    const auto = PAT_STATUS_TIMER ? ' · 自动每分钟' : '';
    const hint = !r.fetched_at
      ? '尚无缓存，点「更新」查询'
      : `数值越高越繁忙 · ${ts} ${r.cached ? '缓存' : '更新'}${auto}`;
    box.innerHTML = `<div class="muted" style="font-size:12px;margin-bottom:6px">${hint}</div>` +
      (rows || '<div class="empty" style="padding:8px 0">无负载数据</div>');
  } catch (e) {
    // 后台静默刷新失败不覆盖已有数据，仅手动/首屏才提示
    if (!silent) box.innerHTML = `<div class="empty" style="padding:8px 0">查询失败：${esc(e.message)}</div>`;
  }
  restore();
}

// 模型负载自动刷新：仅当「打卡 & 额度」页可见时每 60s 强制查一次；
// 切走或标签页隐藏即停，避免后台持续打上游。
let PAT_STATUS_TIMER = null;
const PAT_STATUS_INTERVAL = 60000;
function patStatusAutoActive() {
  const page = document.getElementById('page-benefits');
  return !!page && page.classList.contains('active')
    && document.visibilityState === 'visible'
    && !!document.getElementById('traepat-status');
}
function syncPatStatusAuto() {
  if (patStatusAutoActive()) {
    if (!PAT_STATUS_TIMER) {
      PAT_STATUS_TIMER = setInterval(() => {
        if (patStatusAutoActive()) loadTraepatStatus(null, true, true);
        else syncPatStatusAuto();  // 定时器还在但已不该跑：清掉
      }, PAT_STATUS_INTERVAL);
    }
  } else if (PAT_STATUS_TIMER) {
    clearInterval(PAT_STATUS_TIMER);
    PAT_STATUS_TIMER = null;
  }
}
document.addEventListener('visibilitychange', syncPatStatusAuto);

async function claimNow(pid) {
  try {
    const r = await api('/ui/api/checkin', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: pid})});
    toast(r.ok ? `✓ ${pid} 打卡成功` : `打卡失败: ${r.message || r.error || '未知原因'}`, !r.ok);
    refreshAll();
  } catch (e) { toast('打卡失败: ' + e.message, true); }
}

async function saveCheckinSettings() {
  try {
    await api('/ui/api/settings', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        auto_checkin: document.getElementById('auto-checkin').checked,
        checkin_time: document.getElementById('checkin-time').value})});
    toast('自动打卡设置已保存');
    refreshAll();
  } catch (e) { toast('保存失败: ' + e.message, true); }
}

