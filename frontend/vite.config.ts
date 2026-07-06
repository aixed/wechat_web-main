import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const configPath = path.join(repoRoot, 'config.yaml')

function readConfigValue(key: string): string | undefined {
  if (!fs.existsSync(configPath)) return undefined
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const pattern = new RegExp(`^\\s*${escapedKey}\\s*:\\s*(.*?)\\s*$`)
  for (const line of fs.readFileSync(configPath, 'utf8').split(/\r?\n/)) {
    const match = line.match(pattern)
    if (!match) continue
    const raw = match[1].trim()
    if ((raw.startsWith('"') || raw.startsWith("'")) && raw.length >= 2) {
      const quote = raw[0]
      const end = raw.indexOf(quote, 1)
      return end >= 0 ? raw.slice(1, end) : raw.slice(1)
    }
    return raw.replace(/\s+#.*$/, '').trim()
  }
  return undefined
}

function readConfigNumber(key: string, fallback: number): number {
  const value = Number(readConfigValue(key))
  return Number.isFinite(value) ? value : fallback
}

const frontendHost = readConfigValue('frontend_host') || '0.0.0.0'
const frontendPort = readConfigNumber('frontend_port', 3001)
const backendHost = readConfigValue('server_host') || '127.0.0.1'
const backendPort = readConfigNumber('server_port', 5000)
const backendHttpTarget = `http://${backendHost}:${backendPort}`
const backendWsTarget = `ws://${backendHost}:${backendPort}`

export default defineConfig({
  plugins: [react(), tailwindcss()],
  appType: 'spa',
  server: {
    host: frontendHost,
    port: frontendPort,
    strictPort: true,
    allowedHosts: true,
    proxy: {
      '/api': {
        target: backendHttpTarget,
        changeOrigin: true,
        ws: true,
      },
      '/agent': {
        target: backendWsTarget,
        changeOrigin: true,
        ws: true,
      },
      '/receiveChatBotMsg': {
        target: backendHttpTarget,
        changeOrigin: true,
      },
      '/uploads': {
        target: backendHttpTarget,
        changeOrigin: true,
      },
    },
  },
})
