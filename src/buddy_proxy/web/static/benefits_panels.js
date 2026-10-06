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
  // 副标题（进度条上面那行 muted 小字）：缺 project / 冷却。
  // blacklist（Google 风控拉黑）不是「冷却」语义——标注成疑似拉黑引导删除；
  // 时长 2h 以上按小时显示（6h 拉黑档读着不像「360min」）。
  // 「token 剩 Xh」删掉了：access token 几小时自动刷新，读了只会误导
  //（qoder 那边还曾把毫秒当秒算出 4.97 亿小时）——用户 2026-10-05 确认三通道一并删。
  if (!a) return '';
  const bits = [];
  if (!a.project_id) bits.push('缺 project');
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

// 删除确认弹窗已抽到 benefits_accounts.js 的 confirmAccountDelete（公共件）。
// 曾在本文件里还有一份同名旧定义（锁是 AG_CONFIRM_OPEN、按钮挂 __agDelYes），
// 经典脚本后加载覆盖前加载，浏览器里实际生效的是它——而 dumate 的 finally 清的是
// 公共件的 ACCT_CONFIRM_OPEN，确认过一次 DuMate ✕ 后 AG_CONFIRM_OPEN 永久卡死、
// 后续所有通道删除确认被当成「取消」。删除旧定义、全链统一走公共件的锁。
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
    ACCT_CONFIRM_OPEN = false;  // 确认时从 confirmAccountDelete 接手的锁，到这里才放
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
    // ✎ 改名：同样要 id（同 ✕ 快照没到先不渲）。放按钮组最前=标题行行尾，
    // 别插在名字和副标题之间——就地回填靠「名字元素的下一个兄弟是副标题」定位。
    const renameBtn = acctRenameButton('antigravity', acct);
    // ↻ 刷新：作废本通道额度缓存重查（quota 缓存 TTL 5 分钟，等不及就用它）
    const refreshBtn = `<button class="ghost" title="刷新本通道额度（绕过缓存重查）" ` +
      `onclick="refreshProviderQuota('antigravity', this)">↻</button>`;
    // ✎ ▲▼ ✕ ↻ 合成一个右上角按钮组（用户视角=「账号名那一行」的行尾）。
    // 仍放块尾、绝对定位到右上角——loadAntigravityAccounts 的就地回填靠
    // 「名字元素的下一个兄弟是副标题」定位，中间插任何元素都会让它错乱。
    // 刷新按钮无条件渲染（单账号没有 ▲▼✕ 也能刷）。
    const rowBtns = `<span class="ag-move">${renameBtn}${refreshBtn}${moveBtns}${delBtn}</span>`;
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-ag-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.email) : grp)}</span>
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
      el.textContent = a.alias || a.email;
      // 副标题总是重写：状态变了（冷却结束消失）要跟上，不是只防重复插
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


// ---- QODER 面板（kimi 同款布局：每账号一块，标题=账号邮箱、副标题=状态，
//      ▲▼ 顺位 / ✕ 删除。组件全部复用 kimi/antigravity 的）----
// 数据两路：额度走 /ui/api/benefits 里 qoder 条目（label 带「Qoder #N · 」前缀，
// 组名/副标题由账号数据回填），账号状态走 /ui/api/qoder/accounts（纯本地不触网）。
let QODER_ACCTS = null;  // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「Qoder #N」
let QODER_MOVING = false;  // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _qoder_acct_for(idx) {
  // idx=null（单账号组名无 Qoder #N 前缀）只在恰有一个账号时能对上
  if (!QODER_ACCTS) return null;
  if (idx == null) return QODER_ACCTS.length === 1 ? QODER_ACCTS[0] : null;
  return QODER_ACCTS.find(a => a.index === idx) || null;
}

