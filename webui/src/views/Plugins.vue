<template>
  <div class="page-container">
    <div class="toolbar mb">
      <el-button @click="load"><el-icon><Refresh /></el-icon>&nbsp;刷新</el-button>
      <el-button type="primary" @click="uploadVisible = true"><el-icon><Upload /></el-icon>&nbsp;上传插件</el-button>
      <el-input v-model="keyword" placeholder="搜索插件名/描述" style="width:220px" clearable
                @keyup.enter="applyFilter" @clear="applyFilter" />
      <span class="dim small">共 {{ filtered.length }} 个插件</span>
    </div>

    <el-row :gutter="16">
      <el-col v-for="p in filtered" :key="p.plugin_name" :xs="24" :sm="12" :md="8" :lg="6">
        <el-card shadow="hover" class="plugin-card">
          <div class="plugin-head">
            <div>
              <div class="plugin-name">{{ p.plugin_name }}</div>
              <div class="plugin-ver small">
                v{{ p.version || '0.0.0' }}
                <el-tag size="small" :type="statusTag(p.status).type">{{ statusTag(p.status).text }}</el-tag>
              </div>
            </div>
            <el-tag size="small" :type="p.is_loaded ? 'success' : 'info'" effect="plain">
              {{ p.is_loaded ? '已加载' : '未加载' }}
            </el-tag>
          </div>
          <div class="plugin-desc">{{ p.description || '暂无描述' }}</div>
          <div class="plugin-badges">
            <el-tag v-if="p.has_missing_deps" type="danger" size="small">缺依赖 {{ p.missing_deps.length }}</el-tag>
            <el-tag v-if="p.has_conflict" type="warning" size="small">版本冲突</el-tag>
            <el-tag size="small" effect="plain">{{ p.command_count || 0 }} 命令</el-tag>
            <el-tag size="small" effect="plain">{{ p.task_count || 0 }} 任务</el-tag>
          </div>
          <div class="plugin-actions">
            <el-button size="small" @click="showDetail(p)">详情</el-button>
            <el-button size="small" @click="showCommands(p)">命令</el-button>
            <el-button size="small" @click="showConfig(p)">配置</el-button>
            <el-button size="small" @click="showGroups(p)">群开关</el-button>
            <el-button v-if="p.has_yaml && p.github_repo" size="small" type="warning" @click="doUpdate(p)">更新</el-button>
            <el-button size="small" @click="doReload(p)">重载</el-button>
            <el-button size="small" :type="p.is_active ? 'danger' : 'success'" @click="doToggle(p)">
              {{ p.is_active ? '禁用' : '启用' }}
            </el-button>
            <el-button size="small" type="danger" @click="doDelete(p)">删除</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>
    <el-empty v-if="!filtered.length" :description="keyword ? '未找到匹配插件' : '暂无插件，点击「上传插件」安装'" />

    <!-- 上传 -->
    <el-dialog v-model="uploadVisible" title="上传插件 (ZIP)" width="460px">
      <el-alert type="info" :closable="false" show-icon title="ZIP 内需包含 main.py；配置文件将自动迁移到 plugins_dat 目录。" style="margin-bottom:12px" />
      <input type="file" ref="upFile" accept=".zip" style="margin-bottom:12px" />
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="doUpload">上传并加载</el-button>
      </template>
    </el-dialog>

    <!-- 详情 -->
    <el-dialog v-model="detailVisible" :title="'插件详情 · ' + curName" width="600px">
      <template v-if="detail">
        <div class="mb">
          <el-button v-if="detail.has_yaml && detail.yaml?.github?.repo" size="small" type="warning"
                     @click="doUpdate({ plugin_name: curName })">GitHub 更新</el-button>
          <el-button size="small" @click="showReadme">README</el-button>
          <el-button size="small" @click="installDeps">安装依赖(全局)</el-button>
          <el-button size="small" @click="createVenv">创建虚拟环境</el-button>
        </div>
        <el-alert v-if="detail.dep_status?.has_missing" type="error" :closable="false"
                  :title="'缺失依赖: ' + detail.dep_status.missing.join(', ')" style="margin-bottom:8px" />
        <el-alert v-if="detail.dep_status?.has_conflict" type="warning" :closable="false"
                  :title="'版本冲突(已自动跳过)'" style="margin-bottom:8px">
          <div v-for="c in detail.dep_status.conflicts" :key="c.name">
            {{ c.name }} {{ c.required }} (已装 {{ c.installed }})
          </div>
        </el-alert>
        <el-descriptions :column="1" border size="small">
          <el-descriptions-item label="GitHub 仓库" v-if="detail.yaml?.github?.repo">
            {{ detail.yaml.github.repo }}@{{ detail.yaml.github.branch || 'main' }}
          </el-descriptions-item>
          <el-descriptions-item label="依赖 (Python)">
            {{ (detail.dependencies?.python || []).join(', ') || '无' }}
          </el-descriptions-item>
          <el-descriptions-item label="配置文件">
            <div v-for="f in detail.config_files" :key="f.name">{{ f.name }} ({{ f.size }}B)</div>
            <span v-if="!(detail.config_files || []).length">无</span>
          </el-descriptions-item>
        </el-descriptions>
      </template>
    </el-dialog>

    <!-- README -->
    <el-dialog v-model="readmeVisible" :title="'README · ' + curName" width="700px">
      <pre class="readme-pre">{{ readmeContent }}</pre>
    </el-dialog>

    <!-- 命令与任务 -->
    <el-dialog v-model="commandsVisible" :title="'命令与任务 · ' + curName" width="760px">
      <template v-if="pluginCmds">
        <h4>命令 ({{ allCmds.length }})</h4>
        <el-table :data="allCmds" size="small" border max-height="300">
          <el-table-column prop="pattern" label="命令" width="180">
            <template #default="{ row }">
              <span class="mono">{{ row.pattern }}</span>
              <div v-if="row.alias" class="dim small">别名: {{ row.alias }}</div>
            </template>
          </el-table-column>
          <el-table-column prop="description" label="描述" />
          <el-table-column label="权限" width="80">
            <template #default="{ row }">
              <el-tag v-if="row.require_level === 'super'" type="warning" size="small">超管</el-tag>
              <el-tag v-else-if="row.require_level === 'admin'" size="small">管理</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="hit_count" label="命中" width="70" />
        </el-table>
        <h4 class="mt">定时任务 ({{ pluginCmds.tasks.length }})</h4>
        <el-table :data="pluginCmds.tasks" size="small" border max-height="200">
          <el-table-column prop="cron_expression" label="Cron" />
          <el-table-column prop="description" label="描述" />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '禁用' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-dialog>

    <!-- 配置 -->
    <el-dialog v-model="configVisible" :title="'插件配置 · ' + curName" width="520px">
      <template v-if="configSchema">
        <el-form label-width="140px">
          <el-form-item v-for="(spec, key) in configSchema.schema" :key="key" :label="String(key)">
            <el-switch v-if="isBool(spec)" v-model="configValues[key]" />
            <el-input-number v-else-if="isNumber(spec)" v-model="configValues[key]" :controls="false" style="width:100%" />
            <el-input v-else-if="Array.isArray(configValues[key])"
                      :model-value="(configValues[key] || []).join(',')"
                      @update:model-value="v => configValues[key] = String(v).split(',').map(s => s.trim()).filter(Boolean)" />
            <el-input v-else :model-value="configValues[key] ?? ''"
                      @update:model-value="v => configValues[key] = v" />
            <div class="dim small" v-if="spec.description">{{ spec.description }}</div>
          </el-form-item>
        </el-form>
      </template>
      <template #footer>
        <el-button @click="configVisible = false">取消</el-button>
        <el-button type="primary" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

    <!-- 群开关 -->
    <el-dialog v-model="groupsVisible" :title="'群级开关 · ' + curName" width="520px">
      <el-table :data="groupRows" size="small" border>
        <el-table-column prop="group_id" label="群号" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '禁用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100">
          <template #default="{ row }">
            <el-button size="small" :type="row.enabled ? 'danger' : 'success'" @click="toggleGroup(row)">
              {{ row.enabled ? '禁用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!groupRows.length" description="该插件在所有群均为默认启用" :image-size="60" />
      <div class="dim small mt">表中无记录 = 默认启用；仅在群内禁用过的插件会出现在此。</div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const plugins = ref([])
const keyword = ref('')
const filtered = computed(() => {
  const k = keyword.value.toLowerCase()
  if (!k) return plugins.value
  return plugins.value.filter(p =>
    p.plugin_name.toLowerCase().includes(k) || (p.description || '').toLowerCase().includes(k))
})

const uploadVisible = ref(false)
const uploading = ref(false)
const upFile = ref(null)

const detailVisible = ref(false)
const detail = ref(null)
const curName = ref('')

const readmeVisible = ref(false)
const readmeContent = ref('')

const commandsVisible = ref(false)
const pluginCmds = ref(null)
const allCmds = computed(() => [...(pluginCmds.value?.static_commands || []), ...(pluginCmds.value?.dynamic_commands || [])])

const configVisible = ref(false)
const configSchema = ref(null)
const configValues = ref({})

const groupsVisible = ref(false)
const groupRows = ref([])

async function load() {
  const r = await apiCall('/api/plugins')
  if (r) plugins.value = r.data || []
}
function statusTag(s) {
  const map = { running: ['success', '运行中'], stopped: ['info', '已停止'], error: ['danger', '错误'], oom: ['warning', 'OOM'] }
  const [type, text] = map[s] || ['info', s || '-']
  return { type, text }
}

async function showDetail(p) {
  curName.value = p.plugin_name
  const r = await apiCall(`/api/plugins/${p.plugin_name}/config`)
  if (r) { detail.value = r.data; detailVisible.value = true }
}
async function showReadme() {
  const r = await apiCall(`/api/plugins/${curName.value}/readme`)
  if (r) { readmeContent.value = r.data.content; readmeVisible.value = true }
}
async function showCommands(p) {
  curName.value = p.plugin_name
  const r = await apiCall(`/api/plugins/${p.plugin_name}/commands`)
  if (r) { pluginCmds.value = r.data; commandsVisible.value = true }
}
async function showConfig(p) {
  curName.value = p.plugin_name
  const r = await apiCall(`/api/plugins/${p.plugin_name}/config_schema`)
  if (!r) return
  const schema = r.data.schema || {}
  const values = r.data.values || {}
  configSchema.value = r.data
  configValues.value = {}
  for (const k of Object.keys(schema)) {
    configValues.value[k] = values[k] !== undefined ? values[k] : schema[k].default
  }
  configVisible.value = true
}
function isBool(s) { return s.type === 'bool' || s.type === 'boolean' }
function isNumber(s) { return s.type === 'int' || s.type === 'float' }
async function saveConfig() {
  const body = {}
  const schema = configSchema.value.schema
  for (const key of Object.keys(schema)) {
    const type = schema[key].type || 'string'
    let v = configValues.value[key]
    if (type === 'bool' || type === 'boolean') body[key] = !!v
    else if (type === 'int') body[key] = parseInt(v, 10) || 0
    else if (type === 'float') body[key] = parseFloat(v) || 0
    else body[key] = v
  }
  const r = await apiCall(`/api/plugins/${curName.value}/config_schema`, { method: 'PUT', body })
  if (r) { ElMessage.success(r.msg); configVisible.value = false }
}

async function showGroups(p) {
  curName.value = p.plugin_name
  const r = await apiCall('/api/plugins/group-settings')
  if (r) {
    groupRows.value = (r.data || []).filter(s => s.plugin_name === p.plugin_name)
    groupsVisible.value = true
  }
}
async function toggleGroup(row) {
  const r = await apiCall(`/api/plugins/${curName.value}/group/${row.group_id}/toggle`,
    { method: 'POST', body: { enabled: !row.enabled } })
  if (r) { ElMessage.success(r.msg); showGroups({ plugin_name: curName.value }) }
}

async function doReload(p) {
  await ElMessageBox.confirm(`确定重新加载插件 [${p.plugin_name}] 吗？`, '提示', { type: 'warning' })
  const r = await apiCall(`/api/plugins/${p.plugin_name}/reload`, { method: 'POST' })
  if (r) { ElMessage.success(r.msg); load() }
}
async function doToggle(p) {
  const r = await apiCall(`/api/plugins/${p.plugin_name}/toggle`, { method: 'POST', body: { is_active: !p.is_active } })
  if (r) { ElMessage.success(r.msg); load() }
}
async function doDelete(p) {
  try {
    const { value } = await ElMessageBox({
      title: '删除插件',
      message: `确定删除插件 ${p.plugin_name} 吗？`,
      type: 'warning',
      showCancelButton: true,
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      distinguishCancelAndClose: true,
    })
    const r = await apiCall(`/api/plugins/${p.plugin_name}`, { method: 'DELETE', body: { delete_data: !!value } })
    if (r) { ElMessage.success(r.msg); load() }
  } catch (e) { /* cancelled */ }
}
async function installDeps() {
  await ElMessageBox.confirm(`确定将 [${curName.value}] 的缺失依赖安装到全局环境吗？`, '提示', { type: 'warning' })
  const r = await apiCall(`/api/plugins/${curName.value}/install_deps`, { method: 'POST' })
  if (r) ElMessage.success(r.msg)
}
async function createVenv() {
  await ElMessageBox.confirm(`为 [${curName.value}] 创建虚拟环境？`, '提示', { type: 'warning' })
  const r = await apiCall(`/api/plugins/${curName.value}/create_isolated_env`, { method: 'POST' })
  if (r) ElMessage.success(r.msg)
}
async function doUpdate(p) {
  const check = await apiCall(`/api/plugins/${p.plugin_name}/check_update`)
  if (!check) return
  const d = check.data
  await ElMessageBox.confirm(
    `插件 [${p.plugin_name}]\n当前版本: ${d.current_version}\n最新提交: ${d.latest_commit} - ${d.commit_message}\n\n确定从 GitHub 拉取更新吗？`,
    'GitHub 更新', { type: 'warning' })
  const r = await apiCall(`/api/plugins/${p.plugin_name}/update`, { method: 'POST' })
  if (r) { ElMessage.success(r.msg); load() }
}

async function doUpload() {
  const file = upFile.value && upFile.value.files[0]
  if (!file) { ElMessage.warning('请选择 ZIP 文件'); return }
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    const r = await apiCall('/api/plugins/upload', { method: 'POST', body: fd })
    if (r) { ElMessage.success(r.msg); uploadVisible.value = false; load() }
  } finally { uploading.value = false }
}

onMounted(load)
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; }
.plugin-card { margin-bottom: 16px; }
.plugin-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
.plugin-name { font-weight: 700; font-size: 15px; }
.plugin-desc { color: var(--el-text-color-secondary); font-size: 13px; min-height: 38px; margin-bottom: 8px; }
.plugin-badges { margin-bottom: 10px; display: flex; gap: 4px; flex-wrap: wrap; }
.plugin-actions { display: flex; gap: 4px; flex-wrap: wrap; }
.readme-pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; max-height: 60vh; overflow: auto; }
</style>