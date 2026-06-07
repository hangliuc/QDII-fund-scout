// ============================================================
// QDII Fund Scout - 前端逻辑
// 数据流：loadConfig → renderChips → 用户操作 → runQuery → 后端
// 持久化：~/.fund-scout/config.json（后端）+ sessionStorage（前端临时）
// ============================================================

'use strict';

// ------------------------------------------------------------
// 状态
// ------------------------------------------------------------
const state = {
  funds: [],              // 我的基金: [{code, name, main_code?}]
  pushConfig: { feishu_webhook: '', wechat_webhook: '' },
  allFunds: [],           // 全部 QDII 清单（懒加载）
  allTags: [],
  selectedTag: '',
  pickedCodes: new Set(), // 模态框里勾选的代码
  lastResult: null,       // 最近查询结果（含预测）
  lastUpdate: null,       // 最近查询时间戳
  sortKey: null,
  sortDesc: false,
  autoRefreshTimer: null,
};

// ------------------------------------------------------------
// 通用：HTTP / 工具函数
// ------------------------------------------------------------
async function api(path, body) {
  const res = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

function toast(msg, opts = {}) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (opts.type ? ' ' + opts.type : '');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), opts.duration || 2200);
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function relativeTime(ts) {
  if (!ts) return '';
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
}

// ------------------------------------------------------------
// 主题
// ------------------------------------------------------------
function initTheme() {
  const saved = localStorage.getItem('fs-theme');
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const theme = saved || (prefersDark ? 'dark' : 'light');
  applyTheme(theme);
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  document.getElementById('themeIconLight').classList.toggle('hidden', theme === 'dark');
  document.getElementById('themeIconDark').classList.toggle('hidden', theme === 'light');
  localStorage.setItem('fs-theme', theme);
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(cur === 'light' ? 'dark' : 'light');
}

// ------------------------------------------------------------
// 配置：加载 / 保存
// ------------------------------------------------------------
async function loadConfig() {
  const cfg = await api('/api/config');
  state.funds = cfg.my_funds || [];
  state.pushConfig = cfg.push || { feishu_webhook: '', wechat_webhook: '' };
  document.getElementById('feishuUrl').value = state.pushConfig.feishu_webhook || '';
  document.getElementById('wechatUrl').value = state.pushConfig.wechat_webhook || '';

  // 自动刷新偏好（前端持久化）
  const autoOn = localStorage.getItem('fs-auto-refresh') === '1';
  document.getElementById('autoRefresh').checked = autoOn;
  if (autoOn) startAutoRefresh();

  renderChips();
  updatePushTogglesEnabled();
  loadScheduleStatus();
}

async function saveFundsToServer() {
  await api('/api/config', { my_funds: state.funds });
}

async function savePushConfig() {
  state.pushConfig.feishu_webhook = document.getElementById('feishuUrl').value.trim();
  state.pushConfig.wechat_webhook = document.getElementById('wechatUrl').value.trim();
  await api('/api/config', {
    my_funds: state.funds,
    push: state.pushConfig,
  });
  updatePushTogglesEnabled();
  document.getElementById('settingsBadge').style.display =
    (state.pushConfig.feishu_webhook || state.pushConfig.wechat_webhook) ? 'block' : 'none';
  toast('已保存', { type: 'success' });
}

function updatePushTogglesEnabled() {
  const fHas = !!state.pushConfig.feishu_webhook;
  const wHas = !!state.pushConfig.wechat_webhook;
  const fLabel = document.getElementById('pushFeishuLabel');
  const wLabel = document.getElementById('pushWechatLabel');
  document.getElementById('pushFeishu').disabled = !fHas;
  document.getElementById('pushWechat').disabled = !wHas;
  fLabel.classList.toggle('disabled', !fHas);
  wLabel.classList.toggle('disabled', !wHas);
  fLabel.title = fHas ? '' : '请先在设置中配置飞书 Webhook';
  wLabel.title = wHas ? '' : '请先在设置中配置企业微信 Webhook';

  document.getElementById('settingsBadge').style.display = (fHas || wHas) ? 'block' : 'none';
}

