<template>
  <div class="page-container">
    <div class="toolbar mb">
      <el-button @click="load"><el-icon><Refresh /></el-icon>&nbsp;刷新</el-button>
      <el-button @click="sourcesVisible = true">自定义源</el-button>
      <el-button @click="showGlobalDeps">依赖关系图</el-button>
      <span class="dim small">{{ filtered.length }} / {{ plugins.length }} 个插件 · {{ sources.length }} 个源</span>
    </div>
    <div class="toolbar mb">
      <el-input v-model="keyword" placeholder="搜索插件名称/描述/作者..." style="width:260px" clearable
                @keyup.enter="applyFilter" @clear="applyFilter" />
      <el-button @click="applyFilter">搜索</el-button>
    </div>

    <el-alert v-for="(e, i) in errors" :key="i" type="warning" :closable="false" show-icon class="mb"
              :title="'源加载失败: ' + e" />

    <el-row :gutter="16">
      <el-col v-for="(p, idx) in filtered" :key="p.name + p.source" :xs="24" :sm="12" :md="8" :lg="6">
        <el-card shadow="hover" class="plugin-card">
          <div class="plugin-head">
            <div>
              <div class="plugin-name">{{ p.name }}</div>
              <div class="plugin-ver small">v{{ p.version || '0.0.0' }}
                <el-tag size="small" :type="p.installed ? 'success' : 'info'" effect="plain">{{ p.installed ? '已安装' : '未安装' }}</el-tag>
              </div>
            </div>
          </div>
          <div class="plugin-desc">{{ p.description || '暂无描述' }}</div>
          <div class="plugin-badges">
            <el-tag size="small" effect="plain">{{ p.author || '未知作者' }}</el-tag>
            <el-tag size="small" effect="plain">{{ p.source || '默认源' }}</el-tag>
          </div>
          <div class="plugin-actions">
            <el-button size="small" @click="showDetail(p)">详情</el-button>
            <el-button v-if="p.installed" size="small" @click="$router.push('/plugins')">已安装</el-button>
            <el-button v-else size="small" type="primary" @click="install(p)">安装</el-button>
            <el-button v-if="p.dependencies?.python?.length" size="small" @click="showDepGraph(p)">依赖图</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>
    <el-empty v-if="!filtered.length" :description="keyword ? '未找到匹配的插件' : '市场为空，请检查网络或添加自定义源'" :image-size="60" />

    <!-- 详情 -->
    <el-dialog v-model="detailVisible" :title="detail?.name ? detail.name + ' v' + (detail.version || '0.0.0') : ''" width="680px">
      <template v-if="detail">
        <div class="mb">
          <el-tag size="small" effect="plain">{{ detail.author || '未知作者' }}</el-tag>
          <el-tag size="small" effect="plain">{{ detail.source || '默认源' }}</el-tag>
          <el-tag v-if="detail.installed" size="small" type="success">已安装</el-tag>
        </div>
        <div class="dim small mb">{{ detail.description || '暂无描述' }}</div>
        <el-descriptions v-if="detail.repo" :column="1" size="small" border>
          <el-descriptions-item label="仓库">
            <el-link type="primary" :href="'https://github.com/' + detail.repo" target="_blank" class="mono">{{ detail.repo }}</el-link>
          </el-descriptions-item>
        </el-descriptions>
        <h4>Python 依赖</h4>
        <div v-for="d in detail.dependencies?.python || []" :key="d" class="mono small">{{ d }}</div>
        <div v-if="!(detail.dependencies?.python || []).length" class="dim small">无 Python 依赖</div>
        <template v-if="detail.repo">
          <h4>README</h4>
          <pre class="readme-pre">{{ readme }}</pre>
          <h4>更新历史</h4>
          <div v-if="commits.length">
            <div v-for="c in commits" :key="c.sha" class="commit-row">
              <span class="mono small dim">{{ c.sha.slice(0, 7) }}</span>
              <span class="small" style="flex:1;margin:0 8px">{{ c.msg }}</span>
              <span class="small dim">{{ c.date }}</span>
            </div>
          </div>
          <div v-else class="dim small">加载中...</div>
        </template>
      </template>
      <template #footer>
        <el-button v-if="detail?.installed" @click="$router.push('/plugins')">前往插件管理</el-button>
        <el-button v-else type="primary" @click="detail && install(detail)">安装插件</el-button>
        <el-button @click="detailVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- 自定义源 -->
    <el-dialog v-model="sourcesVisible" title="插件源管理" width="560px">
      <div class="small dim mb">默认源（不可修改）</div>
      <el-input :model-value="defaultSource ? defaultSource.name + '  ' + defaultSource.url : ''" readonly style="margin-bottom:12px" />
      <div class="small dim mb">自定义源（Registry JSON 地址）</div>
      <div v-for="(s, i) in customSources" :key="i" class="source-row">
        <el-input v-model="s.name" placeholder="名称" style="width:140px" />
        <el-input v-model="s.url" placeholder="https://.../registry.json" />
        <el-button type="danger" @click="customSources.splice(i, 1)">✕</el-button>
      </div>
      <el-button size="small" @click="customSources.push({ name: '', url: '' })">＋ 添加源</el-button>
      <template #footer>
        <el-button @click="sourcesVisible = false">取消</el-button>
        <el-button type="primary" @click="saveSources">保存</el-button>
      </template>
    </el-dialog>

    <!-- 依赖图 -->
    <el-dialog v-model="depVisible" :title="depTitle" width="780px">
      <div class="dim small mb" v-if="globalDeps">{{ globalDeps.total_plugins }} 插件 · {{ globalDeps.total_edges }} 关联</div>
      <div class="dep-svg" v-html="depSvg"></div>
      <template v-if="globalDeps">
        <h4>依赖详情</h4>
        <div v-for="n in globalDeps.nodes" :key="n.id" class="dep-row">
          <span><b>{{ n.id }}</b> <span class="dim small">v{{ n.version }}</span></span>
          <span class="small">{{ n.dep_count }} 个依赖
            <el-tag v-if="n.missing_count" type="danger" size="small">缺 {{ n.missing_count }}</el-tag>
          </span>
        </div>
        <div v-if="globalDeps.edges.length" class="dim small mt">共享依赖（多个插件依赖同一包）：</div>
        <div v-for="e in globalDeps.edges" :key="e.source + e.target" class="small">
          {{ e.source }} ↔ {{ e.target }} <span class="dim">({{ e.label }})</span>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const plugins = ref([])
