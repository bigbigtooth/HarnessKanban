import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Dev server is local-only (127.0.0.1:7100) and proxies /api to the Django
// WebAPI on 127.0.0.1:7101. AgentAPI listens on 127.0.0.1:7102.
// `VITE_API_PROXY` 可覆盖代理目标（例如预览演示数据时指向另一套后端）。
const apiTarget = process.env.VITE_API_PROXY ?? 'http://127.0.0.1:7101'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 7100,
    strictPort: true,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: false,
      },
    },
  },
})
