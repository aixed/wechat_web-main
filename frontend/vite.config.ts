import { createLogger, defineConfig, type LogErrorOptions, type ProxyOptions } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import fs from 'node:fs'
import type { ServerResponse } from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const configPaths = ['config.yaml', 'config.example.yaml'].map((name) => path.join(repoRoot, name))

function readConfigValue(key: string): string | undefined {
  const escapedKey = key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const pattern = new RegExp(`^${escapedKey}\\s*:\\s*(.*?)\\s*$`)
  for (const configPath of configPaths) {
    if (!fs.existsSync(configPath)) continue
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
  }
  return undefined
}

function readConfigNumber(key: string): number {
  const value = Number(requireConfigValue(key))
  if (!Number.isFinite(value)) throw new Error(`Invalid configured number: ${key}`)
  return value
}

function requireConfigValue(key: string): string {
  const value = readConfigValue(key)
  if (!value) throw new Error(`Missing configuration: ${key}`)
  return value
}

function readEnvNumber(key: string, fallback: number): number {
  const value = Number(process.env[key])
  return Number.isFinite(value) ? value : fallback
}

const frontendHost = requireConfigValue('frontend_host')
const frontendPort = readEnvNumber('FRONTEND_PORT', readConfigNumber('frontend_port'))
const configuredBackendHost = requireConfigValue('server_host')
const backendHost = normalizeProxyHost(configuredBackendHost)
const backendPort = readConfigNumber('server_port')
const backendHttpTarget = `http://${backendHost}:${backendPort}`
const backendWsTarget = `ws://${backendHost}:${backendPort}`
const proxyUnavailableMessage = '后端服务正在启动或暂时不可用，请稍后重试。'
const logger = createLogger()
const logProxyError = createProxyErrorLogger()
const defaultLoggerError = logger.error.bind(logger)

logger.error = (message: string, options?: LogErrorOptions) => {
  if (logProxyError(message, options?.error)) return
  defaultLoggerError(message, options)
}

function normalizeProxyHost(host: string): string {
  const value = host.trim().toLowerCase()
  if (value === readConfigValue('network_bind_all_host') || !value.replace(/[\[\]:]/g, '')) return requireConfigValue('network_loopback_host')
  return host
}

function errorMessage(error: Error): string {
  const code = 'code' in error ? String(error.code || '') : ''
  if (code === 'ECONNREFUSED') return '后端服务还未就绪'
  if (code === 'ECONNRESET') return '后端连接已断开'
  return error.message || '代理请求失败'
}

function canWriteResponse(res: unknown): res is ServerResponse {
  return Boolean(res && typeof res === 'object' && 'writeHead' in res && 'end' in res)
}

function backendProxy(target: string, ws = false): ProxyOptions {
  return {
    target,
    changeOrigin: true,
    ws,
    configure(proxy) {
      proxy.on('error', (_error, _req, res) => {
        if (canWriteResponse(res) && !res.headersSent) {
          res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' })
          res.end(JSON.stringify({
            error: 'backend_unavailable',
            message: proxyUnavailableMessage,
          }))
        }
      })
    },
  }
}

function createProxyErrorLogger() {
  const lastSeen = new Map<string, number>()
  const intervalMs = 5000

  return (message: string, error?: unknown): boolean => {
    const isProxyError = (
      message.includes('http proxy error:') ||
      message.includes('ws proxy error:') ||
      message.includes('ws proxy socket error:')
    )
    if (!isProxyError) return false

    const path = message.match(/(?:http|ws) proxy error:\s*([^\n]+)/)?.[1] || 'websocket'
    const reason = error instanceof Error ? errorMessage(error) : '代理请求失败'
    const key = `${path}:${reason}`
    const now = Date.now()
    if (now - (lastSeen.get(key) || 0) < intervalMs) return true
    lastSeen.set(key, now)
    logger.warn(`[proxy] ${proxyUnavailableMessage} 原因：${reason}；请求：${path}；后端：${backendHttpTarget}`, {
      timestamp: true,
    })
    return true
  }
}

export default defineConfig({
  customLogger: logger,
  plugins: [react(), tailwindcss()],
  appType: 'spa',
  server: {
    host: frontendHost,
    port: frontendPort,
    strictPort: true,
    allowedHosts: true,
    proxy: {
      '/api': backendProxy(backendHttpTarget, true),
      '/agent': backendProxy(backendWsTarget, true),
      '/receiveChatBotMsg': backendProxy(backendHttpTarget),
      '/uploads': backendProxy(backendHttpTarget),
    },
  },
})
