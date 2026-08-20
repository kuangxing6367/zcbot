<template>
  <div class="page-container">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          群组管理 ({{ rows.length }})
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-table :data="rows" size="small" border>
        <el-table-column prop="group_id" label="群号" width="130">
          <template #default="{ row }"><span class="mono">{{ row.group_id }}</span></template>
        </el-table-column>
        <el-table-column prop="group_name" label="群名称" />
        <el-table-column prop="member_count" label="成员数" width="90" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag v-if="row.is_blacklist" type="danger" size="small">黑名单</el-tag>
            <el-tag v-else-if="row.is_active" type="success" size="small">活跃</el-tag>
            <el-tag v-else size="small">已退群</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="入群时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.join_at) }}</template>
        </el-table-column>
        <el-table-column v-for="e in extCols" :key="e.key" :label="e.title" :width="120">
          <template #default="{ row }">
            {{ extData[String(row.group_id)]?.[e.key]?.data ?? '-' }}
          </template>
        </el-table-column>
        <el-table-column label="操作" :width="extCols.length ? 160 : 110">
          <template #default="{ row }">
            <el-button size="small" :type="row.is_blacklist ? 'success' : 'danger'" @click="toggleBlacklist(row)">
              {{ row.is_blacklist ? '移出黑名单' : '拉黑' }}
            </el-button>
            <el-button v-if="hasPanel" size="small" @click="showDetail(row)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!rows.length" description="暂无群组" :image-size="60" />
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
import { api, apiCall, fmtTime } from '../api'

const rows = ref([])
const extCols = ref([])
const hasPanel = ref(false)
const extData = ref({})

const detailVisible = ref(false)
const detailName = ref('')
const detailPanels = ref([])

async function load() {
  const r = await api('/api/groups').catch(() => null)
  if (!r) return
  rows.value = r.data || []
  try {
    const m = await api('/api/extensions/groups')
    const meta = (m && m.data) || []
    extCols.value = meta.filter(e => e.type === 'column')
    hasPanel.value = meta.some(e => e.type === 'panel')
  } catch (e) { extCols.value = []; hasPanel.value = false }
  if (rows.value.length) {
    const ids = rows.value.map(g => g.group_id)
    const ext = await api('/api/groups/extensions/data', { method: 'POST', body: { ids } }).catch(() => null)
    extData.value = (ext && ext.data) || {}
  } else extData.value = {}
}

async function toggleBlacklist(row) {
  const r = await apiCall(`/api/groups/${row.group_id}/blacklist`, { method: 'POST', body: { is_blacklist: !row.is_blacklist } })
  if (r) { ElMessage.success(r.msg); load() }
}

async function showDetail(row) {
  detailName.value = String(row.group_name || row.group_id)
  const r = await api(`/api/groups/${row.group_id}/extensions`).catch(() => null)
  const data = (r && r.data) || {}
  detailPanels.value = Object.values(data).filter(e => e && e.type === 'panel')
  detailVisible.value = true
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>