function _qoder_sub_html(a) {
  // 副标题（进度条上面那行 muted 小字）：区域 / 冷却
  // （「token 剩 Xh」删了：access token 几小时自动刷新，读了只会误导）
  if (!a) return '';
  const bits = [];
  if (a.region) bits.push(a.region === 'cn' ? 'CN 区' : 'Global 区');
  for (const c of a.cooling || []) {
    const left = c.minutes_left >= 120 ? (c.minutes_left / 60).toFixed(1) + 'h' : c.minutes_left + 'min';
    bits.push(`${c.kind === 'quota' ? '额度' : '账号'}冷却 ${left}`);
  }
  const sub = bits.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px" data-qoder-sub>${esc(bits.join(' · '))}</div>`
    : '';
  // 模型受限小字（覆盖表反查的 models_limited）：转发会自动跳过受限模型，
  // 列出名字让人知道这个账号还能用哪些，不用等调失败才发现
  const lim = a.models_limited || [];
  const limHtml = lim.length
    ? `<div class="muted" style="font-size:11px;margin:1px 0 6px">模型受限（仅 Qwen3.8 两档可用；其余 ${lim.length} 个：${esc(lim.join('、'))}）</div>`
    : '';
  return sub + limHtml;
}

function _qoder_move_btns(idx, n, id) {
  // 上/下移按钮（与 _kimi_move_btns 同构；顺位语义按快照里该 id 的实际位次挪）
  const arg = id ? `,'${id}'` : '';
  return `<button class="ghost" title="上移（更优先使用）" ${idx <= 1 ? 'disabled' : ''} ` +
    `onclick="qoderMoveAccount(${idx},-1${arg})">▲</button>` +
    `<button class="ghost" title="下移" ${idx >= n ? 'disabled' : ''} ` +
    `onclick="qoderMoveAccount(${idx},1${arg})">▼</button>`;
}

