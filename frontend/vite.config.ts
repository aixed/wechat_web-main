import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const frontendPort = Number(process.env.FRONTEND_PORT || 3001)

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    port: frontendPort,
    strictPort: true,
    allowedHosts: ['chat.solvernow.com', '1.14.149.2'],
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:5000',
        changeOrigin: true,
        ws: true,
      },
    },
  },
})
