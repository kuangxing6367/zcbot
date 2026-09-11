<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header><div class="card-head">全局设置</div></template>
          <el-tabs v-model="activeTab">
            <el-tab-pane v-for="g in SETTING_GROUPS" :key="g.key" :label="g.title" :name="g.key">
              <template v-if="g.key === 'sidebar'">
                <div class="dim small mb">勾选要在左侧栏显示的官方菜单，用「上移 / 下移」调整顺序（「设置」固定在底部）。保存后立即生效。</div>
                <div v-for="(it, idx) in sidebarItems" :key="it.key" class="sb-row">
                  <el-switch v-model="it.visible" />
                  <span class="sb-title">{{ it.title }}</span>
                  <span class="mono dim small">{{ it.key }}</span>
                  <span class="sb-spacer" />
                  <el-button size="small" :disabled="idx === 0" @click="moveItem(idx, -1)">上移</el-button>
                  <el-button size="small" :disabled="idx === sidebarItems.length - 1" @click="moveItem(idx, 1)">下移</el-button>
                </div>
                <el-alert v-if="session.admin?.role !== 'super'" type="warning" :closable="false" title="仅超管可修改配置" style="margin-bottom:12px" />
                <div class="mt">
                  <el-button v-if="session.admin?.role === 'super'" type="primary" @click="saveSidebar">保存侧边栏</el-button>
                  <el-button v-if="session.admin?.role === 'super'" @click="resetSidebar">恢复默认</el-button>
                </div>
              </template>
              <template v-else>
                <el-form label-width="180px">
                  <el-form-item v-for="(v, k) in yamlCfg[g.key]" :key="k" :label="(SETTING_LABELS[g.key] || {})[k] || k">
                    <el-switch v-if="typeof v === 'boolean'" v-model="yamlCfg[g.key][k]" />
                    <el-input-number v-else-if="typeof v === 'number'" v-model="yamlCfg[g.key][k]" :controls="false" style="width:100%" />
                    <el-input v-else-if="v === null || v === undefined" v-model="yamlCfg[g.key][k]" />
                    <el-input v-else-if="typeof v === 'object'" v-model="yamlCfg[g.key][k]" type="textarea" :rows="3" class="mono"
                              :model-value="jsonText(g.key, k)"
                              @update:model-value="parseJson(g.key, k, $event)" />
                    <el-input v-else v-model="yamlCfg[g.key][k]"
                              :type="(k === 'access_token' || k === 'password' || k === 'secret_key') ? 'password' : 'text'" show-password />
                  </el-form-item>
                </el-form>
                <el-alert v-if="session.admin?.role !== 'super'" type="warning" :closable="false" title="仅超管可修改配置" style="margin-bottom:12px" />
                <el-button v-if="session.admin?.role === 'super'" type="primary" @click="saveSection">保存 {{ activeTabTitle }}</el-button>
                <span class="dim small ml">部分字段（端口、SSL 证书等）需重启框架生效；证书路径支持绝对路径或相对项目根目录</span>
              </template>
            </el-tab-pane>
          </el-tabs>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never" class="mb">
          <template #header><div class="card-head">修改密码</div></template>
          <el-form label-width="80px">
            <el-form-item label="旧密码"><el-input v-model="pwd.old" type="password" show-password /></el-form-item>
            <el-form-item label="新密码"><el-input v-model="pwd.new" type="password" show-password /></el-form-item>
          </el-form>
          <el-button type="primary" @click="changePassword">修改密码</el-button>
        </el-card>

        <el-card shadow="never">
          <template #header><div class="card-head">框架操作</div></template>
          <div class="mb">
            <el-button @click="checkUpdate">检查框架更新</el-button>
            <el-button type="danger" @click="restart">重启框架</el-button>
          </div>
          <div v-if="fwInfo">
            <div class="small mb">
              本地: <span class="mono">{{ fwInfo.local_version }}</span> |
              最新: <span class="mono">{{ fwInfo.latest_version }}</span>
              <div class="dim">{{ fwInfo.commit_message }} · {{ fwInfo.author || '' }} · {{ fwInfo.commit_date || '' }}</div>
            </div>
            <div class="mb">
              <div class="small mb" style="color:var(--el-text-color-secondary)">目标版本</div>
              <el-select v-model="targetVersion" placeholder="选择更新版本" style="width: 200px">
                <el-option v-if="fwInfo.latest_version && fwInfo.latest_version !== '未知'" :label="`最新版 ${fwInfo.latest_version}`" :value="fwInfo.latest_version" />
                <el-option
                  v-for="v in (fwInfo.available_versions || []).filter(x => x.version !== fwInfo.latest_version)"
                  :key="v.tag" :label="`v${v.version}`" :value="v.version" />
              </el-select>
              <el-button type="primary" size="small" style="margin-left:8px" @click="doFrameworkUpdate">更新到所选版本</el-button>
            </div>
          </div>
          <div class="dim small mt">更新框架仅覆盖代码（framework/web/main.py 等），自动保留 plugins/、data/、config.yaml；更新后需重启生效。</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-head">
          管理员账号
          <el-button v-if="session.admin?.role === 'super'" size="small" type="primary" @click="addVisible = true">＋ 添加</el-button>
        </div>
      </template>
      <el-table :data="adminList" size="small" border>
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="username" label="用户名" />
        <el-table-column label="角色" width="90">
          <template #default="{ row }">
            <el-tag :type="row.role === 'super' ? 'warning' : 'primary'" size="small">{{ row.role === 'super' ? '超管' : '管理' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '禁用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后登录" width="160">
          <template #default="{ row }">{{ fmtTime(row.last_login_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button v-if="session.admin?.role === 'super' && row.id !== session.admin?.id" size="small" type="danger" @click="deleteAdmin(row)">删除</el-button>
            <span v-else-if="row.id === session.admin?.id" class="dim small">当前账号</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-dialog v-model="addVisible" title="添加管理员" width="420px">
      <el-form label-width="80px">
        <el-form-item label="用户名"><el-input v-model="admForm.username" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="admForm.password" type="password" show-password /></el-form-item>
        <el-form-item label="角色">
          <el-select v-model="admForm.role" style="width:100%">
            <el-option label="管理员" value="admin" />
            <el-option label="超级管理员" value="super" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" @click="addAdmin">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall, session, fmtTime } from '../api'
import { OFFICIAL_SIDEBAR_ITEMS } from '../sidebar'

const SETTING_GROUPS = [
  { key: 'web', title: 'Web 服务' },
  { key: 'ssl', title: 'SSL / TLS' },
  { key: 'sidebar', title: '侧边栏' },
  { key: 'onebot', title: 'OneBot 连接' },
  { key: 'database', title: '数据库' },
  { key: 'log', title: '日志' },
  { key: 'plugin', title: '插件' },
  { key: 'system', title: '系统' },
]
const SETTING_LABELS = {
  web: { host: '监听地址', port: '监听端口', secret_key: 'Secret Key', session_timeout: '会话超时（秒）', official_sidebar: '显示官方侧边栏' },
  ssl: { enabled: '启用 HTTPS / WSS', cert: '证书路径（cert）', key: '私钥路径（key）' },
  onebot: { listen_host: '监听地址', listen_port: '监听端口', access_token: 'Access Token' },
  database: { type: '数据库类型', path: '数据库路径', host: '主机', port: '端口', user: '用户名', password: '密码', database: '库名' },
  log: { level: '日志级别', file: '日志文件', retention_days: '日志保留（天）', log_raw_message: '记录原始消息', log_sent_message: '记录发送消息' },
  plugin: { dir: '插件目录', dat_dir: '插件数据目录', heartbeat_interval: '心跳间隔（秒）', auto_install_deps_on_startup: '启动自动装依赖', max_memory_mb: '单插件内存上限（MB）' },
  system: { show_cpu: '显示 CPU', show_disk: '显示磁盘', status_interval: '状态刷新间隔（秒）' },
}

const activeTab = ref('web')
const activeTabTitle = computed(() => SETTING_GROUPS.find(g => g.key === activeTab.value)?.title || '')
const yamlCfg = ref({})
const sidebarItems = ref([])
const adminList = ref([])
const pwd = ref({ old: '', new: '' })
const fwInfo = ref(null)
const targetVersion = ref('')
const addVisible = ref(false)
const admForm = ref({ username: '', password: '', role: 'admin' })

function jsonText(group, key) {
  const v = yamlCfg.value[group] && yamlCfg.value[group][key]
  return typeof v === 'object' ? JSON.stringify(v) : ''
}
function parseJson(group, key, text) {
  try { yamlCfg.value[group][key] = JSON.parse(text) }
  catch (e) { /* 保持原值 */ }
}

function buildSidebarItems() {
  const cfg = (yamlCfg.value.web && yamlCfg.value.web.sidebar) || {}
  const hidden = new Set(cfg.hidden || [])
  const order = cfg.order || []
  const pos = {}
  order.forEach((k, i) => { pos[k] = i })
  const items = OFFICIAL_SIDEBAR_ITEMS.slice()
    .sort((a, b) => (pos[a.key] ?? 999) - (pos[b.key] ?? 999))
  sidebarItems.value = items.map(it => ({ key: it.key, title: it.title, visible: !hidden.has(it.key) }))
}

function moveItem(idx, dir) {
  const arr = sidebarItems.value
  const j = idx + dir
  if (j < 0 || j >= arr.length) return
  const tmp = arr[idx]; arr[idx] = arr[j]; arr[j] = tmp
}

async function saveSidebar() {
  const order = sidebarItems.value.map(it => it.key)
  const hidden = sidebarItems.value.filter(it => !it.visible).map(it => it.key)
  const r = await apiCall('/api/config/yaml/web', { method: 'PUT', body: { data: { sidebar: { order, hidden } } } })
  if (!r) return
  ElMessage.success(r.msg)
  // 更新共享状态：左侧栏立即按新配置重绘，无需刷新页面
  session.sidebar = { order, hidden }
  yamlCfg.value.web = yamlCfg.value.web || {}
  yamlCfg.value.web.sidebar = { order, hidden }
}

function resetSidebar() {
  sidebarItems.value = OFFICIAL_SIDEBAR_ITEMS.map(it => ({ key: it.key, title: it.title, visible: true }))
}

async function load() {
  const [admins, yamlRes] = await Promise.all([
    api('/api/admins').catch(() => null),
    api('/api/config/yaml').catch(() => null),
  ])
  adminList.value = (admins && admins.data) || []
  yamlCfg.value = (yamlRes && yamlRes.data) || {}
  buildSidebarItems()
}

async function saveSection() {
  const section = activeTab.value
  const r = await apiCall(`/api/config/yaml/${section}`, { method: 'PUT', body: { data: yamlCfg.value[section] || {} } })
  if (r) ElMessage.success(r.msg)
}

async function changePassword() {
  if (!pwd.value.old || !pwd.value.new) { ElMessage.warning('请填写完整'); return }
  const r = await apiCall('/api/change_password', { method: 'POST', body: { old_password: pwd.value.old, new_password: pwd.value.new } })
  if (r) { ElMessage.success(r.msg); pwd.value.old = ''; pwd.value.new = '' }
}

async function addAdmin() {
  const r = await apiCall('/api/admins', { method: 'POST', body: { ...admForm.value } })
  if (r) { ElMessage.success(r.msg); addVisible.value = false; load() }
}
async function deleteAdmin(row) {
  await ElMessageBox.confirm('确定删除该管理员吗？', '提示', { type: 'warning' })
  const r = await apiCall(`/api/admins/${row.id}`, { method: 'DELETE' })
  if (r) { ElMessage.success(r.msg); load() }
}

async function restart() {
  await ElMessageBox.confirm('确定重启框架吗？正在处理的消息可能丢失。', '重启框架', { type: 'warning' })
  const r = await apiCall('/api/restart', { method: 'POST' })
  if (r) ElMessage.success(r.msg)
}

async function checkUpdate() {
  const r = await apiCall('/api/framework/check_update')
  if (r) { fwInfo.value = r.data; targetVersion.value = (r.data && r.data.latest_version !== '未知') ? r.data.latest_version : '' }
}
async function doFrameworkUpdate() {
  const ver = targetVersion.value
  const desc = ver ? `将更新框架到版本 ${ver}` : '将更新框架到最新版本'
  await ElMessageBox.confirm(`确定更新框架吗？\n${desc}\n将覆盖 framework/web/main.py 等代码，自动保留插件与配置，更新后需重启生效。`, '更新框架', { type: 'warning' })
  const body = ver ? { version: ver } : {}
  const r = await apiCall('/api/framework/update', { method: 'POST', body })
  if (r) { ElMessage.success(r.msg); checkUpdate() }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
.ml { margin-left: 8px; }
.sb-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--el-border-color); }
.sb-title { min-width: 84px; }
.sb-spacer { flex: 1; }
</style>