// ------------------------------------------------------------
// 我的基金 chip 区
// ------------------------------------------------------------
function renderChips() {
  const wrap = document.getElementById('fundChips');
  const meta = document.getElementById('fundCountMeta');
  if (state.funds.length === 0) {
    wrap.innerHTML = '<div class="empty-tip">还未添加基金，点击下方按钮选择</div>';
    meta.textContent = '';
    return;
  }
  meta.textContent = `${state.funds.length} 只`;
  wrap.innerHTML = state.funds.map((f, i) => `
    <span class="fund-chip" data-i="${i}">
      <span class="fc-code">${escapeHtml(f.code)}</span>
      <span class="fc-name" title="${escapeHtml(f.name || f.code)}">${escapeHtml(f.name || '')}</span>
      <button class="fc-remove" data-i="${i}" aria-label="移除" title="移除">&times;</button>
    </span>
  `).join('');
  wrap.querySelectorAll('.fc-remove').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const i = +btn.dataset.i;
      state.funds.splice(i, 1);
      await saveFundsToServer();
      renderChips();
    });
  });
}

// ------------------------------------------------------------
// 添加基金 模态框
// ------------------------------------------------------------
function openAddFundModal() {
  state.pickedCodes = new Set();
  document.getElementById('addFundModal').classList.add('show');
  document.getElementById('fundSearchInput').value = '';
  document.getElementById('manualCode').value = '';
  document.getElementById('manualError').classList.remove('show');
  document.getElementById('lookupStatus').textContent = '';
  state.selectedTag = '';
  refreshAddFundConfirm();
  setTimeout(() => document.getElementById('fundSearchInput').focus(), 50);
  loadAllFunds();
}

function closeAddFundModal() {
  document.getElementById('addFundModal').classList.remove('show');
}

async function loadAllFunds() {
  if (state.allFunds.length === 0) {
    try {
      const data = await api('/api/funds/list');
      state.allFunds = data.funds || [];
      state.allTags = data.tags || [];
      renderTagPills();
    } catch (e) {
      toast('加载基金清单失败: ' + e.message, { type: 'error' });
    }
  }
  renderFundPickList();
}

function renderTagPills() {
  const row = document.getElementById('tagRow');
  const tags = ['全部', ...state.allTags];
  row.innerHTML = tags.map(t => {
    const v = (t === '全部') ? '' : t;
    const active = state.selectedTag === v ? 'active' : '';
    return `<button class="tag-pill ${active}" data-tag="${escapeHtml(v)}">${escapeHtml(t)}</button>`;
  }).join('');
  row.querySelectorAll('.tag-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      state.selectedTag = btn.dataset.tag;
      renderTagPills();
      renderFundPickList();
    });
  });
}

