<template>
  <div class="page-container">
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span>权限管理 · LuckPerms 风格</span>
          <div>
            <el-button size="small" @click="loadAll">刷新</el-button>
            <el-button size="small" @click="cleanup">清理过期</el-button>
          </div>
        </div>
      </template>

      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 14px">
        <template #title>
          节点 <code>group.xxx</code> 表示「继承/加入 xxx 组」；上下文支持
          <code>group</code>（群号）、<code>bot</code>（实例名）、<code>msgtype</code>（group/private），留空=全局。
          内置角色组 <code>__super / __owner / __admin / __member</code> 由框架注入、不可编辑。
        </template>
      </el-alert>

      <el-tabs v-model="tab">
        <!-- ══════════ 权限组 ══════════ -->
        <el-tab-pane label="权限组" name="groups">
          <div class="toolbar">
            <el-button type="primary" size="small" @click="openGroupCreate">新建权限组</el-button>
            <span class="dim small">共 {{ groups.length }} 个组（weight 越大优先级越高）</span>
          </div>
          <el-table :data="groups" size="small" border>
            <el-table-column prop="name" label="组名" width="160">
              <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
            </el-table-column>
            <el-table-column prop="display_name" label="显示名" width="140" />
            <el-table-column prop="weight" label="权重" width="80" />
            <el-table-column label="默认组" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.is_default" type="success" size="small">是</el-tag>
                <span v-else class="dim">否</span>
              </template>
            </el-table-column>
            <el-table-column prop="node_count" label="节点数" width="90" />
            <el-table-column label="操作" width="200">
              <template #default="{ row }">
                <el-button size="small" @click="openNodes(row)">节点</el-button>
                <el-button size="small" @click="openGroupEdit(row)">编辑</el-button>
                <el-button size="small" type="danger" @click="removeGroup(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ══════════ 用户授权 ══════════ -->
        <el-tab-pane label="用户授权" name="users">
          <div class="toolbar">
            <el-input v-model="userQuery" size="small" placeholder="输入 用户 ID" style="width: 200px" @keyup.enter="loadUser" />
            <el-button size="small" type="primary" @click="loadUser">查询</el-button>
            <el-select v-model="userCtx" size="small" placeholder="上下文（可选）" clearable style="width: 220px">
              <el-option v-for="c in ctxOptions" :key="c.value" :label="c.label" :value="c.value" />
            </el-select>
          </div>

          <template v-if="userData">
            <h4 class="sec">生效组
              <span class="dim small">（按 weight 降序，主组：
                <el-tag size="small">{{ userData.snapshot.primary_group }}</el-tag>）</span>
            </h4>
            <div class="tag-row">
              <el-tag v-for="g in userData.snapshot.groups" :key="g" size="small"
                      :type="g.startsWith('__') ? 'warning' : 'primary'" effect="plain">{{ g }}</el-tag>
              <span v-if="!userData.snapshot.groups.length" class="dim">无</span>
            </div>

            <h4 class="sec">生效节点（{{ nodeList.length }}）</h4>
            <el-table :data="nodeList" size="small" border max-height="240">
              <el-table-column prop="node" label="节点" />
              <el-table-column label="值" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.value ? 'success' : 'danger'" size="small">{{ row.value ? '授予' : '否决' }}</el-tag>
                </template>
              </el-table-column>
            </el-table>

            <h4 class="sec">直接节点 / 组归属</h4>
            <el-table :data="userData.raw_nodes" size="small" border>
              <el-table-column prop="node" label="节点" />
              <el-table-column label="值" width="90">
                <template #default="{ row }">
                  <el-tag :type="row.value ? 'success' : 'danger'" size="small">{{ row.value ? '授予' : '否决' }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="上下文" width="180">
                <template #default="{ row }">
                  <span class="mono">{{ row.context_key ? `${row.context_key}=${row.context_val}` : '全局' }}</span>
                </template>
              </el-table-column>
              <el-table-column label="过期" width="160">
                <template #default="{ row }">{{ row.expire_at ? fmtUnix(row.expire_at) : '永久' }}</template>
              </el-table-column>
              <el-table-column label="操作" width="80">
                <template #default="{ row }">
                  <el-button size="small" type="danger" @click="unsetUserNode(row)">删除</el-button>
                </template>
              </el-table-column>
            </el-table>

            <div class="toolbar" style="margin-top: 12px">
              <el-button size="small" @click="userNodeDlg = true">设置节点</el-button>
              <el-button size="small" @click="userGroupDlg = true">加入权限组</el-button>
              <el-button size="small" @click="trackDlg = true">升降级</el-button>
            </div>
          </template>
          <el-empty v-else description="输入 用户 ID后查询" :image-size="60" />
        </el-tab-pane>

        <!-- ══════════ 升降级轨道 ══════════ -->
        <el-tab-pane label="升降级轨道" name="tracks">
          <div class="toolbar">
            <el-button type="primary" size="small" @click="openTrackCreate">新建轨道</el-button>
            <span class="dim small">promote = 向右一格，demote = 向左一格</span>
          </div>
          <el-table :data="tracks" size="small" border>
            <el-table-column prop="name" label="轨道名" width="160">
              <template #default="{ row }"><span class="mono">{{ row.name }}</span></template>
            </el-table-column>
            <el-table-column prop="display_name" label="显示名" width="140" />
            <el-table-column prop="groups_order" label="晋升链（左 → 右）" />
            <el-table-column label="操作" width="160">
              <template #default="{ row }">
                <el-button size="small" @click="openTrackEdit(row)">编辑</el-button>
                <el-button size="small" type="danger" @click="removeTrack(row)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ══════════ 权限检查器 ══════════ -->
        <el-tab-pane label="权限检查器" name="check">
          <div class="toolbar">
            <el-input v-model="checkForm.user_id" size="small" placeholder="用户 ID" style="width: 180px" />
            <el-input v-model="checkForm.node" size="small" placeholder="权限节点，如 chat.ban" style="width: 260px" />
            <el-select v-model="checkForm.role" size="small" placeholder="身份（可选）" clearable style="width: 150px">
              <el-option v-for="r in roleOptions" :key="r" :label="r" :value="r" />
            </el-select>
            <el-button size="small" type="primary" @click="runCheck">检查</el-button>
          </div>
          <el-input v-model="checkForm.context" size="small" placeholder="上下文 JSON，如 {&quot;group&quot;:&quot;123456&quot;}" style="max-width: 520px" />
          <div v-if="checkResult" style="margin-top: 14px">
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="节点">{{ checkResult.node }}</el-descriptions-item>
              <el-descriptions-item label="结果">
                <el-tag :type="stateTag(checkResult.state).type" size="small">{{ stateTag(checkResult.state).text }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="生效组">
                <el-tag v-for="g in checkResult.groups" :key="g" size="small" effect="plain" style="margin-right:4px">{{ g }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="主组">{{ checkResult.primary_group }}</el-descriptions-item>
            </el-descriptions>
          </div>
        </el-tab-pane>

        <!-- ══════════ 审计日志 ══════════ -->
        <el-tab-pane label="审计日志" name="audit">
          <el-table :data="audits" size="small" border max-height="460">
            <el-table-column label="时间" width="160">
              <template #default="{ row }">{{ fmtUnix(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="operator" label="操作者" width="120" />
            <el-table-column prop="action" label="动作" width="120" />
            <el-table-column prop="target_type" label="类型" width="90" />
            <el-table-column prop="target" label="目标" width="140" />
            <el-table-column prop="node" label="节点" />
            <el-table-column prop="context" label="上下文" width="140" />
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </el-card>

    <!-- 组 新建/编辑 -->
    <el-dialog v-model="groupDlg" :title="groupForm._new ? '新建权限组' : '编辑权限组'" width="460px">
      <el-form :model="groupForm" label-width="90px" size="small">
        <el-form-item label="组名">
          <el-input v-model="groupForm.name" :disabled="!groupForm._new" placeholder="小写字母数字下划线" />
        </el-form-item>
        <el-form-item label="显示名"><el-input v-model="groupForm.display_name" /></el-form-item>
        <el-form-item label="权重"><el-input-number v-model="groupForm.weight" :min="-100" :max="9999" /></el-form-item>
        <el-form-item label="前缀"><el-input v-model="groupForm.prefix" /></el-form-item>
        <el-form-item label="后缀"><el-input v-model="groupForm.suffix" /></el-form-item>
        <el-form-item label="默认组">
          <el-switch v-model="groupForm.is_default" />
          <span class="dim small" style="margin-left:8px">开启后全员自动拥有</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="groupDlg = false">取消</el-button>
        <el-button size="small" type="primary" @click="saveGroup">保存</el-button>
      </template>
    </el-dialog>

    <!-- 组节点 -->
    <el-dialog v-model="nodeDlg" :title="`节点管理 · ${curGroup}`" width="760px">
      <div class="toolbar">
        <el-input v-model="nodeForm.node" size="small" placeholder="节点，如 chat.ban 或 group.vip" style="width: 240px" />
        <el-select v-model="nodeForm.value" size="small" style="width: 110px">
          <el-option :value="true" label="授予" />
          <el-option :value="false" label="否决" />
        </el-select>
        <el-select v-model="nodeForm.context_key" size="small" placeholder="全局" clearable style="width: 130px">
          <el-option v-for="k in contextKeys" :key="k" :label="k" :value="k" />
        </el-select>
        <el-input v-model="nodeForm.context_val" size="small" placeholder="上下文值" style="width: 140px" />
        <el-date-picker v-model="nodeForm.expire" type="datetime" size="small" placeholder="永久" value-format="x" style="width: 190px" />
        <el-button size="small" type="primary" @click="addGroupNode">添加</el-button>
      </div>
      <el-table :data="groupNodes" size="small" border max-height="320">
        <el-table-column prop="node" label="节点" />
        <el-table-column label="值" width="80">
          <template #default="{ row }">
            <el-tag :type="row.value ? 'success' : 'danger'" size="small">{{ row.value ? '授予' : '否决' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上下文" width="160">
          <template #default="{ row }">
            <span class="mono">{{ row.context_key ? `${row.context_key}=${row.context_val}` : '全局' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="过期" width="150">
          <template #default="{ row }">{{ row.expire_at ? fmtUnix(row.expire_at) : '永久' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ row }">
            <el-button size="small" type="danger" @click="delGroupNode(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-dialog>

    <!-- 用户节点 -->
    <el-dialog v-model="userNodeDlg" title="设置用户节点" width="520px">
      <el-form :model="userNodeForm" label-width="90px" size="small">
        <el-form-item label="节点">
          <el-input v-model="userNodeForm.node" placeholder="chat.ban 或 group.vip" />
        </el-form-item>
        <el-form-item label="值">
          <el-select v-model="userNodeForm.value" style="width: 120px">
            <el-option :value="true" label="授予" />
            <el-option :value="false" label="否决" />
          </el-select>
        </el-form-item>
        <el-form-item label="上下文">
          <el-select v-model="userNodeForm.context_key" placeholder="全局" clearable style="width: 140px">
            <el-option v-for="k in contextKeys" :key="k" :label="k" :value="k" />
          </el-select>
          <el-input v-model="userNodeForm.context_val" placeholder="值" style="width: 180px; margin-left: 8px" />
        </el-form-item>
        <el-form-item label="过期">
          <el-date-picker v-model="userNodeForm.expire" type="datetime" placeholder="永久" value-format="x" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="userNodeDlg = false">取消</el-button>
        <el-button size="small" type="primary" @click="saveUserNode">保存</el-button>
      </template>
    </el-dialog>

    <!-- 加入组 -->
    <el-dialog v-model="userGroupDlg" title="加入权限组" width="460px">
      <el-form :model="userGroupForm" label-width="90px" size="small">
        <el-form-item label="权限组">
          <el-select v-model="userGroupForm.group" filterable style="width: 240px">
            <el-option v-for="g in groups" :key="g.name" :label="`${g.name} (${g.display_name})`" :value="g.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="上下文">
          <el-select v-model="userGroupForm.context_key" placeholder="全局" clearable style="width: 140px">
            <el-option v-for="k in contextKeys" :key="k" :label="k" :value="k" />
          </el-select>
          <el-input v-model="userGroupForm.context_val" placeholder="值" style="width: 160px; margin-left: 8px" />
        </el-form-item>
        <el-form-item label="过期">
          <el-date-picker v-model="userGroupForm.expire" type="datetime" placeholder="永久" value-format="x" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="userGroupDlg = false">取消</el-button>
        <el-button size="small" type="primary" @click="saveUserGroup">加入</el-button>
      </template>
    </el-dialog>

    <!-- 升降级 -->
    <el-dialog v-model="trackDlg" title="升降级" width="420px">
      <el-form :model="trackForm" label-width="80px" size="small">
        <el-form-item label="轨道">
          <el-select v-model="trackForm.track" style="width: 240px">
            <el-option v-for="t in tracks" :key="t.name" :label="`${t.name} · ${t.groups_order}`" :value="t.name" />
          </el-select>
        </el-form-item>
        <el-form-item label="方向">
          <el-radio-group v-model="trackForm.direction">
            <el-radio value="promote">晋升</el-radio>
            <el-radio value="demote">降级</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="trackDlg = false">取消</el-button>
        <el-button size="small" type="primary" @click="runTrack">执行</el-button>
      </template>
    </el-dialog>

    <!-- 轨道编辑 -->
    <el-dialog v-model="trackEditDlg" :title="trackEditForm._new ? '新建轨道' : '编辑轨道'" width="480px">
      <el-form :model="trackEditForm" label-width="90px" size="small">
        <el-form-item label="轨道名">
          <el-input v-model="trackEditForm.name" :disabled="!trackEditForm._new" />
        </el-form-item>
        <el-form-item label="显示名"><el-input v-model="trackEditForm.display_name" /></el-form-item>
        <el-form-item label="晋升链">
          <el-input v-model="trackEditForm.groups_order" placeholder="lv1,lv2,lv3" />
          <div class="dim small">逗号分隔，从左到右为晋升方向</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button size="small" @click="trackEditDlg = false">取消</el-button>
        <el-button size="small" type="primary" @click="saveTrack">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, apiCall } from '../api'

const tab = ref('groups')
const contextKeys = ref(['group', 'bot', 'msgtype'])
const roleOptions = ['member', 'admin', 'owner', 'super']

const groups = ref([])
const tracks = ref([])
const audits = ref([])

const groupDlg = ref(false)
const groupForm = ref({})
const nodeDlg = ref(false)
const curGroup = ref('')
const groupNodes = ref([])
const nodeForm = ref({ node: '', value: true, context_key: '', context_val: '', expire: null })

const userQuery = ref('')
const userCtx = ref('')
const userData = ref(null)
const userNodeDlg = ref(false)
const userGroupDlg = ref(false)
const trackDlg = ref(false)
const userNodeForm = ref({ node: '', value: true, context_key: '', context_val: '', expire: null })
const userGroupForm = ref({ group: '', context_key: '', context_val: '', expire: null })
const trackForm = ref({ track: '', direction: 'promote' })

const trackEditDlg = ref(false)
const trackEditForm = ref({})

const checkForm = ref({ user_id: '', node: '', role: '', context: '' })
const checkResult = ref(null)

const ctxOptions = computed(() => [
  { label: '（全局）', value: '' },
  { label: 'msgtype=group', value: 'msgtype=group' },
  { label: 'msgtype=private', value: 'msgtype=private' },
])

const nodeList = computed(() => {
  const nodes = userData.value?.snapshot?.nodes || {}
  return Object.entries(nodes).map(([node, value]) => ({ node, value }))
})

function fmtUnix(ts) {
  const n = Number(ts)
  if (!n) return '-'
  const d = new Date(n * 1000)
  const p = (x) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function stateTag(state) {
  if (state === 'true') return { type: 'success', text: '放行' }
  if (state === 'false') return { type: 'danger', text: '显式否决' }
  return { type: 'info', text: '未定义（按拒绝处理）' }
}

function expireOf(v) {
  return v ? Math.floor(Number(v) / 1000) : null
}

// ── 加载 ──
async function loadAll() {
  const [g, t, a, b] = await Promise.all([
    api('/api/perm/groups').catch(() => null),
    api('/api/perm/tracks').catch(() => null),
    api('/api/perm/audit?limit=200').catch(() => null),
    api('/api/perm/builtins').catch(() => null),
  ])
  groups.value = g?.data || []
  tracks.value = t?.data || []
  audits.value = a?.data || []
  if (b?.data?.context_keys) contextKeys.value = b.data.context_keys
  if (userQuery.value) loadUser()
}

// ── 权限组 ──
function openGroupCreate() {
  groupForm.value = { _new: true, name: '', display_name: '', weight: 0, prefix: '', suffix: '', is_default: false }
  groupDlg.value = true
}
function openGroupEdit(row) {
  groupForm.value = { _new: false, ...row, is_default: !!row.is_default }
  groupDlg.value = true
}
async function saveGroup() {
  const f = groupForm.value
  if (!f.name) return ElMessage.warning('组名必填')
  const body = {
    display_name: f.display_name, weight: f.weight, prefix: f.prefix,
    suffix: f.suffix, is_default: f.is_default ? 1 : 0,
  }
  const r = f._new
    ? await apiCall('/api/perm/groups', { method: 'POST', body: { ...body, name: f.name } })
    : await apiCall(`/api/perm/groups/${encodeURIComponent(f.name)}`, { method: 'PUT', body })
  if (r) { ElMessage.success(r.msg); groupDlg.value = false; loadAll() }
}
async function removeGroup(row) {
  try { await ElMessageBox.confirm(`删除权限组 ${row.name}？该组的节点、其他组对它的继承、用户归属都会一并清理。`, '确认', { type: 'warning' }) }
  catch (e) { return }
  const r = await apiCall(`/api/perm/groups/${encodeURIComponent(row.name)}`, { method: 'DELETE' })
  if (r) { ElMessage.success(r.msg); loadAll() }
}

// ── 组节点 ──
async function openNodes(row) {
  curGroup.value = row.name
  nodeForm.value = { node: '', value: true, context_key: '', context_val: '', expire: null }
  await loadGroupNodes()
  nodeDlg.value = true
}
async function loadGroupNodes() {
  const r = await api(`/api/perm/groups/${encodeURIComponent(curGroup.value)}/nodes`).catch(() => null)
  groupNodes.value = r?.data || []
}
async function addGroupNode() {
  if (!nodeForm.value.node) return ElMessage.warning('节点名必填')
  const r = await apiCall(`/api/perm/groups/${encodeURIComponent(curGroup.value)}/nodes`, {
    method: 'POST',
    body: {
      node: nodeForm.value.node, value: nodeForm.value.value,
      context_key: nodeForm.value.context_key || null,
      context_val: nodeForm.value.context_val || null,
      expire_at: expireOf(nodeForm.value.expire),
    },
  })
  if (r) { ElMessage.success(r.msg); nodeForm.value.node = ''; loadGroupNodes(); loadAll() }
}
async function delGroupNode(row) {
  const r = await apiCall(`/api/perm/groups/${encodeURIComponent(curGroup.value)}/nodes`, {
    method: 'DELETE',
    body: { node: row.node, context_key: row.context_key, context_val: row.context_val },
  })
  if (r) { ElMessage.success(r.msg); loadGroupNodes(); loadAll() }
}

// ── 用户 ──
async function loadUser() {
  const uid = String(userQuery.value).trim()
  if (!uid) return
  const q = userCtx.value ? `?context=${encodeURIComponent(userCtx.value)}` : ''
  const r = await api(`/api/perm/users/${encodeURIComponent(uid)}${q}`).catch(() => null)
  userData.value = r?.data || null
  if (!r) ElMessage.error('查询失败')
}
async function saveUserNode() {
  if (!userNodeForm.value.node) return ElMessage.warning('节点名必填')
  const r = await apiCall(`/api/perm/users/${encodeURIComponent(userQuery.value)}/nodes`, {
    method: 'POST',
    body: {
      node: userNodeForm.value.node, value: userNodeForm.value.value,
      context_key: userNodeForm.value.context_key || null,
      context_val: userNodeForm.value.context_val || null,
      expire_at: expireOf(userNodeForm.value.expire),
    },
  })
  if (r) { ElMessage.success(r.msg); userNodeDlg.value = false; loadUser() }
}
async function unsetUserNode(row) {
  const r = await apiCall(`/api/perm/users/${encodeURIComponent(userQuery.value)}/nodes`, {
    method: 'DELETE', body: { node: row.node, context_key: row.context_key, context_val: row.context_val },
  })
  if (r) { ElMessage.success(r.msg); loadUser() }
}
async function saveUserGroup() {
  if (!userGroupForm.value.group) return ElMessage.warning('请选择权限组')
  const r = await apiCall(`/api/perm/users/${encodeURIComponent(userQuery.value)}/groups`, {
    method: 'POST',
    body: {
      group: userGroupForm.value.group,
      context_key: userGroupForm.value.context_key || null,
      context_val: userGroupForm.value.context_val || null,
      expire_at: expireOf(userGroupForm.value.expire),
    },
  })
  if (r) { ElMessage.success(r.msg); userGroupDlg.value = false; loadUser() }
}
async function runTrack() {
  if (!trackForm.value.track) return ElMessage.warning('请选择轨道')
  const r = await apiCall(`/api/perm/users/${encodeURIComponent(userQuery.value)}/track`, {
    method: 'POST', body: { track: trackForm.value.track, direction: trackForm.value.direction },
  })
  if (r) { ElMessage.success(r.msg); trackDlg.value = false; loadUser() }
}

// ── 轨道 ──
function openTrackCreate() {
  trackEditForm.value = { _new: true, name: '', display_name: '', groups_order: '' }
  trackEditDlg.value = true
}
function openTrackEdit(row) {
  trackEditForm.value = { _new: false, ...row }
  trackEditDlg.value = true
}
async function saveTrack() {
  const f = trackEditForm.value
  if (!f.name || !f.groups_order) return ElMessage.warning('轨道名与晋升链必填')
  const r = await apiCall('/api/perm/tracks', {
    method: 'POST',
    body: { name: f.name, display_name: f.display_name, groups_order: f.groups_order },
  })
  if (r) { ElMessage.success(r.msg); trackEditDlg.value = false; loadAll() }
}
async function removeTrack(row) {
  try { await ElMessageBox.confirm(`删除轨道 ${row.name}？`, '确认', { type: 'warning' }) } catch (e) { return }
  const r = await apiCall(`/api/perm/tracks/${encodeURIComponent(row.name)}`, { method: 'DELETE' })
  if (r) { ElMessage.success(r.msg); loadAll() }
}

// ── 检查器 / 清理 ──
async function runCheck() {
  let context = null
  if (checkForm.value.context) {
    try { context = JSON.parse(checkForm.value.context) }
    catch (e) { return ElMessage.error('上下文不是合法 JSON') }
  }
  const r = await api('/api/perm/check', {
    method: 'POST',
    body: { user_id: checkForm.value.user_id, node: checkForm.value.node, context, role: checkForm.value.role || null },
  }).catch(() => null)
  checkResult.value = r?.data || null
  if (!r) ElMessage.error('检查失败')
}
async function cleanup() {
  const r = await apiCall('/api/perm/cleanup', { method: 'POST' })
  if (r) { ElMessage.success(r.msg); loadAll() }
}

onMounted(loadAll)
</script>

<style scoped>
.card-head { display: flex; align-items: center; justify-content: space-between; }
.toolbar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }
.sec { margin: 16px 0 8px; font-size: 14px; font-weight: 500; }
.tag-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.dim { color: var(--el-text-color-secondary); }
.small { font-size: 12px; }
.mono { font-family: var(--font-mono, Consolas, Monaco, monospace); }
code { background: var(--el-fill-color-light); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
</style>
