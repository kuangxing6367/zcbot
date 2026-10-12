/*
 * JS 人机验证（工作量证明 / Proof-of-Work）
 * =========================================
 * 流程：GET /api/challenge 取题 → 本地暴力搜索 nonce 使
 *       sha256("<challenge>:<nonce>") 前 difficulty 位为 0
 *       → POST /api/challenge/verify 换通行票据 → 之后所有请求带 X-ZC-Pass。
 *
 * 为什么自带 SHA-256：crypto.subtle 只在安全上下文（HTTPS / localhost）可用，
 * 而管理后台常跑在 http://内网IP:8080，此时 crypto.subtle 为 undefined。
 * 因此这里内置一份纯 JS SHA-256，任何环境都能跑。
 *
 * 本文件随业务代码一起被构建混淆，协议无法被直接照抄。
 */

import { API_BASE } from './config'

const PASS_KEY = 'zcbot_pass'
const MAX_ITER = 30000000

let ticket = ''
let solving = null

try { ticket = sessionStorage.getItem(PASS_KEY) || '' } catch (e) { /* 忽略 */ }

const K = [
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]

function sha256Hex(msg) {
  const rr = (x, n) => (x >>> n) | (x << (32 - n))
  const H = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]

  const bytes = []
  for (let i = 0; i < msg.length; i++) {
    let c = msg.charCodeAt(i)
    if (c < 0x80) {
      bytes.push(c)
    } else if (c < 0x800) {
      bytes.push(0xc0 | (c >> 6), 0x80 | (c & 63))
    } else if (c < 0xd800 || c >= 0xe000) {
      bytes.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
    } else {
      i++
      c = 0x10000 + (((c & 0x3ff) << 10) | (msg.charCodeAt(i) & 0x3ff))
      bytes.push(0xf0 | (c >> 18), 0x80 | ((c >> 12) & 63), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63))
    }
  }

  const bitLen = bytes.length * 8
  bytes.push(0x80)
  while (bytes.length % 64 !== 56) bytes.push(0)
  bytes.push(0, 0, 0, 0,
    (bitLen >>> 24) & 255, (bitLen >>> 16) & 255, (bitLen >>> 8) & 255, bitLen & 255)

  const w = new Array(64)
  for (let off = 0; off < bytes.length; off += 64) {
    for (let i = 0; i < 16; i++) {
      w[i] = (bytes[off + i * 4] << 24) | (bytes[off + i * 4 + 1] << 16) |
        (bytes[off + i * 4 + 2] << 8) | bytes[off + i * 4 + 3]
    }
    for (let i = 16; i < 64; i++) {
      const x = w[i - 15]
      const y = w[i - 2]
      const s0 = rr(x, 7) ^ rr(x, 18) ^ (x >>> 3)
      const s1 = rr(y, 17) ^ rr(y, 19) ^ (y >>> 10)
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0
    }
    let a = H[0], b = H[1], c = H[2], d = H[3]
    let e = H[4], f = H[5], g = H[6], h = H[7]
    for (let i = 0; i < 64; i++) {
      const S1 = rr(e, 6) ^ rr(e, 11) ^ rr(e, 25)
      const ch = (e & f) ^ (~e & g)
      const t1 = (h + S1 + ch + K[i] + w[i]) | 0
      const S0 = rr(a, 2) ^ rr(a, 13) ^ rr(a, 22)
      const maj = (a & b) ^ (a & c) ^ (b & c)
      const t2 = (S0 + maj) | 0
      h = g; g = f; f = e; e = (d + t1) | 0
      d = c; c = b; b = a; a = (t1 + t2) | 0
    }
    H[0] = (H[0] + a) | 0; H[1] = (H[1] + b) | 0
    H[2] = (H[2] + c) | 0; H[3] = (H[3] + d) | 0
    H[4] = (H[4] + e) | 0; H[5] = (H[5] + f) | 0
    H[6] = (H[6] + g) | 0; H[7] = (H[7] + h) | 0
  }

  let out = ''
  for (let i = 0; i < 8; i++) out += (H[i] >>> 0).toString(16).padStart(8, '0')
  return out
}

/** 暴力搜索满足前 difficulty 位为 0 的 nonce */
export function solve(challenge, difficulty) {
  const prefix = '0'.repeat(Math.max(1, Math.min(8, difficulty | 0)))
  for (let n = 0; n < MAX_ITER; n++) {
    if (sha256Hex(challenge + ':' + n).startsWith(prefix)) return String(n)
  }
  return null
}

export function getPass() {
  return ticket
}

export function clearPass() {
  ticket = ''
  try { sessionStorage.removeItem(PASS_KEY) } catch (e) { /* 忽略 */ }
}

async function fetchChallenge() {
  const r = await fetch(API_BASE + '/api/challenge', { headers: { Accept: 'application/json' } })
  const j = await r.json()
  if (!j || j.code !== 0 || !j.data) throw new Error('challenge 获取失败')
  return j.data
}

async function submitProof(challenge, nonce) {
  const r = await fetch(API_BASE + '/api/challenge/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ challenge, nonce }),
  })
  const j = await r.json().catch(() => null)
  if (!j || j.code !== 0 || !j.data || !j.data.ticket) throw new Error('verify 失败')
  return j.data.ticket
}

/**
 * 确保拿到通行票据（并发调用只求解一次）。
 * 失败返回空串 —— 上层照常发请求，由服务端决定是否丢弃。
 */
export function ensurePass() {
  if (ticket) return Promise.resolve(ticket)
  if (solving) return solving
  solving = (async () => {
    try {
      const d = await fetchChallenge()
      const nonce = solve(d.challenge, d.difficulty)
      if (nonce === null) throw new Error('未能在限定次数内求解')
      ticket = await submitProof(d.challenge, nonce)
      try { sessionStorage.setItem(PASS_KEY, ticket) } catch (e) { /* 忽略 */ }
      return ticket
    } catch (e) {
      ticket = ''
      return ''
    } finally {
      solving = null
    }
  })()
  return solving
}

export { sha256Hex }
