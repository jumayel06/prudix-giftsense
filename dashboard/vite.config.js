import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    allowedHosts: 'all',
    proxy: {
      '/api': { target: 'http://localhost:8001', changeOrigin: true },
      '/auth': { target: 'http://localhost:8001', changeOrigin: true },
      '/billing': { target: 'http://localhost:8001', changeOrigin: true },
      '/webhooks': { target: 'http://localhost:8001', changeOrigin: true },
    },
  },
})