function renderFundPickList() {
  const list = document.getElementById('fundPickList');
  const kw = document.getElementById('fundSearchInput').value.trim().toLowerCase();
  const tag = state.selectedTag;
  const existingCodes = new Set(state.funds.map(f => f.code));

  let filtered = state.allFunds;
  if (tag) filtered = filtered.filter(f => f.tags.includes(tag));
  if (kw) {
    filtered = filtered.filter(f =>
      f.code.includes(kw) ||
      f.name.toLowerCase().includes(kw) ||
      (f.pinyin || '').toLowerCase().includes(kw) ||
      (f.abbr || '').toLowerCase().includes(kw)
    );
  }

  // 截断防爆炸
  const MAX = 200;
  const truncated = filtered.length > MAX;
  filtered = filtered.slice(0, MAX);

  if (filtered.length === 0) {
    list.innerHTML = '<div class="empty-tip" style="padding:20px;text-align:center">无匹配基金</div>';
    return;
  }
  list.innerHTML = filtered.map(f => {
    const checked = state.pickedCodes.has(f.code) ? 'checked' : '';
    const inList = existingCodes.has(f.code);
    const klass = (state.pickedCodes.has(f.code) || inList) ? 'checked' : '';
    const tagsHtml = (f.tags || []).slice(0, 3).map(t =>
      `<span class="fpi-tag">${escapeHtml(t)}</span>`
    ).join('');
    const disabledNote = inList ? '<span class="fpi-tag" style="color:var(--green);border-color:var(--green)">已在列表</span>' : '';
    return `
      <label class="fund-pick-item ${klass}" data-code="${escapeHtml(f.code)}">
        <input type="checkbox" ${checked} ${inList ? 'disabled' : ''}>
        <span class="fpi-code">${escapeHtml(f.code)}</span>
        <span class="fpi-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>
        <span class="fpi-tags">${disabledNote || tagsHtml}</span>
      </label>
    `;
  }).join('') + (truncated ? `<div class="empty-tip" style="padding:10px;text-align:center">仅显示前 ${MAX} 条，请细化搜索</div>` : '');

  list.querySelectorAll('.fund-pick-item').forEach(el => {
    el.addEventListener('change', (e) => {
      const cb = el.querySelector('input[type="checkbox"]');
      if (!cb || cb.disabled) return;
      const code = el.dataset.code;
      if (cb.checked) state.pickedCodes.add(code);
      else state.pickedCodes.delete(code);
      el.classList.toggle('checked', cb.checked);
      refreshAddFundConfirm();
    });
  });
}

function refreshAddFundConfirm() {
  const btn = document.getElementById('addFundConfirm');
  const n = state.pickedCodes.size;
  btn.disabled = n === 0;
  btn.textContent = n === 0 ? '添加' : `添加 ${n} 只`;
}

async function confirmAddFunds() {
  const codes = Array.from(state.pickedCodes);
  if (codes.length === 0) return;
  const byCode = new Map(state.allFunds.map(f => [f.code, f]));
  const existing = new Set(state.funds.map(f => f.code));
  for (const code of codes) {
    if (existing.has(code)) continue;
    const meta = byCode.get(code);
    state.funds.push({ code, name: meta ? meta.name : code });
  }
  await saveFundsToServer();
  renderChips();
  closeAddFundModal();
  toast(`已添加 ${codes.length} 只基金`, { type: 'success' });
}

// 手动输入代码：自动反查名称
async function lookupCode(code) {
  const status = document.getElementById('lookupStatus');
  if (!/^\d{6}$/.test(code)) {
    status.textContent = '';
    return null;
  }
  status.textContent = '查询中…';
  try {
    const r = await api(`/api/funds/lookup?code=${encodeURIComponent(code)}`);
    if (r.found) {
      status.textContent = '✓ ' + (r.name || '').slice(0, 12);
      status.style.color = 'var(--green)';
      return r;
    }
    status.textContent = '未找到';
    status.style.color = 'var(--orange)';
    return null;
  } catch (e) {
    status.textContent = '查询失败';
    status.style.color = 'var(--red)';
    return null;
  }
}

let lookupTimer;
function bindManualInput() {
  const input = document.getElementById('manualCode');
  const errEl = document.getElementById('manualError');

  input.addEventListener('input', () => {
    errEl.classList.remove('show');
    input.classList.remove('invalid');
    clearTimeout(lookupTimer);
    const code = input.value.trim();
    if (!code) {
      document.getElementById('lookupStatus').textContent = '';
      return;
    }
    if (!/^\d*$/.test(code)) {
      input.classList.add('invalid');
      errEl.textContent = '只能输入数字';
      errEl.classList.add('show');
      return;
    }
    if (code.length === 6) {
      lookupTimer = setTimeout(() => lookupCode(code), 250);
    } else {
      document.getElementById('lookupStatus').textContent = '';
    }
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      document.getElementById('btnManualAdd').click();
    }
  });

  document.getElementById('btnManualAdd').addEventListener('click', async () => {
    const code = input.value.trim();
    if (!/^\d{6}$/.test(code)) {
      input.classList.add('invalid');
      errEl.textContent = '基金代码必须是 6 位数字';
      errEl.classList.add('show');
      return;
    }
    if (state.funds.some(f => f.code === code)) {
      errEl.textContent = '该基金已在列表中';
      errEl.classList.add('show');
      return;
    }
    const meta = await lookupCode(code);
    state.funds.push({ code, name: meta ? meta.name : code });
    await saveFundsToServer();
    renderChips();
    input.value = '';
    document.getElementById('lookupStatus').textContent = '';
    toast(`已添加 ${code}`, { type: 'success' });
  });
}

