<template>
  <div class="page-container">
    <el-card shadow="never">
      <div class="fb-toolbar mb">
        <div>
          <h3 class="mb0">接口令牌（API Key）</h3>
          <div class="dim small">独立长效令牌，不随登录/登出轮换，专供外部程序调用框架 REST API。创建后仅显示一次，请妥善保存。</div>
        </div>
        <el-button type="primary" @click="openCreate">＋ 新建令牌</el-button>
      </div>

      <el-table :data="list" size="small" border stripe v-loading="loading">
        <el-table-column label="名称" prop="name" min-width="120" />
        <el-table-column label="身份" width="90">
          <template #default="{ row }">
            <el-tag :type="row.role === 'super' ? 'danger' : 'primary'" size="small">{{ row.role }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建者" prop="created_by" width="120" />
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmt(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="过期时间" width="160">
          <template #default="{ row }">{{ row.expires_at ? fmt(row.expires_at) : '永久' }}</template>
        </el-table-column>
        <el-table-column label="最近调用" width="160">
          <template #default="{ row }">{{ row.last_used_at ? fmt(row.last_used_at) : '从未' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="!row.is_active" type="info" size="small">已吊销</el-tag>
            <el-tag v-else-if="row.expired" type="warning" size="small">已过期</el-tag>
            <el-tag v-else type="success" size="small">有效</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.is_active" size="small" type="danger" @click="revoke(row)">吊销</el-button>
            <span v-else class="dim">—</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建令牌 -->
    <el-dialog v-model="createVisible" title="新建接口令牌" width="460px">
      <el-form label-width="90px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" placeholder="如：监控脚本 / 部署机器人" maxlength="100" />
        </el-form-item>
        <el-form-item label="身份">
          <el-select v-model="form.role" style="width:100%">
            <el-option label="admin（普通管理员）" value="admin" />
            <el-option label="super（超级管理员）" value="super" />
          </el-select>
        </el-form-item>
        <el-form-item label="有效期">
          <el-select v-model="form.expires_in" style="width:100%">
            <el-option label="永久有效" :value="0" />
            <el-option label="7 天" :value="604800" />
            <el-option label="30 天" :value="2592000" />
            <el-option label="90 天" :value="7776000" />
            <el-option label="365 天" :value="31536000" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 创建结果：仅显示一次 token -->
    <el-dialog v-model="resultVisible" title="令牌已创建（请立即复制保存）" width="560px" :close-on-click-modal="false" :show-close="false">
      <el-alert type="warning" :closable="false" class="mb">
        这是令牌唯一一次明文展示，关闭后将无法再次查看。请立即复制保存到调用方。
      </el-alert>
      <el-input :model-value="createdToken" type="textarea" :rows="3" readonly class="mono" />
      <div class="mt">
        <el-button @click="copyToken">复制令牌</el-button>
        <el-button type="primary" @click="resultVisible = false">我已保存</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const list = ref([])
const loading = ref(false)
const createVisible = ref(false)
const resultVisible = ref(false)
const submitting = ref(false)
const createdToken = ref('')

const form = ref({ name: '', role: 'admin', expires_in: 0 })

function fmt(ts) {
  if (!ts) return '-'
  const n = Number(ts)
  if (isNaN(n)) return String(ts)
  const d = new Date(n * 1000)
  if (isNaN(d.getTime())) return String(ts)
  const p = x => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

async function load() {
  loading.value = true
  const r = await apiCall('/api/apikeys')
  loading.value = false
  if (r) list.value = r.data || []
}

function openCreate() {
  form.value = { name: '', role: 'admin', expires_in: 0 }
  createVisible.value = true
}

async function submitCreate() {
  if (!form.value.name.trim()) { ElMessage.warning('请填写名称'); return }
  submitting.value = true
  const r = await apiCall('/api/apikeys', {
    method: 'POST',
    body: {
      name: form.value.name.trim(),
      role: form.value.role,
      expires_in: Number(form.value.expires_in),
    },
  })
  submitting.value = false
  if (r && r.data && r.data.token) {
    createdToken.value = r.data.token
    createVisible.value = false
    resultVisible.value = true
    load()
  }
}

async function revoke(row) {
  try {
    await ElMessageBox.confirm(`确定吊销令牌「${row.name}」吗？吊销后使用该令牌的调用方将立即失效。`, '吊销', { type: 'warning' })
  } catch (e) { return }
  const r = await apiCall(`/api/apikeys/${row.id}/revoke`, { method: 'POST' })
  if (r) { ElMessage.success('已吊销'); load() }
}

async function copyToken() {
  try {
    await navigator.clipboard.writeText(createdToken.value)
    ElMessage.success('已复制到剪贴板')
  } catch (e) {
    ElMessage.warning('复制失败，请手动选择文本复制')
  }
}

onMounted(load)
</script>

<style scoped>
.fb-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.mb0 { margin: 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.mt { margin-top: 12px; }
.mb { margin-bottom: 12px; }
.dim { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
</style>
