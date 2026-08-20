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
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api, apiCall } from '../api'

const cmds = ref([])
const dynCmds = ref([])

const editVisible = ref(false)
const editId = ref(0)
const editForm = ref({ alias: '', description: '', require_level: '' })

async function load() {
  const [res, dyn] = await Promise.all([
    api('/api/commands').catch(() => null),
    api('/api/commands/dynamic').catch(() => null),
  ])
  cmds.value = (res && res.data) || []
  dynCmds.value = (dyn && dyn.data) || []
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

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>