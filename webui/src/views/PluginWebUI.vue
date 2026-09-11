<template>
  <div class="page-container">
    <el-card shadow="never" class="mb" v-if="!singleMode">
      <div class="toolbar">
        <span>选择插件页面</span>
        <el-select v-model="selected" style="width:260px" @change="onSelect">
          <el-option v-for="w in session.pluginWebUIs" :key="w.plugin_name" :label="w.title" :value="w.plugin_name" />
        </el-select>
      </div>
    </el-card>
    <el-empty v-if="!session.pluginWebUIs.length" description="没有可用的插件页面" :image-size="60" />
    <el-alert v-else-if="singleMode && !current" type="warning" :closable="false"
              :title="`未找到插件「${route.params.name}」的 WebUI`" />
    <iframe v-else-if="current" :key="frameKey" class="webui-frame"
            :src="`/api/plugin_webui/${encodeURIComponent(current.plugin_name)}?entry=${encodeURIComponent(current.entry || 'index.html')}`" />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { session, api } from '../api'

const route = useRoute()
const selected = ref('')
const frameKey = ref(0)

const singleMode = computed(() => !!route.params.name)
const current = computed(() => {
  const name = singleMode.value ? route.params.name : selected.value
  return (session.pluginWebUIs || []).find(x => x.plugin_name === name) || null
})

function onSelect() { frameKey.value++ }
async function ensureList() {
  if (!session.pluginWebUIs.length) {
    const r = await api('/api/plugin_webuis').catch(() => null)
    if (r && r.code === 0) session.pluginWebUIs = r.data || []
  }
  if (!singleMode.value && !selected.value && session.pluginWebUIs.length) {
    selected.value = session.pluginWebUIs[0].plugin_name
  }
  frameKey.value++
}
watch(() => route.params.name, () => { frameKey.value++ })

onMounted(ensureList)
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; }
</style>
