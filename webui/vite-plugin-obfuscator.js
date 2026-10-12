/**
 * ZCBOT 生产构建 JS 混淆插件
 * ==========================
 * 在 Vite 完成打包与压缩后（generateBundle 阶段），对业务 chunk 施加
 * javascript-obfuscator 混淆：字符串数组加密、控制流扁平化、死代码注入、
 * 标识符十六进制重命名、数字表达式化、对象键转换、console 抹除等。
 *
 * 设计取舍：
 * - 默认跳过第三方 vendor chunk（element-plus / vue-vendor）。这些库本身开源，
 *   混淆它们只会让产物体积与构建耗时成倍增长，收益极低。
 * - 混淆失败不中断构建，仅告警（保证 CI 稳定）。
 * - 关闭 selfDefending：与后续再压缩/某些 ESM 形态兼容性差，容易造成线上白屏。
 */
import JavaScriptObfuscator from 'javascript-obfuscator'

export default function obfuscatorPlugin(options = {}) {
  const {
    include = [],
    exclude = [],
    obfuscatorOptions = {},
  } = options

  const shouldObfuscate = (fileName) => {
    if (exclude.some((re) => re.test(fileName))) return false
    if (include.length && !include.some((re) => re.test(fileName))) return false
    return true
  }

  return {
    name: 'zcbot-js-obfuscator',
    apply: 'build',
    enforce: 'post',
    generateBundle(_outputOptions, bundle) {
      let obfuscated = 0
      let rawBytes = 0
      let outBytes = 0
      for (const [fileName, chunk] of Object.entries(bundle)) {
        if (chunk.type !== 'chunk') continue
        if (!fileName.endsWith('.js')) continue
        if (!shouldObfuscate(fileName)) continue
        try {
          const before = chunk.code.length
          const result = JavaScriptObfuscator.obfuscate(chunk.code, obfuscatorOptions)
          const code = result.getObfuscatedCode()
          chunk.code = code
          obfuscated += 1
          rawBytes += before
          outBytes += code.length
        } catch (e) {
          this.warn(`[混淆] ${fileName} 失败，已保留原代码: ${e.message}`)
        }
      }
      if (obfuscated > 0) {
        const ratio = rawBytes ? ((outBytes / rawBytes) * 100).toFixed(0) : '0'
        this.info(`[混淆] 已混淆 ${obfuscated} 个 JS chunk（体积 ${ratio}% 于压缩后）`)
      }
    },
  }
}
