<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never">
          <template #header><div class="card-head">全局设置</div></template>
          <el-tabs v-model="activeTab">
            <el-tab-pane v-for="g in SETTING_GROUPS" :key="g.key" :label="g.title" :name="g.key">
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
              <span class="dim small ml">部分字段（端口、地址等）需重启框架生效</span>
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
            <el-button @click="loadBackups">查看备份 / 回滚</el-button>
            <el-button type="danger" @click="restart">重启框架</el-button>
          </div>
          <div v-if="fwInfo">
            <div class="small mb">
              本地: <span class="mono">{{ fwInfo.local_version }}</span> |
              最新: <span class="mono">{{ fwInfo.latest_version }}</span>
              <div class="dim">{{ fwInfo.commit_message }} · {{ fwInfo.author || '' }} · {{ fwInfo.commit_date || '' }}</div>
            </div>
            <el-button v-if="fwInfo.has_update === true" type="primary" size="small" @click="doFrameworkUpdate">立即更新框架</el-button>
            <el-tag v-else-if="fwInfo.has_update === null || fwInfo.has_update === undefined" type="warning" size="small">本地版本未知</el-tag>
            <el-tag v-else type="success" size="small">已是最新版本</el-tag>
          </div>
          <div v-if="backups.length" class="mt">
            <div class="small mb">框架更新备份（{{ backups.length }}）:</div>
            <div v-for="b in backups" :key="b.name" class="backup-row">
              <span class="small mono">{{ b.name }} <span class="dim">({{ b.time }})</span></span>
              <el-button size="small" type="danger" @click="rollback(b.name)">回滚</el-button>
            </div>
          </div>
          <div class="dim small mt">更新框架仅覆盖代码（framework/web/main.py 等），自动保留 plugins/、data/、config.yaml；旧代码备份到 data/backups/，更新后需重启生效。</div>
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

const SETTING_GROUPS = [
  { key: 'web', title: 'Web 服务' },
  { key: 'onebot', title: 'OneBot 连接' },
  { key: 'database', title: '数据库' },
  { key: 'log', title: '日志' },
  { key: 'plugin', title: '插件' },
  { key: 'system', title: '系统' },
]
const SETTING_LABELS = {
  web: { host: '监听地址', port: '监听端口', secret_key: 'Secret Key', session_timeout: '会话超时（秒）' },
  onebot: { listen_host: '监听地址', listen_port: '监听端口', access_token: 'Access Token' },
  database: { type: '数据库类型', path: '数据库路径', host: '主机', port: '端口', user: '用户名', password: '密码', database: '库名' },
  log: { level: '日志级别', file: '日志文件', retention_days: '日志保留（天）', log_raw_message: '记录原始消息', log_sent_message: '记录发送消息' },
  plugin: { dir: '插件目录', dat_dir: '插件数据目录', heartbeat_interval: '心跳间隔（秒）', auto_install_deps_on_startup: '启动自动装依赖', max_memory_mb: '单插件内存上限（MB）' },
  system: { show_cpu: '显示 CPU', show_disk: '显示磁盘', status_interval: '状态刷新间隔（秒）' },
}

const activeTab = ref('web')
const activeTabTitle = computed(() => SETTING_GROUPS.find(g => g.key === activeTab.value)?.title || '')
const yamlCfg = ref({})
const adminList = ref([])
const pwd = ref({ old: '', new: '' })
const fwInfo = ref(null)
const backups = ref([])
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

async function load() {
  const [admins, yamlRes] = await Promise.all([
    api('/api/admins').catch(() => null),
    api('/api/config/yaml').catch(() => null),
  ])
  adminList.value = (admins && admins.data) || []
  yamlCfg.value = (yamlRes && yamlRes.data) || {}
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
  if (r) fwInfo.value = r.data
}
async function doFrameworkUpdate() {
  await ElMessageBox.confirm('确定更新框架吗？\n将覆盖 framework/web/main.py 等代码，自动保留插件与配置，更新后需重启生效。', '更新框架', { type: 'warning' })
  const r = await apiCall('/api/framework/update', { method: 'POST' })
  if (r) { ElMessage.success(r.msg); checkUpdate() }
}
async function loadBackups() {
  const r = await apiCall('/api/framework/backups')
  if (r) backups.value = (r.data && r.data.backups) || []
}
async function rollback(name) {
  await ElMessageBox.confirm(`确定回滚到 ${name} 吗？\n当前 framework 代码将被替换（旧代码留档 data/backups/current/），回滚后需重启生效。`, '回滚框架', { type: 'warning' })
  const r = await apiCall('/api/framework/rollback', { method: 'POST', body: { backup: name } })
  if (r) { ElMessage.success(r.msg); loadBackups() }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
.backup-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
.ml { margin-left: 8px; }
</style>