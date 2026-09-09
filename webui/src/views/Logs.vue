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
      <!-- 终端命令输入 -->
      <div class="terminal-bar">
        <el-input
          v-model="cmdInput"
          placeholder="输入终端命令 (如 update, status, plugins, help...)"
          @keyup.enter="execCmd"
          :disabled="cmdRunning"
          clearable
        >
          <template #prefix>
            <span style="color:var(--el-color-success);font-weight:bold">$</span>
          </template>
          <template #append>
            <el-button @click="execCmd" :loading="cmdRunning" type="primary">执行</el-button>
          </template>
        </el-input>
        <div v-if="cmdOutput" class="cmd-output">
          <pre>{{ cmdOutput }}</pre>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import { api, apiCall } from '../api'

const logs = ref([])
const category = ref('')
const level = ref('')
const keyword = ref('')
const autoScroll = ref(true)
const logBoxRef = ref(null)
let lastSeq = 0
let timer = null

const cmdInput = ref('')
const cmdOutput = ref('')
const cmdRunning = ref(false)

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

async function execCmd() {
  const cmd = cmdInput.value.trim()
  if (!cmd) return
  cmdRunning.value = true
  cmdOutput.value = ''
  try {
    const r = await apiCall('/api/terminal/exec', {
      method: 'POST',
      body: JSON.stringify({ command: cmd })
    })
    if (r && r.code === 0) {
      cmdOutput.value = r.data?.output || '(无输出)'
    } else {
      cmdOutput.value = r?.msg || '执行失败'
    }
  } catch (e) {
    cmdOutput.value = '请求失败: ' + e.message
  } finally {
    cmdRunning.value = false
  }
}

onMounted(async () => {
  await loadLogs()
  timer = setInterval(poll, 2000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.log-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.terminal-bar { margin-top: 12px; border-top: 1px solid var(--el-border-color-lighter); padding-top: 12px; }
.cmd-output {
  margin-top: 8px;
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 10px 12px;
  border-radius: 6px;
  font-family: 'Consolas', 'Courier New', monospace;
  font-size: 13px;
  max-height: 300px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.cmd-output pre { margin: 0; }
</style>
