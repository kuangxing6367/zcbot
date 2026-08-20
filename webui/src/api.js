import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'

export const TOKEN_KEY = 'zcbot_token'

export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t) }
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
  document.cookie = 'zcbot_token=; Max-Age=0; path=/'
}

export async function api(url, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } }
  const token = getToken()
  if (token) opts.headers['Authorization'] = 'Bearer ' + token
  if (opts.body && typeof opts.body === 'object' && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(opts.body)
  }
  let res
  try {
    res = await fetch(url, opts)
  } catch (e) {
    throw new Error('网络错误: ' + e.message)
  }
  let data = null
  try { data = await res.json() } catch (e) { /* 非 JSON */ }
  if (res.status === 401) {
    clearToken()
    if (!location.hash.includes('/login')) {
      location.hash = '#/login'
    }
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
