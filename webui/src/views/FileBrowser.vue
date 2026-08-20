<template>
  <div class="page-container">
    <el-card shadow="never">
      <div class="fb-toolbar mb">
        <el-button size="small" @click="mkdirVisible = true">＋ 新建目录</el-button>
        <el-button size="small" @click="uploadInput.click()">↑ 上传文件</el-button>
        <input type="file" ref="uploadInput" multiple style="display:none" @change="doUpload" />
        <el-input v-model="search" placeholder="搜索名称..." style="width:200px" clearable />
        <el-button size="small" @click="load">刷新</el-button>
      </div>

      <el-breadcrumb separator="/" class="mb">
        <el-breadcrumb-item><a @click="goPath('')">根目录</a></el-breadcrumb-item>
        <el-breadcrumb-item v-for="(seg, i) in crumbs" :key="i">
          <a @click="goPath(seg.path)">{{ seg.label }}</a>
        </el-breadcrumb-item>
      </el-breadcrumb>

      <el-table :data="filtered" size="small" border @row-click="onRowClick" highlight-current-row>
        <el-table-column label="名称">
          <template #default="{ row }">
            <el-icon v-if="row.is_dir" style="margin-right:6px"><Folder /></el-icon>
            <el-icon v-else style="margin-right:6px"><Document /></el-icon>
            <span :class="{ 'dir-name': row.is_dir }">{{ row.name }}</span>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ row.is_dir ? '-' : fmtFileSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="修改时间" width="170">
          <template #default="{ row }">{{ fmtMtime(row.mtime) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="260">
          <template #default="{ row }">
            <el-button size="small" @click.stop="row.is_dir ? enterDir(row) : openFile(row)">打开</el-button>
            <el-button v-if="!row.is_dir" size="small" @click.stop="download(row)">下载</el-button>
            <el-button size="small" @click.stop="copy(row)">复制</el-button>
            <el-button size="small" @click.stop="rename(row)">重命名</el-button>
            <el-button size="small" type="danger" @click.stop="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-if="!filtered.length" description="空目录" :image-size="50" />
    </el-card>

    <el-card shadow="never" class="mt">
      <div class="dim small mb">{{ currentFile || '点击文件名查看 / 编辑内容' }}</div>
      <el-input v-model="fileContent" type="textarea" :rows="10" :readonly="!canEdit" class="mono" />
      <div class="mt">
        <el-button type="primary" :disabled="!canEdit" @click="saveFile">保存</el-button>
        <span class="dim small ml" v-if="saveMsg">{{ saveMsg }}</span>
      </div>
    </el-card>

    <el-dialog v-model="mkdirVisible" title="新建目录" width="400px">
      <el-form label-width="80px">
        <el-form-item label="目录名"><el-input v-model="mkdirName" placeholder="请输入目录名" /></el-form-item>
      </el-form>
      <div class="dim small">位置：{{ path || '根目录' }}</div>
      <template #footer>
        <el-button @click="mkdirVisible = false">取消</el-button>
        <el-button type="primary" @click="mkdirDo">创建</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="renameVisible" title="重命名" width="400px">
      <el-form label-width="80px">
        <el-form-item label="新名称"><el-input v-model="renameName" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" @click="renameDo">重命名</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall, fmtFileSize, fmtMtime } from '../api'

const path = ref('')
const entries = ref([])
const search = ref('')
const currentFile = ref('')
const fileContent = ref('')
const canEdit = ref(false)
const saveMsg = ref('')

const uploadInput = ref(null)
const mkdirVisible = ref(false)
const mkdirName = ref('')
const renameVisible = ref(false)
const renameName = ref('')
let renamePath = ''

const crumbs = computed(() => {
  const segs = path.value ? path.value.split(/[\\/]+/).filter(Boolean) : []
  const parts = [{ label: '根目录', path: '' }]
  let acc = ''
  segs.forEach(s => { acc = acc ? acc + '/' + s : s; parts.push({ label: s, path: acc }) })
  return parts
})

const filtered = computed(() => {
  const q = search.value.toLowerCase()
  if (!q) return entries.value
  return entries.value.filter(e => e.name.toLowerCase().includes(q))
})

async function load() {
  const r = await apiCall('/api/files/list?path=' + encodeURIComponent(path.value))
  if (r) entries.value = (r.data && r.data.entries) || []
}

function goPath(p) { path.value = p || ''; currentFile.value = ''; fileContent.value = ''; canEdit.value = false; load() }
function enterDir(row) { path.value = row.path; currentFile.value = ''; fileContent.value = ''; canEdit.value = false; load() }
function onRowClick(row) { if (row.is_dir) enterDir(row); else openFile(row) }

async function openFile(row) {
  currentFile.value = row.path
  const r = await apiCall('/api/files/read?path=' + encodeURIComponent(row.path))
  if (!r) return
  fileContent.value = r.data.content
  canEdit.value = true
  saveMsg.value = ''
}
async function saveFile() {
  const r = await apiCall('/api/files/write', { method: 'PUT', body: { path: currentFile.value, content: fileContent.value } })
  if (r) { saveMsg.value = '✓ 已保存'; setTimeout(() => saveMsg.value = '', 2000) }
}

function download(row) { window.open('/api/files/download?path=' + encodeURIComponent(row.path), '_blank') }

async function copy(row) {
  const r = await apiCall('/api/files/copy', { method: 'POST', body: { src: row.path, dest_dir: path.value || '' } })
  if (r) { ElMessage.success(r.msg); load() }
}

async function mkdirDo() {
  const name = mkdirName.value.trim()
  if (!name || /[\\/]/.test(name) || name === '.' || name === '..') { ElMessage.warning('非法目录名'); return }
  const target = path.value ? path.value.replace(/[\\/]+$/, '') + '/' + name : name
  const r = await apiCall('/api/files/mkdir', { method: 'POST', body: { path: target } })
  if (r) { ElMessage.success(r.msg); mkdirVisible.value = false; load() }
}

function rename(row) {
  renamePath = row.path
  renameName.value = row.name
  renameVisible.value = true
}
async function renameDo() {
  const newName = renameName.value.trim()
  if (!newName || /[\\/]/.test(newName) || newName === '.' || newName === '..') { ElMessage.warning('非法名称'); return }
  const r = await apiCall('/api/files/rename', { method: 'POST', body: { path: renamePath, new_name: newName } })
  if (r) { ElMessage.success(r.msg); renameVisible.value = false; load() }
}

async function remove(row) {
  await ElMessageBox.confirm(`确定删除「${row.name}」吗？目录将递归删除，不可恢复。`, '删除', { type: 'warning' })
  const r = await apiCall('/api/files/delete', { method: 'POST', body: { path: row.path } })
  if (r) { ElMessage.success(r.msg); load() }
}

async function doUpload() {
  const input = uploadInput.value
  if (!input || !input.files || !input.files.length) return
  const fd = new FormData()
  fd.append('dir', path.value || '')
  for (const f of input.files) fd.append('files', f)
  const r = await apiCall('/api/files/upload', { method: 'POST', body: fd })
  if (r) { ElMessage.success(r.msg); input.value = ''; load() }
}

onMounted(load)
</script>

<style scoped>
.fb-toolbar { display: flex; align-items: center; gap: 8px; }
.dir-name { font-weight: 600; }
.ml { margin-left: 8px; }
</style>