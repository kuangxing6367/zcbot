/**
 * ZCBOT 生产构建 JS 混淆插件（分层 / 多形态）
 * ==========================================
 * 在 Vite 完成打包与压缩后（generateBundle 阶段），对业务 chunk 施加混淆。
 *
 * 为什么必须「多形态」：
 *   全站共用一套混淆参数时，每个 chunk 的头部会长得一模一样
 *   （同样的 _0x 十六进制字符串数组 + 同样的自执行轮转函数）。
 *   这是一个极强的指纹 —— 一眼就看出「整站被同一个工具统一刷过」，
 *   攻击者只要把这套壳脱掉，剩下的分析成本骤降。
 *   因此这里按 chunk 分配不同「形态（profile）」，并给每个 chunk 不同的 seed，
 *   让产物看起来像「多年迭代、不同模块不同时期分别处理过」的真实项目。
 *
 * profiles 条目：{ name, test: RegExp, options?: {...}, skip?: true }
 *   - skip: true  → 该 chunk 保持明文（诱饵层：故意留一段可读代码给人捡）
 *   - options     → 用该形态参数覆盖默认 obfuscatorOptions
 *   - 未匹配      → 使用默认 obfuscatorOptions
 *
 * 每个 chunk 都会生成一个由文件名派生的独立 seed：即便同一形态，产物也不雷同。
 *
 * 其余取舍：
 * - 默认跳过第三方 vendor chunk（element-plus / vue-vendor）：开源库，混淆收益低。
 * - 混淆失败不中断构建，仅告警（保证 CI 稳定）。
 * - selfDefending 默认关闭：与部分 ESM 形态兼容性差，易造成线上白屏。
 */
import JavaScriptObfuscator from 'javascript-obfuscator'

// ---------------------------------------------------------------------------
// API 路径字面量编码
// ---------------------------------------------------------------------------
// 压缩后产物里散落着 '/api/xxx' 这类明文，抓包前扫一眼就能把真实接口全列出来。
// 这里在混淆之前把它们逐个 XOR 成十六进制分片，并注入一个解码器，运行时才拼回
// 真实路径 —— 产物里 grep 不到任何 /api/ 字面量。
const _DEC = '_$zr9'

function _decoderDef(name) {
  return `function ${name}(k){var h='',i=1;for(;i<arguments.length;i++)h+=arguments[i];`
    + `var s='',j=0;for(;j<h.length;j+=2)s+=String.fromCharCode(parseInt(h.substr(j,2),16)^k);`
    + `return s}\n`
}

function _encCall(str, name) {
  const key = 1 + Math.floor(Math.random() * 254)
  let hex = ''
  for (let i = 0; i < str.length; i++) {
    hex += ((str.charCodeAt(i) ^ key) & 0xff).toString(16).padStart(2, '0')
  }
  // 拆成 2~3 片，运行时由解码器拼回（"多个片段拼起来"）
  const n = Math.min(3, Math.max(1, Math.ceil(hex.length / 12)))
  const size = Math.ceil(hex.length / n)
  const parts = []
  for (let i = 0; i < hex.length; i += size) parts.push(`'${hex.slice(i, i + size)}'`)
  return `${name}(${key},${parts.join(',')})`
}

function _walk(node, cb) {
  if (!node || typeof node !== 'object') return
  if (Array.isArray(node)) { for (const c of node) _walk(c, cb); return }
  if (typeof node.type === 'string') cb(node)
  for (const k in node) {
    if (k === 'type' || k === 'start' || k === 'end' || k === 'loc' || k === 'range' || k === 'raw') continue
    const v = node[k]
    if (v && typeof v === 'object') _walk(v, cb)
  }
}

function _encodeApiLiterals(code, ctx, prefix, decName) {
  let ast
  try {
    ast = ctx.parse(code)
  } catch (e) {
    ctx.warn(`[API编码] 解析失败，已跳过本 chunk: ${e.message}`)
    return { code, count: 0 }
  }
  // 对象键 / import 源不参与编码，避免语义被改坏
  const skip = new Set()
  _walk(ast, (n) => {
    if (n.type === 'Property' && !n.computed && n.key) skip.add(n.key)
    if ((n.type === 'ImportDeclaration' || n.type === 'ExportNamedDeclaration'
      || n.type === 'ExportAllDeclaration') && n.source) skip.add(n.source)
  })
  const hits = []
  _walk(ast, (n) => {
    if (n.type === 'Literal' && typeof n.value === 'string'
      && n.value.indexOf(prefix) === 0 && !skip.has(n)) hits.push(n)
  })
  if (!hits.length) return { code, count: 0 }

  hits.sort((a, b) => b.start - a.start)
  let out = code
  for (const n of hits) out = out.slice(0, n.start) + _encCall(n.value, decName) + out.slice(n.end)
  // 同名解码器已存在时不再重复注入（两轮编码用不同名字，天然不会撞）
  const needDef = out.indexOf(`function ${decName}(`) === -1
  return { code: (needDef ? _decoderDef(decName) : '') + out, count: hits.length }
}


