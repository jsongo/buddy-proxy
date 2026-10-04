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

// ---- 权益到期告警横幅 ----
// 后端算好 expiring（谁快到期、还剩几天），这里只渲染不判规则——阈值与
// 「积分类才做量过滤」的逻辑都在 benefits._expiring 一处，免得两边各写一份
// 慢慢走偏（比如前端漏掉 unit 判断，就会给 mimo 的「还剩 3 天」也套 300 门槛）。
//
// 汇总文案要写「将在 N 天内到期」，这个 N 由后端随 expiring 一起下发
// （``expiry_warn_days``），前端不自己存一份——两处各写一个常量，改了一处
// 就会出现「文案写着 7 天、实际窗口 3 天」，比不写更让人困惑。
//
// 形态用 <details>：默认收起，汇总行说明有几项、最早是哪天；展开才列明细
// （通道 / 名称 / 到期日 / 剩余天数）。已过期项用红色单独标——那是最该看的。
function fmtExpireDate(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const mmdd = `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  // 不在今年的补上年份，免得「01-05」看不出是明年
  return d.getFullYear() === now.getFullYear() ? mmdd : `${d.getFullYear()}-${mmdd}`;
}
function fmtDaysLeft(days) {
  if (days < 0) return `已过期 ${Math.abs(Math.round(days))} 天`;
  if (days < 1) return '今天到期';
  return `剩 ${Math.round(days)} 天`;
}
function renderExpiryBanner() {
  const el = document.getElementById('expirybar');
  if (!el) return;
  const list = (BENEFITS && BENEFITS.expiring) || [];
  const low = (BENEFITS && BENEFITS.low_quota) || [];
  if (!list.length && !low.length) {
    el.className = 'expirybar';
    el.innerHTML = '';
    el.open = false;
    return;
  }
  const hasOver = list.some(e => e.days_left < 0);
  el.className = 'expirybar show' + (hasOver ? ' expired' : '');
  // 窗口天数以后端下发为准；老响应缺字段时退回 7（与后端默认一致）
  const warnDays = Number(BENEFITS && BENEFITS.expiry_warn_days) || 7;
  // 两种告警各说各的段，都命中就用「 · 」接上
  const expHead = list.length
    ? (hasOver ? `有 ${list.length} 项权益已过期或即将到期`
               : `有 ${list.length} 项权益将在 ${warnDays} 天内到期`)
    : '';
  const lowHead = low.length ? `${low.length} 个通道余额告急` : '';
  const head = [expHead, lowHead].filter(Boolean).join(' · ');
  const expRows = list.map(e => {
    const over = e.days_left < 0;
    // 余量只对积分类有意义（天数/次数/千分制的 remaining 是各自的量纲，
    // 铺出来只会让人困惑），故只给 credit 带余量
    const amt = e.unit === 'credit' && e.remaining != null
      ? `<span class="eb-hint">· 剩 ${fmtNum(e.remaining)}</span>` : '';
    return `<span class="eb-prov">${esc(e.provider_name || e.provider)}</span>` +
      `<span class="eb-label">${esc(e.label)}${amt}</span>` +
      `<span class="eb-date">${esc(fmtExpireDate(e.expire_ts))}</span>` +
      `<span class="eb-days${over ? ' over' : ''}">${esc(fmtDaysLeft(e.days_left))}</span>`;
  });
  // 余额告急没有日期可填（eb-date 用 — 占位）；判定与面板标题行同口径，
  // 后端算好剩余与占比，这里照搬，不自己重算。credit 通道铺绝对值
  // （「剩 263 credits」——绝对值才是它们的告警依据），其余量纲铺占比。
  const lowRows = low.map(p => {
    const isCredit = p.unit === 'credit';
    const amt = isCredit
      ? `剩 ${fmtNum(p.remaining)} credits`
      : (p.percent_left != null ? `剩 ${fmtNum(p.percent_left)}%` : '余额不足');
    const lbl = !isCredit && p.label ? ` · ${esc(p.label)}` : '';
    return `<span class="eb-prov">${esc(p.provider_name || p.provider)}</span>` +
      `<span class="eb-label">余额告急${lbl} · ${amt}</span>` +
      `<span class="eb-date">—</span>` +
      `<span class="eb-days over">${p.percent_left != null ? esc(fmtNum(p.percent_left)) + '%' : '告急'}</span>`;
  });
  const soonest = list[0];  // 后端按 expire_ts 升序，第一条就是最紧的
  const soonHint = soonest
    ? `<span class="eb-hint"> · 最近一项 ${esc(fmtExpireDate(soonest.expire_ts))}（${esc(fmtDaysLeft(soonest.days_left))}）</span>`
    : '';
  el.innerHTML =
    `<summary><span class="eb-count"><b>⏳ ${esc(head)}</b>${soonHint}</span>` +
    `<span class="eb-hint">展开明细 ▾</span></summary>` +
    `<div class="eb-list">${expRows.concat(lowRows).join('')}</div>`;
}

// ---- 打卡日历 + 自动打卡 + 各通道额度 ----
function renderBenefits() {
  if (!BENEFITS) return;
  renderExpiryBanner();
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
  // traepat/antigravity/kimi 的多账号额度挪到底部专属面板内展示，这里排除，避免重复且缩短页面
  const qps = (BENEFITS.providers || []).filter(p => p.quota.supported && p.id !== 'traepat' && p.id !== 'antigravity' && p.id !== 'kimi');
  document.getElementById('quota-list').innerHTML = qps.length ? qps.map(p => {
    const q = p.quota;
    const items = quotaItemsHtml(q.items || [], p.id);
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
  renderKimiPanel();
  syncQuotaFold();  // 样式与布局就位后按实际高度校准（各面板都已重建完）
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
    const used = total > 0 ? ` · 已用 ${Math.round((1 - rem / total) * 100)}%` : '';
    return `<span class="mono" style="margin-left:auto" title="${esc(label || '剩余 / 总额')}">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(rem)}</span>
      <span class="muted">/ ${fmtNum(total)}${used}</span>
      ${label ? `<span class="muted" style="font-size:11px"> · ${esc(label)}</span>` : ''}</span>`;
  }
  // 未声明可合计：取第一条有数的条目。此时**列表顺序就是语义**——各家自己
  // 决定先展示哪一档（ZCode 是「窗口由小到大」，5 小时档在前；Trae 是总额度
  // 在前），前端不再自作主张挑「最紧的」。
  const head = usable[0];
  if (!head) return '';
  // 「已用 N%」放在总量后面（用户 2026-10-03：参考明细行的写法）——各条
  // 量纲能否相加无所谓，这是单条自己的比例，永远成立
  const hRem = Number(head.remaining), hTot = Number(head.total);
  const hUsed = hTot > 0 ? ` · 已用 ${Math.round((1 - hRem / hTot) * 100)}%` : '';
  return `<span class="mono" style="margin-left:auto">
      <span style="color:var(--ok);font-weight:600">剩 ${fmtNum(head.remaining)}</span>
      <span class="muted">/ ${fmtNum(head.total)}${hUsed}</span></span>`;
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
  // reset_note：说明「这个时间点到底是什么」——如 antigravity 的 resetTime 只是
  // 下一个 5 小时窗口的滚动刷新点，不代表整组/每周池重置。拼进 title 而不是
  // 正文，免得窄卡更挤。
  const resetNote = typeof it.reset_note === 'string' && it.reset_note ? `（${esc(it.reset_note)}）` : '';
  const reset = fmtReset && !pending
    ? (resetNote ? `<span title="${resetNote}"> · ${fmtReset} 重置</span>`
                 : ` · ${fmtReset} 重置`)
    : '';
  // 到期时间（expire_ts）与重置时间是两回事：到期是这份权益作废、不再回来，
  // 重置是周期回满。早先 Qoder 的 expiresAt 被塞进 reset_ts，界面上显示成
  // 「10-30 重置」——用户以为到期日没被记录。这里分开渲染，措辞也分开。
  const fmtExpire = it.expire_ts
    ? new Date(it.expire_ts * 1000).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit'})
    : '';
  const expire = fmtExpire ? ` · ${fmtExpire} 到期` : '';
  // 查询失败说明条：后端给出 remaining 文案 + unreachable/query_failed 标记，
  // 用警告色区分于正常额度，并说明「是网络问题、已缓存、无需反复刷新」——
  // 以前这种情况页面只是转圈没有任何解释，用户不知道卡在哪。
  const noticeText = (!hasNums && !pending && typeof it.remaining === 'string') ? it.remaining : '';
  // 三类说明条分开配色：unreachable=红（网关不可达）、query_failed=黄（账号
  // 取不到额度，要动手查/删号）、query_empty=灰（账号是好的，只是 Free 层没
  // 分桶额度——不该当告警，否则用户会以为通道坏了）
  const notice = it.unreachable === true || it.query_failed === true || it.query_empty === true;
  const noticeColor = it.unreachable ? 'var(--err)'
    : (it.query_failed ? 'var(--warn)' : 'var(--muted)');
  const nums = hasNums
    ? (hasVolume
        ? `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> / ${fmtNum(it.total)} · 已用 ${pct}%</span>`
        : `<span class="mono"><span style="color:var(--text)">剩 ${fmtNum(rem)}</span> · 已用未知</span>`)
    : (pending
        ? `<span class="mono muted" title="standard 池无主动查询接口，用量仅在账号撞 4031 时被动采集；重置后成功请求不带账单事件，故用量待下次撞码确认">已于 ${fmtReset || '—'} 重置 · 用量待确认</span>`
        : (notice
            ? `<span class="mono" style="color:${noticeColor}">${esc(noticeText)}</span>`
            : '<span class="mono"></span>'));
  // 用量为 0 时留空条（不画那撮绿点，避免「0% 却有进度」的观感）；
  // >0 时至少给 2% 让细条可见
  const bar = hasVolume && hasBar ? `<div class="qbar"><div style="width:${pct > 0 ? Math.max(2, pct) : 0}%;background:${color}"></div></div>` : '';
  // note：后端的解释性附注（如 antigravity 的「组内包含哪些模型 / 满额含义」）。
  // 以前只挂 label 的 title，但面板是窄卡、悬停提示基本看不到——用户实际反馈
  // 「额度都是 0、看不明白」就是没看到这层解释。改为正文一行小号 muted 展示：
  // 塞进 label 会把日期和数字挤折行，单独一行则不影响标题行布局。
  const note = typeof it.note === 'string' && it.note ? String(it.note) : '';
  const noteHtml = note
    ? `<div class="muted" style="font-size:11px;line-height:1.45;margin:2px 0 0">${esc(note)}</div>`
    : '';
  return `<div class="qitem"><div class="qhead">` +
    `<span class="qlabel">${esc(it.label)}${expire}${reset}</span>` +
    `${nums}</div>${bar}${noteHtml}</div>`;
}

// ---- 额度条目分组：只铺没消耗完的 + 限高折叠 ----
//
// 为什么必须收：Trae 一次返回 28 条，其中 12 条签到奖励各自独立、且大多已
// 花光，全铺出来一屏都是「200/200 · 已用 0%」的重复行，真正还有余额的几条
// 反而被淹掉。所以默认只显示还有剩余的，已用完的收进底部那一块；条数再多，
// 整块超过最大高度就裁一刀（点按钮展开）。三处渲染（主列表 / PAT / antigravity）
// 共用这一套，免得只有 trae 变好看、别的通道还是老样子。
//
// 展开状态存在 QUOTA_FOLD 而不是 DOM 上：整页每 30s 重建一次 innerHTML，
// 状态挂 DOM 里会被下一轮刷掉——用户刚点开，30 秒后自己收回去。
const QUOTA_FOLD = { open: {} };

// 条目是否「已用完」。只认余量明确 ≤0 的（含 "0" 这类数字字符串，上游发字符串
// 是常事）。拿不到余量的一律不算：null / 说明条文案（"2/9 个账号查询失败"）/
// reset_pending 的「用量待确认」都是**未知**，不是「没花」——按用完收起来，
// 正好会把最该看见的那条藏掉。
function quotaSpent(it) {
  let r = it.remaining;
  // 与 quotaItemHtml 同一条回填规则：只见 total/used 时也能算出余量
  if (r == null && it.used != null && it.total != null) r = Number(it.total) - Number(it.used);
  if (r == null || r === '' || typeof r === 'boolean') return false;
  const n = Number(r);
  return Number.isFinite(n) && n <= 0;
}

// 一组额度条目 → HTML。key 是这组的稳定标识（通道 id / PAT #N / AG #N），
// 用来记住展开状态；同 key 在两次渲染之间保持展开。
function quotaItemsHtml(items, key) {
  if (!items.length) return '';
  // head_only：这条只为标题行「剩 X / Y」提供数字（Trae 的「总额度」——它是
  // 下面各权益包的合计，再单列一条带进度条的明细就是重复）。明细列表跳过它，
  // 标题行（quotaHeadSum）与余额告警仍照取 items[0]。
  const shown = items.filter(it => !it.head_only);
  if (!shown.length) return '';
  const act = [], spent = [];
  for (const it of shown) (quotaSpent(it) ? spent : act).push(it);
  const open = !!QUOTA_FOLD.open[key];
  const spentHtml = spent.length
    ? `<div class="qspent"${open ? '' : ' hidden'}>` +
      `<div class="qspent-head">已用完 ${spent.length} 项</div>` +
      spent.map(quotaItemHtml).join('') + '</div>'
    : '';
  // 一条都没剩时给个占位：否则收起状态下这组整个是空的，看着像加载失败
  const empty = !act.length && spent.length
    ? '<div class="empty" style="padding:10px 0">权益已全部用完</div>' : '';
  return `<div class="qbody${open ? ' open' : ''}" data-qfold="${esc(key)}">` +
      act.map(quotaItemHtml).join('') + empty + spentHtml + '</div>' +
    `<button class="qmore" data-qfold="${esc(key)}" data-act="${act.length}" ` +
      `data-spent="${spent.length}" data-total="${shown.length}" ` +
      `onclick="quotaFoldToggle(this)" hidden></button>`;
}

// 渲染后校准：按**实际高度**决定裁不裁、按钮露不露。
// 为什么不用「超过 N 条就收」：同一屏里窄卡片（两栏）和整宽卡片的可用高度
// 不同，按条数定界在窄卡片上会裁错。CSS 只管裁剪，判断在这儿做。
//
// 高度上限只写在 CSS 变量 --qfold-max 一处，这里读它——两边各写一个 208，
// 改了 CSS 忘了 JS 就会出现「已经裁了但按钮不出现」的死角。
function syncQuotaFold() {
  document.querySelectorAll('.qbody[data-qfold]').forEach(box => {
    const open = !!QUOTA_FOLD.open[box.dataset.qfold];
    // 先定已用完区块的显隐，再量高度：收起时量的正是「没消耗完的那部分」
    const spent = box.querySelector('.qspent');
    if (spent) spent.hidden = !open;
    // scrollHeight 报的是内容自然高度，不被 max-height 夹住（headless Chrome
    // 实测：12 条时 465，而 clientHeight 被夹到 208），所以不必先摘掉 clipped
    // 类再量。加不加类量到的**不完全相等**（实测差 18px：overflow:hidden 让
    // 盒子自成 BFC，首尾两条 .qitem 的 9px 外边距不再塌陷出去），所以这里
    // 只拿它跟上限比大小、不拿它当精确像素用。
    const limit = parseFloat(getComputedStyle(box).getPropertyValue('--qfold-max')) || 208;
    const clipped = !open && box.scrollHeight > limit + 1;
    box.classList.toggle('open', open);
    box.classList.toggle('clipped', clipped);
    const btn = box.nextElementSibling;
    if (!btn || !btn.classList || !btn.classList.contains('qmore')) return;
    // 展开后总是留个「收起」入口；收起时只有真被裁了、或底下还压着已用完的
    // 条目，才值得给按钮——否则是个点了没有任何变化的假按钮
    btn.hidden = !open && !clipped && !spent;
    btn.textContent = open ? '收起 ▴'
      : (!Number(btn.dataset.act) ? `展开 ${btn.dataset.spent} 项已用完 ▾`
        : `展开全部 ${btn.dataset.total} 项 ▾`);
  });
}

function quotaFoldToggle(btn) {
  const key = btn.dataset.qfold;
  QUOTA_FOLD.open[key] = !QUOTA_FOLD.open[key];
  syncQuotaFold();
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
      ${quotaItemsHtml(its, 'pat:' + grp)}
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

// ---- ANTIGRAVITY 面板（trae PAT 同款布局：每账号一块，标题=邮箱、副标题=状态，
//      下面是它自己的各组进度条）----
// 数据两路：额度走 /ui/api/benefits 里 antigravity 条目（label 带「AG #N · 」前缀，
// 组名/副标题由账号数据回填），账号状态走 /ui/api/antigravity/accounts（纯本地不触
// 网）。未登录时整个面板隐藏。
let AG_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「AG #N」
let AG_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _ag_acct_for(idx) {
  // idx=null（单账号组名无 AG #N 前缀）只在恰有一个账号时能对上
  if (!AG_ACCTS) return null;
  if (idx == null) return AG_ACCTS.length === 1 ? AG_ACCTS[0] : null;
  return AG_ACCTS.find(a => a.index === idx) || null;
}

function _ag_sub_html(a) {
  // 副标题（进度条上面那行 muted 小字）：缺 project / token 剩余 / 冷却。
  // blacklist（Google 风控拉黑）不是「冷却」语义——标注成疑似拉黑引导删除；
  // 时长 2h 以上按小时显示（6h 拉黑档读着不像「360min」）。
  if (!a) return '';
  const bits = [];
  if (!a.project_id) bits.push('缺 project');
  if (a.hours_left != null) bits.push(`token 剩 ${a.hours_left}h`);
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(c.kind === 'blacklist' ? `疑似拉黑 剩${left}`
      : `${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-ag-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function _ag_move_btns(idx, n, id) {
  // 上/下移按钮（idx 是渲染时的 1-based 顺位）；首尾各自禁用对应的那个。
  // 有账号 id 就内联带上：重排响应回来后、面板重绘前（要先重取一次额度），
  // DOM 里还挂着旧按钮，其 idx 按旧顺序标号——点按时按「快照里该 id 的实际
  // 位次」挪才不会动到别的账号（只凭 idx 挪实测会把刚调好的顺序点回去）。
  // id 字符集由后端 _ID_RE 约束（字母数字 ._@-），内联进 onclick 安全。
  // 只出裸按钮——外壳 <span class="ag-move"> 由调用方统一搭（✕ 删除同排）。
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="agMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="agMoveAccount(${idx},1${arg})">▼</button>`;
}

// 调 POST /ui/api/antigravity/accounts/order 提交完整顺序；后端重写
// priority 后 quota 缓存键（quota_epoch 带 priority）随之失效，所以这里
// 拿到响应后直接 refreshAll() 走一遍 benefits 重取，进度条组顺序即更新。
//
// id 是按钮渲染时所在账号（可选）：按钮可能比面板旧一拍（重排响应已回、
// 额度还没重取完），按快照里该 id 的**当前**位次挪，而不是按按钮上的 idx
// ——否则连点两下 = 连提交两次同方向重排（第二次拿旧 idx 在新顺序里挪到了
// 别的账号，整轮被点回去）。在飞期间 AG_MOVING 挡住后续点按。
async function agMoveAccount(idx, delta, id) {
  if (AG_MOVING) return;
  AG_MOVING = true;
  try {
    // 按钮随额度数据先到、账号状态可能还没回：按需补一次快照再算全量 id
    if (!AG_ACCTS) {
      const r0 = await api('/ui/api/antigravity/accounts');
      AG_ACCTS = r0.accounts || [];
    }
    const accts = AG_ACCTS;
    if (id) {  // 按 id 校正到快照里的真实位次（面板重绘滞后于快照时 idx 会过期）
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;  // 该账号已不在快照里（刚被删？）：宁可不动，别按过期 idx 挪错人
      idx = at + 1;
    }
    const to = idx + delta;  // idx 是 1-based 顺位，to 是目标顺位
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/antigravity/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    AG_ACCTS = r.accounts || [];
    const movedEmail = (accts.find(a => a.id === moved) || {}).email || moved;
    toast(`${movedEmail} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { AG_MOVING = false; }
}

// 删除确认弹窗：复用 index.html 的 overlay/modal 骨架（与「测试上游」同款），
// 不用系统 confirm——样式割裂、标题还是文件路径，观感差。Promise 化：
// 「删除该账号」resolve(true)；取消按钮/遮罩点击/Esc（都汇入 closeModal）
// resolve(false)。closeModal 是 app.js 的全局函数（浏览器里即 window.closeModal，
// 各关闭入口解析到的都是它），临时替换拦下全部关闭路径，关完立刻还原。
let AG_CONFIRM_OPEN = false;   // 确认流程占位：弹窗在开，或已确认、请求还没起来
// 通用删除确认弹窗（antigravity / kimi 共用）：复用 index.html 的 overlay/modal
// 骨架（与「测试上游」同款），不用系统 confirm——样式割裂、标题还是文件路径，
// 观感差。Promise 化：「删除该账号」resolve(true)；取消按钮/遮罩点击/Esc
// （都汇入 closeModal）resolve(false)。closeModal 是 app.js 的全局函数（浏览器
// 里即 window.closeModal，各关闭入口解析到的都是它），临时替换拦下全部关闭
// 路径，关完立刻还原。opts: {title, name, extra, yes}（文案各通道自带）。
function confirmAccountDelete(opts) {
  if (AG_CONFIRM_OPEN) return Promise.resolve(false);
  AG_CONFIRM_OPEN = true;
  return new Promise(resolve => {
    let done = false;
    const finish = v => {
      if (done) return;
      done = true;
      // 确认（v=true）时**不在这里清锁**：resolve 是微任务，调用方的 MOVING
      // 置位要等下一拍——这中间的窗口里再点 ✕ 会叠开第二个确认框并永远等
      // 不到表态（前端测试 test_delete_account_inflight_clicks_ignored 抓过）。
      // 锁交给确认方（各 DeleteAccount 的 finally）在请求真正收尾后清；
      // 取消（v=false）没有后续请求，立即清。
      if (!v) AG_CONFIRM_OPEN = false;
      resolve(v);
    };
    document.getElementById('modal-title').textContent = opts.title;
    document.getElementById('modal-body').innerHTML =
      `<p style="margin:0 0 6px">确定删除 <b>${esc(opts.name)}</b>？</p>` +
      `<p class="muted" style="margin:0;font-size:12px">${opts.extra}</p>`;
    setModalFoot(
      `<button onclick="closeModal()">取消</button>` +
      `<button class="danger" onclick="globalThis.__agDelYes()">${esc(opts.yes || '删除该账号')}</button>`);
    const prevClose = globalThis.closeModal;
    globalThis.closeModal = () => {
      globalThis.closeModal = prevClose;   // 先还原再走原关闭，别的弹窗不受污染
      finish(false);
      if (prevClose) prevClose();
    };
    globalThis.__agDelYes = () => { finish(true); globalThis.closeModal(); };
    document.getElementById('overlay').classList.add('show');
  });
}

function agConfirmDelete(id) {
  const a = (AG_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.email || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Antigravity 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。' +
      '被 Google 拉黑的账号（副标题标「疑似拉黑」）删掉后即不再白耗一轮 failover。',
  });
}

// 删除账号（POST /ui/api/antigravity/accounts/delete）：被 Google 拉黑
// （403 Verify your account，副标题会标「疑似拉黑」）或不再使用的账号从
// 轮换里摘掉——留着只会每轮 failover 白打一次上游。确认弹窗（agConfirmDelete）
// 后提交，与顺位调整共用 AG_MOVING 在飞锁（确认后的连点不再发第二个请求）。
async function agDeleteAccount(id) {
  if (AG_MOVING) return;
  if (!(await agConfirmDelete(id))) return;
  const a = (AG_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.email || a.id)) || id;
  AG_MOVING = true;
  try {
    const r = await api('/ui/api/antigravity/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    AG_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    AG_MOVING = false;
    AG_CONFIRM_OPEN = false;  // 确认时从 agConfirmDelete 接手的锁，到这里才放
  }
}

// 卡片级「↻ 刷新」：只作废本通道的额度缓存并重查（POST 返回新快照），其余
// 通道不打扰。antigravity / kimi 卡片共用；按钮自己转圈，完成后走 force 刷新
// 渲染（写后刷新竞态已在 ensureData 修掉，这里必然读到刚返回的新数据）。
const QREFRESH = {};  // pid -> 在飞标记：转圈期间忽略再点（整页 30s 重建 innerHTML，
                      // 按钮态挂 DOM 会被刷掉，和 QUOTA_FOLD 同理放模块级）
async function refreshProviderQuota(pid, btn) {
  if (QREFRESH[pid]) return;
  QREFRESH[pid] = true;
  const prev = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; btn.innerHTML = '⟳'; btn.classList.add('spinning'); }
  try {
    await api('/ui/api/benefits/refresh', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({provider: pid})});
    await loadData(['benefits'], true);
  } catch (e) { toast('刷新失败: ' + e.message, true); }
  finally {
    QREFRESH[pid] = false;
    if (btn) { btn.disabled = false; btn.innerHTML = prev; btn.classList.remove('spinning'); }
  }
}

function renderAntigravityPanel() {
  const panel = document.getElementById('antigravity-panel');
  if (!panel) return;
  const ag = (BENEFITS.providers || []).find(p => p.id === 'antigravity' && p.quota && p.quota.supported);
  if (!ag) { panel.innerHTML = ''; return; }

  // 按「AG #N」分组（label 形如「AG #1 · Gemini 组（…）」），与 trae 的 PAT #N 同款切法；
  // 单账号时 label 无前缀，整体落进一个组。query_failed 说明条不进分组，横贯全宽展示。
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
  // 组名=邮箱、副标题=账号状态：额度接口（benefits）不含邮箱，先挂 data-ag-idx
  // 占位；已有 AG_ACCTS 快照则同步直接渲染（30s 轮询重建面板不闪内部代号），
  // 没有就等 loadAntigravityAccounts 回填。
  // 顺位按钮的界标 n：全部「AG #N」序号的最大值（首屏首渲就有按钮，
  // 不依赖 accounts 快照时序）。
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^AG #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^AG #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _ag_acct_for(idx);
    // 顺位按钮：多账号才有意义。界标 n 直接取分组序号的最大值（自包含，
    // 不依赖 AG_ACCTS 快照的到达时序——首屏首渲就有按钮，快照只是点按时
    // 提交全量 id 列表的数据源）。正常情况所有账号都有额度分组，n=账号数；
    // 个别账号额度查询失败时它没有卡片，按「可见卡片」定界正好。
    // acct.id 内联进按钮：点按时按快照里的真实位次挪（面板重绘延迟见 agMoveAccount）。
    const moveBtns = (m && n > 1) ? _ag_move_btns(idx, n, acct && acct.id) : '';
    // 删除按钮：要账号 id 才能删，快照没到（首屏首渲）时先不渲染，等下轮。
    // id 字符集由后端 _ID_RE 约束（字母数字 ._@-），内联进 onclick 安全。
    // ghost danger：与 app.js「删除此目标」同款危险样式（悬停变红），且
    // .ag-move button.danger 常红——小尺寸 ✕ 里红色是唯一的危险提示。
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（被 Google 拉黑/不再使用时）" ` +
        `onclick="agDeleteAccount('${acct.id}')">✕</button>` : '';
    // ↻ 刷新：作废本通道额度缓存重查（quota 缓存 TTL 5 分钟，等不及就用它）
    const refreshBtn = `<button class="ghost" title="刷新本通道额度（绕过缓存重查）" ` +
      `onclick="refreshProviderQuota('antigravity', this)">↻</button>`;
    // ▲▼ 顺位 + ✕ 删除 + ↻ 刷新合成一个右上角按钮组（用户视角=「账号名那一行」的行尾）。
    // 仍放块尾、绝对定位到右上角——loadAntigravityAccounts 的就地回填靠
    // 「名字元素的下一个兄弟是副标题」定位，中间插任何元素都会让它错乱。
    // 刷新按钮无条件渲染（单账号没有 ▲▼✕ 也能刷）。
    const rowBtns = `<span class="ag-move">${refreshBtn}${moveBtns}${delBtn}</span>`;
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-ag-idx="${idx}"` : ''}>${esc(acct ? acct.email : grp)}</span>
      ${_ag_sub_html(acct)}
      ${quotaItemsHtml(its, 'ag:' + grp)}
      ${rowBtns}
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
    </div>`;
  loadAntigravityAccounts();
}

async function loadAntigravityAccounts() {
  try {
    const r = await api('/ui/api/antigravity/accounts');
    if (!r.enabled) { AG_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderAntigravityPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(AG_ACCTS)) return;
    const snapshotMissing = !AG_ACCTS || !AG_ACCTS.length;
    AG_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      // 首份快照到位（首屏刚开/刚登录）：带着快照整卡重渲——邮箱直出、
      // ▲▼/✕ 按钮带上账号 id。只靠就地回填不够：回填补不了按钮（✕ 要
      // id 才能渲），会干等下一轮 30s 重渲才出现。重渲尾部的再拉取数据
      // 已同、走上面的早退，不会循环。
      renderAntigravityPanel();
      // 重渲换了整块 DOM（邮箱、副标题都变了高度），校准要跟着走一次，
      // 否则首屏这次校准要拖到下个 30s 轮询：renderBenefits 末尾那次
      // syncQuotaFold 跑在本函数 await 之前，看不到重渲后的高度。
      syncQuotaFold();
      return;
    }
    // 就地回填：组名换成邮箱、组名后插副标题。多账号按 data-ag-idx 对应；
    // 单账号组名没有前缀（后端 label 不加），唯一 .pat-pkg-name 直接替换。
    for (const a of accts) {
      const el = (accts.length === 1)
        ? document.querySelector('#antigravity-panel .pat-pkg-name')
        : document.querySelector(`#antigravity-panel [data-ag-idx="${a.index}"]`);
      if (!el) continue;
      el.textContent = a.email;
      // 副标题总是重写：状态变了（token 走低/冷却结束消失）要跟上，不是只防重复插
      if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-ag-sub')) {
        el.nextElementSibling.remove();
      }
      const sub = _ag_sub_html(a);
      if (sub) el.insertAdjacentHTML('afterend', sub);
    }
    // 就地回填会插入/移除副标题行（高度变了），重新校准一次折叠：
    // 不补这一步，卡在阈值附近的分组会出现「已经裁掉了但按钮不露」。
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
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



// ---- KIMI 面板（antigravity 同款布局：每账号一块，标题=账号名、副标题=状态，
//      ▲▼ 顺位 / ✕ 删除 / 导入账号。组件全部复用 antigravity 的）----
// 数据两路：额度走 /ui/api/benefits 里 kimi 条目（label 带「Kimi #N · 」前缀，
// 组名/副标题由账号数据回填），账号状态走 /ui/api/kimi/accounts（纯本地不触网）。
// 与 antigravity 的差别：未登录（quota 返回静态说明条）也要渲染整卡——
// 「导入账号」入口（粘贴 kimi cli 导出的 token JSON）就长在卡头上。
let KIMI_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「Kimi #N」
let KIMI_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _kimi_acct_for(idx) {
  // idx=null（单账号组名无 Kimi #N 前缀）只在恰有一个账号时能对上
  if (!KIMI_ACCTS) return null;
  if (idx == null) return KIMI_ACCTS.length === 1 ? KIMI_ACCTS[0] : null;
  return KIMI_ACCTS.find(a => a.index === idx) || null;
}

function _kimi_sub_html(a) {
  // 副标题（进度条上面那行 muted 小字）：token 剩余 / 冷却（kimi 只有
  // quota/account 两档冷却，没有 antigravity 的拉黑档）
  if (!a) return '';
  const bits = [];
  if (a.hours_left != null) bits.push(`token 剩 ${a.hours_left}h`);
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  return bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-kimi-sub>${esc(bits.join(' · '))}</div>`
    : '';
}

function _kimi_move_btns(idx, n, id) {
  // 上/下移按钮（与 _ag_move_btns 同构；顺位语义照 antigravity：按快照里
  // 该 id 的实际位次挪，不按按钮上的 idx——面板重绘滞后时 idx 会过期）
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="kimiMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="kimiMoveAccount(${idx},1${arg})">▼</button>`;
}

function kimiConfirmDelete(id) {
  const a = (KIMI_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Kimi 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。refresh_token 已作废' +
      '（副标题 token 剩 0h 且转发持续失败）的账号删掉后即不再白耗一轮 failover。',
  });
}

// 调 POST /ui/api/kimi/accounts/order 提交完整顺序（agMoveAccount 同构）。
// 后端重写 priority 后 quota 缓存键（quota_epoch 带 priority）随之失效，
// 这里拿到响应后 refreshAll() 重取 benefits，进度条组顺序即更新。
async function kimiMoveAccount(idx, delta, id) {
  if (KIMI_MOVING) return;
  KIMI_MOVING = true;
  try {
    if (!KIMI_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      const r0 = await api('/ui/api/kimi/accounts');
      KIMI_ACCTS = r0.accounts || [];
    }
    const accts = KIMI_ACCTS;
    if (id) {  // 按 id 校正到快照里的真实位次
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;
      idx = at + 1;
    }
    const to = idx + delta;
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/kimi/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    KIMI_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).name || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { KIMI_MOVING = false; }
}

async function kimiDeleteAccount(id) {
  if (KIMI_MOVING) return;
  if (!(await kimiConfirmDelete(id))) return;
  const a = (KIMI_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.id)) || id;
  KIMI_MOVING = true;
  try {
    const r = await api('/ui/api/kimi/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    KIMI_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    KIMI_MOVING = false;
    AG_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
  }
}

// 导入账号：粘贴 kimi cli 导出的 token JSON（buddy login kimi 的等价入口，
// 适合「token 在别的机器上导出、这边只是接进来」的场景）。
function openKimiImport() {
  document.getElementById('modal-title').textContent = '导入 Kimi 账号';
  document.getElementById('modal-body').innerHTML =
    `<p style="margin:0 0 8px">粘贴 kimi cli 导出的 token JSON（<span class="mono">kimi-&lt;时间戳&gt;.json</span> 文件内容，` +
    `含 <span class="mono">access_token</span> / <span class="mono">refresh_token</span> / <span class="mono">base_url</span>）。</p>` +
    `<textarea id="kimi-import-text" class="mono" style="width:100%;height:180px;resize:vertical" ` +
    `placeholder='{"access_token": "...", "refresh_token": "...", "base_url": "https://api.kimi.com/coding", ...}'></textarea>`;
  setModalFoot('<button onclick="closeModal()">取消</button>' +
    '<button onclick="kimiImportSubmit()">导入</button>');
  document.getElementById('overlay').classList.add('show');
  const ta = document.getElementById('kimi-import-text');
  if (ta) ta.focus();
}

async function kimiImportSubmit() {
  const ta = document.getElementById('kimi-import-text');
  const text = ((ta && ta.value) || '').trim();
  if (!text) { toast('先粘贴 JSON 再导入', true); return; }
  const btns = document.querySelectorAll('#modal-foot button');
  btns.forEach(b => { b.disabled = true; });
  try {
    const r = await api('/ui/api/kimi/accounts/import', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({payload: text})});
    KIMI_ACCTS = r.accounts || [];
    closeModal();
    toast('Kimi 账号已导入');
    refreshAll();
  } catch (e) { toast('导入失败: ' + e.message, true); }
  finally { btns.forEach(b => { b.disabled = false; }); }
}

function renderKimiPanel() {
  const panel = document.getElementById('kimi-panel');
  if (!panel) return;
  const kimi = (BENEFITS.providers || []).find(p => p.id === 'kimi');
  if (!kimi) { panel.innerHTML = ''; return; }  // 通道未注册（没加 --kimi）：整块不渲染

  // 按「Kimi #N」分组（label 形如「Kimi #1 · 5 小时窗口」），antigravity 同款切法。
  // 静态说明条（未登录引导/查询失败）没有进度条语义，不进账号分组，横贯全宽。
  const groups = new Map();
  const notices = [];
  for (const it of (kimi.quota && kimi.quota.supported ? kimi.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Kimi';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  // 界标 n = 全部「Kimi #N」序号的最大值（自包含，不依赖快照到达时序）
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^Kimi #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^Kimi #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _kimi_acct_for(idx);
    const moveBtns = (m && n > 1) ? _kimi_move_btns(idx, n, acct && acct.id) : '';
    // 删除按钮要账号 id，快照没到（首屏首渲）时先不渲染，等下轮（同 antigravity）
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（refresh_token 作废/不再使用时）" ` +
        `onclick="kimiDeleteAccount('${acct.id}')">✕</button>` : '';
    // ↻ 刷新：与 antigravity 卡片同款（refreshProviderQuota 通用）
    const refreshBtn = `<button class="ghost" title="刷新本通道额度（绕过缓存重查）" ` +
      `onclick="refreshProviderQuota('kimi', this)">↻</button>`;
    const rowBtns = `<span class="ag-move">${refreshBtn}${moveBtns}${delBtn}</span>`;
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-kimi-idx="${idx}"` : ''}>${esc(acct ? (acct.name || acct.id) : grp)}</span>
      ${_kimi_sub_html(acct)}
      ${quotaItemsHtml(its, 'kimi:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Kimi</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'Kimi Code 订阅'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '429/403 自动冷却换号（按导入顺位）' : '额度耗尽自动切换下一个账号'}</span>
        <button class="ghost" title="粘贴 kimi cli 导出的 token JSON" onclick="openKimiImport()">导入账号</button>
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据</div>'}
    </div>`;
  loadKimiAccounts();
}

async function loadKimiAccounts() {
  try {
    const r = await api('/ui/api/kimi/accounts');
    if (!r.enabled) { KIMI_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderKimiPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(KIMI_ACCTS)) return;
    const snapshotMissing = !KIMI_ACCTS || !KIMI_ACCTS.length;
    KIMI_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      // 首份快照到位：带快照整卡重渲（账号名直出、▲▼/✕ 按钮带上 id），
      // 再拉取数据已同、走早退，不会循环（antigravity 同款）
      renderKimiPanel();
      syncQuotaFold();
      return;
    }
    // 就地回填：组名换成账号名、组名后插副标题（antigravity 同款）
    for (const a of accts) {
      const el = (accts.length === 1)
        ? document.querySelector('#kimi-panel .pat-pkg-name')
        : document.querySelector(`#kimi-panel [data-kimi-idx="${a.index}"]`);
      if (!el) continue;
      el.textContent = a.name || a.id;
      if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-kimi-sub')) {
        el.nextElementSibling.remove();
      }
      const sub = _kimi_sub_html(a);
      if (sub) el.insertAdjacentHTML('afterend', sub);
    }
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
