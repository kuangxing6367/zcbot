/**
 * common.js - 全局共享脚本
 * 提供：API 封装（token 认证）、鉴权守卫、Toast 通知、头部导航渲染、工具函数
 * 认证方式：Bearer token（2048位随机字符串），存 localStorage
 */

/* ─────────── Token 管理 ─────────── */

const TOKEN_KEY = 'zcbot_token';

function getToken() {
    return localStorage.getItem(TOKEN_KEY) || '';
}

function setToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
}

function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
}

/* ─────────── API 封装 ─────────── */

async function api(url, options = {}) {
    const opts = {
        ...options,
        headers: { ...options.headers },
    };
    // 注入 Authorization 头
    const token = getToken();
    if (token) {
        opts.headers['Authorization'] = 'Bearer ' + token;
    }
    // GET 请求不设 Content-Type
    if (options.body && typeof options.body === 'string') {
        opts.headers['Content-Type'] = opts.headers['Content-Type'] || 'application/json';
    }
    const res = await fetch(url, opts);
    let data;
    try { data = await res.json(); } catch { data = null; }

    if (res.status === 401) {
        // token 无效或过期，清除并跳转登录页
        clearToken();
        redirectToLogin();
        return null;
    }
    return data;
}

function redirectToLogin() {
    if (!location.pathname.endsWith('login.html')) {
        location.href = '/login.html';
    }
}

/* ─────────── 鉴权守卫 ─────────── */

async function checkAuth() {
    if (!getToken()) {
        redirectToLogin();
        return null;
    }
    const me = await api('/api/me');
    if (!me || me.code !== 0) {
        redirectToLogin();
        return null;
    }
    return me.data;
}

/* ─────────── Toast 通知 ─────────── */

function ensureToastContainer() {
    let c = document.querySelector('.toast-container');
    if (!c) {
        c = document.createElement('div');
        c.className = 'toast-container';
        document.body.appendChild(c);
    }
    return c;
}

function toast(msg, type = 'info', duration = 3000) {
    const c = ensureToastContainer();
    const t = document.createElement('div');
    t.className = `toast ${type}`;
    t.textContent = msg;
    c.appendChild(t);
    setTimeout(() => {
        t.style.opacity = '0';
        t.style.transform = 'translateX(40px)';
        t.style.transition = 'all 0.3s';
        setTimeout(() => t.remove(), 300);
    }, duration);
}

/* ─────────── 头部导航 ─────────── */

const NAV_ITEMS = [
    { href: '/index.html',    label: '仪表盘', key: 'dashboard' },
    { href: '/plugins.html',  label: '插件',   key: 'plugins' },
    { href: '/commands.html', label: '命令',   key: 'commands' },
    { href: '/users.html',    label: '用户',   key: 'users' },
    { href: '/tasks.html',    label: '任务',   key: 'tasks' },
    { href: '/logs.html',     label: '日志',   key: 'logs' },
    { href: '/settings.html', label: '设置',   key: 'settings' },
];

// 插件 WebUI 缓存
let _pluginWebUIs = [];

/**
 * 加载插件 WebUI 列表
 */
async function loadPluginWebUIs() {
    const res = await api('/api/plugin_webuis');
    if (res && res.code === 0) {
        _pluginWebUIs = res.data || [];
    }
    return _pluginWebUIs;
}

/**
 * 渲染插件 WebUI 下拉菜单
 */
