(function () {
  'use strict';

  var KINDS = {
    user: ['QQ 筛选', '逗号分隔的 QQ 号'],
    group: ['群筛选', '逗号分隔的群号'],
    plugin: ['插件', '插件名，如 echo'],
    exclude: ['排除插件', '插件名'],
    stop: ['终止', '到此结束节点链']
  };

  var state = { list: [], current: null, nodes: [] };

  function $(id) { return document.getElementById(id); }
  function status(text) { $('wf-status').textContent = text || ''; }

  function api(path, options) {
    return fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' }
    }, options || {})).then(function (r) {
      return r.json().then(function (body) {
        if (body.code !== 0) throw new Error(body.msg || '请求失败');
        return body.data;
      });
    });
  }

  function renderList() {
    var box = $('wf-list');
    box.innerHTML = '';
    state.list.forEach(function (item) {
      var el = document.createElement('div');
      el.className = 'wf-item' + (state.current && state.current.id === item.id ? ' active' : '');
      el.innerHTML = '<div>' + escapeHtml(item.name) + '</div>' +
        '<div class="wf-tag">' + (item.is_default ? '默认 · ' : '') +
        (item.enabled ? '启用' : '停用') + ' · ' + (item.nodes || []).length + ' 个节点</div>';
      el.addEventListener('click', function () { load(item.id); });
      box.appendChild(el);
    });
  }

  function escapeHtml(text) {
    return String(text == null ? '' : text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function renderNodes() {
    var box = $('wf-nodes');
    box.innerHTML = '';
    state.nodes.forEach(function (node, index) {
      var meta = KINDS[node.type] || [node.type, ''];
      var row = document.createElement('div');
      row.className = 'wf-node' + (node.type === 'stop' ? ' wf-stop' : '');
      var value = node.type === 'user' || node.type === 'group'
        ? ((node.config && node.config.values) || []).join(',')
        : ((node.config && node.config.plugin) || '');
      row.innerHTML = '<div class="wf-kind">' + (index + 1) + '. ' + meta[0] + '</div>' +
        '<input type="text" value="' + escapeHtml(value) + '" placeholder="' + meta[1] + '"' +
        (node.type === 'stop' ? ' disabled' : '') + '>' +
        '<button type="button">删除</button>';
      row.querySelector('input').addEventListener('input', function (e) {
        if (node.type === 'user' || node.type === 'group') {
          node.config = { values: e.target.value.split(/[,，\s]+/).filter(Boolean) };
        } else {
          node.config = { plugin: e.target.value.trim() };
        }
      });
      row.querySelector('button').addEventListener('click', function () {
        state.nodes.splice(index, 1);
        renderNodes();
      });
      box.appendChild(row);
    });
  }

  function load(id) {
    api('/api/workflows/' + id).then(function (wf) {
      state.current = wf;
      state.nodes = wf.nodes || [];
      $('wf-name').value = wf.name || '';
      $('wf-desc').value = wf.description || '';
      $('wf-enabled').checked = !!wf.enabled;
      $('wf-default').checked = !!wf.is_default;
      $('wf-users').value = (wf.user_ids || []).join('\n');
      $('wf-name-hint').textContent = '#' + wf.id;
      renderNodes();
      renderList();
      status('');
    }).catch(function (e) { status(e.message); });
  }

  function refresh() {
    return api('/api/workflows').then(function (list) {
      state.list = list;
      renderList();
    });
  }

  function collect() {
    return {
      name: $('wf-name').value.trim(),
      description: $('wf-desc').value.trim(),
      enabled: $('wf-enabled').checked,
      is_default: $('wf-default').checked,
      nodes: state.nodes.map(function (n, i) {
        return { id: 'n' + i, type: n.type, config: n.config || {}, name: KINDS[n.type][0] };
      }),
      user_ids: $('wf-users').value.split(/[\n,，\s]+/).filter(Boolean)
    };
  }

  function save() {
    var payload = collect();
    if (!payload.name) { status('请填写名称'); return; }
    var path = state.current ? '/api/workflows/' + state.current.id : '/api/workflows';
    api(path, { method: state.current ? 'PUT' : 'POST', body: JSON.stringify(payload) })
      .then(function () { status('已保存'); return refresh(); })
      .catch(function (e) { status(e.message); });
  }

  document.addEventListener('DOMContentLoaded', function () {
    refresh().catch(function (e) { status(e.message); });

    $('wf-new').addEventListener('click', function () {
      state.current = null;
      state.nodes = [];
      $('wf-name').value = '';
      $('wf-desc').value = '';
      $('wf-enabled').checked = true;
      $('wf-default').checked = false;
      $('wf-users').value = '';
      $('wf-name-hint').textContent = '';
      renderNodes();
      renderList();
    });

    document.querySelectorAll('[data-add]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var type = btn.getAttribute('data-add');
        var config = type === 'user' || type === 'group' ? { values: [] } :
          type === 'stop' ? {} : { plugin: '' };
        state.nodes.push({ type: type, config: config });
        renderNodes();
      });
    });

    $('wf-save').addEventListener('click', save);

    $('wf-preview').addEventListener('click', function () {
      api('/api/workflows/preview', {
        method: 'POST',
        body: JSON.stringify({
          workflow: collect(),
          user_id: $('wf-users').value.split(/[\n,，\s]+/).filter(Boolean)[0] || ''
        })
      }).then(function (data) {
        $('wf-preview-out').textContent = JSON.stringify(data, null, 2);
      }).catch(function (e) { status(e.message); });
    });

    $('wf-delete').addEventListener('click', function () {
      if (!state.current) { status('请先选择一个工作流'); return; }
      if (!window.confirm('确认删除工作流 #' + state.current.id + '？')) return;
      api('/api/workflows/' + state.current.id, { method: 'DELETE' })
        .then(function () {
          state.current = null;
          $('wf-new').click();
          status('已删除');
          return refresh();
        }).catch(function (e) { status(e.message); });
    });
  });
})();