// ------------------------------------------------------------
// 查询 + 推送
// ------------------------------------------------------------
async function runQuery() {
  if (state.funds.length === 0) { toast('请先添加基金'); return; }
  const codes = state.funds.map(f => f.code);
  const pushFeishu = document.getElementById('pushFeishu').checked;
  const pushWechat = document.getElementById('pushWechat').checked;
  const pushTargets = [];
  if (pushFeishu) pushTargets.push('feishu');
  if (pushWechat) pushTargets.push('wechat');

  const loading = document.getElementById('loading');
  const loadingText = document.getElementById('loadingText');
  loading.classList.add('show');
  loadingText.textContent = '查询基础数据…';
  document.getElementById('btnQuery').disabled = true;
  document.getElementById('warningsBanner').classList.remove('show');
  document.getElementById('predHint').classList.remove('show');

  try {
    const data = await api('/api/query', { codes });
    state.lastResult = data;
    state.lastUpdate = Date.now();
    sessionStorage.setItem('fs-last-result', JSON.stringify({ data, ts: state.lastUpdate }));
    renderResult(data);
    loading.classList.remove('show');

    // 异步拉预测
    fetchPredictionsAsync(codes);

    // 推送
    if (pushTargets.length > 0) {
      loading.classList.add('show');
      loadingText.textContent = `推送到 ${pushTargets.join(' + ')}…`;
      try {
        const res = await api('/api/push', { target: pushTargets.join(','), codes });
        loading.classList.remove('show');
        if (res.ok) {
          const labels = (res.ok_targets || pushTargets).map(t =>
            t === 'feishu' ? '飞书' : (t === 'wechat' ? '企业微信' : t)
          );
          toast('已推送到 ' + labels.join(' + '), { type: 'success' });
        } else {
          toast('推送失败: ' + (res.error || '未知错误'), { type: 'error' });
        }
      } catch (e) {
        loading.classList.remove('show');
        toast('推送失败: ' + e.message, { type: 'error' });
      }
    }
  } catch (e) {
    loading.classList.remove('show');
    toast('查询失败: ' + e.message, { type: 'error' });
  } finally {
    document.getElementById('btnQuery').disabled = false;
  }
}

async function fetchPredictionsAsync(codes) {
  const tbody = document.getElementById('resultBody');
  // 标记预测列加载中
  tbody.querySelectorAll('tr[data-code]').forEach(tr => {
    const cell = tr.querySelector('.pred-col');
    if (cell && cell.textContent.trim() === '-') {
      cell.innerHTML = '<span class="ink-mute">加载中…</span>';
    }
  });

  try {
    const res = await api('/api/predict', { codes });
    if (!res || !res.predictions) return;

    // 把预测结果合并到 state.lastResult
    if (state.lastResult && state.lastResult.funds) {
      for (const f of state.lastResult.funds) {
        const p = res.predictions[f.code];
        if (p) f.t1_prediction = p;
      }
      sessionStorage.setItem('fs-last-result',
        JSON.stringify({ data: state.lastResult, ts: state.lastUpdate }));
    }

    // 更新表格预测列
    tbody.querySelectorAll('tr[data-code]').forEach(tr => {
      const code = tr.dataset.code;
      const p = res.predictions[code];
      const cell = tr.querySelector('.pred-col');
      if (cell) cell.innerHTML = p ? fmtPrediction(p) : '-';
    });

    // 估算提示
    const hasEst = Object.values(res.predictions).some(p => p && p.is_estimate && p.value !== null);
    const hint = document.getElementById('predHint');
    if (hasEst) {
      hint.innerHTML = '⚠ 带「估算」标注的涨跌为模型预估值，仅供参考，请以基金公司公告为准';
      hint.classList.add('show');
    }
  } catch (e) {
    // 静默失败
    tbody.querySelectorAll('.pred-col').forEach(cell => {
      if (cell.textContent.trim() === '加载中…') cell.innerHTML = '-';
    });
  }
}

