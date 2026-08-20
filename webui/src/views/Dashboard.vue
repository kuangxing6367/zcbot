<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col v-for="s in stats" :key="s.label" :xs="12" :sm="8" :md="6" :lg="3">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-label">{{ s.label }}</div>
          <div class="stat-value">{{ s.value }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" class="mt">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">OneBot 连接
              <span class="dim small" v-if="d.ws_port">WS :{{ d.ws_port }}</span>
            </div>
          </template>
          <el-empty v-if="!(d.bots || []).length" description="暂无在线客户端" :image-size="60" />
          <div v-else>
            <el-tag v-for="b in d.bots" :key="b" type="success" style="margin:4px">{{ b }}</el-tag>
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              框架信息
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
          插件卡片
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

async function load() {
  const [dash, cards] = await Promise.all([
    api('/api/dashboard').catch(() => null),
    api('/api/dashboard/cards').catch(() => null),
  ])
  d.value = (dash && dash.data) || {}
  pluginCards.value = (cards && cards.data) || []

  const data = d.value
  stats.value = [
    { label: '活跃插件', value: data.plugins_active ?? 0 },
    { label: '插件总数', value: data.plugins_total ?? 0 },
    { label: '命令数', value: data.commands_total ?? 0 },
    { label: '动态命令', value: data.dynamic_commands ?? 0 },
    { label: '用户数', value: data.users_total ?? 0 },
    { label: '活跃群', value: data.groups_active ?? 0 },
    { label: '定时任务', value: data.tasks_active ?? 0 },
    { label: '在线 Bot', value: (data.bots || []).length },
  ]
}

onMounted(load)
</script>

<style scoped>
.stat-card { margin-bottom: 0; text-align: center; }
.plugin-card { margin-bottom: 16px; text-align: center; }
.card-head { display: flex; align-items: center; justify-content: space-between; }
</style>