<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header><div class="card-head">OneBot11 反向 WS 配置</div></template>
          <el-form label-width="120px">
            <el-form-item label="监听地址"><el-input v-model="form.host" /></el-form-item>
            <el-form-item label="监听端口"><el-input-number v-model="form.port" :controls="false" style="width:100%" /></el-form-item>
            <el-form-item label="Access Token"><el-input v-model="form.token" type="password" show-password /></el-form-item>
          </el-form>
          <div>
            <el-button type="primary" @click="save">保存配置</el-button>
            <el-button @click="load">刷新</el-button>
          </div>
          <div class="dim small mt">监听地址 / 端口改动需重启框架生效；Access Token 立即生效。</div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never" class="mb">
          <template #header>
            <div class="card-head">实时连接状态
              <el-tag size="small" :type="bots.length ? 'success' : 'info'">{{ bots.length }} 个在线</el-tag>
            </div>
          </template>
          <div v-for="b in bots" :key="b" class="bot-row">
            <span class="mono">{{ b }}</span>
            <el-tag size="small" type="success">在线</el-tag>
          </div>
          <el-empty v-if="!bots.length" description="暂无客户端连接" :image-size="50" />
        </el-card>
        <el-card shadow="never">
          <template #header><div class="card-head">接入指引</div></template>
          <div class="dim" style="line-height:2">
            <div>反向 WS 服务端地址：<code class="mono">ws://{{ wsUrl }}/ws</code></div>
            <div>OneBot 客户端（NapCat / Lagrange / LLOneBot 等）添加「反向 WebSocket」连接，填写上述地址即可接入。</div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api, apiCall } from '../api'

const form = ref({ host: '', port: 6830, token: '' })
const bots = ref([])

const wsUrl = computed(() => {
  const host = form.value.host === '0.0.0.0' || form.value.host === '::' ? '127.0.0.1' : form.value.host
  return `${host}:${form.value.port}`
})

async function load() {
  const r = await api('/api/connection').catch(() => null)
  if (!r) return
  const c = r.data.config || {}
  const st = r.data.status || {}
  bots.value = st.connected_bots || []
  form.value.host = c.listen_host || '0.0.0.0'
  form.value.port = c.listen_port || st.ws_port || 6830
  form.value.token = c.access_token || ''
}

async function save() {
  const r = await apiCall('/api/connection', {
    method: 'PUT',
    body: { listen_host: form.value.host.trim(), listen_port: form.value.port, access_token: form.value.token },
  })
  if (r) { ElMessage.success(r.msg); load() }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
.bot-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--el-border-color); }
</style>