// ------------------------------------------------------------
// 表格渲染
// ------------------------------------------------------------
const COLUMNS = [
  { key: 'name',        label: '基金名称',     sortable: true },
  { key: 'code',        label: '代码',         sortable: true },
  { key: 'return_1y',   label: '近1年',        sortable: true, num: true },
  { key: 'drawdown_1y', label: '近1年回撤',    sortable: true, num: true },
  { key: 'scale',       label: '规模(亿)',     sortable: true, num: true },
  { key: 'total_fee',   label: '总费率',       sortable: true, num: true },
  { key: 'purchase_info', label: '申购状态',   sortable: true },
  { key: 't1_prediction', label: '最新涨跌',   sortable: false },
  { key: 'market_top3', label: '市场投资TOP3', sortable: false },
];

function renderTableHeader() {
  const tr = document.getElementById('theadRow');
  tr.innerHTML = COLUMNS.map(col => {
    const sorted = state.sortKey === col.key;
    const arrow = sorted ? (state.sortDesc ? '▼' : '▲') : '↕';
    const klass = (col.num ? 'num ' : '') + (sorted ? 'sorted' : '');
    return `
      <th class="${klass}" data-key="${col.key}" data-sortable="${col.sortable}">
        ${escapeHtml(col.label)}
        ${col.sortable ? `<span class="sort-arrow">${arrow}</span>` : ''}
      </th>
    `;
  }).join('');
  tr.querySelectorAll('th[data-sortable="true"]').forEach(th => {
    th.addEventListener('click', () => {
      const key = th.dataset.key;
      if (state.sortKey === key) state.sortDesc = !state.sortDesc;
      else { state.sortKey = key; state.sortDesc = true; }
      renderTableHeader();
      renderTableBody();
    });
  });
}

function getSortValue(f, key) {
  if (key === 't1_prediction') return f.t1_prediction ? f.t1_prediction.value : null;
  if (key === 'purchase_info') {
    const s = f.purchase_info || '';
    if (s.includes('暂停')) return 0;
    if (s.includes('限小额')) return 1;
    if (s.includes('限大额')) return 2;
    return 3;
  }
  const v = f[key];
  if (v == null || v === '') return null;
  if (typeof v === 'number') return v;
  const n = parseFloat(String(v).replace(/[%,]/g, ''));
  return isNaN(n) ? v : n;
}

function renderTableBody() {
  const data = state.lastResult;
  if (!data || !data.funds || data.funds.length === 0) {
    document.getElementById('tableWrap').style.display = 'none';
    document.getElementById('emptyResult').style.display = data ? 'block' : 'none';
    return;
  }
  document.getElementById('tableWrap').style.display = 'block';
  document.getElementById('emptyResult').style.display = 'none';

  let funds = [...data.funds];
  if (state.sortKey) {
    const k = state.sortKey;
    funds.sort((a, b) => {
      const va = getSortValue(a, k), vb = getSortValue(b, k);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') {
        return state.sortDesc ? vb - va : va - vb;
      }
      const sa = String(va), sb = String(vb);
      return state.sortDesc ? sb.localeCompare(sa) : sa.localeCompare(sb);
    });
  }

  const tbody = document.getElementById('resultBody');
  tbody.innerHTML = funds.map(f => `
    <tr data-code="${escapeHtml(f.code)}">
      <td class="name-col"><strong title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</strong></td>
      <td class="code-col">${escapeHtml(f.code)}</td>
      <td class="num">${fmtReturn(f.return_1y)}</td>
      <td class="num">${fmtDrawdown(f.drawdown_1y)}</td>
      <td class="num">${f.scale != null ? f.scale : '-'}</td>
      <td class="num">${fmtFee(f.total_fee)}</td>
      <td>${fmtStatus(f.purchase_info || '')}</td>
      <td class="pred-col">${fmtPrediction(f.t1_prediction)}</td>
      <td class="market-top3" title="${escapeHtml(f.market_top3 || '')}">${escapeHtml(f.market_top3 || '-')}</td>
    </tr>
  `).join('');
}