const errors = ref([])
const sources = ref([])
const keyword = ref('')

const detailVisible = ref(false)
const detail = ref(null)
const readme = ref('')
const commits = ref([])

const sourcesVisible = ref(false)
const defaultSource = ref(null)
const customSources = ref([])

const depVisible = ref(false)
const depTitle = ref('')
const depSvg = ref('')
const globalDeps = ref(null)

const filtered = computed(() => {
  const kw = keyword.value.toLowerCase()
  if (!kw) return plugins.value
  return plugins.value.filter(p =>
    (p.name || '').toLowerCase().includes(kw) ||
    (p.description || '').toLowerCase().includes(kw) ||
    (p.author || '').toLowerCase().includes(kw))
})

async function load() {
  const r = await apiCall('/api/plugins/market')
  if (!r) return
  const d = r.data || { plugins: [], errors: [], sources: [] }
  plugins.value = d.plugins || []
  errors.value = d.errors || []
  sources.value = d.sources || []
}
function applyFilter() { /* computed 自动过滤 */ }

function renderDepGraphSVG(nodes, edges) {
  if (!nodes || !nodes.length) return '<div>无数据</div>'
  const W = 700, H = Math.max(300, nodes.length * 50 + 60)
  const centerX = W / 2
  const pluginNodes = nodes.filter(n => n.type === 'plugin')
  const pkgNodes = nodes.filter(n => n.type === 'pkg')
  const positions = {}
  const spacing = Math.min(50, (H - 60) / Math.max(pluginNodes.length + pkgNodes.length || 1, 1))
  pluginNodes.forEach((n, i) => { positions[n.id] = { x: 120, y: 40 + i * spacing + 20 } })
  pkgNodes.forEach((n, i) => { positions[n.id] = { x: W - 120, y: 40 + i * spacing + 20 } })

  const edgePaths = edges.map(e => {
    const s = positions[e.source], t = positions[e.target]
    if (!s || !t) return ''
    const midX = (s.x + t.x) / 2
    return `<path class="dep-edge" d="M${s.x},${s.y} C${midX},${s.y} ${midX},${t.y} ${t.x},${t.y}"/>
      <text class="dep-edge-label" x="${midX}" y="${(s.y + t.y) / 2 - 6}">${escapeHtml(e.label || '')}</text>`
  }).join('\n')

  const nodeRects = nodes.map(n => {
    const pos = positions[n.id]
    if (!pos) return ''
    const isPlugin = n.type === 'plugin'
    const w = isPlugin ? 140 : 160, h = 32
    const x = pos.x - w / 2, y = pos.y - h / 2
    const subText = isPlugin ? `v${n.version || '?'}` : (n.dep_count ? `${n.dep_count} 引用` : '')
    return `<g>
      <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="8" ry="8"
            fill="${isPlugin ? '#eef2ff' : '#fef3c7'}" stroke="${isPlugin ? '#6366f1' : '#f59e0b'}" stroke-width="1.5"/>
      <text class="dep-node-text" x="${pos.x}" y="${pos.y - 4}" fill="${isPlugin ? '#4f46e5' : '#d97706'}">${escapeHtml(n.id)}</text>
      ${subText ? `<text class="dep-node-sub" x="${pos.x}" y="${pos.y + 10}">${escapeHtml(subText)}</text>` : ''}
    </g>`
  }).join('\n')

  return `<svg class="dep-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">
    <defs><style>
      .dep-edge { stroke: #2f3550; stroke-width: 1.5; fill: none; }
      .dep-edge-label { font-size: 10px; fill: #5c6277; text-anchor: middle; }
      .dep-node-text { font-size: 12px; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
      .dep-node-sub { font-size: 10px; fill: #5c6277; text-anchor: middle; dominant-baseline: central; pointer-events: none; }
    </style></defs>
    ${edgePaths}
    ${nodeRects}
  </svg>`
}

