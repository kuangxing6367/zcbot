<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : '220px'" class="sidebar">
      <div class="brand">
        <img v-show="!collapsed" src="/img/logo.png" alt="ZCBOT" class="brand-logo" />
        <el-icon v-show="collapsed" :size="24" color="#6366f1"><Cpu /></el-icon>
        <span v-show="!collapsed" class="brand-name">ZCBOT</span>
      </div>
      <el-menu :default-active="activeKey" router :collapse="collapsed" class="nav-menu">
        <el-menu-item index="/dashboard"><el-icon><Odometer /></el-icon><span>仪表盘</span></el-menu-item>
        <el-menu-item index="/marketplace"><el-icon><Shop /></el-icon><span>插件市场</span></el-menu-item>
        <el-menu-item index="/plugins"><el-icon><Grid /></el-icon><span>插件管理</span></el-menu-item>
        <el-menu-item index="/commands"><el-icon><ChatDotRound /></el-icon><span>命令管理</span></el-menu-item>
        <el-menu-item index="/users"><el-icon><User /></el-icon><span>用户管理</span></el-menu-item>
        <el-menu-item index="/groups"><el-icon><Avatar /></el-icon><span>群组管理</span></el-menu-item>
        <el-menu-item index="/tasks"><el-icon><Timer /></el-icon><span>定时任务</span></el-menu-item>
        <el-menu-item index="/runtime"><el-icon><DataLine /></el-icon><span>运行状态</span></el-menu-item>
        <el-menu-item index="/connection"><el-icon><Connection /></el-icon><span>连接设置</span></el-menu-item>
        <el-menu-item index="/filebrowser"><el-icon><Folder /></el-icon><span>文件浏览</span></el-menu-item>
        <el-menu-item index="/logs"><el-icon><Document /></el-icon><span>日志中心</span></el-menu-item>
        <el-menu-item index="/database"><el-icon><Coin /></el-icon><span>数据库</span></el-menu-item>
        <el-menu-item v-if="session.pluginWebUIs.length" index="/plugin_webui"><el-icon><Monitor /></el-icon><span>插件页面</span></el-menu-item>
      </el-menu>
      <div class="sidebar-foot">
        <el-menu class="nav-menu" :default-active="activeKey" router :collapse="collapsed">
          <el-menu-item index="/settings"><el-icon><Setting /></el-icon><span>设置</span></el-menu-item>
        </el-menu>
        <div v-show="!collapsed" class="sb-version">v{{ session.frameworkVersion || '-' }}</div>
      </div>
    </el-aside>

    <el-container class="right">
      <el-header class="topbar" height="52px">
        <div class="topbar-left">
          <el-button text circle @click="collapsed = !collapsed">
            <el-icon :size="18"><Expand v-if="collapsed" /><Fold v-else /></el-icon>
          </el-button>
          <span class="page-title">{{ pageTitle }}</span>
        </div>
        <div class="topbar-right">
          <el-button text circle @click="toggleTheme" title="切换亮暗主题" class="theme-btn">
            <el-icon :size="18" :key="theme === 'dark' ? 'sun' : 'moon'">
              <Sunny v-if="theme === 'light'" />
              <Moon v-else />
            </el-icon>
          </el-button>
          <el-dropdown @command="onUserCommand">
            <span class="user-chip">
              <el-icon><UserFilled /></el-icon>
              <span>{{ session.admin?.username || '' }}</span>
              <el-tag size="small" :type="session.admin?.role === 'super' ? 'warning' : 'primary'" effect="plain">
                {{ session.admin?.role === 'super' ? '超管' : '管理' }}
              </el-tag>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="main"><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { session, api, clearToken } from '../api'
import { theme, toggleTheme } from '../theme'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)

const titleMap = {
  dashboard: '仪表盘', marketplace: '插件市场', plugins: '插件管理', commands: '命令管理',
  users: '用户管理', groups: '群组管理', tasks: '定时任务', runtime: '运行状态',
  connection: '连接设置', filebrowser: '文件浏览', logs: '日志中心', database: '数据库',
  plugin_webui: '插件页面', settings: '设置',
}
const activeKey = computed(() => '/' + (route.path.replace(/^\//, '').split('/')[0] || 'dashboard'))
const pageTitle = computed(() => titleMap[route.name] || 'ZCBOT')

let statusTimer = null

async function loadMe() {
  try {
    const me = await api('/api/me')
    if (me && me.code === 0) session.admin = me.data
  } catch (e) { /* 401 已处理 */ }
}

async function loadWebUIs() {
  const w = await api('/api/plugin_webuis').catch(() => null)
  if (w && w.code === 0) session.pluginWebUIs = w.data || []
}

async function loadVersion() {
  const r = await fetch('/api/version').then(r => r.json()).catch(() => null)
  if (r && r.code === 0) session.frameworkVersion = r.data.version || ''
}

function onUserCommand(cmd) {
  if (cmd === 'logout') {
    api('/api/logout', { method: 'POST' }).catch(() => {})
    clearToken()
    router.push('/login')
  }
}

onMounted(async () => {
  await Promise.all([loadMe(), loadWebUIs(), loadVersion()])
})
onBeforeUnmount(() => { if (statusTimer) clearInterval(statusTimer) })
</script>

<style scoped>
.layout { height: 100vh; }
.sidebar {
  background: var(--el-bg-color);
  border-right: 1px solid var(--el-border-color);
  display: flex;
  flex-direction: column;
  transition: width .2s;
  overflow: hidden;
}
.brand {
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  border-bottom: 1px solid var(--el-border-color);
  font-weight: 700;
}
.brand-logo { height: 30px; width: 30px; object-fit: contain; }
.nav-menu { border-right: none; flex: 1; overflow-y: auto; }
.sidebar-foot { border-top: 1px solid var(--el-border-color); }
.sb-version { text-align: center; color: var(--el-text-color-secondary); font-size: 12px; padding: 6px 0; }
.right { min-width: 0; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--el-border-color);
  background: var(--el-bg-color);
}
.topbar-left { display: flex; align-items: center; gap: 8px; }
.page-title { font-size: 16px; font-weight: 600; }
.brand-name { font-size: 18px; font-weight: 800; letter-spacing: .02em;
  background: var(--brand-gradient); -webkit-background-clip: text; background-clip: text; color: transparent; }
.theme-btn { transition: transform .3s ease, color .3s ease; }
.theme-btn:hover { transform: rotate(20deg) scale(1.1); color: var(--el-color-primary); }
.user-chip { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; outline: none; }
.main { padding: 0; background: var(--el-bg-color-page, #f5f7fa); overflow: auto; }
</style>