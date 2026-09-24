<template>
  <div class="page-container">
    <el-row :gutter="16">
      <el-col v-for="a in adapters" :key="a.id" :span="12" class="mb">
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              {{ a.name || a.id }}
              <el-tag size="small" :type="(a.status?.total || 0) ? 'success' : 'info'">
                {{ a.status?.total || 0 }} 个在线
              </el-tag>
            </div>
          </template>
          <el-form v-if="a.fields?.length" label-width="120px">
            <el-form-item v-for="f in a.fields" :key="f.key" :label="f.label || f.key">
              <el-input-number
                v-if="f.type === 'number'"
                v-model="a.config[f.key]"
                :controls="false"
                style="width:100%"
              />
              <el-input
                v-else
                v-model="a.config[f.key]"
                :type="f.type === 'password' ? 'password' : 'text'"
                :show-password="f.type === 'password'"
              />
            </el-form-item>
            <div>
              <el-button type="primary" @click="save(a)">保存配置</el-button>
              <el-button @click="load">刷新</el-button>
            </div>
            <div class="dim small mt" v-if="a.restart_keys?.length">
              {{ a.restart_keys.join(' / ') }} 改动需重启框架生效；其余字段立即生效。
            </div>
          </el-form>
          <div v-else class="dim small">该接入端无本地可编辑连接配置。</div>
          <div v-if="a.endpoint_hint" class="dim small mt mono">
            接入地址：{{ a.endpoint_hint }}
          </div>
          <div v-if="a.guide" class="dim small mt" style="line-height:1.8">{{ a.guide }}</div>
          <div class="mt">
            <div v-for="b in (a.status?.connected_bots || [])" :key="b" class="bot-row">
              <span class="mono">{{ b }}</span>
              <el-tag size="small" type="success">在线</el-tag>
            </div>
            <el-empty
              v-if="!(a.status?.connected_bots || []).length"
              description="暂无客户端连接"
              :image-size="40"
            />
          </div>
        </el-card>
      </el-col>
      <el-col v-if="!adapters.length" :span="24">
        <el-card shadow="never">
          <el-empty description="未加载任何协议接入端（请启用 onebot_adapter / http_inject 等）" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api, apiCall } from '../api'

const adapters = ref([])

async function load() {
  const r = await api('/api/connection').catch(() => null)
  if (!r) return
  const list = (r.data && r.data.adapters) || []
  adapters.value = list.map(a => ({
    ...a,
    config: { ...(a.config || {}) },
    fields: a.fields || [],
    status: a.status || {},
    restart_keys: a.restart_keys || [],
  }))
}

async function save(a) {
  const body = { adapter: a.id, data: { ...(a.config || {}) } }
  const r = await apiCall('/api/connection', { method: 'PUT', body })
  if (r) { ElMessage.success(r.msg); load() }
}

onMounted(load)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
.bot-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--el-border-color); }
</style>