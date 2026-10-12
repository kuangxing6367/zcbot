<template>
  <div class="page-container">
    <!-- 统计卡片 -->
    <el-row :gutter="16">
      <el-col v-for="s in stats" :key="s.label" :xs="12" :sm="8" :md="6" :lg="3">
        <el-card shadow="never" class="stat-card">
          <div class="stat-top">
            <span class="stat-icon" :style="{ background: s.bg, color: s.color }">
              <el-icon :size="18"><component :is="s.icon" /></el-icon>
            </span>
          </div>
          <div class="stat-value">{{ s.value }}</div>
          <div class="stat-label">{{ s.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mt">
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="h-card">
          <template #header>
            <div class="card-head">
              <span class="card-title">{{ d.adapter_name || '接入端' }} 连接</span>
              <span class="dim small" v-if="d.ws_port">WS :{{ d.ws_port }}</span>
            </div>
          </template>
          <el-empty v-if="!(d.bots || []).length" description="暂无在线客户端" :image-size="60" />
          <div v-else class="bot-tags">
            <el-tag v-for="b in d.bots" :key="b" type="success" effect="light" style="margin:4px">{{ b }}</el-tag>
          </div>
        </el-card>
      </el-col>
      <el-col :xs="24" :md="12">
        <el-card shadow="never" class="h-card">
          <template #header>
            <div class="card-head">
              <span class="card-title">框架信息</span>
              <el-tag v-if="d.framework_alpha" type="warning" size="small" effect="plain">Alpha 预览版</el-tag>
            </div>
          </template>
          <el-descriptions :column="1" size="small">
            <el-descriptions-item label="名称">{{ d.framework_name || '-' }}</el-descriptions-item>
            <el-descriptions-item label="版本">{{ d.framework_version || '-' }}</el-descriptions-item>
            <el-descriptions-item label="仓库">
              <el-link v-if="d.github_repo" type="primary" :href="d.github_repo" target="_blank">
                {{ String(d.github_repo).replace('https://', '') }}
              </el-link>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt">
      <template #header>
        <div class="card-head">
          <span class="card-title">插件卡片</span>
          <el-button size="small" @click="load">刷新</el-button>
        </div>
      </template>
      <el-row v-if="pluginCards.length" :gutter="16">
        <el-col v-for="c in pluginCards" :key="c.plugin_name + c.title" :xs="12" :sm="8" :md="6" :lg="4">
          <el-card shadow="hover" class="plugin-card">
            <div class="stat-label">{{ c.title }}</div>
            <div class="stat-value" style="font-size:17px">{{ c.data?.value ?? '' }}</div>
            <div class="stat-label">{{ c.data?.label || '' }}</div>
            <div class="dim small">来自插件 {{ c.plugin_name }}</div>
          </el-card>
        </el-col>
      </el-row>
      <el-empty v-else description="暂无插件卡片" :image-size="60" />
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'

const d = ref({})
const pluginCards = ref([])

const stats = ref([])

// 统计项元数据：图标 + 配色（art-design-pro 风格的小色块图标）
const META = [
  { icon: 'Grid', bg: 'rgba(93,135,255,.12)', color: '#5D87FF' },
  { icon: 'Files', bg: 'rgba(180,141,243,.14)', color: '#B48DF3' },
  { icon: 'ChatDotRound', bg: 'rgba(56,192,252,.14)', color: '#38C0FC' },
  { icon: 'Lightning', bg: 'rgba(249,144,31,.14)', color: '#F9901F' },
  { icon: 'User', bg: 'rgba(96,192,65,.14)', color: '#60C041' },
  { icon: 'Avatar', bg: 'rgba(255,128,200,.14)', color: '#FF80C8' },
  { icon: 'Timer', bg: 'rgba(93,135,255,.12)', color: '#5D87FF' },
  { icon: 'Connection', bg: 'rgba(56,192,252,.14)', color: '#38C0FC' },
]

async function load() {
  const [dash, cards] = await Promise.all([
    api('/api/dashboard').catch(() => null),
    api('/api/dashboard/cards').catch(() => null),
  ])
  d.value = (dash && dash.data) || {}
  pluginCards.value = (cards && cards.data) || []

  const data = d.value
  const raw = [
    { label: '活跃插件', value: data.plugins_active ?? 0 },
    { label: '插件总数', value: data.plugins_total ?? 0 },
    { label: '命令数', value: data.commands_total ?? 0 },
    { label: '动态命令', value: data.dynamic_commands ?? 0 },
    { label: '用户数', value: data.users_total ?? 0 },
    { label: '活跃群', value: data.groups_active ?? 0 },
    { label: '定时任务', value: data.tasks_active ?? 0 },
    { label: '在线 Bot', value: (data.bots || []).length },
  ]
  stats.value = raw.map((s, i) => ({ ...s, ...META[i % META.length] }))
}

onMounted(load)
</script>

<style scoped>
.stat-card { text-align: left; }
.stat-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.stat-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 36px;
  height: 36px;
  border-radius: calc(var(--custom-radius) / 2);
}
.stat-card .stat-value { font-size: 24px; }
.plugin-card { margin-bottom: 16px; text-align: center; }
.h-card { height: 100%; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
.card-title { font-size: 15px; font-weight: 600; }
.bot-tags { display: flex; flex-wrap: wrap; }
</style>
