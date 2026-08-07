/**
 * app.js - ZCBOT 统一管理面板核心
 * 提供：API 封装（Bearer token）、Toast、Modal、确认框、Hash 路由、导航渲染
 */
const TOKEN_KEY = 'zcbot_token';

/* ─────────── Token 管理 ─────────── */
function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
function clearToken() { localStorage.removeItem(TOKEN_KEY); document.cookie = 'zcbot_token=; Max-Age=0; path=/'; }

/* ─────────── API 封装 ─────────── */
async function api(url, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  const token = getToken();
  if (token) opts.headers['Authorization'] = 'Bearer ' + token;
  if (opts.body && typeof opts.body === 'string' && !opts.headers['Content-Type']) {
    opts.headers['Content-Type'] = 'application/json';
  }
  let res;
  try {
    res = await fetch(url, opts);
  } catch (e) {
    toast('网络错误: ' + e.message, 'error');
    return null;
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (res.status === 401) {
    clearToken();
    location.href = '/login.html';
    return null;
  }
  return data;
}

/* ─────────── Toast ─────────── */
function toast(msg, type = 'info', duration = 3000) {
  let c = document.getElementById('toasts');
  if (!c) {
    c = document.createElement('div');
    c.className = 'toast-container';
    document.body.appendChild(c);
  }
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => {
    t.style.opacity = '0';
    t.style.transition = 'all .3s';
    setTimeout(() => t.remove(), 300);
  }, duration);
}

/* ─────────── Modal ─────────── */
function openModal(html, opts = {}) {
  closeModal();
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.id = 'modalMask';
  mask.innerHTML = `<div class="modal ${opts.lg ? 'lg' : ''}">${html}</div>`;
  document.body.appendChild(mask);
  // 点击遮罩关闭
  mask.addEventListener('mousedown', (e) => { if (e.target === mask) closeModal(); });
  return mask;
}

function closeModal() {
  const m = document.getElementById('modalMask');
  if (m) m.remove();
}

/* ─────────── 确认框 ─────────── */
function confirmDialog(msg, title = '确认操作') {
  return new Promise((resolve) => {
    const mask = openModal(`
      <div class="modal-head"><span>${escapeHtml(title)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
      <div class="modal-body">${escapeHtml(msg)}</div>
      <div class="modal-foot">
        <button class="btn" onclick="closeModal();resolveConfirm(false)">取消</button>
        <button class="btn danger" onclick="closeModal();resolveConfirm(true)">确认</button>
      </div>`);
    window._confirmResolve = resolve;
  });
}
function resolveConfirm(v) { if (window._confirmResolve) { window._confirmResolve(v); window._confirmResolve = null; } }

/* ─────────── 工具函数 ─────────── */
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtTime(dt) {
  if (!dt) return '-';
  const d = new Date(dt);
  if (isNaN(d.getTime())) return dt;
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function timeAgo(dt) {
  if (!dt) return '-';
  const d = new Date(dt);
  if (isNaN(d.getTime())) return dt;
  const diff = Date.now() - d.getTime();
  const s = Math.floor(Math.abs(diff) / 1000);
  if (s < 60) return `${s}秒前`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}分钟前`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}小时前`;
  return fmtTime(dt);
}

function badgeHtml(ok, okText = '启用', errText = '禁用') {
  return ok
    ? `<span class="badge ok">${okText}</span>`
    : `<span class="badge err">${errText}</span>`;
}

function statusBadge(status) {
  const map = {
    running: ['ok', '运行中'], stopped: ['muted', '已停止'],
    error: ['err', '错误'], oom: ['warn', 'OOM'],
  };
  const [cls, txt] = map[status] || ['muted', status || '-'];
  return `<span class="badge ${cls}">${txt}</span>`;
}

/* ─────────── 路由定义 ─────────── */
const ROUTES = [
  { key: 'dashboard', title: '仪表盘', icon: '▦', group: '总览' },
  { key: 'marketplace', title: '插件市场', icon: '◈', group: '总览' },
  { key: 'plugins', title: '插件管理', icon: '◫', group: '总览' },
  { key: 'commands', title: '命令管理', icon: '⌘', group: '总览' },
  { key: 'users', title: '用户管理', icon: '☺', group: '运营' },
  { key: 'groups', title: '群组管理', icon: '☰', group: '运营' },
  { key: 'tasks', title: '定时任务', icon: '◷', group: '运营' },
  { key: 'runtime', title: '运行状态', icon: '↯', group: '运维' },
  { key: 'connection', title: '连接设置', icon: '⇄', group: '运维' },
  { key: 'filebrowser', title: '文件浏览', icon: '⊞', group: '运维' },
  { key: 'logs', title: '日志中心', icon: '☰', group: '运维' },
  { key: 'database', title: '数据库', icon: '▤', group: '运维' },
  { key: 'plugin_webui', title: '插件页面', icon: '⊞', group: '运维', webui: true },
];

let _pluginWebUIs = [];
let _admin = null;

