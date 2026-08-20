<template>
  <div class="page-container">
    <el-card shadow="never" class="mb">
      <template #header>
        <div class="card-head">静态命令 ({{ cmds.length }})
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table :data="cmds" size="small" border>
        <el-table-column prop="plugin_name" label="插件" width="140" />
        <el-table-column label="命令" width="180">
          <template #default="{ row }"><span class="mono">{{ row.pattern }}</span></template>
        </el-table-column>
        <el-table-column prop="alias" label="别名" width="120" />
        <el-table-column prop="description" label="描述" />
        <el-table-column label="权限" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.require_level === 'super'" type="warning" size="small">超管</el-tag>
            <el-tag v-else-if="row.require_level === 'admin'" size="small">管理</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="hit_count" label="命中" width="70" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '禁用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" :type="row.is_active ? 'danger' : 'success'" @click="toggleCmd(row)">
              {{ row.is_active ? '禁用' : '启用' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="mb">
      <template #header>
        <div class="card-head">
          关键词回复 ({{ kwRules.length }})
          <el-button size="small" type="primary" @click="openKwForm(null)">＋ 新增</el-button>
        </div>
      </template>
      <el-alert type="info" :closable="false" show-icon
                title="由插件统一注册管理的只读规则（如 keyword_api），此表可增删改查" style="margin-bottom:12px" />
      <el-table :data="kwRules" size="small" border>
        <el-table-column prop="plugin_name" label="来源插件" width="120" />
        <el-table-column label="关键词/正则" width="160">
          <template #default="{ row }"><span class="mono">{{ row.keyword }}</span></template>
        </el-table-column>
        <el-table-column label="匹配方式" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.match_type === 'regex'" type="warning" size="small">正则</el-tag>
            <span v-else>{{ kwTypeLabel[row.match_type] || row.match_type }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="response" label="回复" show-overflow-tooltip />
        <el-table-column prop="handler" label="handler" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.handler" size="small">{{ row.handler }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="hit_count" label="命中" width="70" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">{{ row.is_active ? '启用' : '禁用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <el-button size="small" @click="openKwForm(row)">编辑</el-button>
            <el-button size="small" :type="row.is_active ? 'danger' : 'success'" @click="toggleKw(row)">
              {{ row.is_active ? '禁用' : '启用' }}
            </el-button>
            <el-button size="small" type="danger" @click="deleteKw(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="card-head">动态命令 ({{ dynCmds.length }})</div>
      </template>
      <el-alert type="info" :closable="false" show-icon
                title="动态命令由插件注册（ctx.command(dynamic=True)），仅作展示，不参与路由匹配。" style="margin-bottom:12px" />
      <el-table :data="dynCmds" size="small" border>
        <el-table-column prop="plugin_name" label="插件" width="160" />
        <el-table-column label="命令" width="200">
          <template #default="{ row }"><span class="mono">{{ row.pattern }}</span></template>
        </el-table-column>
        <el-table-column prop="description" label="描述" />
        <el-table-column prop="hit_count" label="命中" width="70" />
      </el-table>
    </el-card>

    <!-- 编辑静态命令 -->
    <el-dialog v-model="editVisible" :title="'编辑命令 #' + editId" width="440px">
      <el-form label-width="90px">
        <el-form-item label="别名"><el-input v-model="editForm.alias" placeholder="逗号分隔，如 /help,/h" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="editForm.description" /></el-form-item>
        <el-form-item label="权限要求">
          <el-select v-model="editForm.require_level" style="width:100%">
            <el-option label="普通" value="" />
            <el-option label="管理员/群主" value="admin" />
            <el-option label="超级管理员" value="super" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveCommand">保存</el-button>
      </template>
    </el-dialog>

    <!-- 关键词表单 -->
    <el-dialog v-model="kwFormVisible" :title="kwForm.id ? '编辑关键词回复' : '新增关键词回复'" width="500px">
      <el-form label-width="90px">
        <el-form-item label="关键词/正则"><el-input v-model="kwForm.keyword" placeholder="如：你好 / ^早安.*$" /></el-form-item>
        <el-form-item label="匹配方式">
          <el-select v-model="kwForm.match_type" style="width:100%">
            <el-option label="完全相等" value="exact" />
            <el-option label="前缀匹配" value="prefix" />
            <el-option label="包含关键词" value="contains" />
            <el-option label="正则表达式" value="regex" />
          </el-select>
        </el-form-item>
        <el-form-item label="回复内容"><el-input v-model="kwForm.response" type="textarea" :rows="3" placeholder="回复给发送者的内容" /></el-form-item>
        <el-form-item label="handler"><el-input v-model="kwForm.handler" placeholder="如：my_plugin:gen_reply，留空用静态回复" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="kwFormVisible = false">取消</el-button>
        <el-button type="primary" @click="saveKeyword">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const cmds = ref([])
const kwRules = ref([])
const dynCmds = ref([])
const kwTypeLabel = { exact: '完全相等', prefix: '前缀', contains: '包含', regex: '正则' }

const editVisible = ref(false)
const editId = ref(0)
const editForm = ref({ alias: '', description: '', require_level: '' })

const kwFormVisible = ref(false)
const kwForm = ref({ id: 0, keyword: '', match_type: 'exact', response: '', handler: '' })

async function load() {
  const [res, dyn, kw] = await Promise.all([
    api('/api/commands').catch(() => null),
    api('/api/commands/dynamic').catch(() => null),
    api('/api/dynamic-commands').catch(() => null),
  ])
  cmds.value = (res && res.data) || []
  dynCmds.value = (dyn && dyn.data) || []
  kwRules.value = (kw && kw.data) || []
}

function openEdit(row) {
  editId.value = row.id
  editForm.value = { alias: row.alias || '', description: row.description || '', require_level: row.require_level || '' }
  editVisible.value = true
}
async function saveCommand() {
  const r = await apiCall(`/api/commands/${editId.value}/alias`, {
    method: 'PUT',
    body: { alias: editForm.value.alias.trim(), description: editForm.value.description.trim(), require_level: editForm.value.require_level },
  })
  if (r) { ElMessage.success(r.msg); editVisible.value = false; load() }
}
async function toggleCmd(row) {
  const r = await apiCall(`/api/commands/${row.id}/toggle`, { method: 'POST', body: { is_active: !row.is_active } })
  if (r) { ElMessage.success(r.msg); load() }
}

function openKwForm(row) {
  kwForm.value = row ? { ...row } : { id: 0, keyword: '', match_type: 'exact', response: '', handler: '' }
  kwFormVisible.value = true
}
async function saveKeyword() {
  const body = {
    keyword: kwForm.value.keyword.trim(),
    match_type: kwForm.value.match_type,
    response: kwForm.value.response.trim(),
    handler: kwForm.value.handler.trim(),
  }
  const url = kwForm.value.id ? `/api/dynamic-commands/${kwForm.value.id}` : '/api/dynamic-commands'
  const r = await apiCall(url, { method: kwForm.value.id ? 'PUT' : 'POST', body })
  if (r) { ElMessage.success(r.msg); kwFormVisible.value = false; load() }
}
async function toggleKw(row) {
  const r = await apiCall(`/api/dynamic-commands/${row.id}/toggle`, { method: 'POST', body: { is_active: !row.is_active } })
  if (r) { ElMessage.success(r.msg); load() }
}
async function deleteKw(row) {
  await ElMessageBox.confirm('确定删除该关键词回复规则吗？', '提示', { type: 'warning' })
  const r = await apiCall(`/api/dynamic-commands/${row.id}`, { method: 'DELETE' })
  if (r) { ElMessage.success(r.msg); load() }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>