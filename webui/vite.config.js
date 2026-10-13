import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import obfuscatorPlugin from './vite-plugin-obfuscator.js'

// ---------------------------------------------------------------------------
// 混淆形态表
// ---------------------------------------------------------------------------
// 全站共用一套参数时，每个 chunk 的头部会一模一样（同样的 _0x 字符串数组 +
// 同样的自执行轮转函数）—— 这是极强的指纹，一眼看出「整站被同一工具刷过」。
// 这里准备几套差异明显的形态按 chunk 分配，再叠加每 chunk 独立 seed，让产物
// 看起来像「多年迭代、不同模块不同时期分别处理过」的真实项目。
const _PROFILE = {
  // A：十六进制命名 + 控制流扁平化（最重）—— 给最敏感的 chunk
  heavyHex: {
    compact: true,
    target: 'browser',
    controlFlowFlattening: true,
    controlFlowFlatteningThreshold: 1.0,
    deadCodeInjection: true,
    deadCodeInjectionThreshold: 0.4,
    stringArray: true,
    stringArrayThreshold: 1.0,
    stringArrayEncoding: ['rc4'],
    stringArrayRotate: true,
    stringArrayShuffle: true,
    stringArrayWrappersCount: 2,
    stringArrayWrappersType: 'function',
    splitStrings: true,
    splitStringsChunkLength: 8,
    identifierNamesGenerator: 'hexadecimal',
    numbersToExpressions: true,
    simplify: true,
    transformObjectKeys: true,
    unicodeEscapeSequence: false,
    disableConsoleOutput: true,
    renameGlobals: false,
    selfDefending: false,
  },
  // B：mangled 命名 + 中度控制流 —— 标识符不再是 _0x，指纹完全不同
  heavyMangled: {
    compact: true,
    target: 'browser',
    controlFlowFlattening: true,
    controlFlowFlatteningThreshold: 0.75,
    deadCodeInjection: true,
    deadCodeInjectionThreshold: 0.2,
    stringArray: true,
    stringArrayThreshold: 0.9,
    stringArrayEncoding: ['base64'],
    stringArrayRotate: true,
    stringArrayShuffle: true,
    stringArrayWrappersCount: 1,
    stringArrayWrappersType: 'variable',
    splitStrings: true,
    splitStringsChunkLength: 12,
    identifierNamesGenerator: 'mangled',
    numbersToExpressions: true,
    simplify: true,
    transformObjectKeys: true,
    disableConsoleOutput: true,
    renameGlobals: false,
    selfDefending: false,
  },
  // C：中等 —— 不做控制流扁平化，以字符串数组为主
  medium: {
    compact: true,
    target: 'browser',
    controlFlowFlattening: false,
    deadCodeInjection: true,
    deadCodeInjectionThreshold: 0.15,
    stringArray: true,
    stringArrayThreshold: 0.8,
    stringArrayEncoding: [],
    stringArrayRotate: true,
    stringArrayShuffle: true,
    stringArrayWrappersCount: 1,
    stringArrayWrappersType: 'variable',
    splitStrings: true,
    splitStringsChunkLength: 10,
    identifierNamesGenerator: 'mangled-shuffled',
    numbersToExpressions: false,
    simplify: true,
    transformObjectKeys: true,
    disableConsoleOutput: true,
    renameGlobals: false,
    selfDefending: false,
  },
  // D：低程度 —— 只改名 + 压少量字符串（诱饵：看起来"有保护但不强"）
  light: {
    compact: true,
    target: 'browser',
    controlFlowFlattening: false,
    deadCodeInjection: false,
    stringArray: true,
    stringArrayThreshold: 0.3,
    stringArrayEncoding: [],
    stringArrayRotate: false,
    stringArrayShuffle: false,
    stringArrayWrappersCount: 1,
    stringArrayWrappersType: 'variable',
    splitStrings: false,
    identifierNamesGenerator: 'mangled',
    numbersToExpressions: false,
    simplify: false,
    transformObjectKeys: false,
    disableConsoleOutput: true,
    renameGlobals: false,
    selfDefending: false,
  },
}

export default defineConfig(({ mode }) => {
  const isProd = mode === 'production'

  return {
    plugins: [
      vue(),
      // 生产构建：混淆业务 JS（跳过第三方 vendor chunk）
      ...(isProd
        ? [
            obfuscatorPlugin({
              // 仅混淆业务代码；element-plus / vue-vendor 为开源库，混淆收益低、代价高
              exclude: [/^js\/element-plus\./, /^js\/vue-vendor\./],
              // 默认形态（最敏感 chunk：app / api / Login / Permissions 走这套）
              obfuscatorOptions: _PROFILE.heavyHex,
              // 多形态分层：打破「全站同一套壳」的指纹
              profiles: [
                // 诱饵 · 低程度：看起来"有保护但不强"，诱导逆向、消耗其时间
                { name: 'light', test: /^js\/(sidebar|PluginWebUI)\./, options: _PROFILE.light },
                // 中等：常规视图
                { name: 'medium', test: /^js\/(Dashboard|Tasks|Groups|Commands|Logs|Connection|Users|ApiKeys|Database|FileBrowser|Runtime)\./, options: _PROFILE.medium },
                // mangled 重度：标识符不再是 _0x，指纹完全不同
                { name: 'heavyMangled', test: /^js\/(Settings|Plugins|Marketplace|Layout)\./, options: _PROFILE.heavyMangled },
                // 其余（app / api / Login / Permissions）走默认 heavyHex
              ],
            }),
          ]
        : []),
    ],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    base: './',
    define: {
      // 生产构建裁剪 Vue 开发期提示，减少信息泄露
      __VUE_PROD_DEVTOOLS__: 'false',
      __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false',
    },
    build: {
      outDir: '../core_plugins/webui/web',
      emptyOutDir: true,
      sourcemap: false,
      // 单文件 CSS：管理后台体量小，合并后路由切换无样式闪烁，也减少产物文件数
      cssCodeSplit: false,
      minify: 'terser',
      terserOptions: {
        compress: {
          drop_console: true,
          drop_debugger: true,
          pure_funcs: ['console.log', 'console.debug', 'console.info'],
        },
        format: {
          comments: false,
        },
      },
      chunkSizeWarningLimit: 4000,
      rollupOptions: {
        output: {
          entryFileNames: 'js/app.[hash].js',
          chunkFileNames: 'js/[name].[hash].js',
          manualChunks: {
            'element-plus': ['element-plus', '@element-plus/icons-vue'],
            'vue-vendor': ['vue', 'vue-router'],
          },
          assetFileNames: (assetInfo) => {
            const name = assetInfo.names && assetInfo.names[0]
            if (name && /\.(css)$/.test(name)) return 'css/app.[hash].css'
            if (name && /\.(png|jpe?g|gif|svg|ico)$/.test(name)) return 'img/[name][extname]'
            return 'css/[name][extname]'
          },
        },
      },
    },
  }
})
