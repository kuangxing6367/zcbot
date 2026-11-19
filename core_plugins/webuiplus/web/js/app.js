/* WebUIplus 前端：配置驱动布局/别名、消息查看、Web 侧发消息 */
(function () {
  'use strict';

  var cfg = {
    theme: 'auto', glass: true, intro: true, display: 'card',
    aliases: {}, layout: ['time', 'sender', 'text', 'images']
  };
  var $ = function (id) { return document.getElementById(id); };

  function status(el, text) { el.textContent = text; }

  function api(path, options) {
    return fetch(path, Object.assign({ credentials: 'same-origin' }, options || {}))
      .then(function (r) { return r.json().then(function (j) { return { r: r, j: j }; }); });
  }

  function apply() {
    document.documentElement.setAttribute('data-theme', cfg.theme);
    document.body.classList.toggle('no-glass', !cfg.glass);
    document.body.classList.toggle('no-intro', !cfg.intro);
    $('messages').className = 'messages ' + (cfg.display === 'queue' ? 'queue' : 'card');
    $('toggle-display').textContent = cfg.display === 'queue' ? '切换为卡片' : '切换为队列';
    $('toggle-theme').textContent = {
      auto: '主题：跟随', light: '主题：浅色', dark: '主题：深色'
    }[cfg.theme] || '主题：跟随';
    $('toggle-glass').textContent = '磨砂：' + (cfg.glass ? '开' : '关');
    $('layout').value = (cfg.layout || []).join(',');
    $('aliases').value = Object.keys(cfg.aliases || {}).map(function (k) {
      return k + '=' + cfg.aliases[k];
    }).join('\n');
  }

  function save(patch, silent) {
    var next = Object.assign({}, cfg, patch || {});
    return api('/api/webuiplus/config', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(next)
    }).then(function (out) {
      if (out.j.code !== 0) { if (!silent) status($('settings-status'), out.j.data); return false; }
      cfg = out.j.data;
      apply();
      if (!silent) status($('settings-status'), '已保存');
      return true;
    }).catch(function (e) {
      if (!silent) status($('settings-status'), String(e));
      return false;
    });
  }

  function alias(id) {
    var key = String(id || '');
    return (cfg.aliases && cfg.aliases[key]) || key || '未知';
  }

  function fieldHtml(name, m) {
    if (name === 'time') {
      var d = new Date((m.at || 0) * 1000);
      return '<div class="meta">' + (isNaN(d.getTime()) ? '' : d.toLocaleString()) + '</div>';
    }
    if (name === 'sender') {
      return '<div class="meta">' + alias(m.user_id) +
        (m.nickname ? ' · ' + m.nickname : '') + '</div>';
    }
    if (name === 'group') {
      return m.group_id ? '<div class="meta">群 ' + alias(m.group_id) + '</div>' : '';
    }
    if (name === 'text') {
      return '<div class="body"></div>';
    }
    if (name === 'images') {
      return (m.images || []).map(function (img) {
        return img.path
          ? '<img loading="lazy" alt="图片" src="/api/webuiplus/media/' +
              encodeURIComponent(img.path) + '">'
          : '<div class="body">[图片引用] ' + String(img.ref || '').slice(0, 120) + '</div>';
      }).join('');
    }
    return '';
  }

  function render(list) {
    var box = $('messages');
    if (!list.length) { box.innerHTML = '<div class="empty">还没有消息</div>'; return; }
    box.innerHTML = '';
    list.forEach(function (m) {
      var card = document.createElement('div');
      card.className = 'card';
      var layout = cfg.layout || ['time', 'sender', 'text', 'images'];
      var html = layout.map(function (name) { return fieldHtml(name, m); }).join('');
      card.innerHTML = html;
      var body = card.querySelector('.body');
      if (body) { body.textContent = m.text || ''; }
      box.appendChild(card);
    });
  }

  function load() {
    return api('/api/webuiplus/messages?limit=50').then(function (out) {
      if (out.j.code === 0) { render(out.j.data); }
    }).catch(function () { render([]); });
  }

  function parseAliases(text) {
    var map = {};
    String(text || '').split(/\r?\n/).forEach(function (line) {
      var i = line.indexOf('=');
      if (i > 0) { map[line.slice(0, i).trim()] = line.slice(i + 1).trim(); }
    });
    return map;
  }

  document.addEventListener('DOMContentLoaded', function () {
    api('/api/webuiplus/config').then(function (out) {
      if (out.j.code === 0) { cfg = out.j.data; }
      apply(); return load();
    }).catch(apply);

    $('toggle-display').onclick = function () {
      save({ display: cfg.display === 'card' ? 'queue' : 'card' }, true);
    };
    $('toggle-theme').onclick = function () {
      var next = { auto: 'light', light: 'dark', dark: 'auto' }[cfg.theme] || 'auto';
      save({ theme: next }, true);
    };
    $('toggle-glass').onclick = function () { save({ glass: !cfg.glass }, true); };
    $('save-settings').onclick = function () {
      save({
        layout: $('layout').value.split(',').map(function (s) { return s.trim(); }).filter(Boolean),
        aliases: parseAliases($('aliases').value)
      });
    };
    $('send').onclick = function () {
      var target = $('target').value.trim();
      var message = $('message').value.trim();
      if (!target || !message) { status($('send-status'), '请填写目标与消息'); return; }
      var payload = { message: message };
      if (target.indexOf('g:') === 0) { payload.group_id = target.slice(2); }
      else { payload.user_id = target; }
      status($('send-status'), '发送中...');
      api('/api/webuiplus/send', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(function (out) {
        status($('send-status'), out.j.code === 0 ? '已发送' : out.j.data);
        if (out.j.code === 0) { $('message').value = ''; }
      }).catch(function (e) { status($('send-status'), String(e)); });
    };
    setInterval(load, 15000);
  });
})();
