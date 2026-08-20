<template>
  <div class="page-container">
    <el-card shadow="never" class="mb">
      <div class="toolbar">
        <span>选择插件页面</span>
        <el-select v-model="selected" style="width:260px" @change="onSelect">
          <el-option v-for="w in session.pluginWebUIs" :key="w.plugin_name" :label="w.title" :value="w.plugin_name" />
        </el-select>
      </div>
    </el-card>
    <el-empty v-if="!session.pluginWebUIs.length" description="没有可用的插件页面" :image-size="60" />
    <iframe v-else :key="frameKey" class="webui-frame"
            :src="`/api/plugin_webui/${encodeURIComponent(selected)}?entry=${encodeURIComponent(entry)}`" />
  </div>
</template>

<script setup>
import { ref, onMounted, computed } from 'vue'
import { session } from '../api'

const selected = ref('')
const entry = ref('index.html')
const frameKey = ref(0)

const frameSrc = computed(() =>
  `/api/plugin_webui/${encodeURIComponent(selected.value)}?entry=${encodeURIComponent(entry.value)}`)

function onSelect(name) {
  const w = session.pluginWebUIs.find(x => x.plugin_name === name)
  if (w) {
    entry.value = w.entry || 'index.html'
    frameKey.value++
  }
}

onMounted(() => {
  if (session.pluginWebUIs.length) {
    const first = session.pluginWebUIs[0]
    selected.value = first.plugin_name
    entry.value = first.entry || 'index.html'
  }
})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; }
</style>