/* ─────────── 导航渲染 ─────────── */
function renderNav(activeKey) {
  const nav = document.getElementById('nav');
  let html = '';
  let lastGroup = '';
  ROUTES.forEach(r => {
    // 插件页面：仅当存在插件 WebUI 时显示
    if (r.webui && _pluginWebUIs.length === 0) return;
    if (r.group !== lastGroup) {
      html += `<div class="nav-group">${r.group}</div>`;
      lastGroup = r.group;
    }
    const active = r.key === activeKey ? 'active' : '';
    html += `<div class="nav-item ${active}" data-route="${r.key}"><span class="icon">${r.icon}</span><span class="label">${r.title}</span></div>`;
  });
  nav.innerHTML = html;

  nav.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => {
      const key = el.dataset.route;
      if (key === 'plugin_webui') { location.hash = '#/plugin_webui'; }
      else { location.hash = '#/' + key; }
      document.getElementById('sidebar').classList.remove('open');
    });
  });

  // 侧边栏底部「设置」项同步激活状态
  document.querySelectorAll('.sidebar-foot .nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.route === activeKey);
    el.addEventListener('click', () => { location.hash = '#/settings'; });
  });
}

function renderUserInfo() {
  const el = document.getElementById('userInfo');
  if (!_admin) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <span class="uname">${escapeHtml(_admin.username)}</span>
    <span class="role-badge ${_admin.role === 'super' ? 'super' : 'admin'}">${_admin.role === 'super' ? '超管' : '管理'}</span>
    <button class="btn sm ghost" onclick="doLogout()">退出</button>`;
}

async function doLogout() {
  await api('/api/logout', { method: 'POST' });
  clearToken();
  location.href = '/login.html';
}

/* ─────────── 路由分发 ─────────── */
async function route() {
  const hash = location.hash || '#/dashboard';
  const key = hash.replace(/^#\//, '').split('/')[0] || 'dashboard';

  const current = ROUTES.find(r => r.key === key) || ROUTES[0];
  document.getElementById('pageTitle').textContent = current.title;
  renderNav(key);
  renderUserInfo();

  const view = document.getElementById('view');
  view.innerHTML = '<div class="spinner"></div>';

  // 刷新状态栏
  updateStatusBar();

  try {
    if (key === 'plugin_webui') await renderPluginWebUI();
    else if (typeof Views !== 'undefined' && Views[key]) await Views[key](view);
    else await Views.dashboard(view);
  } catch (e) {
    console.error(e);
    view.innerHTML = `<div class="card"><div class="empty">页面加载失败: ${escapeHtml(e.message || e)}</div></div>`;
  }
}

async function updateStatusBar() {
  try {
    const [dashRes, now] = await Promise.all([
      api('/api/dashboard'),
      Promise.resolve(new Date()),
    ]);
    const data = (dashRes && dashRes.data) || {};
    const bots = (data.bots || []).length;
    const plugins = data.plugins_active ?? '-';
    const connEl = document.getElementById('sbConn');
    const plgEl = document.getElementById('sbPlugins');
    const timeEl = document.getElementById('sbTime');
    if (connEl) connEl.textContent = `Bot: ${bots} 在线`;
    if (plgEl) plgEl.textContent = `插件: ${plugins}`;
    if (timeEl) timeEl.textContent = now.toLocaleTimeString();
  } catch (e) { /* 静默 */ }
}

async function loadFrameworkVersion() {
  try {
    const r = await fetch('/api/version').then(res => res.json());
    const d = (r && r.data) || {};
    const el = document.getElementById('sbVersion');
    if (el) el.textContent = `v${d.version || '-'}${d.alpha ? ' · Alpha' : ''}`;
  } catch (e) { /* 静默 */ }
}

async function renderPluginWebUI() {
  const view = document.getElementById('view');
  const params = new URLSearchParams(location.hash.split('?')[1] || '');
  let name = params.get('name') || (_pluginWebUIs[0] && _pluginWebUIs[0].plugin_name) || '';
  const entry = params.get('entry') || 'index.html';

  if (!name || _pluginWebUIs.length === 0) {
    view.innerHTML = '<div class="card"><div class="empty">没有可用的插件页面</div></div>';
    return;
  }

  const selHtml = _pluginWebUIs.map(w =>
    `<option value="${w.plugin_name}" ${w.plugin_name === name ? 'selected' : ''}>${escapeHtml(w.title)}</option>`
  ).join('');

  view.innerHTML = `
    <div class="card">
      <div class="form-row">
        <label>选择插件页面</label>
        <select class="select" id="webuiSelect">${selHtml}</select>
      </div>
    </div>
    <div style="border:1px solid var(--border);border-radius:10px;overflow:hidden;background:#fff;height:calc(100vh - 220px)">
      <iframe id="webuiFrame" style="width:100%;height:100%;border:none" src="/api/plugin_webui/${encodeURIComponent(name)}?entry=${encodeURIComponent(entry)}"></iframe>
    </div>`;

  document.getElementById('webuiSelect').addEventListener('change', (e) => {
    const w = _pluginWebUIs.find(x => x.plugin_name === e.target.value);
    if (w) location.hash = `#/plugin_webui?name=${encodeURIComponent(w.plugin_name)}&entry=${encodeURIComponent(w.entry || 'index.html')}`;
  });
}

/* ─────────── 初始化 ─────────── */
async function init() {
  const token = getToken();
  if (!token) { location.href = '/login.html'; return; }
  const me = await api('/api/me');
  if (!me || me.code !== 0) { location.href = '/login.html'; return; }
  _admin = me.data;

  // 加载插件 WebUI 列表
  const w = await api('/api/plugin_webuis');
  if (w && w.code === 0) _pluginWebUIs = w.data || [];

  document.getElementById('menuBtn').addEventListener('click', () => {
    document.getElementById('sidebar').classList.toggle('open');
  });

  loadFrameworkVersion();
  window.addEventListener('hashchange', route);
  route();
}

window.addEventListener('DOMContentLoaded', init);
