<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col :span="5">
        <el-card shadow="never">
          <div class="db-sidebar-title">数据表</div>
          <el-menu :default-active="selected" @select="selectTable">
            <el-menu-item v-for="t in tables" :key="t.name" :index="String(t.name)">
              <el-icon><Coin /></el-icon>
              <span>{{ t.name }}</span>
              <span class="dim small" style="margin-left:auto">{{ t.rows === null ? '?' : t.rows }}</span>
            </el-menu-item>
          </el-menu>
          <div class="db-sidebar-divider" />
          <el-button size="small" style="width:100%" @click="sqlVisible = true">SQL 控制台</el-button>
          <el-button size="small" style="width:100%;margin-top:8px" @click="loadTables">刷新</el-button>
        </el-card>
      </el-col>
      <el-col :span="19">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span v-if="selected">{{ selected }} · {{ total }} 行</span>
              <el-button size="small" @click="sqlVisible = true">SQL</el-button>
            </div>
          </template>
          <el-table :data="rows" size="small" border max-height="calc(100vh - 260px)">
            <el-table-column v-for="c in cols" :key="c" :prop="c" :label="c" show-overflow-tooltip>
              <template #default="{ row }">
                <span v-if="row[c] === null || row[c] === undefined" class="dim">NULL</span>
                <span v-else>{{ String(row[c]).substring(0, 120) }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="selected" class="pager mt">
            <el-pagination background layout="prev, pager, next" :total="total" :page-size="pageSize"
                           :current-page="page" @current-change="changePage" />
          </div>
          <el-empty v-if="!selected" description="请选择一个数据表" :image-size="60" />
        </el-card>

        <el-card v-if="queryRows.length" shadow="never" class="mt">
          <template #header><div class="card-head">SQL 查询 · {{ queryRows.length }} 行</div></template>
          <el-table :data="queryRows" size="small" border max-height="300">
            <el-table-column v-for="c in queryCols" :key="c" :prop="c" :label="c" show-overflow-tooltip />
          </el-table>
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="sqlVisible" title="SQL 控制台" width="720px">
      <el-input v-model="sql" type="textarea" :rows="5" class="mono" placeholder="SELECT * FROM commands LIMIT 10" />
      <div class="dim small mt">支持 SELECT / INSERT / UPDATE / DELETE / CREATE / ALTER / DROP 等语句，非 SELECT 语句需要二次确认。</div>
      <div class="mt" v-if="sqlResult !== null">
        <el-alert v-if="sqlResult.error" type="error" :closable="false" :title="sqlResult.error" />
        <template v-else>
          <div class="dim small">返回 {{ sqlResult.count }} 行</div>
          <el-table :data="sqlResult.rows" size="small" border max-height="260">
            <el-table-column v-for="c in sqlResult.cols" :key="c" :prop="c" :label="c" show-overflow-tooltip />
          </el-table>
        </template>
      </div>
      <template #footer>
        <el-button @click="sqlVisible = false">关闭</el-button>
        <el-button type="primary" :loading="sqlRunning" @click="runSql">执行</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const tables = ref([])
const selected = ref('')
const rows = ref([])
const cols = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = 50

const sqlVisible = ref(false)
const sql = ref('')
const sqlRunning = ref(false)
const sqlResult = ref(null)
const queryRows = ref([])
const queryCols = ref([])

async function loadTables() {
  const r = await api('/api/db/tables').catch(() => null)
  if (!r) return
  tables.value = r.data || []
  if (!selected.value && tables.value.length) selected.value = String(tables.value[0].name)
  if (selected.value) loadRows()
}

async function loadRows() {
  const [sRes, rRes] = await Promise.all([
    api(`/api/db/tables/${encodeURIComponent(selected.value)}/schema`).catch(() => null),
    api(`/api/db/tables/${encodeURIComponent(selected.value)}/rows?page=${page.value}&page_size=${pageSize}`).catch(() => null),
  ])
  cols.value = (sRes && sRes.data || []).map(c => c.Field || c.name || c.cid)
  rows.value = (rRes && rRes.data && rRes.data.rows) || []
  total.value = (rRes && rRes.data && rRes.data.total) || 0
}

function selectTable(name) {
  selected.value = String(name)
  page.value = 1
  loadRows()
}
function changePage(p) { page.value = p; loadRows() }

async function runSql() {
  const text = sql.value.trim()
  if (!text) return
  sqlRunning.value = true
  sqlResult.value = null
  try {
    let r = await api('/api/db/query', { method: 'POST', body: { sql: text } }).catch(() => null)
    if (!r) return
    if (r.code === 400 && r.write === true) {
      try {
        await ElMessageBox.confirm('确认要执行以下写入操作吗？\n\n' + text.substring(0, 500), 'SQL 写入确认', { type: 'warning' })
      } catch (e) { sqlResult.value = { error: '已取消' }; return }
      r = await api('/api/db/query', { method: 'POST', body: { sql: text, write: true } }).catch(() => null)
    }
    if (!r) return
    if (r.code !== 0) { sqlResult.value = { error: r.msg || '执行失败' }; return }
    const data = r.data || {}
    const rrows = data.rows || []
    sqlResult.value = {
      count: data.count,
      write: data.write,
      rows: rrows,
      cols: rrows.length ? Object.keys(rrows[0]) : [],
    }
    queryRows.value = rrows
    queryCols.value = rrows.length ? Object.keys(rrows[0]) : []
    if (data.write) loadTables()
  } finally { sqlRunning.value = false }
}

onMounted(loadTables)
</script>

<style scoped>
.db-sidebar-title { font-weight: 700; margin-bottom: 8px; }
.db-sidebar-divider { height: 1px; background: var(--el-border-color); margin: 12px 0; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.pager { display: flex; justify-content: center; }
</style>