function renderResult(data) {
  // 警告
  const wb = document.getElementById('warningsBanner');
  if (data.warnings && data.warnings.length > 0) {
    wb.innerHTML = data.warnings.map(w => `<div>${escapeHtml(w)}</div>`).join('');
    wb.classList.add('show');
  }

  // 元信息条
  document.getElementById('metaBar').style.display = 'flex';
  updateFreshness();

  renderTableHeader();
  renderTableBody();
}

function updateFreshness() {
  const el = document.getElementById('freshness');
  if (!state.lastUpdate) { el.textContent = ''; return; }
  const rel = relativeTime(state.lastUpdate);
  const stale = (Date.now() - state.lastUpdate) > 30 * 60 * 1000; // 30 分钟
  el.innerHTML = `<span class="${stale ? 'stale' : 'fresh'}">最后更新: ${rel}</span>`;
}

// ------------------------------------------------------------
// 表格单元格格式化
// ------------------------------------------------------------
function fmtReturn(val) {
  if (val == null || val === '') return '-';
  const v = typeof val === 'string' ? parseFloat(val.replace('%', '')) : val;
  if (isNaN(v)) return '-';
  const sign = v > 0 ? '+' : '';
  return `<span class="${v > 0 ? 'red' : (v < 0 ? 'green' : '')}">${sign}${v.toFixed(2)}%</span>`;
}
function fmtDrawdown(val) {
  if (val == null || val === '') return '-';
  const v = typeof val === 'string' ? parseFloat(val) : val;
  if (isNaN(v)) return '-';
  return `${(v * 100).toFixed(2)}%`;
}
function fmtFee(val) {
  if (val == null || val === '') return '-';
  const v = typeof val === 'string' ? parseFloat(val) : val;
  if (isNaN(v)) return '-';
  return v.toFixed(2) + '%';
}
function fmtStatus(s) {
  if (!s) return '-';
  if (s.includes('暂停')) return `<span class="status-pause">${escapeHtml(s)}</span>`;
  if (s.includes('限小额') || s.includes('限大额')) return `<span class="status-limit">${escapeHtml(s)}</span>`;
  if (s.includes('开放')) return `<span class="status-open">${escapeHtml(s)}</span>`;
  return escapeHtml(s);
}
function fmtPrediction(p) {
  if (!p || typeof p !== 'object' || p.value == null) return '-';
  const v = p.value;
  const sign = v > 0 ? '+' : '';
  const cls = v > 0 ? 'red' : (v < 0 ? 'green' : '');
  const dateStr = p.date ? `<span class="ink-mute">${escapeHtml(p.date.slice(5))}</span> ` : '';
  const suffix = p.is_estimate ? ' <span class="ink-mute">(估算)</span>' : '';
  return `${dateStr}<span class="${cls}">${sign}${v.toFixed(2)}%</span>${suffix}`;
}

// ------------------------------------------------------------
// 设置抽屉
// ------------------------------------------------------------
function openDrawer() {
  document.getElementById('drawerMask').classList.add('show');
  document.getElementById('settingsDrawer').classList.add('show');
}
function closeDrawer() {
  document.getElementById('drawerMask').classList.remove('show');
  document.getElementById('settingsDrawer').classList.remove('show');
}