function escapeHtml(str) {
  if (str === null || str === undefined) return ''
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

async function showDetail(p) {
  detail.value = p
  detailVisible.value = true
  readme.value = ''
  commits.value = []
  if (p.repo) {
    const repoPath = p.repo.replace(/^https?:\/\/github\.com\//, '')
    try {
      const r = await fetch(`https://api.github.com/repos/${repoPath}/readme`, { headers: { 'Accept': 'application/vnd.github.v3.raw' } })
      if (r.ok) readme.value = (await r.text()).slice(0, 3000)
    } catch (e) { /* ignore */ }
    try {
      const r = await fetch(`https://api.github.com/repos/${repoPath}/commits?sha=${p.branch || 'main'}&per_page=10`)
      if (r.ok) {
        const list = await r.json()
        commits.value = (list || []).map(c => ({
          sha: c.sha || '',
          msg: (c.commit?.message || '').split('\n')[0],
          date: c.commit?.author?.date ? new Date(c.commit.author.date).toLocaleDateString() : '',
        }))
      }
    } catch (e) { /* ignore */ }
  }
}

async function install(p) {
  await ElMessageBox.confirm(`确定安装插件 [${p.name}] 吗？将从 ${p.repo || ''} 下载。`, '安装插件', { type: 'warning' })
  const r = await apiCall('/api/plugins/market/install', {
    method: 'POST',
    body: { name: p.name, repo: p.repo, branch: p.branch, sub_path: p.sub_path },
  })
  if (r) { ElMessage.success(r.msg); detailVisible.value = false; load() }
}

function showDepGraph(p) {
  const deps = p.dependencies?.python || []
  const nodes = [{ id: p.name, type: 'plugin', version: p.version || '?' }]
  const edges = []
  deps.forEach(d => {
    const pkgName = d.replace(/[>=<!~].*$/, '').trim()
    nodes.push({ id: pkgName, type: 'pkg' })
    edges.push({ source: p.name, target: pkgName, label: d })
  })
  depTitle.value = '依赖关系图 · ' + p.name
  globalDeps.value = null
  depSvg.value = renderDepGraphSVG(nodes, edges)
  depVisible.value = true
}

async function showGlobalDeps() {
  const r = await apiCall('/api/plugins/deps/graph')
  if (!r) return
  globalDeps.value = r.data
  depTitle.value = '全局插件依赖关系图'
  depSvg.value = renderDepGraphSVG(r.data.nodes || [], r.data.edges || [])
  depVisible.value = true
}

async function openSources() {
  const r = await apiCall('/api/plugins/market/sources')
  if (!r) return
  defaultSource.value = r.data.default || null
  customSources.value = (r.data.custom || []).map(x => ({ ...x }))
  sourcesVisible.value = true
}
async function saveSources() {
  const r = await apiCall('/api/plugins/market/sources', { method: 'POST', body: { sources: customSources.value } })
  if (r) { ElMessage.success(r.msg); sourcesVisible.value = false; load() }
}

onMounted(() => {
  load()
})
</script>

<style scoped>
.toolbar { display: flex; align-items: center; gap: 8px; }
.plugin-card { margin-bottom: 16px; }
.plugin-head { margin-bottom: 8px; }
.plugin-name { font-weight: 700; font-size: 15px; }
.plugin-desc { color: var(--el-text-color-secondary); font-size: 13px; min-height: 38px; margin-bottom: 8px; }
.plugin-badges { margin-bottom: 10px; }
.plugin-actions { display: flex; gap: 4px; flex-wrap: wrap; }
.readme-pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; max-height: 300px; overflow: auto; }
.commit-row { display: flex; padding: 4px 0; border-bottom: 1px solid var(--el-border-color); }
.source-row { display: flex; gap: 8px; margin-bottom: 8px; }
.dep-row { display: flex; justify-content: space-between; padding: 4px 0; border-bottom: 1px solid var(--el-border-color); }
.mb { margin-bottom: 12px; }
.mt { margin-top: 12px; }
</style>