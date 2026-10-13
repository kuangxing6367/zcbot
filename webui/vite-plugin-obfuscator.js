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
      let rawBytes = 0
      let outBytes = 0
      const used = {}

      for (const [fileName, chunk] of Object.entries(bundle)) {
        if (chunk.type !== 'chunk') continue
        if (!fileName.endsWith('.js')) continue
        if (!shouldObfuscate(fileName)) continue

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
          const code = result.getObfuscatedCode()
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
    },
  }
}