function _seedFor(name) {
  let h = 2166136261
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  h = h >>> 0
  return (h % 2147483647) || 1
}


export default function obfuscatorPlugin(options = {}) {
  const {
    include = [],
    exclude = [],
    obfuscatorOptions = {},
    profiles = [],
    encodeApiPaths = false,
    apiLiteralPrefix = '/api/',
  } = options

  const shouldObfuscate = (fileName) => {
    if (exclude.some((re) => re.test(fileName))) return false
    if (include.length && !include.some((re) => re.test(fileName))) return false
    return true
  }

  const profileFor = (fileName) => {
    for (const p of profiles) {
      if (p.test && p.test.test(fileName)) return p
    }
    return null
  }

  return {
    name: 'zcbot-js-obfuscator',
    apply: 'build',
    enforce: 'post',
    generateBundle(_outputOptions, bundle) {
      let obfuscated = 0
      let plain = 0
      let encodedApi = 0
      let rawBytes = 0
      let outBytes = 0
      const used = {}

      for (const [fileName, chunk] of Object.entries(bundle)) {
        if (chunk.type !== 'chunk') continue
        if (!fileName.endsWith('.js')) continue
        if (!shouldObfuscate(fileName)) continue

        // API 路径字面量编码（先于混淆执行，产物里不再有 /api/ 明文）
        if (encodeApiPaths) {
          const r = _encodeApiLiterals(chunk.code, this, apiLiteralPrefix, _DEC)
          if (r.count > 0) {
            chunk.code = r.code
            encodedApi += r.count
          } else if (chunk.code.indexOf(apiLiteralPrefix) >= 0) {
            this.warn(`[API编码] ${fileName} 含 ${apiLiteralPrefix} 字面量但未能编码（保留明文）`)
          }
        }

        const prof = profileFor(fileName)
        const pname = prof ? (prof.name || '(匿名形态)') : 'default'

        // 诱饵层：显式保持明文，让攻击者「捡到可读代码」
        if (prof && prof.skip) {
          plain += 1
          used[pname] = (used[pname] || 0) + 1
          continue
        }

        const base = (prof && prof.options) || obfuscatorOptions
        // 每个 chunk 独立 seed：即便同一形态，产物也不雷同
        const seed = (prof && typeof prof.seed === 'number') ? prof.seed : _seedFor(fileName)
        try {
          const before = chunk.code.length
          const result = JavaScriptObfuscator.obfuscate(chunk.code, { ...base, seed })
          let code = result.getObfuscatedCode()
          // 混淆后复查：stringArray / 死代码注入等过程会把字符串重新排布，
          // 可能让 /api/ 字面量再次出现在产物里 —— 这里再扫一遍兜底。
          if (encodeApiPaths) {
            const r2 = _encodeApiLiterals(code, this, apiLiteralPrefix, _DEC + 'x')
            if (r2.count > 0) {
              code = r2.code
              encodedApi += r2.count
            }
          }
          chunk.code = code
          obfuscated += 1
          rawBytes += before
          outBytes += code.length
          used[pname] = (used[pname] || 0) + 1
        } catch (e) {
          this.warn(`[混淆] ${fileName} 失败，已保留原代码: ${e.message}`)
        }
      }

      if (obfuscated > 0) {
        const ratio = rawBytes ? ((outBytes / rawBytes) * 100).toFixed(0) : '0'
        this.info(`[混淆] 已混淆 ${obfuscated} 个 chunk（体积 ${ratio}% 于压缩后）；`
          + `形态分布 ${JSON.stringify(used)}`)
      }
      if (plain > 0) {
        this.info(`[混淆] ${plain} 个 chunk 按形态保持明文（诱饵层）`)
      }
      if (encodedApi > 0) {
        this.info(`[API编码] 已编码 ${encodedApi} 处 ${apiLiteralPrefix} 路径字面量（运行时解码拼装）`)
      }
    },
  }
}
