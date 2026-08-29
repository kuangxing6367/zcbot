<template>
  <div class="page-container">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          定时任务 ({{ tasks.length }})
          <div>
            <el-button size="small" @click="load">刷新</el-button>
            <el-button size="small" type="primary" @click="createVisible = true">＋ 新建任务</el-button>
          </div>
        </div>
      </template>
      <el-table :data="tasks" size="small" border>
        <el-table-column prop="id" label="#" width="60" />
        <el-table-column prop="plugin_name" label="来源" width="140" />
        <el-table-column label="Cron" width="120">
          <template #default="{ row }"><span class="mono">{{ row.cron_expression }}</span></template>
        </el-table-column>
        <el-table-column prop="description" label="描述" />
        <el-table-column label="上次执行" width="160">
          <template #default="{ row }">{{ row.last_run_at ? fmtTime(row.last_run_at) : '-' }}</template>
        </el-table-column>
        <el-table-column prop="run_count" label="次数" width="70" />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.last_status === 'success'" type="success" size="small">成功</el-tag>
            <el-tag v-else-if="row.last_status === 'error' || row.last_status === 'failure'" type="danger" size="small">失败</el-tag>
            <el-tag v-else size="small">-</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230">
          <template #default="{ row }">
            <el-button size="small" @click="trigger(row)">立即执行</el-button>
            <el-button size="small" :type="row.is_active ? 'warning' : 'success'" @click="toggle(row)">
              {{ row.is_active ? '暂停' : '启用' }}
            </el-button>
            <el-button v-if="row.plugin_name === '__web__'" size="small" type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="dim small mt">插件注册的任务无法在此删除，请到插件管理页处理。</div>
    </el-card>

    <el-dialog v-model="createVisible" title="新建定时任务" width="440px">
      <el-form label-width="120px">
        <el-form-item label="Cron 表达式">
          <el-input v-model="form.cron_expression" placeholder="分 时 日 月 周，如 0 8 * * *" class="mono" />
        </el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" placeholder="任务描述" /></el-form-item>
      </el-form>
      <div class="dim small">框架仅支持 5 字段 cron（分 时 日 月 周），任务处理器由系统内置。</div>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall, fmtTime } from '../api'

const tasks = ref([])
const createVisible = ref(false)
const form = ref({ cron_expression: '', description: '' })

async function load() {
  const r = await api('/api/tasks').catch(() => null)
  if (r) tasks.value = r.data || []
}
async function create() {
  const r = await apiCall('/api/tasks', { method: 'POST', body: { ...form.value } })
  if (r) { ElMessage.success(r.msg); createVisible.value = false; load() }
}
async function toggle(row) {
  const r = await apiCall(`/api/tasks/${row.id}/toggle`, { method: 'POST', body: { is_active: !row.is_active } })
  if (r) { ElMessage.success(r.msg); load() }
}
async function trigger(row) {
  await ElMessageBox.confirm('确定立即执行该任务吗？', '提示', { type: 'warning' })
  const r = await apiCall(`/api/tasks/${row.id}/trigger`, { method: 'POST' })
  if (r) { ElMessage.success(r.msg); load() }
}
async function remove(row) {
  await ElMessageBox.confirm('确定删除该任务吗？', '提示', { type: 'warning' })
  const r = await apiCall(`/api/tasks/${row.id}`, { method: 'DELETE' })
  if (r) { ElMessage.success(r.msg); load() }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>