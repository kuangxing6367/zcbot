import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/login', name: 'login', component: () => import('./views/Login.vue'), meta: { public: true } },
  {
    path: '/',
    component: () => import('./views/Layout.vue'),
    children: [
      { path: '', redirect: '/dashboard' },
      { path: 'dashboard', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
      { path: 'marketplace', name: 'marketplace', component: () => import('./views/Marketplace.vue') },
      { path: 'plugins', name: 'plugins', component: () => import('./views/Plugins.vue') },
      { path: 'commands', name: 'commands', component: () => import('./views/Commands.vue') },
      { path: 'users', name: 'users', component: () => import('./views/Users.vue') },
      { path: 'groups', name: 'groups', component: () => import('./views/Groups.vue') },
      { path: 'tasks', name: 'tasks', component: () => import('./views/Tasks.vue') },
      { path: 'runtime', name: 'runtime', component: () => import('./views/Runtime.vue') },
      { path: 'connection', name: 'connection', component: () => import('./views/Connection.vue') },
      { path: 'filebrowser', name: 'filebrowser', component: () => import('./views/FileBrowser.vue') },
      { path: 'logs', name: 'logs', component: () => import('./views/Logs.vue') },
      { path: 'database', name: 'database', component: () => import('./views/Database.vue') },
      { path: 'plugin_webui', name: 'plugin_webui', component: () => import('./views/PluginWebUI.vue') },
      { path: 'settings', name: 'settings', component: () => import('./views/Settings.vue') },
    ],
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const token = localStorage.getItem('zcbot_token')
  if (to.meta.public) return true
  if (!token) return { name: 'login', query: { redirect: to.fullPath } }
  return true
})

export default router
