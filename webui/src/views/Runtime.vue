<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">CPU 使用率</div>
          <div class="stat-value">{{ d.cpu_percent ?? '-' }}<span class="dim small">%</span></div>
          <el-progress :percentage="Math.min(100, d.cpu_percent || 0)" :show-text="false" :stroke-width="6" />
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">进程内存</div>
          <div class="stat-value">{{ d.process_memory_mb ?? '-' }}<span class="dim small"> MB</span></div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">系统内存</div>
          <div class="stat-value">{{ memPct ?? '-' }}<span class="dim small">%</span></div>
          <div class="dim small" v-if="d.memory">{{ d.memory.used_mb }} / {{ d.memory.total_mb }} MB</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">运行时长</div>
          <div class="stat-value" style="font-size:18px">{{ fmtUptime(d.uptime_seconds) }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mt">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">OneBot 连接
              <el-tag size="small" :type="bots.length ? 'success' : 'info'">{{ bots.length }} 在线</el-tag>
            </div>
          </template>
          <div v-for="b in bots" :key="b" style="padding:4px 0">
            <span>{{ b }}</span> <el-tag size="small" type="success">在线</el-tag>
          </div>
          <el-empty v-if="!bots.length" description="无在线连接" :image-size="50" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header><div class="card-head">运行时信息</div></template>
          <el-descriptions :column="1" size="small">
            <el-descriptions-item label="Python">{{ d.python_version || '-' }}</el-descriptions-item>
            <el-descriptions-item label="数据库">{{ d.db_type || '-' }}</el-descriptions-item>
            <el-descriptions-item label="线程数">{{ d.threads ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="更新时间">{{ nowStr }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>

    <el-card v-if="cmds.length" shadow="never" class="mt">
      <template #header>
        <div class="card-head">命令命中排行 (Top {{ cmds.length }})</div>
      </template>
      <div v-for="c in cmds" :key="c.pattern" class="chart-row">
        <span class="chart-label">{{ c.pattern }}</span>
        <el-progress :percentage="Math.max(1, (c.hit_count / maxHits) * 100)" :show-text="false" :stroke-width="14" style="flex:1" />
        <span class="chart-val">{{ c.hit_count }}</span>
      </div>
    </el-card>

    <el-card v-if="msgDays.length" shadow="never" class="mt">
      <template #header>
        <div class="card-head">消息统计 (近 30 天)</div>
      </template>
      <div v-for="day in msgDays" :key="day.day" class="chart-row">
        <span class="chart-label" style="width:80px">{{ day.day }}</span>
        <el-progress :percentage="Math.max(1, (day.cnt / maxMsg) * 100)" :show-text="false" :stroke-width="12" style="flex:1" />
        <span class="chart-val">{{ day.cnt }}</span>
      </div>
    </el-card>

    <el-card v-if="env.os" shadow="never" class="mt">
      <template #header><div class="card-head">环境信息</div></template>
      <el-row :gutter="12">
        <el-col :span="8">
          <el-descriptions title="操作系统" :column="1" size="small" border>
            <el-descriptions-item label="系统">{{ env.os.system || '-' }}</el-descriptions-item>
            <el-descriptions-item label="版本">{{ env.os.release || '-' }}</el-descriptions-item>
            <el-descriptions-item label="架构">{{ env.os.arch || '-' }}</el-descriptions-item>
            <el-descriptions-item label="机器">{{ env.os.machine || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-col>
        <el-col :span="8">
          <el-descriptions title="CPU" :column="1" size="small" border>
            <el-descriptions-item label="核心数">{{ env.cpu?.count ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="物理核心">{{ env.cpu?.physical_count ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="频率">{{ env.cpu?.freq_mhz ? env.cpu.freq_mhz + ' MHz' : '-' }}</el-descriptions-item>
            <el-descriptions-item label="使用率">{{ env.cpu ? env.cpu.percent + '%' : '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-col>
        <el-col :span="8">
          <el-descriptions title="内存" :column="1" size="small" border>
            <el-descriptions-item label="总量">{{ env.memory ? env.memory.total_mb + ' MB' : '-' }}</el-descriptions-item>
            <el-descriptions-item label="可用">{{ env.memory ? env.memory.available_mb + ' MB' : '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-col>
        <el-col :span="8" class="mt">
          <el-descriptions title="磁盘" :column="1" size="small" border>
            <el-descriptions-item label="总量">{{ env.disk ? env.disk.total_gb + ' GB' : '-' }}</el-descriptions-item>
            <el-descriptions-item label="已用">{{ env.disk ? env.disk.used_gb + ' GB' : '-' }}</el-descriptions-item>
            <el-descriptions-item label="剩余">{{ env.disk ? env.disk.free_gb + ' GB' : '-' }}</el-descriptions-item>
            <el-descriptions-item label="使用率">{{ env.disk ? env.disk.percent + '%' : '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-col>
        <el-col :span="8" class="mt">
          <el-descriptions title="网络" :column="1" size="small" border>
            <el-descriptions-item label="发送">{{ env.network ? env.network.bytes_sent_mb + ' MB' : '-' }}</el-descriptions-item>
            <el-descriptions-item label="接收">{{ env.network ? env.network.bytes_recv_mb + ' MB' : '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-col>
        <el-col :span="8" class="mt">
          <el-descriptions title="Python" :column="1" size="small" border>
            <el-descriptions-item label="版本">{{ env.python?.version ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="包数">{{ env.python?.packages_total ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="进程 PID">{{ env.process?.pid ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="线程数">{{ env.process?.threads ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="打开文件">{{ env.process?.open_files ?? '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-col>
      </el-row>
    </el-card>

    <div class="dim small mt center" style="text-align:center">每 5 秒自动刷新</div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { fmtUptime } from '../api'

const d = ref({})
const cmds = ref([])
const msgDays = ref([])
const env = ref({})
const nowStr = ref('')
let timer = null

const memPct = computed(() => d.value.memory?.percent)
const bots = computed(() => d.value.ws?.bots || [])
const maxHits = computed(() => Math.max(...cmds.value.map(c => c.hit_count), 1))
const maxMsg = computed(() => Math.max(...msgDays.value.map(d => d.cnt), 1))

async function load() {
  const [statsRes, cmdRes, msgRes, envRes] = await Promise.all([
    api('/api/runtime/stats').catch(() => null),
    api('/api/stats/commands?top=10').catch(() => null),
    api('/api/stats/messages').catch(() => null),
    api('/api/envinfo').catch(() => null),
  ])
  d.value = (statsRes && statsRes.data) || {}
  cmds.value = (cmdRes && cmdRes.data && cmdRes.data.commands) || []
  msgDays.value = (msgRes && msgRes.data && msgRes.data.days) || []
  env.value = (envRes && envRes.data) || {}
  nowStr.value = new Date().toLocaleTimeString()
}

onMounted(() => {
  load()
  timer = setInterval(load, 5000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<style scoped>
.stat-card { text-align: center; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.chart-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; }
.chart-label { width: 200px; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chart-val { width: 40px; text-align: right; font-size: 12px; }
</style>