function qoderConfirmDelete(id) {
  const a = (QODER_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.email || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Qoder 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。不再使用或凭据失效' +
      '（转发持续 401/403）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function qoderMoveAccount(idx, delta, id) {
  if (QODER_MOVING) return;
  QODER_MOVING = true;
  try {
    if (!QODER_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      const r0 = await api('/ui/api/qoder/accounts');
      QODER_ACCTS = r0.accounts || [];
    }
    const accts = QODER_ACCTS;
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
    const r = await api('/ui/api/qoder/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    QODER_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).name || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { QODER_MOVING = false; }
}

async function qoderDeleteAccount(id) {
  if (QODER_MOVING) return;
  if (!(await qoderConfirmDelete(id))) return;
  const a = (QODER_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.name || a.email || a.id)) || id;
  QODER_MOVING = true;
  try {
    const r = await api('/ui/api/qoder/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    QODER_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    QODER_MOVING = false;
    ACCT_CONFIRM_OPEN = false;
  }
}

function renderQoderPanel() {
  const panel = document.getElementById('qoder-panel');
  if (!panel) return;
  const qoder = (BENEFITS.providers || []).find(p => p.id === 'qoder');
  if (!qoder) { panel.innerHTML = ''; return; }  // 通道未注册（没加 --qoder）：整块不渲染

  // 按「Qoder #N」分组（label 形如「Qoder #1 · 订阅额度」），kimi 同款切法。
  const groups = new Map();
  const notices = [];
  for (const it of (qoder.quota && qoder.quota.supported ? qoder.quota.items : []) || []) {
    if (it.query_failed || (it.percent == null && it.used == null)) { notices.push(it); continue; }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Qoder';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^Qoder #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^Qoder #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _qoder_acct_for(idx);
    const moveBtns = (m && n > 1) ? _qoder_move_btns(idx, n, acct && acct.id) : '';
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（不再使用/凭据失效时）" ` +
        `onclick="qoderDeleteAccount('${acct.id}')">✕</button>` : '';
    const refreshBtn = `<button class="ghost" title="刷新本通道额度（绕过缓存重查）" ` +
      `onclick="refreshProviderQuota('qoder', this)">↻</button>`;
    const renameBtn = acctRenameButton('qoder', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${refreshBtn}${moveBtns}${delBtn}</span>`;
    // 合计行：qoder 的订阅额度/加油包/专属积分是并存的份额，加起来才是账号
    // 总量（后端 sum_items 声明可合计）。quotaHeadSum 原本服务「列表头部」，
    // 借用它产出同款「剩 X / Y」chip，放进账号卡标题行下。
    const headSum = quotaHeadSum({items: its, sum_items: true});
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-qoder-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.name || acct.email || acct.id) : grp)}</span>
      ${_qoder_sub_html(acct)}
      ${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}
      ${quotaItemsHtml(its, 'qoder:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Qoder</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'Qoder 订阅'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '403/额度尽自动冷却换号（按登录顺位）' : '403/额度尽自动切换下一个账号'}</span>
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据（未登录：跑 <span class="mono">buddy login qoder</span>）</div>'}
    </div>`;
  loadQoderAccounts();
}

async function loadQoderAccounts() {
  try {
    const r = await api('/ui/api/qoder/accounts');
    if (!r.enabled) { QODER_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderQoderPanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(QODER_ACCTS)) return;
    const snapshotMissing = !QODER_ACCTS || !QODER_ACCTS.length;
    QODER_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      renderQoderPanel();
      syncQuotaFold();
      return;
    }
    // 就地回填：组名换成账号名、组名后插副标题（kimi 同款）
    for (const a of accts) {
      const el = (accts.length === 1)
        ? document.querySelector('#qoder-panel .pat-pkg-name')
        : document.querySelector(`#qoder-panel [data-qoder-idx="${a.index}"]`);
      if (!el) continue;
      el.textContent = a.alias || a.name || a.email || a.id;
      if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-qoder-sub')) {
        el.nextElementSibling.remove();
      }
      const sub = _qoder_sub_html(a);
      if (sub) el.insertAdjacentHTML('afterend', sub);
    }
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}


// ---- TRAE WORK 面板（qoder 同款布局：每账号一块，标题=账号名、副标题=区域/冷却，
//      ▲▼ 顺位 / ✕ 删除 / ✎ 改名 / ↻ 刷新）----
// 数据两路：额度走 /ui/api/benefits 里 trae 条目（label 带「Trae #N · 」前缀），
// 账号状态走 /ui/api/trae/accounts（纯本地不触网，failover.accounts_status）。
// trae 与 qoder 的结构差异：总额度条是 head_only（各权益包的合计），明细列表
// **不能相加**（权益包是总额度的拆分，sum 会重复计算）——合计行改用 head_only
// 那条自己的数字（quotaHeadSum 非 sum_items 分支正好「取第一条有数的」）。
// 签到仍在上方「各通道签到」卡里（supports_checkin=True），本面板只管额度。
let TRAE_ACCTS = null;   // 最近一次 accounts 快照；render 先用它，避免每 30s 闪回「Trae #N」
let TRAE_MOVING = false; // 面板账号操作（重排/删除）在途：期间忽略新的点按

function _trae_acct_for(idx) {
  // idx=null（单账号组名无 Trae #N 前缀）只在恰有一个账号时能对上
  if (!TRAE_ACCTS) return null;
  if (idx == null) return TRAE_ACCTS.length === 1 ? TRAE_ACCTS[0] : null;
  return TRAE_ACCTS.find(a => a.index === idx) || null;
}

function _trae_sub_html(a) {
  // 副标题：区域 / 冷却（acctSubHtml 公共件；trae 无通道特有 bit）
  return acctSubHtml(a, null, 'data-trae-sub');
}

function _trae_move_btns(idx, n, id) {
  return acctMoveButtons('trae', idx, n, id);
}

function traeConfirmDelete(id) {
  const a = (TRAE_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.nickname || a.id)) || id;
  return confirmAccountDelete({
    title: '删除 Trae 账号',
    name,
    extra: '该账号的凭据文件一并移除，转发不再使用它。不再使用或凭据失效' +
      '（转发持续 401）的账号删掉后即不再白耗一轮 failover。',
  });
}

async function traeMoveAccount(idx, delta, id) {
  if (TRAE_MOVING) return;
  TRAE_MOVING = true;
  try {
    if (!TRAE_ACCTS) {  // 按钮随额度数据先到、账号状态可能还没回：补一次快照
      const r0 = await api('/ui/api/trae/accounts');
      TRAE_ACCTS = r0.accounts || [];
    }
    const accts = TRAE_ACCTS;
    if (id) {  // 按 id 校正到快照里的真实位次（面板重绘滞后时 idx 会过期）
      const at = accts.findIndex(a => a.id === id);
      if (at < 0) return;
      idx = at + 1;
    }
    const to = idx + delta;
    if (to < 1 || to > accts.length) return;
    const ids = accts.map(a => a.id);
    const [moved] = ids.splice(idx - 1, 1);
    ids.splice(to - 1, 0, moved);
    const r = await api('/ui/api/trae/accounts/order', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids})});
    TRAE_ACCTS = r.accounts || [];
    const movedName = (accts.find(a => a.id === moved) || {}).nickname || moved;
    toast(`${movedName} 已移到顺位 #${to}`);
    refreshAll();
  } catch (e) { toast('调整失败: ' + e.message, true); }
  finally { TRAE_MOVING = false; }
}

