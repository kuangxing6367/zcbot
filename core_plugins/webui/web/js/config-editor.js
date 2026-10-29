// ZCBOT 配置编辑器前端逻辑（纯原生 JS，无框架依赖；CSP 允许同源脚本）
(function () {
  'use strict';

  function apiBase() {
    // 端口隔离模式下，/webui-config.js 已注入 window.__ZC_API_BASE__；否则同源
    return (window.__ZC_API_BASE__ && window.__ZC_API_BASE__ !== 'null') ? window.__ZC_API_BASE__ : '';
  }

  function fmtMsg(el, text, ok) {
    el.textContent = text;
    el.className = 'msg ' + (ok ? 'ok' : 'err');
  }

  function loadFramework() {
    fetch(apiBase() + '/api/config/yaml', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (j.code !== 0) { return; }
        var fw = (j.data && j.data.framework) || {};
        var bb = fw.blackbox || {}, cr = fw.crash || {}, cv = fw.conventions || {};
        document.getElementById('bb_enabled').checked = bb.enabled !== false;
        document.getElementById('bb_max_bytes').value = bb.max_bytes || 102400;
        document.getElementById('cr_enabled').checked = cr.enabled !== false;
        document.getElementById('cr_max_restarts').value = cr.max_restarts || 5;
        document.getElementById('cr_path').value = cr.path || 'error.log';
        document.getElementById('cr_alert_command').value = cr.alert_command || '';
        document.getElementById('cr_alert_webhook').value = cr.alert_webhook || '';
        document.getElementById('cv_command_prefix').value = cv.command_prefix || '/';
        document.getElementById('cv_event_workers').value = cv.event_workers || 0;
      })
      .catch(function (e) { console.error(e); });
  }

  function saveFramework() {
    var el = document.getElementById('fwMsg');
    var payload = {
      data: {
        blackbox: {
          enabled: document.getElementById('bb_enabled').checked,
          max_bytes: parseInt(document.getElementById('bb_max_bytes').value, 10) || 102400
        },
        crash: {
          enabled: document.getElementById('cr_enabled').checked,
          max_restarts: parseInt(document.getElementById('cr_max_restarts').value, 10) || 5,
          path: document.getElementById('cr_path').value || 'error.log',
          alert_command: document.getElementById('cr_alert_command').value || '',
          alert_webhook: document.getElementById('cr_alert_webhook').value || ''
        },
        conventions: {
          command_prefix: document.getElementById('cv_command_prefix').value || '/',
          event_workers: parseInt(document.getElementById('cv_event_workers').value, 10) || 0
        }
      }
    };
    fetch(apiBase() + '/api/config/yaml/framework', {
      method: 'PUT', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        fmtMsg(el, j.msg || ('code ' + j.code), j.code === 0);
      })
      .catch(function (e) { fmtMsg(el, '请求失败: ' + e, false); });
  }

  function loadPlugins() {
    fetch(apiBase() + '/api/config/core-plugins', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var tb = document.querySelector('#pluginTable tbody');
        tb.innerHTML = '';
        if (j.code !== 0) { return; }
        (j.data || []).forEach(function (p) {
          var tr = document.createElement('tr');
          tr.innerHTML = '<td>' + esc(p.name) + '</td><td>' + esc(p.desc || '') +
            '</td><td><span class="pill ' + (p.enabled ? 'on' : 'off') + '">' +
            (p.enabled ? '启用' : '禁用') + '</span></td>' +
            '<td><button class="btn ghost" data-name="' + esc(p.name) +
            '" data-en="' + (!p.enabled) + '">' + (p.enabled ? '禁用' : '启用') + '</button></td>';
          tb.appendChild(tr);
        });
        Array.prototype.forEach.call(tb.querySelectorAll('button[data-name]'), function (b) {
          b.addEventListener('click', function () {
            togglePlugin(b.getAttribute('data-name'), b.getAttribute('data-en') === 'true');
          });
        });
      })
      .catch(function (e) { console.error(e); });
  }

  function togglePlugin(name, enabled) {
    var el = document.getElementById('plMsg');
    fetch(apiBase() + '/api/config/core-plugins', {
      method: 'PUT', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, enabled: enabled })
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        fmtMsg(el, j.msg || ('code ' + j.code), j.code === 0);
        if (j.code === 0) { loadPlugins(); }
      })
      .catch(function (e) { fmtMsg(el, '请求失败: ' + e, false); });
  }

  function loadBB() {
    fetch(apiBase() + '/api/debug/blackbox?limit=200', { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        var tb = document.querySelector('#bbTable tbody');
        tb.innerHTML = '';
        if (j.code !== 0) { return; }
        var s = (j.data && j.data.stats) || {};
        document.getElementById('bbStat').textContent =
          '已用 ' + (s.used_bytes || 0) + '/' + (s.max_bytes || 0) + 'B，帧数 ' +
          (s.frames || 0) + '，丢弃 ' + (s.dropped || 0) + '，启用 ' + s.enabled;
        (j.data.frames || []).forEach(function (f) {
          var tr = document.createElement('tr');
          tr.innerHTML = '<td>' + esc(f.ts || '') + '</td><td>' + esc(f.stage || '') +
            '</td><td>' + esc(f.plugin || '') + '</td><td>' + esc(f.fn || '') +
            '</td><td>' + esc(f.msg || '') + '</td>';
          tb.appendChild(tr);
        });
      })
      .catch(function (e) { console.error(e); });
  }

  function clearBB() {
    var el = document.getElementById('bbMsg');
    fetch(apiBase() + '/api/debug/blackbox/clear', {
      method: 'POST', credentials: 'include'
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        fmtMsg(el, j.msg || ('code ' + j.code), j.code === 0);
        if (j.code === 0) { loadBB(); }
      })
      .catch(function (e) { fmtMsg(el, '请求失败: ' + e, false); });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    loadFramework();
    loadPlugins();
    document.getElementById('saveFramework').addEventListener('click', saveFramework);
    document.getElementById('reloadPlugins').addEventListener('click', loadPlugins);
    document.getElementById('loadBB').addEventListener('click', loadBB);
    document.getElementById('clearBB').addEventListener('click', clearBB);
  });
})();
