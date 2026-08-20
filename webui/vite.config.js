import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  base: '/',
  build: {
    outDir: '../web',
    emptyOutDir: true,
    sourcemap: false,
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
})
