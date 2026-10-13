/*
 * 会话令牌（Session Tokens）
 * ==========================
 * 协议：POST /api/session/handshake 取主令牌 → 拆 6 份、随机打乱 →
 *       POST /api/session/confirm 回传（返回 444 = 静默成功）→
 *       之后每个请求在 X-ZC-Set 请求头里带上全部 6 个令牌。
 *
 * **服务端未开启时 handshake 会返回 404** → 这里自动置为 off，后续不再尝试，
 * 行为与开启前完全一致（默认不变）。
 *
 * 令牌用 Cookie 持久化（30 分钟），sid 由服务端通过 HttpOnly Cookie 下发，JS 不接触。
 * 全部令牌过期后业务请求会拿到 444，现有逻辑（444 = 会话失效）会自动回到登录流程。
 */
import { API_BASE } from './config'

const TOKENS = 6
const TSET_KEY = 'zcbot_tset'
const TTL = 1800

let tokens = []
let state = 'unknown'   // unknown | on | off
let initPromise = null

function readCookie(name) {
  try {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'))
    return m ? decodeURIComponent(m[1]) : ''
  } catch (e) {
    return ''
  }
}

function writeCookie(name, value, maxAge) {
  try {
    document.cookie = `${name}=${encodeURIComponent(value)}; max-age=${maxAge}; path=/; SameSite=Lax`
  } catch (e) { /* 忽略 */ }
}

function dropCookie(name) {
  try {
    document.cookie = `${name}=; Max-Age=0; path=/`
  } catch (e) { /* 忽略 */ }
}

function splitEven(s, n) {
  const size = Math.ceil(s.length / n)
  const out = []
  for (let i = 0; i < s.length; i += size) out.push(s.slice(i, i + size))
  return out
}

function shuffle(arr) {
  const r = arr.slice()
  for (let i = r.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1))
    const t = r[i]; r[i] = r[j]; r[j] = t
  }
  return r
}

/** 确保拿到令牌集；服务端未开启时返回 false（不影响正常请求） */
export function ensureSession() {
  if (state === 'off') return Promise.resolve(false)
  if (tokens.length === TOKENS) return Promise.resolve(true)

  // 刷新/重开页面：先尝试从 Cookie 恢复
  if (!tokens.length) {
    const saved = readCookie(TSET_KEY)
    if (saved) {
      const arr = saved.split(',').filter(Boolean)
      if (arr.length === TOKENS) {
        tokens = arr
        state = 'on'
        return Promise.resolve(true)
      }
    }
  }

  if (initPromise) return initPromise
  initPromise = (async () => {
    try {
      const r = await fetch(API_BASE + '/api/session/handshake', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
      if (r.status === 404 || r.status === 0) { state = 'off'; return false }
      const j = await r.json().catch(() => null)
      const master = j && j.data && j.data.master
      if (!master) { state = 'off'; return false }

      const parts = shuffle(splitEven(master, TOKENS))
      const c = await fetch(API_BASE + '/api/session/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tokens: parts }),
      })
      // 444 = 服务端静默接受；200 视为兼容
      if (c.status !== 444 && c.status !== 200) { state = 'off'; return false }

      tokens = parts
      state = 'on'
      writeCookie(TSET_KEY, parts.join(','), TTL)
      return true
    } catch (e) {
      state = 'off'
      return false
    } finally {
      initPromise = null
    }
  })()
  return initPromise
}

/** 每个请求附带的 X-ZC-Set 值（6 个令牌逗号分隔） */
export function sessionSetHeader() {
  return tokens.length === TOKENS ? tokens.join(',') : ''
}

/** 444（全部令牌过期）时清空，让客户端回到登录流程重新握手 */
export function clearSession() {
  tokens = []
  state = 'unknown'
  dropCookie(TSET_KEY)
}

export function sessionEnabled() {
  return state === 'on'
}