async function testWebhook(type) {
  const url = document.getElementById(type === 'feishu' ? 'feishuUrl' : 'wechatUrl').value.trim();
  if (!url) { toast('请先输入 Webhook 地址'); return; }
  const btn = event.target;
  const orig = btn.textContent;
  btn.textContent = '测试中…'; btn.disabled = true;
  try {
    const r = await api('/api/test-webhook', { type, url });
    if (r.ok) toast((type === 'feishu' ? '飞书' : '企业微信') + ' 连接成功', { type: 'success' });
    else toast(r.error || '连接失败', { type: 'error' });
  } catch (e) {
    toast('测试失败: ' + e.message, { type: 'error' });
  }
  btn.textContent = orig; btn.disabled = false;
}

function onSchedulePresetChange() {
  const sel = document.getElementById('schedulePreset');
  document.getElementById('customTimeRow').style.display =
    sel.value === 'custom' ? 'block' : 'none';
}

async function setupSchedule() {
  const sel = document.getElementById('schedulePreset');
  let val = sel.value;
  if (!val) { toast('请选择推送时段'); return; }
  let times, weekdays;
  if (val === 'custom') {
    times = document.getElementById('customTime').value.trim();
    weekdays = document.getElementById('customDayType').value;
    if (!times) { toast('请输入推送时间'); return; }
  } else {
    [times, weekdays] = val.split('|');
  }
  const r = await api('/api/schedule', { action: 'setup', times, weekdays });
  toast(r.ok ? '定时推送已启用' : '启用失败: ' + (r.error || ''),
    { type: r.ok ? 'success' : 'error' });
  loadScheduleStatus();
}

async function removeSchedule() {
  const r = await api('/api/schedule', { action: 'remove' });
  toast(r.ok ? '已取消定时推送' : '取消失败',
    { type: r.ok ? 'success' : 'error' });
  loadScheduleStatus();
}

async function loadScheduleStatus() {
  const data = await api('/api/schedule');
  const el = document.getElementById('scheduleStatus');
  if (data.active) {
    let desc = '';
    if (data.times && data.times.length > 0) {
      desc = (data.weekdays ? '工作日' : '每天') + ' ' + data.times.join(', ');
    } else if (data.cron) {
      desc = data.cron;
    }
    el.innerHTML = `<span class="ok">● 已启用</span> ${desc ? '— ' + escapeHtml(desc) : ''}`;
  } else {
    el.innerHTML = '<span class="off">○ 未启用</span>';
  }
}

