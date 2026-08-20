<template>
  <div class="page-container">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          用户管理 (共 {{ total }} 人)
          <div>
            <el-input v-model="keyword" placeholder="搜索昵称/QQ号/备注" style="width:220px" clearable
                      @keyup.enter="search" @clear="search" />
            <el-button @click="search">搜索</el-button>
          </div>
        </div>
      </template>
      <el-table :data="rows" size="small" border>
        <el-table-column prop="user_id" label="QQ号" width="120">
          <template #default="{ row }"><span class="mono">{{ row.user_id }}</span></template>
        </el-table-column>
        <el-table-column prop="nickname" label="昵称" />
        <el-table-column label="角色" width="90">
          <template #default="{ row }">
            <el-tag :type="row.role === 'super' ? 'warning' : 'info'" size="small">{{ row.role === 'super' ? '超管' : '普通' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_blacklist ? 'danger' : 'success'" size="small">{{ row.is_blacklist ? '黑名单' : '正常' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后活跃" width="150">
          <template #default="{ row }">{{ timeAgo(row.last_active_at) }}</template>
        </el-table-column>
        <el-table-column v-for="e in extCols" :key="e.key" :label="e.title" :width="120">
          <template #default="{ row }">
            {{ extData[String(row.user_id)]?.[e.key]?.data ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" :width="extCols.length ? 260 : 200">
          <template #default="{ row }">
            <el-button size="small" :type="row.role === 'super' ? 'danger' : 'warning'" @click="setRole(row)">
              {{ row.role === 'super' ? '取消超管' : '设为超管' }}
            </el-button>
            <el-button size="small" :type="row.is_blacklist ? 'success' : 'danger'" @click="toggleBlacklist(row)">
              {{ row.is_blacklist ? '移出黑名单' : '拉黑' }}
            </el-button>
            <el-button v-if="hasPanel" size="small" @click="showDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager mt">
        <el-pagination background layout="prev, pager, next" :total="total" :page-size="20"
                       :current-page="page" @current-change="changePage" />
      </div>
    </el-card>

    <el-dialog v-model="detailVisible" :title="detailName + ' 详情'" width="520px">
      <template v-for="e in detailPanels" :key="e.title + e.plugin">
        <h4>{{ e.title }} <span class="dim small">· {{ e.plugin }}</span></h4>
        <el-descriptions v-if="e.data && typeof e.data === 'object' && !Array.isArray(e.data)" :column="1" border size="small">
          <el-descriptions-item v-for="(v, k) in e.data" :key="k" :label="String(k)">{{ String(v) }}</el-descriptions-item>
        </el-descriptions>
        <div v-else>{{ e.data == null ? '-' : String(e.data) }}</div>
      </template>
      <div v-if="!detailPanels.length" class="dim">该条目暂无插件扩展面板</div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api, apiCall, timeAgo } from '../api'

const rows = ref([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const extMeta = ref([])
const extCols = ref([])
const hasPanel = ref(false)
const extData = ref({})

const detailVisible = ref(false)
const detailName = ref('')
const detailPanels = ref([])

async function loadUiExtMeta() {
  try {
    const r = await api(`/api/extensions/users`)
    const meta = (r && r.data) || []
    extMeta.value = meta
    extCols.value = meta.filter(e => e.type === 'column')
    hasPanel.value = meta.some(e => e.type === 'panel')
  } catch (e) { extCols.value = []; hasPanel.value = false }
}

async function load() {
  const url = `/api/users?page=${page.value}&size=20&keyword=${encodeURIComponent(keyword.value)}`
  const r = await api(url).catch(() => null)
  if (!r) return
  rows.value = r.data || []
  total.value = r.total || 0
  await loadUiExtMeta()
  if (rows.value.length) {
    const ids = rows.value.map(u => u.user_id)
    const ext = await api('/api/users/extensions/data', { method: 'POST', body: { ids } }).catch(() => null)
    extData.value = (ext && ext.data) || {}
  } else {
    extData.value = {}
  }
}

function search() { page.value = 1; load() }
function changePage(p) { page.value = p; load() }

async function setRole(row) {
  const role = row.role === 'super' ? '' : 'super'
  const r = await apiCall(`/api/users/${row.user_id}/role`, { method: 'PUT', body: { role } })
  if (r) { ElMessage.success(r.msg); load() }
}
async function toggleBlacklist(row) {
  const r = await apiCall(`/api/users/${row.user_id}/blacklist`, { method: 'POST', body: { is_blacklist: !row.is_blacklist } })
  if (r) { ElMessage.success(r.msg); load() }
}

async function showDetail(row) {
  detailName.value = String(row.nickname || row.user_id)
  const r = await api(`/api/users/${row.user_id}/extensions`).catch(() => null)
  const data = (r && r.data) || {}
  detailPanels.value = Object.values(data).filter(e => e && e.type === 'panel')
  detailVisible.value = true
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
.pager { display: flex; justify-content: center; }
</style>