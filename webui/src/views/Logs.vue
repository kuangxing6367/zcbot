<template>
  <div class="page-container">
    <el-card shadow="never">
      <div class="log-toolbar mb">
        <el-select v-model="category" style="width:130px" placeholder="全部分类" clearable>
          <el-option label="消息" value="message" />
          <el-option label="插件" value="plugin" />
          <el-option label="连接" value="connection" />
          <el-option label="系统" value="system" />
          <el-option label="框架" value="framework" />
        </el-select>
        <el-select v-model="level" style="width:120px" placeholder="全部级别" clearable>
          <el-option label="INFO" value="INFO" />
          <el-option label="WARN" value="WARN" />
          <el-option label="ERROR" value="ERROR" />
          <el-option label="DEBUG" value="DEBUG" />
        </el-select>
        <el-input v-model="keyword" placeholder="搜索关键词" style="width:220px" clearable @keyup.enter="loadLogs" />
        <el-button @click="loadLogs">筛选</el-button>
        <el-button @click="clearLogs">清空</el-button>
        <el-checkbox v-model="autoScroll" style="margin-left:auto">自动滚动</el-checkbox>
      </div>
      <div class="log-box" ref="logBoxRef">
        <div v-for="(l, i) in logs" :key="i" class="log-line">
          <span class="t">{{ fmtLogTime(l.time) }}</span>
          <span :class="'lv-' + l.level">{{ l.level }}</span>
          <span>[{{ l.category }}] {{ l.message }}</span>
        </div>
        <el-empty v-if="!logs.length" description="暂无日志" :image-size="50" />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import { ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const logs = ref([])
const category = ref('')
const level = ref('')
const keyword = ref('')
const autoScroll = ref(true)
const logBoxRef = ref(null)
let lastSeq = 0
let timer = null

function fmtLogTime(t) {
  const d = new Date(t * 1000)
  const p = n => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

function matchesFilter(entry) {
  if (category.value && entry.category !== category.value) return false
  if (level.value && entry.level !== level.value) return false
  if (keyword.value && !String(entry.message).toLowerCase().includes(keyword.value.toLowerCase())) return false
  return true
}

function append(entry) {
  if (!matchesFilter(entry)) return
  logs.value.push(entry)
  if (logs.value.length > 1000) logs.value.splice(0, logs.value.length - 1000)
  scrollBottom()
}

function scrollBottom() {
  nextTick(() => {
    if (autoScroll.value && logBoxRef.value) logBoxRef.value.scrollTop = logBoxRef.value.scrollHeight
  })
}

async function loadLogs() {
  const params = new URLSearchParams()
  if (category.value) params.set('category', category.value)
  if (level.value) params.set('level', level.value)
  if (keyword.value) params.set('keyword', keyword.value)
  params.set('limit', '200')
  const r = await api('/api/runtime_logs?' + params.toString()).catch(() => null)
  if (!r) return
  logs.value = r.data || []
  if (r.latest_seq) lastSeq = r.latest_seq
  scrollBottom()
}

async function poll() {
  const r = await api('/api/runtime_logs?after_seq=' + lastSeq + '&limit=100').catch(() => null)
  if (!r) return
  ;(r.data || []).forEach(append)
  if (r.latest_seq) lastSeq = r.latest_seq
}

async function clearLogs() {
  await ElMessageBox.confirm('确定清空日志缓存吗？', '提示', { type: 'warning' })
  const r = await apiCall('/api/runtime_logs/clear', { method: 'POST' })
  if (r) { loadLogs() }
}

onMounted(async () => {
  await loadLogs()
  timer = setInterval(poll, 2000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.log-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
</style>