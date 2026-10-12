import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'
import obfuscatorPlugin from './vite-plugin-obfuscator.js'

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
              obfuscatorOptions: {
                compact: true,
                target: 'browser',
                // 控制流扁平化：打乱执行路径，显著提高逆向成本
                controlFlowFlattening: true,
                controlFlowFlatteningThreshold: 0.5,
                // 死代码注入
                deadCodeInjection: true,
                deadCodeInjectionThreshold: 0.2,
                // 字符串数组：把字面量抽到加密数组里，base64 编码 + 轮转 + 洗牌
                stringArray: true,
                stringArrayThreshold: 0.75,
                stringArrayEncoding: ['base64'],
                stringArrayRotate: true,
                stringArrayShuffle: true,
                stringArrayWrappersCount: 1,
                stringArrayWrappersType: 'variable',
                splitStrings: true,
                splitStringsChunkLength: 10,
                // 标识符重命名 / 数字与对象键变形
                identifierNamesGenerator: 'hexadecimal',
                numbersToExpressions: true,
                simplify: true,
                transformObjectKeys: true,
                unicodeEscapeSequence: false,
                // 抹除 console 输出（与 terser drop_console 双保险）
                disableConsoleOutput: true,
                // 保持 ESM 稳定性
                renameGlobals: false,
                selfDefending: false,
                seed: 0,
              },
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
