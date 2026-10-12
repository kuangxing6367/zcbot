<template>
  <el-container class="layout">
    <el-aside :width="collapsed ? '64px' : '220px'" class="sidebar">
      <div class="brand">
        <img v-show="!collapsed" :src="logoUrl" alt="ZCBOT" class="brand-logo" />
        <img v-show="collapsed" :src="logoMiniUrl" alt="ZCBOT" class="brand-logo-mini" />
        <span v-show="!collapsed" class="brand-name">ZCBOT</span>
      </div>
      <el-scrollbar class="nav-scroll">
        <el-menu :default-active="activeKey" router :collapse="collapsed" class="nav-menu">
          <template v-if="session.officialSidebar">
            <el-menu-item v-for="it in officialItems" :key="it.key" :index="it.path">
              <el-icon><component :is="it.icon" /></el-icon><span>{{ it.title }}</span>
            </el-menu-item>
          </template>
          <el-menu-item v-for="p in pluginSidebarItems" :key="p.plugin_name" :index="'/plugin/' + p.plugin_name">
            <el-icon v-if="!p.icon"><Menu /></el-icon>
            <span v-else class="plugin-ico">{{ p.icon }}</span>
            <span>{{ p.title }}</span>
          </el-menu-item>
          <el-menu-item v-if="aggregatePlugins.length" index="/plugin_webui">
            <el-icon><Monitor /></el-icon><span>插件页面</span>
          </el-menu-item>
        </el-menu>
      </el-scrollbar>
      <div class="sidebar-foot">
        <el-menu class="nav-menu" :default-active="activeKey" router :collapse="collapsed">
          <el-menu-item index="/settings"><el-icon><Setting /></el-icon><span>设置</span></el-menu-item>
        </el-menu>
        <div v-show="!collapsed" class="sb-version">v{{ session.frameworkVersion || '-' }}</div>
      </div>
    </el-aside>

    <el-container class="right">
      <el-header class="topbar" height="60px">
        <div class="topbar-left">
          <button class="icon-btn" @click="collapsed = !collapsed" title="折叠菜单">
            <el-icon :size="18"><Expand v-if="collapsed" /><Fold v-else /></el-icon>
          </button>
          <div class="crumb">
            <span class="crumb-root">ZCBOT</span>
            <span class="crumb-sep">/</span>
            <span class="page-title">{{ pageTitle }}</span>
          </div>
        </div>
        <div class="topbar-right">
          <button class="icon-btn" @click="toggleTheme" title="切换亮暗主题" :key="theme">
            <el-icon :size="18">
              <Sunny v-if="theme === 'light'" />
              <Moon v-else />
            </el-icon>
          </button>
          <el-dropdown @command="onUserCommand" trigger="click">
            <span class="user-chip">
              <span class="avatar">{{ avatarText }}</span>
              <span class="user-meta">
                <span class="user-name">{{ session.admin?.username || '' }}</span>
                <span class="user-role">{{ session.admin?.role === 'super' ? '超级管理员' : '管理员' }}</span>
              </span>
              <el-icon class="user-caret"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="settings">
                  <el-icon><Setting /></el-icon>设置
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
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
import { session, api, clearToken, secureHeaders } from '../api'
import { API_BASE } from '../config'
import { theme, toggleTheme } from '../theme'
import { OFFICIAL_SIDEBAR_ITEMS } from '../sidebar'

const route = useRoute()
const router = useRouter()
const collapsed = ref(false)
// 标志按主题切换明暗两版：亮底用 #5D87FF，暗底提亮
const logoUrl = computed(() => import.meta.env.BASE_URL + (theme.value === 'dark' ? 'img/zcbot-icon-dark.svg' : 'img/zcbot-icon-light.svg'))
// 折叠态 24px 用简化版：标准版断口在 24px 下只有 2px，会糊
const logoMiniUrl = computed(() => import.meta.env.BASE_URL + (theme.value === 'dark' ? 'img/zcbot-icon-mini-dark.svg' : 'img/zcbot-icon-mini-light.svg'))

const titleMap = {
  dashboard: '仪表盘', marketplace: '插件市场', plugins: '插件管理', commands: '命令管理',
  users: '用户管理', groups: '群组管理', permissions: '权限管理', apikeys: '接口令牌', tasks: '定时任务', runtime: '运行状态',
  connection: '连接设置', filebrowser: '文件浏览', logs: '日志中心', database: '数据库',
  plugin_webui: '插件页面', settings: '设置',
}
const pluginSidebarItems = computed(() => (session.pluginWebUIs || []).filter(p => p.sidebar))
const aggregatePlugins = computed(() => (session.pluginWebUIs || []).filter(p => !p.sidebar))
const avatarText = computed(() => {
  const n = session.admin?.username || 'A'
  return n.slice(0, 1).toUpperCase()
})
// 官方菜单按自定义配置渲染：hidden 隐藏、order 指定顺序（未列出的按默认次序追加）
const officialItems = computed(() => {
  const cfg = session.sidebar || {}
  const hidden = new Set(cfg.hidden || [])
  const order = cfg.order || []
  const items = OFFICIAL_SIDEBAR_ITEMS.filter(it => !hidden.has(it.key))
  if (!order.length) return items
  const pos = {}
  order.forEach((k, i) => { pos[k] = i })
  return items.slice().sort((a, b) => (pos[a.key] ?? 999) - (pos[b.key] ?? 999))
})