async function traeDeleteAccount(id) {
  if (TRAE_MOVING) return;
  if (!(await traeConfirmDelete(id))) return;
  const a = (TRAE_ACCTS || []).find(x => x.id === id);
  const name = (a && (a.alias || a.nickname || a.id)) || id;
  TRAE_MOVING = true;
  try {
    const r = await api('/ui/api/trae/accounts/delete', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id})});
    TRAE_ACCTS = r.accounts || [];
    toast(`${name} 已删除`);
    refreshAll();
  } catch (e) { toast('删除失败: ' + e.message, true); }
  finally {
    TRAE_MOVING = false;
    ACCT_CONFIRM_OPEN = false;
  }
}

function renderTraePanel() {
  const panel = document.getElementById('trae-panel');
  if (!panel) return;
  const trae = (BENEFITS.providers || []).find(p => p.id === 'trae');
  // 通道未注册（没加 --trae）或海外的 traeintl（独立 id）：整块不渲染
  if (!trae) { panel.innerHTML = ''; return; }

  // 按「Trae #N」分组（label 形如「Trae #1 · 总额度」）。qoder 同款切法，
  // 两点差异：
  // - head_only 条（「总额度」= 各权益包合计）不进明细列表，但**要**用作
  //   合计行数字——quotaItemsHtml 跳过它、quotaHeadSum 非 sum_items 分支取
  //   「第一条有数的」正好是它（items[0]，后端按总额度在前排列）。
  // - 无数字条目分两种：带组前缀的（如「免费」包，只有到期日没有额度数字）
  //   归进各账号子卡片当一行明细（横贯全宽反而像独立告警——用户 2026-10-06
  //   反馈）；连前缀都没有的孤条（未登录说明等）才进 notices 横贯全宽。
  const groups = new Map();
  const notices = [];
  for (const it of (trae.quota && trae.quota.supported ? trae.quota.items : []) || []) {
    if (it.query_failed ||
        (it.percent == null && it.used == null && it.label.indexOf(' · ') < 0)) {
      notices.push(it); continue;
    }
    if (it.head_only) {
      // head_only 条挂到「它前缀对应的组」的合计位；组还没出现就先攒着，
      // 循环结束后按 grp 补进去（后端顺序保证它在该组条目最前）
      const cut = it.label.indexOf(' · ');
      const grp = cut >= 0 ? it.label.slice(0, cut) : 'Trae';
      if (!groups.has(grp)) groups.set(grp, []);
      groups.get(grp).unshift(Object.assign({}, it, {
        label: cut >= 0 ? it.label.slice(cut + 3) : it.label}));
      continue;
    }
    const idx = it.label.indexOf(' · ');
    const grp = idx >= 0 ? it.label.slice(0, idx) : 'Trae';
    const sub = idx >= 0 ? it.label.slice(idx + 3) : it.label;
    if (!groups.has(grp)) groups.set(grp, []);
    groups.get(grp).push(Object.assign({}, it, {label: sub}));
  }
  const n = Math.max(...[...groups.keys()]
    .map(g => g.match(/^Trae #(\d+)$/)).filter(Boolean).map(mm => Number(mm[1])), 1);
  const quotaHtml = [...groups.entries()].map(([grp, its]) => {
    const m = grp.match(/^Trae #(\d+)$/);
    const idx = m ? Number(m[1]) : null;
    const acct = _trae_acct_for(idx);
    const moveBtns = (m && n > 1) ? _trae_move_btns(idx, n, acct && acct.id) : '';
    const delBtn = acct
      ? `<button class="ghost danger" title="删除该账号（不再使用/凭据失效时）" ` +
        `onclick="traeDeleteAccount('${acct.id}')">✕</button>` : '';
    const refreshBtn = `<button class="ghost" title="刷新本通道额度（绕过缓存重查）" ` +
      `onclick="refreshProviderQuota('trae', this)">↻</button>`;
    const renameBtn = acctRenameButton('trae', acct);
    const rowBtns = `<span class="ag-move">${renameBtn}${refreshBtn}${moveBtns}${delBtn}</span>`;
    // 合计行：总额度那条（head_only，unshift 到组首）单独供数——各权益包是
    // 它的明细不能相加，quotaHeadSum 走非 sum_items 分支取第一条有数的。
    const headSum = quotaHeadSum({items: its});
    return `
    <div class="pat-pkg">
      <span class="pat-pkg-name"${m ? ` data-trae-idx="${idx}"` : ''}>${esc(acct ? (acct.alias || acct.nickname || acct.id) : grp)}</span>
      ${_trae_sub_html(acct)}
      ${headSum ? `<div style="display:flex;align-items:center;margin:0 0 6px">${headSum}</div>` : ''}
      ${quotaItemsHtml(its, 'trae:' + grp)}
      ${rowBtns}
    </div>`;
  }).join('');
  const noticeHtml = notices.map(quotaItemHtml).join('');
  const multi = n > 1;

  panel.innerHTML = `
    <div class="chart-card" style="margin-top:14px">
      <div class="pat-head">
        <span class="name">Trae</span>
        <span class="tag">${multi ? '多账号 · 自动切换' : 'Trae Work'}</span>
        <span class="grow"></span>
        <span class="muted" style="font-size:11px">${multi ? '401/额度尽自动冷却换号（按登录顺位）' : '401/额度尽自动切换下一个账号'}</span>
      </div>
      ${noticeHtml ? `<div style="margin:6px 0">${noticeHtml}</div>` : ''}
      ${groups.size ? `<div class="pat-quota-grid">${quotaHtml}</div>`
        : '<div class="empty" style="padding:12px 0">暂无账号额度数据（未登录：跑 <span class="mono">buddy login trae</span>）</div>'}
    </div>`;
  loadTraeAccounts();
}

async function loadTraeAccounts() {
  try {
    const r = await api('/ui/api/trae/accounts');
    if (!r.enabled) { TRAE_ACCTS = []; return; }
    const accts = r.accounts || [];
    // 数据没变就不动 DOM——renderTraePanel 已用同一份快照渲染过
    if (JSON.stringify(accts) === JSON.stringify(TRAE_ACCTS)) return;
    const snapshotMissing = !TRAE_ACCTS || !TRAE_ACCTS.length;
    TRAE_ACCTS = accts;
    if (!accts.length) return;
    if (snapshotMissing) {
      renderTraePanel();
      syncQuotaFold();
      return;
    }
    // 就地回填：组名换成账号名、组名后插副标题（qoder 同款）
    for (const a of accts) {
      const el = (accts.length === 1)
        ? document.querySelector('#trae-panel .pat-pkg-name')
        : document.querySelector(`#trae-panel [data-trae-idx="${a.index}"]`);
      if (!el) continue;
      el.textContent = a.alias || a.nickname || a.id;
      if (el.nextElementSibling && el.nextElementSibling.hasAttribute('data-trae-sub')) {
        el.nextElementSibling.remove();
      }
      const sub = _trae_sub_html(a);
      if (sub) el.insertAdjacentHTML('afterend', sub);
    }
    syncQuotaFold();
  } catch (e) {
    // 静默：账号接口抖动不清面板（额度还在），下轮 30s 自动重试
  }
}
