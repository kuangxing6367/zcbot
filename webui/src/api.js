import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getPass, ensurePass, clearPass } from './human-check'
import { API_BASE } from './config'

export const TOKEN_KEY = 'zcbot_token'

export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
  document.cookie = 'zcbot_token=; Max-Age=0; path=/'
}

function gotoLogin() {
  if (!location.hash.includes('/login')) location.hash = '#/login'
}

/** 静默丢弃（444 / 无响应）：服务端「不对就不理」，前端据此判定会话失效 */
function isSilentDrop(res) {
  return res.status === 444 || res.status === 0
}

/** 为裸 fetch 附加 token 与通行票据（用于不便走 api() 的场景，如登录、版本查询） */
export async function secureHeaders(extra = {}) {
  const h = { ...extra }
  const token = getToken()
  if (token) h['Authorization'] = 'Bearer ' + token
  const pass = getPass() || await ensurePass()
  if (pass) h['X-ZC-Pass'] = pass
  return h
}

export async function api(url, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } }
  const token = getToken()
  if (token) opts.headers['Authorization'] = 'Bearer ' + token
  // 人机验证通行票据（首次调用会自动求解并缓存）
  const pass = getPass() || await ensurePass()
  if (pass) opts.headers['X-ZC-Pass'] = pass
  if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(opts.body)
  }
  let res
  try {
    res = await fetch(url.startsWith('http') ? url : API_BASE + url, opts)
  } catch (e) {
    throw new Error('网络错误: ' + e.message)
  }

  // 票据过期 / 服务端重启 → 重新求解一次再试
  if (res.status === 444 && !options.__retried) {
    clearPass()
    const fresh = await ensurePass()
    if (fresh) return api(url, { ...options, __retried: true })
  }

  let data = null
  try { data = await res.json() } catch (e) { /* 非 JSON（含静默丢弃的空响应） */ }

  if (isSilentDrop(res)) {
    clearToken()
    clearPass()
    gotoLogin()
    throw new Error('会话已失效，请重新登录')
  }
  if (res.status === 401) {
    clearToken()
    gotoLogin()
    throw new Error(data?.msg || '登录已过期')
  }
  return data
}

/** 统一错误提示辅助：API 返回 {code, msg}，非 0 时提示并返回 null */
export async function apiCall(url, options = {}) {
  try {
    const r = await api(url, options)
    if (r && r.code !== 0) {
      ElMessage.error(r.msg || '操作失败')
      return null
    }
    return r
  } catch (e) {
    ElMessage.error(e.message)
    return null
  }
}

/** 全局共享状态：当前管理员信息 */
export const session = reactive({
  admin: null,
  pluginWebUIs: [],
  officialSidebar: true,
  sidebar: { order: [], hidden: [] },
  frameworkVersion: '',
})

export function fmtTime(dt) {
  if (!dt) return '-'
  const d = new Date(dt)
  if (isNaN(d.getTime())) return String(dt)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function timeAgo(dt) {
  if (!dt) return '-'
  const d = new Date(dt)
  if (isNaN(d.getTime())) return String(dt)
  const diff = Date.now() - d.getTime()
  const s = Math.floor(Math.abs(diff) / 1000)
  if (s < 60) return `${s}秒前`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}小时前`
  return fmtTime(dt)
}

export function fmtUptime(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return '-'
  sec = Math.max(0, sec)
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60), s = sec % 60
  let out = ''
  if (d) out += d + '天'
  if (h) out += h + '时'
  if (m) out += m + '分'
  out += s + '秒'
  return out
}

export function fmtFileSize(n) {
  if (n === null || n === undefined || isNaN(n)) return '-'
  if (n < 1024) return n + ' B'
  const units = ['KB', 'MB', 'GB', 'TB']
  let v = n, i = -1
  do { v = v / 1024; i++ } while (v >= 1024 && i < units.length - 1)
  return v.toFixed(1) + ' ' + units[i]
}

export function fmtMtime(t) {
  if (!t) return '-'
  try { return new Date(t * 1000).toLocaleString('zh-CN', { hour12: false }) } catch (e) { return '-' }
}
