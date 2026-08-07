/**
 * views.js - ZCBOT 管理面板视图模块
 * 每个视图: async function Views[key](container)
 */
const Views = {};

/* ═══════════════ 仪表盘 ═══════════════ */
Views.dashboard = async function (view) {
  const [d, cards] = await Promise.all([
    api('/api/dashboard'),
    api('/api/dashboard/cards'),
  ]);
  const data = (d && d.data) || {};
  const pluginCards = (cards && cards.data) || [];

  let botsHtml = '';
  (data.bots || []).forEach(b => {
    botsHtml += `<span class="badge ok">${escapeHtml(b)}</span>`;
  });

  const stats = [
    { label: '活跃插件', value: data.plugins_active ?? 0, icon: '◫' },
    { label: '插件总数', value: data.plugins_total ?? 0, icon: '▦' },
    { label: '命令数', value: data.commands_total ?? 0, icon: '⌘' },
    { label: '动态命令', value: data.dynamic_commands ?? 0, icon: '☰' },
    { label: '用户数', value: data.users_total ?? 0, icon: '☺' },
    { label: '活跃群', value: data.groups_active ?? 0, icon: '▤' },
    { label: '定时任务', value: data.tasks_active ?? 0, icon: '◷' },
    { label: '在线 Bot', value: (data.bots || []).length, icon: '⚡' },
  ];

  view.innerHTML = `
    <div class="grid cols-4">
      ${stats.map(s => `
        <div class="stat-card">
          <div class="head"><span class="stat-label">${s.label}</span><span class="stat-icon">${s.icon}</span></div>
          <div class="stat-value">${s.value}</div>
        </div>`).join('')}
    </div>

    <div class="grid cols-2 mt">
      <div class="card">
        <div class="card-title">OneBot 连接 <span class="small muted">WS :${data.ws_port ?? '-'}</span></div>
        ${botsHtml || '<div class="empty">暂无在线客户端</div>'}
      </div>
      <div class="card">
        <div class="card-title">框架信息 ${data.framework_alpha ? '<span class="badge warn">Alpha 预览版</span>' : ''}</div>
        <div class="flex between" style="padding:4px 0"><span class="muted">名称</span><span>${escapeHtml(data.framework_name || '-')}</span></div>
        <div class="flex between" style="padding:4px 0"><span class="muted">版本</span><span>${escapeHtml(data.framework_version || '-')}</span></div>
        <div class="flex between" style="padding:4px 0"><span class="muted">仓库</span><a href="${escapeHtml(data.github_repo || '#')}" target="_blank">${escapeHtml((data.github_repo || '').replace('https://', ''))}</a></div>
        ${data.framework_alpha ? '<div class="small dim mt">Alpha 预览版：功能可能变更，仅供测试，生产环境谨慎使用。</div>' : ''}
      </div>
    </div>

    <div class="card mt">
      <div class="card-title">插件卡片 <button class="btn sm" onclick="Views.dashboard(document.getElementById('view'))">刷新</button></div>
      ${pluginCards.length ? `
        <div class="grid cols-3">
          ${pluginCards.map(c => `
            <div class="stat-card">
              <div class="head"><span class="stat-label">${escapeHtml(c.title || '')}</span><span>${escapeHtml(c.icon || '')}</span></div>
              <div class="stat-value" style="font-size:17px">${escapeHtml((c.data && (c.data.value ?? '')) ?? '')}</div>
              <div class="stat-label">${escapeHtml((c.data && c.data.label) || '')}</div>
              <div class="small dim">来自插件 ${escapeHtml(c.plugin_name)}</div>
            </div>`).join('')}
        </div>` : '<div class="empty">暂无插件卡片</div>'}
    </div>`;
};

/* ═══════════════ 插件管理 ═══════════════ */
Views.plugins = async function (view) {
  const res = await api('/api/plugins');
  const plugins = (res && res.data) || [];

  view.innerHTML = `
    <div class="flex between mb">
      <div class="flex">
        <button class="btn" onclick="Views.plugins(document.getElementById('view'))">⟳ 刷新</button>
        <button class="btn primary" onclick="Views.uploadPluginModal()">⬆ 上传插件</button>
      </div>
      <span class="muted small">共 ${plugins.length} 个插件</span>
    </div>
    <div class="grid cols-3" id="pluginGrid">
      ${plugins.map(p => `
        <div class="plugin-card">
          <div class="plugin-head">
            <div>
              <div class="plugin-name">${escapeHtml(p.plugin_name)}</div>
              <div class="plugin-ver">v${escapeHtml(p.version || '0.0.0')} · ${statusBadge(p.status)}</div>
            </div>
            ${p.is_loaded ? '<span class="badge ok">已加载</span>' : '<span class="badge muted">未加载</span>'}
          </div>
          <div class="plugin-desc">${escapeHtml(p.description || '暂无描述')}</div>
          <div class="flex wrap small" style="color:var(--text-muted)">
            ${p.has_missing_deps ? `<span class="badge err">缺依赖 ${p.missing_deps.length}</span>` : ''}
            ${p.has_conflict ? '<span class="badge warn">版本冲突</span>' : ''}
            <span class="badge muted">${p.command_count || 0} 命令</span>
            <span class="badge muted">${p.task_count || 0} 任务</span>
          </div>
          <div class="plugin-foot">
            <button class="btn sm" onclick="Views.pluginDetail('${p.plugin_name}')">详情</button>
            <button class="btn sm" onclick="Views.pluginCommands('${p.plugin_name}')">命令</button>
            <button class="btn sm" onclick="Views.pluginConfigSchema('${p.plugin_name}')">配置</button>
            <button class="btn sm" onclick="Views.pluginGroups('${p.plugin_name}')">群开关</button>
            ${p.has_yaml && p.github_repo ? `<button class="btn sm warn" onclick="Views.pluginUpdate('${p.plugin_name}')">更新</button>` : ''}
            <button class="btn sm" onclick="Views.reloadPlugin('${p.plugin_name}')">重载</button>
            <button class="btn sm ${p.is_active ? 'danger' : 'success'}" onclick="Views.togglePlugin('${p.plugin_name}', ${p.is_active ? 0 : 1})">${p.is_active ? '禁用' : '启用'}</button>
            <button class="btn sm danger" onclick="Views.deletePlugin('${p.plugin_name}')">删除</button>
          </div>
        </div>`).join('') || '<div class="empty">暂无插件，点击右上角「上传插件」安装</div>'}
    </div>`;
};

Views.uploadPluginModal = function () {
  openModal(`
    <div class="modal-head"><span>上传插件 (ZIP)</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="form-row"><label>插件包</label><input type="file" class="input" id="upFile" accept=".zip"></div>
      <div class="small muted">ZIP 内需包含 main.py；配置文件将自动迁移到 plugins_dat 目录。</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" id="upBtn" onclick="Views.doUpload()">上传并加载</button>
    </div>`);
};

Views.doUpload = async function () {
  const file = document.getElementById('upFile').files[0];
  if (!file) { toast('请选择 ZIP 文件', 'warning'); return; }
  const btn = document.getElementById('upBtn');
  btn.disabled = true; btn.textContent = '上传中...';
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res = await api('/api/plugins/upload', { method: 'POST', body: fd });
    if (res && res.code === 0) { toast(res.msg, 'success'); closeModal(); Views.plugins(document.getElementById('view')); }
    else toast((res && res.msg) || '上传失败', 'error');
  } finally { btn.disabled = false; }
};