// ------------------------------------------------------------
// 自动刷新
// ------------------------------------------------------------
function startAutoRefresh() {
  stopAutoRefresh();
  state.autoRefreshTimer = setInterval(() => {
    if (state.funds.length === 0) return;
    if (document.hidden) return; // 后台标签页不刷
    runQuery();
  }, 30 * 60 * 1000);
}
function stopAutoRefresh() {
  if (state.autoRefreshTimer) {
    clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
}

// ------------------------------------------------------------
// 缓存诊断（保留低频）
// ------------------------------------------------------------
async function loadCacheStats() {
  try {
    const data = await api('/api/cache');
    const el = document.getElementById('cacheStats');
    if (data && typeof data === 'object') {
      el.textContent = `${data.n_indexed_funds || 0} 只基金已索引 · ${data.n_pdfs || 0} 份 PDF · ${data.n_parsed || 0} 份解析数据 · ${data.total_size_mb || 0} MB`;
    }
  } catch (e) {
    document.getElementById('cacheStats').textContent = '读取缓存状态失败';
  }
}

async function cacheRefresh(action) {
  const labels = {refresh: '刷新中…', force_refresh: '强制重新检查中…', clear_index: '清除中…'};
  const resultEl = document.getElementById('cacheResult');
  resultEl.textContent = labels[action] || '处理中…';
  try {
    const res = await api('/api/cache', { action });
    if (res.ok) {
      if (action === 'clear_index') {
        resultEl.textContent = `✓ 已清除 ${res.cleared || 0} 个索引（PDF 和解析数据保留）`;
      } else if (res.stats) {
        const s = res.stats;
        resultEl.textContent = `✓ 完成：共 ${s.n_total} 只，更新 ${s.n_new}，不变 ${s.n_unchanged}，失败 ${s.n_failed}`;
      } else {
        resultEl.textContent = '✓ 完成';
      }
      loadCacheStats();
    } else {
      resultEl.textContent = '✗ ' + (res.error || '操作失败');
    }
  } catch (e) {
    resultEl.textContent = '✗ ' + e.message;
  }
}

// 暴露给 HTML 内联 onclick
window.cacheRefresh = cacheRefresh;
window.testWebhook = testWebhook;
window.onSchedulePresetChange = onSchedulePresetChange;
window.setupSchedule = setupSchedule;
window.removeSchedule = removeSchedule;

// ------------------------------------------------------------
// 启动 + 事件绑定
// ------------------------------------------------------------
async function init() {
  initTheme();

  // sessionStorage 恢复上次查询结果
  try {
    const cached = JSON.parse(sessionStorage.getItem('fs-last-result') || 'null');
    if (cached && cached.data) {
      state.lastResult = cached.data;
      state.lastUpdate = cached.ts || null;
      renderResult(cached.data);
    }
  } catch (e) {}

  await loadConfig();

  // 顶栏按钮
  document.getElementById('btnRefresh').addEventListener('click', runQuery);
  document.getElementById('btnSettings').addEventListener('click', openDrawer);
  document.getElementById('btnTheme').addEventListener('click', toggleTheme);

  // 主操作
  document.getElementById('btnQuery').addEventListener('click', runQuery);
  document.getElementById('btnAddFund').addEventListener('click', openAddFundModal);

  // 模态框
  document.getElementById('addFundClose').addEventListener('click', closeAddFundModal);
  document.getElementById('addFundCancel').addEventListener('click', closeAddFundModal);
  document.getElementById('addFundConfirm').addEventListener('click', confirmAddFunds);
  document.getElementById('addFundModal').addEventListener('click', (e) => {
    if (e.target.id === 'addFundModal') closeAddFundModal();
  });
  document.getElementById('fundSearchInput').addEventListener('input',
    debounce(renderFundPickList, 150));
  bindManualInput();

  // 抽屉
  document.getElementById('drawerClose').addEventListener('click', closeDrawer);
  document.getElementById('drawerMask').addEventListener('click', closeDrawer);
  document.getElementById('drawerSave').addEventListener('click', savePushConfig);

  // 自动刷新切换
  document.getElementById('autoRefresh').addEventListener('change', (e) => {
    if (e.target.checked) {
      localStorage.setItem('fs-auto-refresh', '1');
      startAutoRefresh();
      toast('已开启自动刷新（30 分钟）', { type: 'success' });
    } else {
      localStorage.setItem('fs-auto-refresh', '0');
      stopAutoRefresh();
      toast('已关闭自动刷新');
    }
  });

  // 元信息条相对时间每分钟更新
  setInterval(updateFreshness, 60 * 1000);

  // 键盘快捷键
  document.addEventListener('keydown', (e) => {
    // 输入框内：只允许 ESC 关弹窗，其它快捷键不触发
    if (e.target.matches('input, textarea, select')) {
      if (e.key === 'Escape') {
        closeAddFundModal();
        closeDrawer();
      }
      return;
    }
    if (e.key === '/') {
      e.preventDefault();
      openAddFundModal();
    } else if (e.key.toLowerCase() === 'r') {
      runQuery();
    } else if (e.key.toLowerCase() === 't') {
      toggleTheme();
    } else if (e.key === 'Escape') {
      closeAddFundModal();
      closeDrawer();
    }
  });

  // 缓存折叠面板首次展开时加载
  document.querySelector('details.cache-card').addEventListener('toggle', (e) => {
    if (e.target.open) loadCacheStats();
  });
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn.apply(null, args), ms);
  };
}

init();