function renderPluginWebUIDropdown(navEl) {
    // 清空旧的下拉
    const oldDD = document.querySelector('.plugin-webui-dropdown');
    if (oldDD) oldDD.remove();

    if (_pluginWebUIs.length === 0) return;

    const dd = document.createElement('div');
    dd.className = 'plugin-webui-dropdown';
    dd.style.cssText = 'display:none;position:absolute;top:100%;left:0;background:var(--card-bg);border:1px solid var(--border);border-radius:8px;min-width:180px;box-shadow:0 4px 20px rgba(0,0,0,0.1);z-index:100;padding:4px;';

    _pluginWebUIs.forEach(w => {
        const a = document.createElement('a');
        a.href = `/plugin_webui.html?name=${encodeURIComponent(w.plugin_name)}&entry=${encodeURIComponent(w.entry || 'index.html')}`;
        a.textContent = w.title;
        a.style.cssText = 'display:block;padding:8px 12px;border-radius:6px;font-size:13px;color:var(--text);text-decoration:none;';
        a.onmouseover = () => { a.style.background = 'var(--surface-hover)'; };
        a.onmouseout = () => { a.style.background = 'transparent'; };
        dd.appendChild(a);
    });

    navEl.style.position = 'relative';
    navEl.appendChild(dd);

    navEl.onmouseenter = () => { dd.style.display = 'block'; };
    navEl.onmouseleave = () => { dd.style.display = 'none'; };
}

/**
 * 渲染公共头部导航
 * @param {string} activeKey - 当前高亮的 nav key
 * @param {object} admin - {username, role}
 */
function renderHeader(activeKey, admin) {
    // 主导航项
    const navHtml = NAV_ITEMS.map(item =>
        `<a href="${item.href}" class="${item.key === activeKey ? 'active' : ''}">${item.label}</a>`
    ).join('');

    // 插件页面下拉（有 WebUI 时才显示）
    const pluginWebuiHtml = _pluginWebUIs.length > 0
        ? `<div class="nav-item plugin-webui-trigger" style="position:relative;display:inline-block;">
               <a href="javascript:;" class="${activeKey === 'plugin_webui' ? 'active' : ''}">插件页面</a>
           </div>`
        : '';

    const header = document.createElement('header');
    header.className = 'header';
    header.innerHTML = `
        <div class="header-left">
            <div class="header-logo"><span>zcbot</span></div>
            <nav class="header-nav">${navHtml}${pluginWebuiHtml}</nav>
        </div>
        <div class="header-right">
            <span class="header-user">
                <span class="uname">${admin ? admin.username : ''}</span>
                <span class="role-badge-mini" style="display:inline-block;padding:1px 8px;border-radius:8px;font-size:11px;margin-left:4px;${admin && admin.role === 'super' ? 'background:#eef2ff;color:#4f46e5;' : 'background:#fef3c7;color:#d97706;'}">${admin ? (admin.role === 'super' ? '超管' : '管理') : ''}</span>
            </span>
            <button class="btn-logout" onclick="doLogout()">退出</button>
        </div>
    `;
    document.body.prepend(header);

    // 渲染插件 WebUI 下拉菜单
    const trigger = header.querySelector('.plugin-webui-trigger');
    if (trigger) {
        renderPluginWebUIDropdown(trigger);
    }
}

async function doLogout() {
    await api('/api/logout', { method: 'POST' });
    clearToken();
    redirectToLogin();
}

/**
 * 页面初始化入口：鉴权 + 渲染头部
 * @param {string} activeKey - 当前页 nav key
 * @param {function} onReady - 鉴权通过后的回调，接收 admin 参数
 */
async function initPage(activeKey, onReady) {
    const admin = await checkAuth();
    if (!admin) return;
    // 先加载插件 WebUI 列表，使导航能渲染出「插件页面」下拉
    await loadPluginWebUIs();
    renderHeader(activeKey, admin);
    if (typeof onReady === 'function') {
        onReady(admin);
    }
}

/* ─────────── 工具函数 ─────────── */

function escapeHtml(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function formatDate(dt) {
    if (!dt) return '-';
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function timeAgo(dt) {
    if (!dt) return '-';
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    const diff = Math.abs(Date.now() - d.getTime());
    const sec = Math.floor(diff / 1000);
    if (sec < 60) return `${sec}秒前`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}分钟前`;
    const hr = Math.floor(min / 60);
    if (hr < 24) return `${hr}小时前`;
    const day = Math.floor(hr / 24);
    if (day < 30) return `${day}天前`;
    return formatDate(dt);
}

function confirmDialog(msg) {
    return confirm(msg);
}