Views.pluginDetail = async function (name) {
  const res = await api(`/api/plugins/${name}/config`);
  if (!res || res.code !== 0) { toast((res && res.msg) || '获取失败', 'error'); return; }
  const d = res.data;
  const yaml = d.yaml || {};
  const github = yaml.github || {};
  const deps = d.dep_status || {};
  const files = (d.config_files || []).map(f => `<div class="small">${escapeHtml(f.name)} (${f.size}B)</div>`).join('') || '<div class="small dim">无配置文件</div>';

  openModal(`
    <div class="modal-head"><span>插件详情 · ${escapeHtml(name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="flex wrap mb">
        ${d.has_yaml && github.repo ? `<button class="btn sm warn" onclick="Views.pluginUpdate('${name}')">GitHub 更新</button>` : ''}
        <button class="btn sm" onclick="Views.pluginReadme('${name}')">README</button>
        <button class="btn sm" onclick="Views.installDeps('${name}')">安装依赖(全局)</button>
        <button class="btn sm" onclick="Views.createVenv('${name}')">创建虚拟环境</button>
      </div>
      ${deps.has_missing ? `<div class="badge err mb" style="margin-bottom:8px">缺失依赖: ${escapeHtml(deps.missing.join(', '))}</div>` : ''}
      ${deps.has_conflict ? `<div class="badge warn" style="margin-bottom:8px">版本冲突(已自动跳过): ${escapeHtml((deps.conflicts || []).map(c => c.name + ' ' + c.required + ' (已装 ' + c.installed + ')').join('; '))}</div>` : ''}
      ${github.repo ? `
        <div class="form-row"><label>GitHub 仓库</label><div class="mono small">${escapeHtml(github.repo)}@${escapeHtml(github.branch || 'main')}</div></div>` : ''}
      <div class="form-row"><label>依赖 (Python)</label><div class="small">${escapeHtml((d.dependencies && d.dependencies.python || []).join(', ') || '无')}</div></div>
      <div class="form-row"><label>配置文件</label><div>${files}</div></div>
      ${(d.config_files || []).length ? `<button class="btn sm" onclick="Views.pluginFiles('${name}')">查看/编辑配置文件</button>` : ''}
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views.pluginReadme = async function (name) {
  const res = await api(`/api/plugins/${name}/readme`);
  if (!res || res.code !== 0) { toast((res && res.msg) || '无 README', 'warning'); return; }
  openModal(`
    <div class="modal-head"><span>README · ${escapeHtml(name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body"><pre class="code">${escapeHtml(res.data.content)}</pre></div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views.pluginFiles = async function (name) {
  const res = await api(`/api/plugins/${name}/config`);
  if (!res || res.code !== 0) return;
  const files = (res.data.config_files || []).map(f => f.name);
  if (!files.length) { toast('无配置文件', 'warning'); return; }
  openModal(`
    <div class="modal-head"><span>配置文件 · ${escapeHtml(name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <select class="select mb" id="cfgFileSel">${files.map(f => `<option>${escapeHtml(f)}</option>`).join('')}</select>
      <textarea class="textarea" id="cfgFileContent" style="min-height:320px" readonly></textarea>
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
  await Views.loadPluginFile(name, files[0]);
  document.getElementById('cfgFileSel').addEventListener('change', e => Views.loadPluginFile(name, e.target.value));
};

Views.loadPluginFile = async function (name, filename) {
  const res = await api(`/api/plugins/${name}/file/${encodeURIComponent(filename)}`);
  const el = document.getElementById('cfgFileContent');
  if (el) el.value = (res && res.data && res.data.content) || '';
};

Views.pluginCommands = async function (name) {
  const res = await api(`/api/plugins/${name}/commands`);
  if (!res || res.code !== 0) { toast('获取失败', 'error'); return; }
  const d = res.data;
  const cmds = [...(d.static_commands || []), ...(d.dynamic_commands || [])];
  const tasks = d.tasks || [];
  openModal(`
    <div class="modal-head"><span>命令与任务 · ${escapeHtml(name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="card-title" style="margin-bottom:8px">命令 (${cmds.length})</div>
      <div class="table-wrap">
      <table class="tbl">
        <tr><th>命令</th><th>描述</th><th>权限</th><th>命中</th></tr>
        ${cmds.map(c => `<tr>
          <td class="mono">${escapeHtml(c.pattern)}${c.alias ? `<div class="dim small">别名: ${escapeHtml(c.alias)}</div>` : ''}</td>
          <td>${escapeHtml(c.description || '-')}</td>
          <td>${c.require_level === 'super' ? '<span class="badge warn">超管</span>' : c.require_level === 'admin' ? '<span class="badge info">管理</span>' : '-'}</td>
          <td>${c.hit_count || 0}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">无命令</td></tr>'}
      </table></div>
      <div class="card-title mt" style="margin-bottom:8px">定时任务 (${tasks.length})</div>
      <div class="table-wrap">
      <table class="tbl">
        <tr><th>Cron</th><th>描述</th><th>状态</th></tr>
        ${tasks.map(t => `<tr>
          <td class="mono">${escapeHtml(t.cron_expression)}</td>
          <td>${escapeHtml(t.description || '-')}</td>
          <td>${badgeHtml(t.is_active)}</td></tr>`).join('') || '<tr><td colspan="3" class="empty">无定时任务</td></tr>'}
      </table></div>
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views.pluginConfigSchema = async function (name) {
  const res = await api(`/api/plugins/${name}/config_schema`);
  if (!res || res.code !== 0) { toast((res && res.msg) || '获取失败', 'error'); return; }
  const schema = res.data.schema || {};
  const values = res.data.values || {};
  const keys = Object.keys(schema);
  if (!keys.length) { toast('该插件没有可配置项', 'warning'); return; }

  const fieldHtml = (key) => {
    const spec = schema[key] || {};
    const type = spec.type || 'string';
    const val = values[key] !== undefined ? values[key] : spec.default;
    const desc = spec.description ? `<div class="small dim">${escapeHtml(spec.description)}</div>` : '';
    if (type === 'bool' || type === 'boolean') {
      const checked = val ? 'checked' : '';
      return `<div class="form-row"><label>${escapeHtml(key)}</label>
        <label class="flex"><input type="checkbox" id="cfg_${key}" ${checked} class="cfg-bool" data-key="${key}"> ${escapeHtml(spec.description || '')}</label></div>`;
    }
    if (type === 'int' || type === 'float') {
      return `<div class="form-row"><label>${escapeHtml(key)}</label><input class="input" id="cfg_${key}" type="number" value="${escapeHtml(val ?? '')}">${desc}</div>`;
    }
    if (Array.isArray(val)) {
      return `<div class="form-row"><label>${escapeHtml(key)}</label><input class="input" id="cfg_${key}" value="${escapeHtml((val || []).join(','))}"><div class="small dim">${escapeHtml(spec.description || '')}（逗号分隔）</div></div>`;
    }
    return `<div class="form-row"><label>${escapeHtml(key)}</label><input class="input" id="cfg_${key}" value="${escapeHtml(val ?? '')}">${desc}</div>`;
  };

  openModal(`
    <div class="modal-head"><span>插件配置 · ${escapeHtml(name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      ${keys.map(fieldHtml).join('')}
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views.savePluginConfig('${name}')">保存</button>
    </div>`);
};

Views.savePluginConfig = async function (name) {
  const res = await api(`/api/plugins/${name}/config_schema`);
  if (!res || res.code !== 0) return;
  const schema = res.data.schema || {};
  const body = {};
  for (const key of Object.keys(schema)) {
    const type = schema[key].type || 'string';
    const el = document.getElementById('cfg_' + key);
    if (!el) continue;
    if (type === 'bool' || type === 'boolean') body[key] = el.checked;
    else if (type === 'int') body[key] = parseInt(el.value, 10);
    else if (type === 'float') body[key] = parseFloat(el.value);
    else body[key] = el.value;
  }
  const r = await api(`/api/plugins/${name}/config_schema`, { method: 'PUT', body: JSON.stringify(body) });
  if (r && r.code === 0) { toast(r.msg, 'success'); closeModal(); }
  else toast((r && r.msg) || '保存失败', 'error');
};

Views.pluginGroups = async function (name) {
  const res = await api('/api/plugins/group-settings');
  if (!res || res.code !== 0) return;
  const settings = res.data || [];
  // 收集该插件在所有群的状态
  const rows = settings.filter(s => s.plugin_name === name);
  openModal(`
    <div class="modal-head"><span>群级开关 · ${escapeHtml(name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="table-wrap"><table class="tbl">
        <tr><th>群号</th><th>状态</th><th>操作</th></tr>
        ${rows.map(r => `<tr>
          <td class="mono">${r.group_id}</td>
          <td>${badgeHtml(!!r.enabled)}</td>
          <td><button class="btn sm ${r.enabled ? 'danger' : 'success'}" onclick="Views.toggleGroupPlugin('${name}', ${r.group_id}, ${r.enabled ? 0 : 1})">${r.enabled ? '禁用' : '启用'}</button></td></tr>`).join('') || '<tr><td colspan="3" class="empty">该插件在所有群均为默认启用</td></tr>'}
      </table></div>
      <div class="small dim mt">表中无记录 = 默认启用；仅在群内禁用过的插件会出现在此。</div>
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views.toggleGroupPlugin = async function (name, groupId, enabled) {
  const r = await api(`/api/plugins/${name}/group/${groupId}/toggle`, {
    method: 'POST', body: JSON.stringify({ enabled: !!enabled }),
  });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.pluginGroups(name); }
  else toast((r && r.msg) || '操作失败', 'error');
};

Views.reloadPlugin = async function (name) {
  if (!await confirmDialog(`确定重新加载插件 [${name}] 吗？`)) return;
  const r = await api(`/api/plugins/${name}/reload`, { method: 'POST' });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.plugins(document.getElementById('view')); }
  else toast((r && r.msg) || '重载失败', 'error');
};

Views.togglePlugin = async function (name, active) {
  const r = await api(`/api/plugins/${name}/toggle`, { method: 'POST', body: JSON.stringify({ is_active: !!active }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.plugins(document.getElementById('view')); }
  else toast((r && r.msg) || '操作失败', 'error');
};

Views.deletePlugin = async function (name) {
  // 带选项的确认框：默认保留插件数据，可勾选一并删除
  const mask = openModal(`
    <div class="modal-head"><span>删除插件</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div style="margin-bottom:10px">确定删除插件 <b>${escapeHtml(name)}</b> 吗？<br>（代码与虚拟环境将删除，且不可恢复）</div>
      <label class="form-row" style="align-items:center">
        <input type="checkbox" id="delDataChk" style="width:auto">
        <span class="ml">同时删除插件数据/配置文件（plugins_dat）</span>
      </label>
      <div class="small dim mt">不勾选则保留插件配置，便于以后重新安装时复用。</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn danger" onclick="Views.confirmDeletePlugin('${name}')">删除</button>
    </div>`);
};

Views.confirmDeletePlugin = async function (name) {
  const deleteData = !!(document.getElementById('delDataChk') && document.getElementById('delDataChk').checked);
  const r = await api(`/api/plugins/${name}`, {
    method: 'DELETE',
    body: JSON.stringify({ delete_data: deleteData }),
  });
  closeModal();
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.plugins(document.getElementById('view')); }
  else toast((r && r.msg) || '删除失败', 'error');
};

Views.installDeps = async function (name) {
  if (!await confirmDialog(`确定将 [${name}] 的缺失依赖安装到全局环境吗？\n（版本冲突的依赖会自动跳过，不覆盖全局包）`)) return;
  const r = await api(`/api/plugins/${name}/install_deps`, { method: 'POST' });
  if (r && r.code === 0) { toast(r.msg, 'success'); }
  else toast((r && r.msg) || '安装失败', 'error');
};

Views.createVenv = async function (name) {
  if (!await confirmDialog(`为 [${name}] 创建虚拟环境？\n将创建在 插件数据目录(plugins_dat) 下，并在其中安装依赖（占用磁盘较大）。`)) return;
  const r = await api(`/api/plugins/${name}/create_isolated_env`, { method: 'POST' });
  if (r && r.code === 0) { toast(r.msg, 'success'); }
  else toast((r && r.msg) || '创建失败', 'error');
};

Views.pluginUpdate = async function (name) {
  const check = await api(`/api/plugins/${name}/check_update`);
  if (!check || check.code !== 0) { toast((check && check.msg) || '检查更新失败', 'error'); return; }
  const d = check.data;
  if (!await confirmDialog(
    `插件 [${name}]\n当前版本: ${d.current_version}\n最新提交: ${d.latest_commit} - ${d.commit_message}\n\n确定从 GitHub 拉取更新吗？`, 'GitHub 更新')) return;
  const r = await api(`/api/plugins/${name}/update`, { method: 'POST' });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.plugins(document.getElementById('view')); }
  else toast((r && r.msg) || '更新失败', 'error');
};

/* ═══════════════ 命令管理 ═══════════════ */
Views.commands = async function (view) {
  const [res, dyn] = await Promise.all([api('/api/commands'), api('/api/commands/dynamic')]);
  const cmds = (res && res.data) || [];
  const dynCmds = (dyn && dyn.data) || [];

  view.innerHTML = `
    <div class="card">
      <div class="card-title">静态命令 (${cmds.length}) <button class="btn sm" onclick="Views.commands(document.getElementById('view'))">⟳ 刷新</button></div>
      <div class="table-wrap"><table class="tbl">
        <tr><th>插件</th><th>命令</th><th>别名</th><th>描述</th><th>权限</th><th>命中</th><th>状态</th><th>操作</th></tr>
        ${cmds.map(c => `<tr>
          <td class="small">${escapeHtml(c.plugin_name)}</td>
          <td class="mono">${escapeHtml(c.pattern)}</td>
          <td class="small">${escapeHtml(c.alias || '-')}</td>
          <td class="small">${escapeHtml(c.description || '-')}</td>
          <td>${c.require_level === 'super' ? '<span class="badge warn">超管</span>' : c.require_level === 'admin' ? '<span class="badge info">管理</span>' : '-'}</td>
          <td>${c.hit_count || 0}</td>
          <td>${badgeHtml(!!c.is_active)}</td>
          <td class="actions">
            <button class="btn sm" onclick="Views.editCommand(${c.id})">编辑</button>
            <button class="btn sm ${c.is_active ? 'danger' : 'success'}" onclick="Views.toggleCommand(${c.id}, ${c.is_active ? 0 : 1})">${c.is_active ? '禁用' : '启用'}</button>
          </td></tr>`).join('') || '<tr><td colspan="8" class="empty">暂无命令</td></tr>'}
      </table></div>
    </div>
    <div class="card">
      <div class="card-title">动态命令 (${dynCmds.length})</div>
      <div class="small muted mb">动态命令由插件注册，仅作展示，不参与路由匹配。</div>
      <div class="table-wrap"><table class="tbl">
        <tr><th>插件</th><th>命令</th><th>描述</th><th>命中</th></tr>
        ${dynCmds.map(c => `<tr>
          <td class="small">${escapeHtml(c.plugin_name)}</td>
          <td class="mono">${escapeHtml(c.pattern)}</td>
          <td class="small">${escapeHtml(c.description || '-')}</td>
          <td>${c.hit_count || 0}</td></tr>`).join('') || '<tr><td colspan="4" class="empty">暂无动态命令</td></tr>'}
      </table></div>
    </div>`;
};

Views.editCommand = function (id) {
  openModal(`
    <div class="modal-head"><span>编辑命令 #${id}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="form-row"><label>别名</label><input class="input" id="cmdAlias" placeholder="逗号分隔，如 /help,/h"></div>
      <div class="form-row"><label>描述</label><input class="input" id="cmdDesc"></div>
      <div class="form-row"><label>权限要求</label>
        <select class="select" id="cmdLevel"><option value="">普通</option><option value="admin">管理员/群主</option><option value="super">超级管理员</option></select>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views.saveCommand(${id})">保存</button>
    </div>`);
};

Views.saveCommand = async function (id) {
  const body = JSON.stringify({
    alias: document.getElementById('cmdAlias').value.trim(),
    description: document.getElementById('cmdDesc').value.trim(),
    require_level: document.getElementById('cmdLevel').value,
  });
  const r = await api(`/api/commands/${id}/alias`, { method: 'PUT', body });
  if (r && r.code === 0) { toast(r.msg, 'success'); closeModal(); Views.commands(document.getElementById('view')); }
  else toast((r && r.msg) || '保存失败', 'error');
};

Views.toggleCommand = async function (id, active) {
  const r = await api(`/api/commands/${id}/toggle`, { method: 'POST', body: JSON.stringify({ is_active: !!active }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.commands(document.getElementById('view')); }
  else toast((r && r.msg) || '操作失败', 'error');
};

/* ═══════════════ 用户管理 ═══════════════ */
let _userPage = 1, _userKeyword = '';

Views.users = async function (view) {
  const url = `/api/users?page=${_userPage}&size=20&keyword=${encodeURIComponent(_userKeyword)}`;
  const res = await api(url);
  const data = (res && res.data) || [];
  const total = (res && res.total) || 0;
  const pages = Math.max(1, Math.ceil(total / 20));

  view.innerHTML = `
    <div class="card">
      <div class="card-title">
        用户管理 (共 ${total} 人)
        <div class="flex">
          <input class="input" id="userSearch" style="width:220px" placeholder="搜索昵称/QQ号/备注" value="${escapeHtml(_userKeyword)}">
          <button class="btn" onclick="Views.userSearch()">搜索</button>
        </div>
      </div>
      <div class="table-wrap"><table class="tbl">
        <tr><th>QQ号</th><th>昵称</th><th>角色</th><th>状态</th><th>最后活跃</th><th>操作</th></tr>
        ${data.map(u => `<tr>
          <td class="mono">${u.user_id}</td>
          <td>${escapeHtml(u.nickname || '-')}</td>
          <td>${u.role === 'super' ? '<span class="badge warn">超管</span>' : '<span class="badge muted">普通</span>'}</td>
          <td>${u.is_blacklist ? '<span class="badge err">黑名单</span>' : '<span class="badge ok">正常</span>'}</td>
          <td class="small">${timeAgo(u.last_active_at)}</td>
          <td class="actions">
            <button class="btn sm ${u.role === 'super' ? 'danger' : 'warn'}" onclick="Views.setUserRole(${u.user_id}, '${u.role === 'super' ? '' : 'super'}')">${u.role === 'super' ? '取消超管' : '设为超管'}</button>
            <button class="btn sm ${u.is_blacklist ? 'success' : 'danger'}" onclick="Views.toggleBlacklist(${u.user_id}, ${u.is_blacklist ? 0 : 1})">${u.is_blacklist ? '移出黑名单' : '拉黑'}</button>
          </td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无用户（用户发消息后自动注册）</td></tr>'}
      </table></div>
      <div class="pager">
        <button class="btn sm" onclick="Views.userPage(${_userPage - 1})" ${_userPage <= 1 ? 'disabled' : ''}>上一页</button>
        <span class="small muted">${_userPage} / ${pages}</span>
        <button class="btn sm" onclick="Views.userPage(${_userPage + 1})" ${_userPage >= pages ? 'disabled' : ''}>下一页</button>
      </div>
    </div>`;
};

Views.userSearch = function () {
  _userKeyword = document.getElementById('userSearch').value.trim();
  _userPage = 1;
  Views.users(document.getElementById('view'));
};

Views.userPage = function (p) {
  if (p < 1) return;
  _userPage = p;
  Views.users(document.getElementById('view'));
};

Views.setUserRole = async function (uid, role) {
  const r = await api(`/api/users/${uid}/role`, { method: 'PUT', body: JSON.stringify({ role }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.users(document.getElementById('view')); }
  else toast((r && r.msg) || '操作失败', 'error');
};

Views.toggleBlacklist = async function (uid, bl) {
  const r = await api(`/api/users/${uid}/blacklist`, { method: 'POST', body: JSON.stringify({ is_blacklist: !!bl }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.users(document.getElementById('view')); }
  else toast((r && r.msg) || '操作失败', 'error');
};

/* ═══════════════ 群组管理 ═══════════════ */
Views.groups = async function (view) {
  const res = await api('/api/groups');
  const groups = (res && res.data) || [];

  view.innerHTML = `
    <div class="card">
      <div class="card-title">群组管理 (${groups.length}) <button class="btn sm" onclick="Views.groups(document.getElementById('view'))">⟳ 刷新</button></div>
      <div class="table-wrap"><table class="tbl">
        <tr><th>群号</th><th>群名称</th><th>成员数</th><th>状态</th><th>入群时间</th><th>操作</th></tr>
        ${groups.map(g => `<tr>
          <td class="mono">${g.group_id}</td>
          <td>${escapeHtml(g.group_name || '-')}</td>
          <td>${g.member_count || 0}</td>
          <td>${g.is_blacklist ? '<span class="badge err">黑名单</span>' : g.is_active ? '<span class="badge ok">活跃</span>' : '<span class="badge muted">已退群</span>'}</td>
          <td class="small">${fmtTime(g.join_at)}</td>
          <td><button class="btn sm ${g.is_blacklist ? 'success' : 'danger'}" onclick="Views.toggleGroupBlacklist(${g.group_id}, ${g.is_blacklist ? 0 : 1})">${g.is_blacklist ? '移出黑名单' : '拉黑'}</button></td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无群组</td></tr>'}
      </table></div>
    </div>`;
};

Views.toggleGroupBlacklist = async function (gid, bl) {
  const r = await api(`/api/groups/${gid}/blacklist`, { method: 'POST', body: JSON.stringify({ is_blacklist: !!bl }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.groups(document.getElementById('view')); }
  else toast((r && r.msg) || '操作失败', 'error');
};

/* ═══════════════ 定时任务 ═══════════════ */
Views.tasks = async function (view) {
  const res = await api('/api/tasks');
  const tasks = (res && res.data) || [];

  view.innerHTML = `
    <div class="card">
      <div class="card-title">定时任务 (${tasks.length})
        <div class="flex">
          <button class="btn sm" onclick="Views.tasks(document.getElementById('view'))">⟳ 刷新</button>
          <button class="btn sm primary" onclick="Views.createTaskModal()">＋ 新建任务</button>
        </div>
      </div>
      <div class="table-wrap"><table class="tbl">
        <tr><th>#</th><th>来源</th><th>Cron</th><th>描述</th><th>上次执行</th><th>次数</th><th>状态</th><th>操作</th></tr>
        ${tasks.map(t => `<tr>
          <td>${t.id}</td>
          <td class="small">${escapeHtml(t.plugin_name)}</td>
          <td class="mono">${escapeHtml(t.cron_expression)}</td>
          <td class="small">${escapeHtml(t.description || '-')}</td>
          <td class="small">${t.last_run_at ? fmtTime(t.last_run_at) : '-'}</td>
          <td>${t.run_count || 0}</td>
          <td>${t.last_status === 'success' ? '<span class="badge ok">成功</span>' : t.last_status === 'error' || t.last_status === 'failure' ? '<span class="badge err">失败</span>' : '<span class="badge muted">-</span>'}</td>
          <td class="actions">
            <button class="btn sm" onclick="Views.triggerTask(${t.id})">立即执行</button>
            <button class="btn sm ${t.is_active ? 'warn' : 'success'}" onclick="Views.toggleTask(${t.id}, ${t.is_active ? 0 : 1})">${t.is_active ? '暂停' : '启用'}</button>
            ${t.plugin_name === '__web__' ? `<button class="btn sm danger" onclick="Views.deleteTask(${t.id})">删除</button>` : ''}
          </td></tr>`).join('') || '<tr><td colspan="8" class="empty">暂无任务</td></tr>'}
      </table></div>
      <div class="small dim mt">插件注册的任务无法在此删除，请到插件管理页处理。</div>
    </div>`;
};

Views.createTaskModal = function () {
  openModal(`
    <div class="modal-head"><span>新建定时任务</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="form-row"><label>Cron 表达式</label><input class="input mono" id="taskCron" placeholder="分 时 日 月 周，如 0 8 * * *"></div>
      <div class="form-row"><label>描述</label><input class="input" id="taskDesc" placeholder="任务描述"></div>
      <div class="small dim">框架仅支持 5 字段 cron（分 时 日 月 周），任务处理器由系统内置。</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views.createTask()">创建</button>
    </div>`);
};

Views.createTask = async function () {
  const body = JSON.stringify({
    cron_expression: document.getElementById('taskCron').value.trim(),
    description: document.getElementById('taskDesc').value.trim(),
  });
  const r = await api('/api/tasks', { method: 'POST', body });
  if (r && r.code === 0) { toast(r.msg, 'success'); closeModal(); Views.tasks(document.getElementById('view')); }
  else toast((r && r.msg) || '创建失败', 'error');
};

Views.toggleTask = async function (id, active) {
  const r = await api(`/api/tasks/${id}/toggle`, { method: 'POST', body: JSON.stringify({ is_active: !!active }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.tasks(document.getElementById('view')); }
  else toast((r && r.msg) || '操作失败', 'error');
};

Views.triggerTask = async function (id) {
  if (!await confirmDialog('确定立即执行该任务吗？')) return;
  const r = await api(`/api/tasks/${id}/trigger`, { method: 'POST' });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.tasks(document.getElementById('view')); }
  else toast((r && r.msg) || '执行失败', 'error');
};

Views.deleteTask = async function (id) {
  if (!await confirmDialog('确定删除该任务吗？')) return;
  const r = await api(`/api/tasks/${id}`, { method: 'DELETE' });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.tasks(document.getElementById('view')); }
  else toast((r && r.msg) || '删除失败', 'error');
};

/* ═══════════════ 日志中心 ═══════════════ */
let _logCategory = '', _logLevel = '', _logKeyword = '', _logAuto = true, _logLastSeq = 0, _logPollTimer = null;

Views.logs = async function (view) {
  _logCategory = ''; _logLevel = ''; _logKeyword = ''; _logAuto = true;

  view.innerHTML = `
    <div class="card">
      <div class="log-toolbar">
        <select class="select" id="logCategory" style="width:130px">
          <option value="">全部分类</option>
          <option value="message">消息</option><option value="plugin">插件</option>
          <option value="connection">连接</option><option value="system">系统</option>
          <option value="framework">框架</option>
        </select>
        <select class="select" id="logLevel" style="width:120px">
          <option value="">全部级别</option><option value="INFO">INFO</option>
          <option value="WARN">WARN</option><option value="ERROR">ERROR</option><option value="DEBUG">DEBUG</option>
        </select>
        <input class="input" id="logKeyword" placeholder="搜索关键词">
        <button class="btn" onclick="Views.applyLogFilter()">筛选</button>
        <button class="btn" onclick="Views.clearLogs()">清空</button>
        <label class="flex small" style="margin-left:auto"><input type="checkbox" id="logAuto" checked> 自动滚动</label>
      </div>
      <div class="log-box" id="logBox"></div>
    </div>`;

  document.getElementById('logAuto').addEventListener('change', e => { _logAuto = e.target.checked; });
  document.getElementById('logKeyword').addEventListener('keydown', e => { if (e.key === 'Enter') Views.applyLogFilter(); });

  // 先加载历史
  await Views.loadLogs(true);
  // 轮询增量日志（不用 SSE 长连接，避免占满 waitress 工作线程导致 WebUI 卡死）
  _logPollTimer = setInterval(async () => {
    const res = await api('/api/runtime_logs?after_seq=' + _logLastSeq + '&limit=100');
    const logs = (res && res.data) || [];
    logs.forEach(Views.appendLog);
    if (res && res.latest_seq) _logLastSeq = res.latest_seq;
  }, 2000);
};

Views.appendLog = function (entry) {
  // 过滤
  if (_logCategory && entry.category !== _logCategory) return;
  if (_logLevel && entry.level !== _logLevel) return;
  if (_logKeyword && !String(entry.message).toLowerCase().includes(_logKeyword.toLowerCase())) return;

  const box = document.getElementById('logBox');
  if (!box) return;
  const line = document.createElement('div');
  line.className = 'log-line';
  const t = new Date(entry.time * 1000);
  const p = n => String(n).padStart(2, '0');
  line.innerHTML = `<span class="log-time">${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}</span>
    <span class="log-level ${escapeHtml(entry.level)}">${escapeHtml(entry.level)}</span>
    <span class="log-msg">[${escapeHtml(entry.category)}] ${escapeHtml(entry.message)}</span>`;
  box.appendChild(line);
  if (box.children.length > 1000) box.removeChild(box.firstChild);
  if (_logAuto) box.scrollTop = box.scrollHeight;
};

Views.loadLogs = async function () {
  const box = document.getElementById('logBox');
  if (!box) return;
  const params = new URLSearchParams();
  if (_logCategory) params.set('category', _logCategory);
  if (_logLevel) params.set('level', _logLevel);
  if (_logKeyword) params.set('keyword', _logKeyword);
  params.set('limit', '200');
  const res = await api('/api/runtime_logs?' + params.toString());
  const logs = (res && res.data) || [];
  if (res && res.latest_seq) _logLastSeq = res.latest_seq;
  box.innerHTML = '';
  logs.forEach(Views.appendLog);
  if (box.children.length === 0) box.innerHTML = '<div class="empty">暂无日志</div>';
};

Views.applyLogFilter = function () {
  _logCategory = document.getElementById('logCategory').value;
  _logLevel = document.getElementById('logLevel').value;
  _logKeyword = document.getElementById('logKeyword').value.trim();
  Views.loadLogs();
};

Views.clearLogs = async function () {
  if (!await confirmDialog('确定清空日志缓存吗？')) return;
  const r = await api('/api/runtime_logs/clear', { method: 'POST' });
  if (r && r.code === 0) { toast('已清空', 'success'); Views.loadLogs(); }
};

/* ═══════════════ 设置 ═══════════════ */
/* ═══════════════ 系统设置（表单化分组） ═══════════════ */
const SETTING_GROUPS = [
  { key: 'web', title: 'Web 服务' },
  { key: 'onebot', title: 'OneBot 连接' },
  { key: 'database', title: '数据库' },
  { key: 'log', title: '日志' },
  { key: 'plugin', title: '插件' },
  { key: 'system', title: '系统' },
];
const SETTING_LABELS = {
  web: { host: '监听地址', port: '监听端口', secret_key: 'Secret Key', session_timeout: '会话超时（秒）' },
  onebot: { listen_host: '监听地址', listen_port: '监听端口', access_token: 'Access Token' },
  database: { type: '数据库类型', path: '数据库路径', host: '主机', port: '端口', user: '用户名', password: '密码', database: '库名' },
  log: { level: '日志级别', file: '日志文件', retention_days: '日志保留（天）', log_raw_message: '记录原始消息', log_sent_message: '记录发送消息' },
  plugin: { dir: '插件目录', dat_dir: '插件数据目录', heartbeat_interval: '心跳间隔（秒）', auto_install_deps_on_startup: '启动自动装依赖', max_memory_mb: '单插件内存上限（MB）' },
  system: { show_cpu: '显示 CPU', show_disk: '显示磁盘', status_interval: '状态刷新间隔（秒）' },
};

Views.settings = async function (view) {
  const isSuper = _admin && _admin.role === 'super';
  const [admins, yamlRes] = await Promise.all([
    api('/api/admins'),
    api('/api/config/yaml'),
  ]);
  const adminList = (admins && admins.data) || [];
  const yamlCfg = (yamlRes && yamlRes.data) || {};
  window._yamlCfg = JSON.parse(JSON.stringify(yamlCfg));
  window._setTab = window._setTab || 'web';

  const renderTabs = () => SETTING_GROUPS.map(g =>
    `<button class="tab-btn ${g.key === window._setTab ? 'active' : ''}" onclick="Views.setSettingsTab('${g.key}')">${g.title}</button>`).join('');

  const renderSection = () => {
    const section = window._setTab;
    const data = window._yamlCfg[section] || {};
    const labels = SETTING_LABELS[section] || {};
    let body = '';
    for (const [k, v] of Object.entries(data)) {
      const label = labels[k] || k;
      if (typeof v === 'boolean') {
        body += `<div class="form-row"><label>${label}</label>
          <label class="switch"><input type="checkbox" data-k="${k}" ${v ? 'checked' : ''}><span class="slider"></span></label></div>`;
      } else if (typeof v === 'number') {
        body += `<div class="form-row"><label>${label}</label><input class="input" type="number" data-k="${k}" value="${escapeHtml(v)}"></div>`;
      } else if (v === null || v === undefined) {
        body += `<div class="form-row"><label>${label}</label><input class="input" data-k="${k}" value=""></div>`;
      } else if (typeof v === 'object') {
        body += `<div class="form-row"><label>${label} <span class="small dim">JSON</span></label>
          <textarea class="textarea" data-k="${k}" style="min-height:80px;font-family:monospace">${escapeHtml(JSON.stringify(v))}</textarea></div>`;
      } else {
        const isPwd = k === 'access_token' || k === 'password' || k === 'secret_key';
        body += `<div class="form-row"><label>${label}</label>
          <input class="input" data-k="${k}" type="${isPwd ? 'password' : 'text'}" value="${escapeHtml(v)}"></div>`;
      }
    }
    return `<div class="card">
      <div class="card-title">${section} 配置 ${!isSuper ? '<span class="badge warn">仅超管可改</span>' : ''}</div>
      <div class="form-col">${body || '<div class="empty">该分组暂无配置项</div>'}</div>
      ${isSuper ? `<div class="mt"><button class="btn primary" onclick="Views.saveSettingsSection()">保存 ${SETTING_GROUPS.find(g => g.key === section).title}</button>
        <span class="small dim ml">部分字段（端口、地址等）需重启框架生效</span></div>` : ''}
    </div>`;
  };

  view.innerHTML = `
    <div class="card mb">
      <div class="card-title">全局设置</div>
      <div class="tabs">${renderTabs()}</div>
      <div id="yamlSection">${renderSection()}</div>
    </div>

    <div class="grid cols-2">
      <div>
        <div class="card">
          <div class="card-title">修改密码</div>
          <div class="form-row"><label>旧密码</label><input class="input" type="password" id="pwdOld"></div>
          <div class="form-row"><label>新密码</label><input class="input" type="password" id="pwdNew"></div>
          <button class="btn primary" onclick="Views.changePassword()">修改密码</button>
        </div>
        <div class="card">
          <div class="card-title">框架操作</div>
          <div class="flex wrap mb">
            <button class="btn" onclick="Views.checkFrameworkUpdate()">检查框架更新</button>
            <button class="btn" onclick="Views.loadFrameworkBackups()">查看备份 / 回滚</button>
            <button class="btn danger" onclick="Views.restartFramework()">重启框架</button>
          </div>
          <div id="fwUpdateInfo"></div>
          <div id="fwBackupInfo"></div>
          <div class="small dim mt">更新框架仅覆盖代码（framework/web/main.py 等），自动保留 plugins/、data/、config.yaml；旧代码备份到 data/backups/，更新后需重启生效。</div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">管理员账号 ${isSuper ? `<button class="btn sm primary" onclick="Views.addAdminModal()">＋ 添加</button>` : ''}</div>
        <div class="table-wrap"><table class="tbl">
          <tr><th>ID</th><th>用户名</th><th>角色</th><th>状态</th><th>最后登录</th><th>操作</th></tr>
          ${adminList.map(a => `<tr>
            <td>${a.id}</td><td>${escapeHtml(a.username)}</td>
            <td>${a.role === 'super' ? '<span class="badge warn">超管</span>' : '<span class="badge info">管理</span>'}</td>
            <td>${badgeHtml(!!a.is_active)}</td>
            <td class="small">${fmtTime(a.last_login_at)}</td>
            <td>${isSuper && a.id !== _admin.id ? `<button class="btn sm danger" onclick="Views.deleteAdmin(${a.id})">删除</button>` : (a.id === _admin.id ? '<span class="small dim">当前账号</span>' : '')}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">暂无管理员</td></tr>'}
        </table></div>
      </div>
    </div>`;
};

Views.setSettingsTab = function (key) {
  window._setTab = key;
  Views.settings(document.getElementById('view'));
};

Views.saveSettingsSection = async function () {
  const section = window._setTab;
  const data = window._yamlCfg[section] || {};
  const collected = {};
  document.querySelectorAll('#yamlSection [data-k]').forEach(el => {
    const k = el.dataset.k;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' && el.type === 'checkbox') collected[k] = el.checked;
    else if (tag === 'input' && el.type === 'number') collected[k] = Number(el.value);
    else if (tag === 'textarea') {
      const raw = el.value.trim();
      try { collected[k] = JSON.parse(raw); }
      catch (e) { collected[k] = raw; }
    }
    else collected[k] = el.value;
  });
  const r = await api(`/api/config/yaml/${section}`, { method: 'PUT', body: JSON.stringify({ data: collected }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); window._yamlCfg[section] = collected; }
  else toast((r && r.msg) || '保存失败', 'error');
};

Views.changePassword = async function () {
  const oldPwd = document.getElementById('pwdOld').value;
  const newPwd = document.getElementById('pwdNew').value;
  if (!oldPwd || !newPwd) { toast('请填写完整', 'warning'); return; }
  const r = await api('/api/change_password', { method: 'POST', body: JSON.stringify({ old_password: oldPwd, new_password: newPwd }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); document.getElementById('pwdOld').value = ''; document.getElementById('pwdNew').value = ''; }
  else toast((r && r.msg) || '修改失败', 'error');
};

Views.addAdminModal = function () {
  openModal(`
    <div class="modal-head"><span>添加管理员</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="form-row"><label>用户名</label><input class="input" id="admUser"></div>
      <div class="form-row"><label>密码</label><input class="input" type="password" id="admPwd"></div>
      <div class="form-row"><label>角色</label>
        <select class="select" id="admRole"><option value="admin">管理员</option><option value="super">超级管理员</option></select></div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views.addAdmin()">添加</button>
    </div>`);
};

Views.addAdmin = async function () {
  const body = JSON.stringify({
    username: document.getElementById('admUser').value.trim(),
    password: document.getElementById('admPwd').value,
    role: document.getElementById('admRole').value,
  });
  const r = await api('/api/admins', { method: 'POST', body });
  if (r && r.code === 0) { toast(r.msg, 'success'); closeModal(); Views.settings(document.getElementById('view')); }
  else toast((r && r.msg) || '添加失败', 'error');
};

Views.deleteAdmin = async function (id) {
  if (!await confirmDialog('确定删除该管理员吗？')) return;
  const r = await api(`/api/admins/${id}`, { method: 'DELETE' });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.settings(document.getElementById('view')); }
  else toast((r && r.msg) || '删除失败', 'error');
};

Views.restartFramework = async function () {
  if (!await confirmDialog('确定重启框架吗？正在处理的消息可能丢失。', '重启框架')) return;
  const r = await api('/api/restart', { method: 'POST' });
  if (r && r.code === 0) toast(r.msg, 'success');
};

Views.checkFrameworkUpdate = async function () {
  const el = document.getElementById('fwUpdateInfo');
  if (el) el.innerHTML = '<div class="small dim">正在检查更新...</div>';
  const r = await api('/api/framework/check_update');
  if (!r || r.code !== 0) {
    if (el) el.innerHTML = `<div class="badge err">${escapeHtml((r && r.msg) || '检查失败')}</div>`;
    return;
  }
  const d = r.data;
  const hasUp = d.has_update;
  if (el) el.innerHTML = `
    <div class="small mb">
      本地: <span class="mono">${escapeHtml(d.local_version)}</span>
      &nbsp;|&nbsp; 最新: <span class="mono">${escapeHtml(d.latest_version)}</span>
      <div class="dim">${escapeHtml(d.commit_message)} · ${escapeHtml(d.author || '')} · ${escapeHtml(d.commit_date || '')}</div>
      <div class="dim mono small">commit: ${escapeHtml(d.local_commit)} → ${escapeHtml(d.latest_commit)}</div>
    </div>
    ${hasUp === true
      ? `<button class="btn primary sm" onclick="Views.doFrameworkUpdate()">立即更新框架</button>`
      : hasUp === null || hasUp === undefined
        ? '<span class="badge warn">本地版本未知，无法自动检测，请使用面板「更新框架」或覆盖部署</span>'
        : '<span class="badge ok">已是最新版本</span>'}`;
};

Views.doFrameworkUpdate = async function () {
  if (!await confirmDialog('确定更新框架吗？\n将覆盖 framework/web/main.py 等代码，自动保留插件与配置，更新后需重启生效。', '更新框架')) return;
  const r = await api('/api/framework/update', { method: 'POST' });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.checkFrameworkUpdate(); }
  else toast((r && r.msg) || '更新失败', 'error');
};

Views.loadFrameworkBackups = async function () {
  const el = document.getElementById('fwBackupInfo');
  const r = await api('/api/framework/backups');
  if (!el) return;
  if (!r || r.code !== 0) { el.innerHTML = `<div class="small dim">${escapeHtml((r && r.msg) || '获取备份失败')}</div>`; return; }
  const list = (r.data && r.data.backups) || [];
  el.innerHTML = `
    <div class="small mb">框架更新备份（${list.length}）:</div>
    ${list.length ? list.map(b => `
      <div class="flex between mb">
        <span class="small mono">${escapeHtml(b.name)} <span class="dim">(${escapeHtml(b.time)})</span></span>
        <button class="btn sm danger" onclick="Views.rollbackFramework('${escapeHtml(b.name)}')">回滚</button>
      </div>`).join('')
      : '<div class="small dim">暂无备份。每次「更新框架」都会自动生成备份。</div>'}
  `;
};

Views.rollbackFramework = async function (name) {
  if (!await confirmDialog(`确定回滚到 ${name} 吗？\n当前 framework 代码将被替换（旧代码留档 data/backups/current/），回滚后需重启生效。`, '回滚框架')) return;
  const r = await api('/api/framework/rollback', { method: 'POST', body: JSON.stringify({ backup: name }) });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.loadFrameworkBackups(); }
  else toast((r && r.msg) || '回滚失败', 'error');
};

// 离开日志页时关闭轮询定时器 / 运行状态定时器
window.addEventListener('hashchange', () => {
  if (_logPollTimer) { clearInterval(_logPollTimer); _logPollTimer = null; }
  if (window._rtTimer) { clearInterval(window._rtTimer); window._rtTimer = null; }
});

/* ═══════════════ 插件市场（增强版） ═══════════════ */
Views.marketplace = async function (view) {
  const mkt = await api('/api/plugins/market');
  const data = (mkt && mkt.data) || { plugins: [], errors: [], sources: [] };
  window._marketPlugins = data.plugins || [];
  window._marketSearchKeyword = '';

  const filtered = (data.plugins || []).filter(p => {
    if (!window._marketSearchKeyword) return true;
    const kw = window._marketSearchKeyword.toLowerCase();
    return (p.name || '').toLowerCase().includes(kw)
      || (p.description || '').toLowerCase().includes(kw)
      || (p.author || '').toLowerCase().includes(kw);
  });

  const emptyText = window._marketSearchKeyword ? '未找到匹配的插件' : '市场为空，请检查网络或添加自定义源';

  view.innerHTML = `
    <div class="flex between mb">
      <div class="flex">
        <button class="btn" onclick="Views.marketplace(document.getElementById('view'))">⟳ 刷新</button>
        <button class="btn" onclick="Views.marketSources()">自定义源</button>
        <button class="btn" onclick="Views.marketDepsGraph()">依赖关系图</button>
      </div>
      <span class="muted small">${filtered.length} / ${data.plugins.length} 个插件 · ${(data.sources || []).length} 个源</span>
    </div>
    <div class="market-search">
      <input class="input" id="mktSearch" placeholder="搜索插件名称/描述/作者..." value="${escapeHtml(window._marketSearchKeyword)}">
      <button class="btn" onclick="Views.marketSearch()">搜索</button>
      <button class="btn ghost" onclick="Views.marketClearSearch()">清除</button>
    </div>
    ${(data.errors || []).map(e => `<div class="alert warn mb">⚠ 源加载失败: ${escapeHtml(e)}</div>`).join('')}
    <div class="grid cols-3" id="mktGrid">
      ${filtered.map((p, i) => {
        const origIdx = (data.plugins || []).indexOf(p);
        return `
        <div class="plugin-card">
          <div class="plugin-head">
            <div>
              <div class="plugin-name">${escapeHtml(p.name)}</div>
              <div class="plugin-ver">v${escapeHtml(p.version || '0.0.0')} ${p.installed ? '<span class="badge ok">已安装</span>' : '<span class="badge muted">未安装</span>'}</div>
            </div>
          </div>
          <div class="plugin-desc">${escapeHtml(p.description || '暂无描述')}</div>
          <div class="flex wrap small" style="color:var(--text-muted)">
            <span class="badge muted">${escapeHtml(p.author || '未知作者')}</span>
            <span class="badge muted">${escapeHtml(p.source || '默认源')}</span>
          </div>
          <div class="plugin-foot">
            <button class="btn sm" onclick="Views.marketDetail(${origIdx})">详情</button>
            ${p.installed
              ? '<button class="btn sm" onclick="location.hash=\'#/plugins\'">已安装</button>'
              : `<button class="btn sm primary" onclick="Views.installMarketPlugin(${origIdx})">安装</button>`}
            ${p.dependencies && p.dependencies.python && p.dependencies.python.length
              ? `<button class="btn sm" onclick="Views.marketDepGraph(${origIdx})">依赖图</button>` : ''}
          </div>
        </div>`}).join('') || '<div class="empty">' + emptyText + '</div>'}
    </div>`;

  document.getElementById('mktSearch').addEventListener('keydown', e => {
    if (e.key === 'Enter') Views.marketSearch();
  });
};

Views.marketSearch = function () {
  const el = document.getElementById('mktSearch');
  window._marketSearchKeyword = (el && el.value.trim()) || '';
  Views.marketplace(document.getElementById('view'));
};

Views.marketClearSearch = function () {
  window._marketSearchKeyword = '';
  Views.marketplace(document.getElementById('view'));
};

Views.marketDetail = async function (idx) {
  const p = window._marketPlugins[idx];
  if (!p) return;
  // 尝试获取 README 和更新历史
  let readme = '', readmeOk = false;
  if (p.repo) {
    try {
      const readmeUrl = `https://api.github.com/repos/${p.repo.replace(/^https?:\/\/github\.com\//, '')}/readme`;
      const r = await fetch(readmeUrl, { headers: { 'Accept': 'application/vnd.github.v3.raw' } });
      if (r.ok) {
        readme = await r.text();
        readmeOk = true;
      }
    } catch (e) { /* ignore */ }
  }
  const deps = (p.dependencies && p.dependencies.python) || [];
  const depHtml = deps.length
    ? deps.map(d => `<div class="small mono" style="padding:2px 0">${escapeHtml(d)}</div>`).join('')
    : '<div class="small dim">无 Python 依赖</div>';

  openModal(`
    <div class="modal-head"><span>${escapeHtml(p.name)} v${escapeHtml(p.version || '0.0.0')}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body" style="max-height:70vh;overflow:auto">
      <div class="flex wrap mb">
        <span class="badge muted">${escapeHtml(p.author || '未知作者')}</span>
        <span class="badge muted">${escapeHtml(p.source || '默认源')}</span>
        ${p.installed ? '<span class="badge ok">已安装</span>' : '<span class="badge muted">未安装</span>'}
      </div>
      <div class="small dim mb">${escapeHtml(p.description || '暂无描述')}</div>
      ${p.repo ? `<div class="form-row"><label>仓库</label><a href="https://github.com/${escapeHtml(p.repo)}" target="_blank" class="small mono">${escapeHtml(p.repo)}</a></div>` : ''}
      <div class="card-title mt" style="margin-bottom:8px">Python 依赖</div>
      <div class="card" style="padding:8px 12px">${depHtml}</div>
      ${deps.length ? `<div class="mt"><button class="btn sm" onclick="Views.marketDepGraph(${idx})">查看依赖关系图</button></div>` : ''}
      ${readmeOk ? `
        <div class="card-title mt" style="margin-bottom:8px">README</div>
        <pre class="code" style="max-height:300px;overflow:auto;font-size:12px">${escapeHtml(readme.slice(0, 3000))}${readme.length > 3000 ? '\n\n...（内容过长已截断）' : ''}</pre>` : ''}
      ${p.repo ? `
        <div class="card-title mt" style="margin-bottom:8px">更新历史（GitHub 提交）</div>
        <div id="mktCommits"><div class="small dim">加载中...</div></div>` : ''}
    </div>
    <div class="modal-foot">
      ${p.installed ? '<button class="btn" onclick="location.hash=\'#/plugins\'">前往插件管理</button>'
        : `<button class="btn primary" onclick="closeModal();Views.installMarketPlugin(${idx})">安装插件</button>`}
      <button class="btn" onclick="closeModal()">关闭</button>
    </div>`);

  // 异步加载更新历史
  if (p.repo) {
    Views._loadMarketCommits(p.repo, p.branch || 'main');
  }
};

Views._loadMarketCommits = async function (repo, branch) {
  const el = document.getElementById('mktCommits');
  if (!el) return;
  try {
    const repoPath = repo.replace(/^https?:\/\/github\.com\//, '');
    const r = await fetch(`https://api.github.com/repos/${repoPath}/commits?sha=${branch}&per_page=10`, {
      headers: { 'Accept': 'application/vnd.github.v3+json' }
    });
    if (!r.ok) { el.innerHTML = '<div class="small dim">无法获取更新历史</div>'; return; }
    const commits = await r.json();
    if (!commits.length) { el.innerHTML = '<div class="small dim">暂无提交记录</div>'; return; }
    el.innerHTML = commits.map(c => {
      const msg = (c.commit && c.commit.message) || '';
      const firstLine = msg.split('\n')[0];
      const date = c.commit && c.commit.author && c.commit.author.date ? new Date(c.commit.author.date).toLocaleDateString() : '';
      const sha = (c.sha || '').slice(0, 7);
      return `<div class="flex between" style="padding:4px 0;border-bottom:1px solid var(--border)">
        <span class="small mono" style="color:var(--text-dim)">${escapeHtml(sha)}</span>
        <span class="small" style="flex:1;margin:0 8px">${escapeHtml(firstLine)}</span>
        <span class="small dim">${date}</span>
      </div>`;
    }).join('');
  } catch (e) {
    if (el) el.innerHTML = '<div class="small dim">加载失败</div>';
  }
};

Views.marketDepGraph = async function (idx) {
  const p = window._marketPlugins[idx];
  if (!p) return;
  const deps = (p.dependencies && p.dependencies.python) || [];
  if (!deps.length) { toast('该插件无 Python 依赖', 'info'); return; }
  // 在弹窗中展示依赖关系图
  const nodes = [{ id: p.name, type: 'plugin', version: p.version || '?' }];
  const edges = [];
  deps.forEach(d => {
    const pkgName = d.replace(/[>=<!~].*$/, '').trim();
    nodes.push({ id: pkgName, type: 'pkg' });
    edges.push({ source: p.name, target: pkgName, label: d });
  });
  openModal(`
    <div class="modal-head"><span>依赖关系图 · ${escapeHtml(p.name)}</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="small muted mb">${nodes.length - 1} 个依赖包</div>
      <div class="dep-graph-wrap">${Views._renderDepGraphSVG(nodes, edges)}</div>
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views.marketDepsGraph = async function () {
  // 查看所有已安装插件的全局依赖关系图
  const res = await api('/api/plugins/deps/graph');
  if (!res || res.code !== 0) { toast((res && res.msg) || '获取失败', 'error'); return; }
  const d = res.data;
  openModal(`
    <div class="modal-head"><span>全局插件依赖关系图 <span class="small muted">${d.total_plugins} 插件 · ${d.total_edges} 关联</span></span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body" style="max-height:75vh;overflow:auto">
      <div class="dep-graph-wrap">${Views._renderDepGraphSVG(d.nodes, d.edges)}</div>
      <div class="card-title mt" style="margin-bottom:8px">依赖详情</div>
      ${d.nodes.map(n => {
        const missing = n.missing_count ? `<span class="badge err">缺 ${n.missing_count}</span>` : '';
        return `<div class="flex between" style="padding:4px 0;border-bottom:1px solid var(--border)">
          <span><b>${escapeHtml(n.id)}</b> <span class="small dim">v${escapeHtml(n.version)}</span></span>
          <span class="small">${n.dep_count} 个依赖 ${missing}</span>
        </div>`;
      }).join('') || '<div class="empty">无插件数据</div>'}
      ${d.edges.length ? `<div class="small dim mt">共享依赖（多个插件依赖同一包）：</div>
        ${d.edges.map(e => `<div class="small" style="padding:2px 0">${escapeHtml(e.source)} ↔ ${escapeHtml(e.target)} <span class="dim">(${escapeHtml(e.label)})</span></div>`).join('')}` : ''}
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views._renderDepGraphSVG = function (nodes, edges) {
  if (!nodes || !nodes.length) return '<div class="empty">无数据</div>';
  const W = 700, H = Math.max(300, nodes.length * 50 + 60);
  const centerX = W / 2;
  // 简单布局：插件节点在左侧，依赖包在右侧
  const pluginNodes = nodes.filter(n => n.type === 'plugin');
  const pkgNodes = nodes.filter(n => n.type === 'pkg');
  const positions = {};
  const spacing = Math.min(50, (H - 60) / Math.max(pluginNodes.length + pkgNodes.length || 1, 1));
  pluginNodes.forEach((n, i) => {
    positions[n.id] = { x: 120, y: 40 + i * spacing + 20 };
  });
  pkgNodes.forEach((n, i) => {
    positions[n.id] = { x: W - 120, y: 40 + i * spacing + 20 };
  });

  const edgePaths = edges.map(e => {
    const s = positions[e.source], t = positions[e.target];
    if (!s || !t) return '';
    const midX = (s.x + t.x) / 2;
    return `<path class="dep-edge" d="M${s.x},${s.y} C${midX},${s.y} ${midX},${t.y} ${t.x},${t.y}"/>
      <text class="dep-edge-label" x="${midX}" y="${(s.y + t.y) / 2 - 6}">${escapeHtml(e.label || '')}</text>`;
  }).join('\n');

  const nodeRects = nodes.map(n => {
    const pos = positions[n.id];
    if (!pos) return '';
    const isPlugin = n.type === 'plugin';
    const rx = 8, ry = 8;
    const fill = isPlugin ? 'var(--accent-soft)' : 'var(--warning-soft)';
    const stroke = isPlugin ? 'var(--accent-border)' : 'rgba(245,158,11,.3)';
    const w = isPlugin ? 140 : 160;
    const h = 32;
    const x = pos.x - w / 2, y = pos.y - h / 2;
    const subText = isPlugin ? `v${n.version || '?'}` : (n.dep_count ? `${n.dep_count} 引用` : '');
    return `<g class="dep-node">
      <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}" ry="${ry}" fill="${fill}" stroke="${stroke}" stroke-width="1.5"/>
      <text class="dep-node-text" x="${pos.x}" y="${pos.y - 4}" fill="${isPlugin ? 'var(--accent-hover)' : 'var(--warning)'}">${escapeHtml(n.id)}</text>
      ${subText ? `<text class="dep-node-sub" x="${pos.x}" y="${pos.y + 10}">${escapeHtml(subText)}</text>` : ''}
    </g>`;
  }).join('\n');

  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs>
      <style>
        .dep-edge { stroke: #2f3550; stroke-width: 1.5; fill: none; }
        .dep-edge-label { font-size: 10px; fill: #5c6277; text-anchor: middle; }
        .dep-node-text { font-size: 12px; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
        .dep-node-sub { font-size: 10px; fill: #5c6277; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
      </style>
    </defs>
    ${edgePaths}
    ${nodeRects}
  </svg>`;
};

Views.installMarketPlugin = async function (idx) {
  const p = window._marketPlugins[idx];
  if (!p) return;
  if (!await confirmDialog(`确定安装插件 [${p.name}] 吗？将从 ${p.repo || ''} 下载。`, '安装插件')) return;
  const r = await api('/api/plugins/market/install', {
    method: 'POST',
    body: JSON.stringify({ name: p.name, repo: p.repo, branch: p.branch, sub_path: p.sub_path }),
  });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.marketplace(document.getElementById('view')); }
  else toast((r && r.msg) || '安装失败', 'error');
};

Views.marketSources = async function () {
  const res = await api('/api/plugins/market/sources');
  const d = (res && res.data) || { default: {}, custom: [] };
  window._srcCustom = (d.custom || []).map(x => ({ ...x }));
  openModal(`
    <div class="modal-head"><span>插件源管理</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="small muted mb">默认源（不可修改）</div>
      <div class="card" style="padding:8px 12px"><b>${escapeHtml(d.default.name || '')}</b> <span class="small dim">${escapeHtml(d.default.url || '')}</span></div>
      <div class="small muted mt mb">自定义源（Registry JSON 地址）</div>
      <div id="srcList"></div>
      <button class="btn sm" onclick="Views.addSourceRow()">＋ 添加源</button>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views.saveSources()">保存</button>
    </div>`);
  Views.renderSourceRows();
};

Views.renderSourceRows = function () {
  const el = document.getElementById('srcList');
  if (!el) return;
  el.innerHTML = (window._srcCustom || []).map((s, i) => `
    <div class="form-row">
      <input class="input" placeholder="名称" value="${escapeHtml(s.name)}" style="width:160px"
        oninput="window._srcCustom[${i}].name=this.value">
      <input class="input" placeholder="https://.../registry.json" value="${escapeHtml(s.url)}" style="flex:1"
        oninput="window._srcCustom[${i}].url=this.value">
      <button class="btn sm danger" onclick="window._srcCustom.splice(${i},1);Views.renderSourceRows()">✕</button>
    </div>`).join('') || '<div class="empty small">暂无自定义源</div>';
};

Views.addSourceRow = function () {
  (window._srcCustom = window._srcCustom || []).push({ name: '', url: '' });
  Views.renderSourceRows();
};

Views.saveSources = async function () {
  const r = await api('/api/plugins/market/sources', {
    method: 'POST',
    body: JSON.stringify({ sources: window._srcCustom || [] }),
  });
  if (r && r.code === 0) { toast(r.msg, 'success'); closeModal(); Views.marketplace(document.getElementById('view')); }
  else toast((r && r.msg) || '保存失败', 'error');
};

/* ═══════════════ OneBot11 连接设置 ═══════════════ */
Views.connection = async function (view) {
  const res = await api('/api/connection');
  const d = (res && res.data) || { config: {}, status: {} };
  const c = d.config || {};
  const st = d.status || {};
  const bots = (st.connected_bots || []);
  const port = c.listen_port || st.ws_port || 6830;
  const host = c.listen_host || '0.0.0.0';
  const wsUrl = `${host === '0.0.0.0' || host === '::' ? '127.0.0.1' : host}:${port}`;

  view.innerHTML = `
    <div class="grid cols-2">
      <div class="card">
        <div class="card-title">OneBot11 反向 WS 配置</div>
        <div class="form-row"><label>监听地址</label><input class="input" id="cnHost" value="${escapeHtml(host)}"></div>
        <div class="form-row"><label>监听端口</label><input class="input" id="cnPort" type="number" value="${escapeHtml(port)}"></div>
        <div class="form-row"><label>Access Token</label><input class="input" id="cnToken" type="password" value="${escapeHtml(c.access_token || '')}"></div>
        <div class="flex"><button class="btn primary" onclick="Views.saveConnection()">保存配置</button>
          <button class="btn" onclick="Views.connection(document.getElementById('view'))">⟳ 刷新</button></div>
        <div class="small dim mt">监听地址 / 端口改动需重启框架生效；Access Token 立即生效。</div>
      </div>
      <div>
        <div class="card">
          <div class="card-title">实时连接状态 <span class="badge ${bots.length ? 'ok' : 'muted'}">${bots.length} 个在线</span></div>
          ${bots.map(b => `<div class="flex between" style="padding:6px 0;border-bottom:1px solid var(--border)">
            <span class="mono">${escapeHtml(b)}</span><span class="badge ok">在线</span></div>`).join('') || '<div class="empty">暂无客户端连接</div>'}
        </div>
        <div class="card">
          <div class="card-title">接入指引</div>
          <div class="small" style="line-height:1.9">
            <div>反向 WS 服务端地址：<code class="mono">ws://${escapeHtml(wsUrl)}/ws</code></div>
            <div class="dim">OneBot 客户端（NapCat / Lagrange / LLOneBot 等）添加「反向 WebSocket」连接，填写上述地址${c.access_token ? '与 Access Token' : ''}即可接入。</div>
          </div>
        </div>
      </div>
    </div>`;
};

Views.saveConnection = async function () {
  const body = JSON.stringify({
    listen_host: document.getElementById('cnHost').value.trim(),
    listen_port: parseInt(document.getElementById('cnPort').value, 10),
    access_token: document.getElementById('cnToken').value,
  });
  const r = await api('/api/connection', { method: 'PUT', body });
  if (r && r.code === 0) { toast(r.msg, 'success'); Views.connection(document.getElementById('view')); }
  else toast((r && r.msg) || '保存失败', 'error');
};

/* ═══════════════ 运行状态（增强版） ═══════════════ */
Views.runtime = async function (view) {
  const load = async () => {
    const [statsRes, cmdRes, msgRes, envRes] = await Promise.all([
      api('/api/runtime/stats'),
      api('/api/stats/commands?top=10'),
      api('/api/stats/messages'),
      api('/api/envinfo'),
    ]);
    const d = (statsRes && statsRes.data) || {};
    const cmds = (cmdRes && cmdRes.data && cmdRes.data.commands) || [];
    const msgDays = (msgRes && msgRes.data && msgRes.data.days) || [];
    const env = (envRes && envRes.data) || {};
    const memPct = d.memory && d.memory.percent;
    const bots = (d.ws && d.ws.bots) || [];
    const maxHits = Math.max(...cmds.map(c => c.hit_count), 1);
    const maxMsg = Math.max(...msgDays.map(d => d.cnt), 1);

    const envinfoHtml = (env.os) ? `
      <div class="card-title" style="margin-bottom:10px">环境信息</div>
      <div class="envinfo-grid">
        <div class="envinfo-card">
          <div class="envinfo-card-title">操作系统</div>
          <div class="envinfo-row"><span class="key">系统</span><span class="val">${escapeHtml(env.os.system || '-')}</span></div>
          <div class="envinfo-row"><span class="key">版本</span><span class="val">${escapeHtml(env.os.release || '-')}</span></div>
          <div class="envinfo-row"><span class="key">架构</span><span class="val">${escapeHtml(env.os.arch || '-')}</span></div>
          <div class="envinfo-row"><span class="key">机器</span><span class="val">${escapeHtml(env.os.machine || '-')}</span></div>
        </div>
        <div class="envinfo-card">
          <div class="envinfo-card-title">CPU</div>
          <div class="envinfo-row"><span class="key">核心数</span><span class="val">${env.cpu ? env.cpu.count : '-'}</span></div>
          <div class="envinfo-row"><span class="key">物理核心</span><span class="val">${env.cpu ? env.cpu.physical_count : '-'}</span></div>
          <div class="envinfo-row"><span class="key">频率</span><span class="val">${env.cpu && env.cpu.freq_mhz ? env.cpu.freq_mhz + ' MHz' : '-'}</span></div>
          <div class="envinfo-row"><span class="key">使用率</span><span class="val">${env.cpu ? env.cpu.percent + '%' : '-'}</span></div>
        </div>
        <div class="envinfo-card">
          <div class="envinfo-card-title">内存</div>
          <div class="envinfo-row"><span class="key">总量</span><span class="val">${env.memory ? env.memory.total_mb + ' MB' : '-'}</span></div>
          <div class="envinfo-row"><span class="key">可用</span><span class="val">${env.memory ? env.memory.available_mb + ' MB' : '-'}</span></div>
        </div>
        <div class="envinfo-card">
          <div class="envinfo-card-title">磁盘</div>
          <div class="envinfo-row"><span class="key">总量</span><span class="val">${env.disk ? env.disk.total_gb + ' GB' : '-'}</span></div>
          <div class="envinfo-row"><span class="key">已用</span><span class="val">${env.disk ? env.disk.used_gb + ' GB' : '-'}</span></div>
          <div class="envinfo-row"><span class="key">剩余</span><span class="val">${env.disk ? env.disk.free_gb + ' GB' : '-'}</span></div>
          <div class="envinfo-row"><span class="key">使用率</span><span class="val">${env.disk ? env.disk.percent + '%' : '-'}</span></div>
          <div class="bar" style="margin-top:4px"><div class="bar-inner" style="width:${Math.min(100, (env.disk && env.disk.percent) || 0)}%"></div></div>
        </div>
        <div class="envinfo-card">
          <div class="envinfo-card-title">网络</div>
          <div class="envinfo-row"><span class="key">发送</span><span class="val">${env.network ? env.network.bytes_sent_mb + ' MB' : '-'}</span></div>
          <div class="envinfo-row"><span class="key">接收</span><span class="val">${env.network ? env.network.bytes_recv_mb + ' MB' : '-'}</span></div>
        </div>
        <div class="envinfo-card">
          <div class="envinfo-card-title">Python</div>
          <div class="envinfo-row"><span class="key">版本</span><span class="val">${env.python ? env.python.version : '-'}</span></div>
          <div class="envinfo-row"><span class="key">包数</span><span class="val">${env.python ? env.python.packages_total : '-'}</span></div>
          <div class="envinfo-row"><span class="key">进程 PID</span><span class="val">${env.process ? env.process.pid : '-'}</span></div>
          <div class="envinfo-row"><span class="key">线程数</span><span class="val">${env.process ? env.process.threads : '-'}</span></div>
          <div class="envinfo-row"><span class="key">打开文件</span><span class="val">${env.process ? env.process.open_files : '-'}</span></div>
        </div>
      </div>` : '';

    view.innerHTML = `
      <div class="grid cols-4">
        <div class="stat-card"><div class="head"><span class="stat-label">CPU 使用率</span></div>
          <div class="stat-value">${d.cpu_percent ?? '-'}<span class="small dim">%</span></div>
          <div class="bar"><div class="bar-inner" style="width:${Math.min(100, d.cpu_percent || 0)}%"></div></div></div>
        <div class="stat-card"><div class="head"><span class="stat-label">进程内存</span></div>
          <div class="stat-value">${d.process_memory_mb ?? '-'}<span class="small dim"> MB</span></div></div>
        <div class="stat-card"><div class="head"><span class="stat-label">系统内存</span></div>
          <div class="stat-value">${memPct ?? '-'}<span class="small dim">%</span></div>
          <div class="small dim">${d.memory ? `${d.memory.used_mb} / ${d.memory.total_mb} MB` : ''}</div></div>
        <div class="stat-card"><div class="head"><span class="stat-label">运行时长</span></div>
          <div class="stat-value" style="font-size:17px">${fmtUptime(d.uptime_seconds)}</div></div>
      </div>

      <div class="grid cols-2 mt">
        <div class="card">
          <div class="card-title">OneBot 连接 <span class="badge ${bots.length ? 'ok' : 'muted'}">${bots.length} 在线</span></div>
          ${bots.map(b => `<div class="flex between" style="padding:4px 0"><span>${escapeHtml(b)}</span><span class="badge ok">在线</span></div>`).join('') || '<div class="empty">无在线连接</div>'}
        </div>
        <div class="card">
          <div class="card-title">运行时信息</div>
          <div class="flex between" style="padding:4px 0"><span class="muted">Python</span><span>${escapeHtml(d.python_version || '-')}</span></div>
          <div class="flex between" style="padding:4px 0"><span class="muted">数据库</span><span>${escapeHtml(d.db_type || '-')}</span></div>
          <div class="flex between" style="padding:4px 0"><span class="muted">线程数</span><span>${d.threads ?? '-'}</span></div>
          <div class="flex between" style="padding:4px 0"><span class="muted">更新时间</span><span>${new Date().toLocaleTimeString()}</span></div>
        </div>
      </div>

      ${cmds.length ? `
      <div class="card">
        <div class="card-title">命令命中排行 (Top ${cmds.length}) <span class="small muted">共 ${cmdRes.data.total_hits} 次命中</span></div>
        <div class="chart-bar-group">
          ${cmds.map(c => {
            const pct = Math.max(1, (c.hit_count / maxHits) * 100);
            return `<div class="chart-bar-row">
              <span class="chart-bar-label">${escapeHtml(c.pattern)}</span>
              <div class="chart-bar-track"><div class="chart-bar-fill" style="width:${pct}%"></div></div>
              <span class="chart-bar-val">${c.hit_count}</span>
            </div>`;
          }).join('')}
        </div>
      </div>` : ''}

      ${msgDays.length ? `
      <div class="card">
        <div class="card-title">消息统计 (近 30 天) <span class="small muted">共 ${msgRes.data.total} 条</span></div>
        ${msgDays.map(d => {
          const pct = Math.max(1, (d.cnt / maxMsg) * 100);
          const groupPct = Math.max(1, (d.group_msg / d.cnt) * 100);
          const privatePct = Math.max(1, (d.private_msg / d.cnt) * 100);
          return `<div class="chart-day">
            <span class="chart-day-label">${escapeHtml(d.day)}</span>
            <div class="chart-day-bar" style="flex:1">
              <div class="chart-day-fill group" style="width:${groupPct}%"></div>
            </div>
            <span class="small dim" style="width:30px;text-align:right">${d.cnt}</span>
          </div>`;
        }).join('')}
        <div class="flex small dim mt" style="gap:16px">
          <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:linear-gradient(90deg,#6366f1,#818cf8);margin-right:4px"></span> 群消息</span>
          <span><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:linear-gradient(90deg,#22c55e,#4ade80);margin-right:4px"></span> 私聊</span>
        </div>
      </div>` : ''}

      ${envinfoHtml ? `
      <div class="card">
        ${envinfoHtml}
      </div>` : ''}

      <div class="small dim mt center">每 5 秒自动刷新</div>`;
  };
  await load();
  if (window._rtTimer) clearInterval(window._rtTimer);
  window._rtTimer = setInterval(load, 5000);
};

/* ═══════════════ 数据库管理 ═══════════════ */
Views.database = async function (view) {
  window._dbState = window._dbState || {};
  const state = window._dbState;

  const render = async () => {
    const tblRes = await api('/api/db/tables');
    const tables = (tblRes && tblRes.data) || [];
    const selected = state.selectedTable || (tables.length ? tables[0].name : '');

    let schema = [], rows = [], total = 0;
    if (selected) {
      const page = state.page || 1;
      const [sRes, rRes] = await Promise.all([
        api(`/api/db/tables/${encodeURIComponent(selected)}/schema`),
        api(`/api/db/tables/${encodeURIComponent(selected)}/rows?page=${page}&page_size=${state.pageSize || 50}`),
      ]);
      schema = (sRes && sRes.data) || [];
      rows = (rRes && rRes.data && rRes.data.rows) || [];
      total = (rRes && rRes.data && rRes.data.total) || 0;
    }

    const cols = schema.map(c => c.Field || c.name || c.cid);
    const totalPages = Math.max(1, Math.ceil(total / (state.pageSize || 50)));
    const queryRows = state.queryRows || [];
    const queryCols = queryRows.length ? Object.keys(queryRows[0]) : [];
    const activeTab = state.activeTab || 'browse';

    view.innerHTML = `
      <div class="db-wrap">
        <div class="db-sidebar">
          <div class="db-sidebar-title">数据表</div>
          ${tables.map(t =>
            `<div class="db-tbl-item ${t.name === selected ? 'active' : ''}"
                 onclick="window._dbState.selectedTable='${t.name}';window._dbState.page=1;Views.database(document.getElementById('view'))">
              <span class="db-tbl-icon">▤</span>
              <span class="db-tbl-name">${escapeHtml(t.name)}</span>
              <span class="db-tbl-rows">${t.rows === null ? '?' : t.rows}</span>
            </div>`
          ).join('') || '<div class="empty small">无数据表</div>'}
          <div class="db-sidebar-divider"></div>
          <div class="db-tbl-item" onclick="Views.sqlConsole()">
            <span class="db-tbl-icon">⌨</span>
            <span class="db-tbl-name">SQL 控制台</span>
          </div>
          <div class="db-tbl-item" onclick="Views.database(document.getElementById('view'))">
            <span class="db-tbl-icon">⟳</span>
            <span class="db-tbl-name">刷新</span>
          </div>
        </div>
        <div class="db-main">
          ${selected ? `
          <div class="db-tabs">
            <span class="db-tab-info">${escapeHtml(selected)} · ${total} 行</span>
            <button class="db-tab ${activeTab === 'sql' ? 'active' : ''}" onclick="Views.sqlConsole();">SQL</button>
          </div>
          <div class="db-content">
            <table class="tbl">
              <thead><tr>${cols.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
              <tbody>
                ${rows.map(r => `<tr>${cols.map(c => `<td class="small" title="${escapeHtml(r[c] === null ? 'NULL' : String(r[c]).substring(0, 200))}">${r[c] === null ? '<span class="dim">NULL</span>' : escapeHtml(String(r[c]).substring(0, 120))}</td>`).join('')}</tr>`).join('') || '<tr><td colspan="99" class="empty">无数据</td></tr>'}
              </tbody>
            </table>
            <div class="db-pager">
              <span class="small dim">第 ${state.page || 1} / ${totalPages} 页</span>
              <div class="flex">
                <button class="btn sm" ${(state.page || 1) <= 1 ? 'disabled' : ''} onclick="window._dbState.page=Math.max(1,(window._dbState.page||1)-1);Views.database(document.getElementById('view'))">上一页</button>
                <button class="btn sm" ${(state.page || 1) >= totalPages ? 'disabled' : ''} onclick="window._dbState.page=(window._dbState.page||1)+1;Views.database(document.getElementById('view'))">下一页</button>
              </div>
            </div>
          </div>` : '<div class="empty">请选择一个数据表</div>'}
          ${queryRows.length ? `
          <div class="db-tabs mt"><span class="db-tab-info">SQL 查询 · ${queryRows.length} 行</span></div>
          <div class="db-content">
            <table class="tbl">
              <thead><tr>${queryCols.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
              <tbody>
                ${queryRows.map(r => `<tr>${queryCols.map(c => `<td class="small">${escapeHtml(r[c] === null ? '' : String(r[c]).substring(0, 200))}</td>`).join('')}</tr>`).join('')}
              </tbody>
            </table>
          </div>` : ''}
        </div>
      </div>`;
  };
  await render();
};

Views.dbTable = async function (name) {
  window._dbTable = { name, page: 1 };
  await Views._dbTableRender();
};

Views._dbTableRender = async function () {
  const { name, page } = window._dbTable || {};
  if (!name) return;
  const [schemaRes, rowsRes] = await Promise.all([
    api(`/api/db/tables/${encodeURIComponent(name)}/schema`),
    api(`/api/db/tables/${encodeURIComponent(name)}/rows?page=${page}&page_size=50`),
  ]);
  const schema = (schemaRes && schemaRes.data) || [];
  const d = (rowsRes && rowsRes.data) || { rows: [], total: 0, page: 1, page_size: 50 };
  const cols = schema.map(c => c.Field || c.name || c.cid);

  openModal(`
    <div class="modal-head"><span>表 ${escapeHtml(name)} <span class="small muted">共 ${d.total} 行</span></span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body" style="max-height:70vh;overflow:auto">
      <table class="tbl">
        <thead><tr>${cols.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
        <tbody>
          ${(d.rows || []).map(r => `<tr>${cols.map(c => `<td class="small">${escapeHtml(r[c] === null || r[c] === undefined ? '' : r[c])}</td>`).join('')}</tr>`).join('') || '<tr><td colspan="99" class="empty">无数据</td></tr>'}
        </tbody>
      </table>
      ${d.total > d.page * d.page_size ? `
        <div class="flex between mt"><span class="small muted">第 ${d.page} 页 / 共 ${Math.ceil(d.total / d.page_size)} 页</span>
        <button class="btn sm" onclick="window._dbTable.page++;Views._dbTableRender()">下一页</button></div>` : (d.page > 1 ? `
        <div class="flex between mt"><span class="small muted">第 ${d.page} 页 / 共 ${Math.ceil(d.total / d.page_size)} 页</span>
        <button class="btn sm" onclick="window._dbTable.page--;Views._dbTableRender()">上一页</button></div>` : '')}
    </div>
    <div class="modal-foot"><button class="btn" onclick="closeModal()">关闭</button></div>`);
};

Views.sqlConsole = function () {
  openModal(`
    <div class="modal-head"><span>SQL 控制台</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <textarea class="textarea" id="sqlInput" style="min-height:100px;font-family:monospace" placeholder="SELECT * FROM commands LIMIT 10"></textarea>
      <div class="small dim mt">支持 SELECT / INSERT / UPDATE / DELETE / CREATE / ALTER / DROP 等语句，非 SELECT 语句需要二次确认。</div>
      <div id="sqlResult" class="mt"></div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">关闭</button>
      <button class="btn primary" onclick="Views.runSql()">执行</button>
    </div>`);
};

Views.runSql = async function () {
  const sql = document.getElementById('sqlInput').value.trim();
  if (!sql) return;
  const el = document.getElementById('sqlResult');
  el.innerHTML = '<div class="small dim">执行中...</div>';
  const r = await api('/api/db/query', { method: 'POST', body: JSON.stringify({ sql }) });
  if (!r) return;
  if (r.code === 400 && r.write === true) {
    const confirmed = await confirmDialog('确认要执行以下写入操作吗？\n\n' + sql.substring(0, 500), 'SQL 写入确认');
    if (!confirmed) { el.innerHTML = '<div class="small dim">已取消</div>'; return; }
    const r2 = await api('/api/db/query', { method: 'POST', body: JSON.stringify({ sql, write: true }) });
    if (!r2 || r2.code !== 0) {
      el.innerHTML = `<div class="alert err">${escapeHtml((r2 && r2.msg) || '执行失败')}</div>`;
      return;
    }
    el.innerHTML = `<div class="alert ok">✓ 执行成功，影响行数: ${r2.data.count}</div>`;
    closeModal();
    if (window._dbState) {
      window._dbState.queryRows = [];
      Views.database(document.getElementById('view'));
    }
    return;
  }
  if (r.code !== 0) {
    el.innerHTML = `<div class="alert err">${escapeHtml(r.msg || '执行失败')}</div>`;
    return;
  }
  const rows = (r.data && r.data.rows) || [];
  const cols = rows.length ? Object.keys(rows[0]) : [];
  el.innerHTML = `
    <div class="small muted mb">返回 ${r.data.count} 行</div>
    ${r.data.write ? `<div class="alert ok">✓ 写入操作已完成</div>` : ''}
    <div class="table-wrap"><table class="tbl">
      <thead><tr>${cols.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>
      <tbody>${rows.map(rw => `<tr>${cols.map(c => `<td class="small">${escapeHtml(rw[c] === null || rw[c] === undefined ? '' : rw[c])}</td>`).join('')}</tr>`).join('') || '<tr><td class="empty">无结果</td></tr>'}</tbody>
    </table></div>`;
  if (window._dbState) {
    window._dbState.queryRows = rows;
    window._dbState.activeTab = 'sql';
  }
};

/* ─────────── 工具函数 ─────────── */
function fmtUptime(s) {
  if (s === null || s === undefined || isNaN(s)) return '-';
  s = Math.max(0, s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60), sec = s % 60;
  let out = '';
  if (d) out += d + '天';
  if (h) out += h + '时';
  if (m) out += m + '分';
  out += sec + '秒';
  return out;
}

/* ═══════════════ 文件浏览器（宝塔风） ═══════════════ */

function fmtFileSize(n) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let v = n, i = -1;
  do { v = v / 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return v.toFixed(1) + ' ' + units[i];
}

function fmtMtime(t) {
  if (!t) return '-';
  try { return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }); }
  catch (e) { return '-'; }
}

function escapeJs(s) {
  return String(s)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'")
    .replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\r/g, '\\r');
}

function buildCrumbs(p) {
  const segs = p ? p.split(/[\\/]+/).filter(Boolean) : [];
  const parts = [{ label: '根目录', path: '' }];
  let acc = '';
  segs.forEach(s => {
    acc = acc ? acc + '/' + s : s;
    parts.push({ label: s, path: acc });
  });
  return parts;
}

Views.filebrowser = async function (view, basePath) {
  if (typeof basePath === 'string') window._fbPath = basePath;
  else window._fbPath = window._fbPath || '';
  const current = window._fbPath;

  const render = async () => {
    const listRes = await api('/api/files/list?path=' + encodeURIComponent(current));
    const data = (listRes && listRes.code === 0 && listRes.data) || { entries: [] };
    const entries = data.entries || [];
    const dirs = entries.filter(e => e.is_dir);
    const files = entries.filter(e => !e.is_dir);
    const crumbs = buildCrumbs(current);

    view.innerHTML = `
      <div class="card mb fb-main">
        <div class="fb-toolbar">
          <button class="btn" onclick="Views._fbMkdir()">＋ 新建目录</button>
          <button class="btn" onclick="document.getElementById('fbUploadInput').click()">↑ 上传文件</button>
          <input type="file" id="fbUploadInput" multiple style="display:none">
          <input class="input" id="fbSearch" placeholder="搜索名称..." style="max-width:220px">
          <button class="btn" onclick="Views.filebrowser(document.getElementById('view'), window._fbPath)">⟳ 刷新</button>
        </div>
        <div class="fb-crumb">
          ${crumbs.map((c, i) => `
            <span class="seg" onclick="Views.filebrowser(document.getElementById('view'), '${escapeJs(c.path)}')">${escapeHtml(c.label)}</span>
            ${i < crumbs.length - 1 ? '<span class="sep">/</span>' : ''}`).join('')}
        </div>
        <table class="tbl">
          <tr><th>名称</th><th style="width:90px">大小</th><th style="width:170px">修改时间</th><th style="width:200px">操作</th></tr>
          ${!current ? '' : `<tr><td><div class="fb-name" onclick="Views._fbGoUp()">↩ ..（上级）</div></td><td class="small dim">-</td><td class="small dim">-</td><td></td></tr>`}
          ${dirs.map(e => `
            <tr data-path="${encodeURIComponent(e.path)}">
              <td><div class="fb-name" onclick="Views._fbEnterDir('${encodeURIComponent(e.path)}')"><span class="ficon">📁</span>${escapeHtml(e.name)}</div></td>
              <td class="small dim">-</td><td class="small dim">${fmtMtime(e.mtime)}</td>
              <td><div class="fb-actions">
                <button class="btn sm" onclick="Views._fbRename('${encodeURIComponent(e.path)}', '${escapeJs(e.name)}')">重命名</button>
                <button class="btn sm danger" onclick="Views._fbDelete('${encodeURIComponent(e.path)}', '${escapeJs(e.name)}')">删除</button>
              </div></td>
            </tr>`).join('')}
          ${files.map(e => `
            <tr data-path="${encodeURIComponent(e.path)}">
              <td><div class="fb-name" onclick="Views._fbOpenFile('${encodeURIComponent(e.path)}')"><span class="ficon">📄</span>${escapeHtml(e.name)}</div></td>
              <td class="small dim">${fmtFileSize(e.size)}</td><td class="small dim">${fmtMtime(e.mtime)}</td>
              <td><div class="fb-actions">
                <button class="btn sm" onclick="Views._fbDownload('${encodeURIComponent(e.path)}')">下载</button>
                <button class="btn sm" onclick="Views._fbRename('${encodeURIComponent(e.path)}', '${escapeJs(e.name)}')">重命名</button>
                <button class="btn sm danger" onclick="Views._fbDelete('${encodeURIComponent(e.path)}', '${escapeJs(e.name)}')">删除</button>
              </div></td>
            </tr>`).join('')}
        </table>
        ${!entries.length ? '<div class="fb-empty">空目录</div>' : ''}
      </div>
      <div class="card">
        <div class="file-path-bar" id="fbPathBar">点击文件名查看 / 编辑内容</div>
        <textarea class="textarea" id="fbContent" placeholder="选择文件后编辑内容..." readonly style="min-height:280px"></textarea>
        <div class="flex">
          <button class="btn primary" id="fbSaveBtn" style="display:none" onclick="Views._fbSaveFile()">保存</button>
          <span class="small dim ml" id="fbSaveMsg"></span>
        </div>
      </div>`;

    // 上传
    const up = document.getElementById('fbUploadInput');
    if (up) up.addEventListener('change', Views._fbUpload);
    // 搜索过滤
    const search = document.getElementById('fbSearch');
    if (search) search.addEventListener('input', () => {
      const q = search.value.toLowerCase();
      view.querySelectorAll('.tbl tr[data-path]').forEach(tr => {
        const name = decodeURIComponent(tr.getAttribute('data-path')).split(/[\\/]/).pop().toLowerCase();
        tr.style.display = name.indexOf(q) >= 0 ? '' : 'none';
      });
    });
    // 右键菜单
    const fbMain = view.querySelector('.fb-main');
    if (fbMain) fbMain.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const x = Math.min(e.clientX, window.innerWidth - 150);
      const y = Math.min(e.clientY, window.innerHeight - 220);
      const tr = e.target.closest('tr[data-path]');
      if (tr) {
        const p = decodeURIComponent(tr.getAttribute('data-path'));
        const isDir = tr.querySelector('.ficon') && tr.querySelector('.ficon').textContent === '📁';
        const name = p.split(/[\\/]/).pop();
        Views._fbShowMenu(x, y, [
          { label: isDir ? '打开目录' : '打开 / 预览', action: () => isDir ? Views._fbEnterDir(encodeURIComponent(p)) : Views._fbOpenFile(encodeURIComponent(p)) },
          { label: '下载', show: !isDir, action: () => Views._fbDownload(encodeURIComponent(p)) },
          { label: '复制副本', action: () => Views._fbCopy(p) },
          { label: '重命名', action: () => Views._fbRename(encodeURIComponent(p), name) },
          { label: '删除', danger: true, action: () => Views._fbDelete(encodeURIComponent(p), name) },
        ]);
      } else {
        Views._fbShowMenu(x, y, [
          { label: '新建目录', action: () => Views._fbMkdir() },
          { label: '上传文件', action: () => document.getElementById('fbUploadInput').click() },
          { label: '刷新', action: () => Views.filebrowser(document.getElementById('view'), window._fbPath) },
        ]);
      }
    });
  };

  await render();
};

Views._fbShowMenu = function (x, y, items) {
  Views._fbCloseMenu();
  const menu = document.createElement('div');
  menu.className = 'fb-menu';
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.innerHTML = items.filter(i => i.show !== false)
    .map(i => `<div class="fb-menu-item${i.danger ? ' danger' : ''}">${i.label}</div>`).join('');
  document.body.appendChild(menu);
  menu.querySelectorAll('.fb-menu-item').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      Views._fbCloseMenu();
      const idx = Array.prototype.indexOf.call(menu.querySelectorAll('.fb-menu-item'), el);
      const it = items.filter(i => i.show !== false)[idx];
      it && it.action && it.action();
    });
  });
  setTimeout(() => {
    document.addEventListener('mousedown', (ev) => {
      if (!menu.contains(ev.target)) Views._fbCloseMenu();
    }, { once: true });
  }, 0);
};

Views._fbCloseMenu = function () {
  const m = document.getElementById('fbMenu');
  if (m) m.remove();
};

Views._fbCopy = async function (path) {
  const r = await api('/api/files/copy', {
    method: 'POST',
    body: JSON.stringify({ src: path, dest_dir: window._fbPath || '' }),
  });
  toast((r && r.msg) || '复制失败', r && r.code === 0 ? 'success' : 'error');
  if (r && r.code === 0) Views.filebrowser(document.getElementById('view'), window._fbPath);
};

Views._fbEnterDir = function (path) {
  const p = decodeURIComponent(path);
  window._fbPath = p;
  window._fbFile = '';
  Views.filebrowser(document.getElementById('view'), p);
};

Views._fbGoUp = function () {
  const parent = window._fbPath ? window._fbPath.replace(/[\\/]+$/, '').split(/[\\/]/).slice(0, -1).join('/') : '';
  window._fbPath = parent || '';
  window._fbFile = '';
  Views.filebrowser(document.getElementById('view'), parent || '');
};

Views._fbMkdir = function () {
  const mask = openModal(`
    <div class="modal-head"><span>新建目录</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="form-row"><label>目录名</label><input class="input" id="fbMkdirName" placeholder="请输入目录名"></div>
      <div class="small dim">位置：${escapeHtml(window._fbPath || '根目录')}</div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views._fbMkdirDo()">创建</button>
    </div>`);
  const input = mask.querySelector('#fbMkdirName');
  setTimeout(() => input && input.focus(), 50);
};

Views._fbMkdirDo = async function () {
  const name = (document.getElementById('fbMkdirName').value || '').trim();
  if (!name || /[\\/]/.test(name) || name === '.' || name === '..') { toast('非法目录名', 'warning'); return; }
  const target = window._fbPath ? window._fbPath.replace(/[\\/]+$/, '') + '/' + name : name;
  const r = await api('/api/files/mkdir', { method: 'POST', body: JSON.stringify({ path: target }) });
  toast((r && r.msg) || '创建失败', r && r.code === 0 ? 'success' : 'error');
  if (r && r.code === 0) { closeModal(); Views.filebrowser(document.getElementById('view'), window._fbPath); }
};

Views._fbUpload = async function () {
  const input = document.getElementById('fbUploadInput');
  if (!input || !input.files || !input.files.length) return;
  const fd = new FormData();
  fd.append('dir', window._fbPath || '');
  for (const f of input.files) fd.append('files', f);
  const r = await api('/api/files/upload', { method: 'POST', body: fd });
  toast((r && r.msg) || '上传失败', r && r.code === 0 ? 'success' : 'error');
  if (r && r.code === 0) Views.filebrowser(document.getElementById('view'), window._fbPath);
};

Views._fbRename = function (path, oldName) {
  const mask = openModal(`
    <div class="modal-head"><span>重命名</span><button class="modal-close" onclick="closeModal()">✕</button></div>
    <div class="modal-body">
      <div class="form-row"><label>新名称</label><input class="input" id="fbRenameName" value="${escapeHtml(oldName)}"></div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn primary" onclick="Views._fbRenameDo('${path}')">重命名</button>
    </div>`);
  const input = mask.querySelector('#fbRenameName');
  setTimeout(() => { if (input) { input.focus(); input.select(); } }, 50);
};

Views._fbRenameDo = async function (path) {
  const newName = (document.getElementById('fbRenameName').value || '').trim();
  if (!newName || /[\\/]/.test(newName) || newName === '.' || newName === '..') { toast('非法名称', 'warning'); return; }
  const r = await api('/api/files/rename', {
    method: 'POST',
    body: JSON.stringify({ path: decodeURIComponent(path), new_name: newName }),
  });
  toast((r && r.msg) || '重命名失败', r && r.code === 0 ? 'success' : 'error');
  if (r && r.code === 0) { closeModal(); Views.filebrowser(document.getElementById('view'), window._fbPath); }
};

Views._fbDelete = async function (path, name) {
  if (!await confirmDialog(`确定删除「${escapeHtml(name)}」吗？目录将递归删除，不可恢复。`, '删除')) return;
  const r = await api('/api/files/delete', { method: 'POST', body: JSON.stringify({ path: decodeURIComponent(path) }) });
  toast((r && r.msg) || '删除失败', r && r.code === 0 ? 'success' : 'error');
  if (r && r.code === 0) {
    if (window._fbFile === decodeURIComponent(path)) window._fbFile = '';
    Views.filebrowser(document.getElementById('view'), window._fbPath);
  }
};

Views._fbDownload = function (path) {
  window.open('/api/files/download?path=' + path, '_blank');
};

Views._fbOpenFile = async function (path) {
  path = decodeURIComponent(path);
  window._fbFile = path;
  const res = await api('/api/files/read?path=' + encodeURIComponent(path));
  const el = document.getElementById('fbContent');
  const bar = document.getElementById('fbPathBar');
  const saveBtn = document.getElementById('fbSaveBtn');
  if (!el) return;
  if (!res || res.code !== 0) {
    el.value = '// ' + ((res && res.msg) || '无法读取文件');
    el.readOnly = true;
    if (saveBtn) saveBtn.style.display = 'none';
    return;
  }
  el.value = res.data.content;
  el.readOnly = false;
  if (bar) bar.textContent = res.data.path;
  if (saveBtn) saveBtn.style.display = 'inline-flex';
  document.querySelectorAll('.tbl tr[data-path]').forEach(tr =>
    tr.classList.toggle('active', tr.getAttribute('data-path') === encodeURIComponent(path)));
};

Views._fbSaveFile = async function () {
  const path = window._fbFile;
  const content = document.getElementById('fbContent').value;
  if (!path) return;
  const msg = document.getElementById('fbSaveMsg');
  if (msg) msg.textContent = '保存中...';
  const r = await api('/api/files/write', {
    method: 'PUT',
    body: JSON.stringify({ path, content }),
  });
  if (msg) {
    if (r && r.code === 0) { msg.textContent = '✓ 已保存'; setTimeout(() => msg.textContent = '', 2000); }
    else msg.textContent = '✗ ' + ((r && r.msg) || '保存失败');
  }
};

/* ═══════════════ 依赖关系图（已安装插件） ═══════════════ */
Views.depgraph = async function (view) {
  const res = await api('/api/plugins/deps/graph');
  if (!res || res.code !== 0) {
    view.innerHTML = `<div class="card"><div class="empty">${escapeHtml((res && res.msg) || '获取依赖图数据失败')}</div></div>`;
    return;
  }
  const d = res.data;
  const nodes = d.nodes || [];
  const edges = d.edges || [];

  view.innerHTML = `
    <div class="flex between mb">
      <div class="flex">
        <button class="btn" onclick="Views.depgraph(document.getElementById('view'))">⟳ 刷新</button>
      </div>
      <span class="muted small">${d.total_plugins} 个插件 · ${d.total_edges} 条共享依赖关联</span>
    </div>
    ${nodes.length ? `
    <div class="card">
      <div class="card-title">依赖关系图</div>
      <div class="dep-graph-wrap">${Views._renderDepGraphSVG(nodes, edges)}</div>
    </div>
    <div class="grid cols-2">
      <div class="card">
        <div class="card-title">插件依赖详情</div>
        ${nodes.map(n => {
          const missing = n.missing_count ? `<span class="badge err">缺 ${n.missing_count}</span>` : '';
          const depsList = n.deps && n.deps.length
            ? n.deps.map(d => `<div class="small mono" style="padding:1px 0">${escapeHtml(d.raw)} ${d.missing ? '<span class="badge err">缺失</span>' : ''}${d.conflict ? '<span class="badge warn">冲突</span>' : ''}</div>`).join('')
            : '<div class="small dim">无 Python 依赖</div>';
          return `<div style="padding:8px 0;border-bottom:1px solid var(--border)">
            <div class="flex between"><b>${escapeHtml(n.id)}</b> <span class="small dim">v${escapeHtml(n.version)}</span></div>
            <div class="small" style="margin-top:4px">${n.dep_count} 个依赖 ${missing}</div>
            <div style="margin-top:4px;padding-left:12px;border-left:2px solid var(--border)">${depsList}</div>
          </div>`;
        }).join('')}
      </div>
      <div class="card">
        <div class="card-title">共享依赖关联</div>
        ${edges.length
          ? edges.map(e => `<div class="flex between" style="padding:6px 0;border-bottom:1px solid var(--border)">
            <span class="small">${escapeHtml(e.source)} ↔ ${escapeHtml(e.target)}</span>
            <span class="small dim">${escapeHtml(e.label)}</span>
          </div>`).join('')
          : '<div class="empty">暂无共享依赖</div>'}
        <div class="small dim mt">共享依赖 = 多个插件依赖同一个 Python 包，可能产生版本冲突。</div>
      </div>
    </div>` : '<div class="card"><div class="empty">暂无已安装插件</div></div>'}
    <div class="card">
      <div class="card-title">依赖状态说明</div>
      <div class="small" style="line-height:1.8">
        <div><span class="badge ok" style="margin-right:6px">已满足</span> 依赖已安装且版本兼容</div>
        <div><span class="badge err" style="margin-right:6px">缺失</span> 依赖包未安装</div>
        <div><span class="badge warn" style="margin-right:6px">冲突</span> 已安装但版本不满足要求</div>
        <div class="dim mt">共享依赖关系图基于已安装插件列表构建，节点表示插件，边表示共享的 Python 包。</div>
      </div>
    </div>`;
};