const activeKey = computed(() => {
  const seg = route.path.replace(/^\//, '').split('/')
  if (seg[0] === 'plugin' && seg[1]) return '/plugin/' + seg[1]
  return '/' + (seg[0] || 'dashboard')
})
const pageTitle = computed(() => {
  const seg = route.path.replace(/^\//, '').split('/')
  if (seg[0] === 'plugin' && seg[1]) {
    const p = (session.pluginWebUIs || []).find(x => x.plugin_name === seg[1])
    return p ? p.title : '插件'
  }
  return titleMap[route.name] || 'ZCBOT'
})

let statusTimer = null

async function loadMe() {
  try {
    const me = await api('/api/me')
    if (me && me.code === 0) session.admin = me.data
  } catch (e) { /* 401 已处理 */ }
}

async function loadWebUIs() {
  const w = await api('/api/menu').catch(() => null)
  if (w && w.code === 0) {
    session.pluginWebUIs = w.data.plugins || []
    session.officialSidebar = w.data.official_sidebar !== false
    session.sidebar = w.data.sidebar || { order: [], hidden: [] }
  }
}

async function loadVersion() {
  const r = await fetch(API_BASE + '/api/version', { headers: await secureHeaders() }).then(r => r.json()).catch(() => null)
  if (r && r.code === 0) session.frameworkVersion = r.data.version || ''
}

function onUserCommand(cmd) {
  if (cmd === 'settings') {
    router.push('/settings')
  } else if (cmd === 'logout') {
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

/* ------------------------------ 侧边栏 ------------------------------ */
.sidebar {
  background: var(--default-box-color);
  border-right: 1px solid var(--art-card-border);
  display: flex;
  flex-direction: column;
  transition: width .22s ease;
  overflow: hidden;
}
.brand {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 9px;
  border-bottom: 1px solid var(--art-card-border);
  flex-shrink: 0;
}
.brand-logo { height: 28px; width: 28px; object-fit: contain; }
.brand-logo-mini { height: 24px; width: 24px; object-fit: contain; }
.brand-name {
  font-size: 18px;
  font-weight: 800;
  letter-spacing: .02em;
  background: var(--brand-gradient);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.nav-scroll { flex: 1; overflow: hidden; }
.nav-menu { border-right: none; padding: 6px 0; }
.plugin-ico { display: inline-flex; width: 1em; justify-content: center; }
.sidebar-foot {
  border-top: 1px solid var(--art-card-border);
  flex-shrink: 0;
  padding-bottom: 4px;
}
.sb-version {
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  padding: 2px 0 8px;
}

/* ------------------------------ 顶栏 ------------------------------ */
.right { min-width: 0; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px;
  border-bottom: 1px solid var(--art-card-border);
  background: var(--default-box-color);
}
.topbar-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.crumb { display: flex; align-items: center; gap: 8px; font-size: 15px; }
.crumb-root { color: var(--el-text-color-secondary); }
.crumb-sep { color: var(--el-text-color-secondary); opacity: .6; }
.page-title { font-size: 15px; font-weight: 600; }

.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border: none;
  border-radius: calc(var(--custom-radius) / 2);
  background: transparent;
  color: var(--el-text-color-regular);
  cursor: pointer;
  transition: background-color .2s, color .2s, transform .3s;
}
.icon-btn:hover {
  background: var(--art-hover-color);
  color: var(--main-color);
}
.topbar-right { display: flex; align-items: center; gap: 8px; }
.topbar-right .icon-btn:hover { transform: rotate(18deg); }

.user-chip {
  display: inline-flex;
  align-items: center;
  gap: 9px;
  padding: 4px 8px 4px 4px;
  border-radius: 999px;
  cursor: pointer;
  outline: none;
  transition: background-color .2s;
}
.user-chip:hover { background: var(--art-hover-color); }
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: var(--brand-gradient);
  color: #fff;
  font-size: 14px;
  font-weight: 600;
}
.user-meta { display: flex; flex-direction: column; line-height: 1.15; }
.user-name { font-size: 13px; font-weight: 500; color: var(--el-text-color-primary); }
.user-role { font-size: 11px; color: var(--el-text-color-secondary); }
.user-caret { color: var(--el-text-color-secondary); font-size: 12px; }

.main {
  padding: 0;
  background: var(--default-bg-color);
  overflow: auto;
}
</style>
