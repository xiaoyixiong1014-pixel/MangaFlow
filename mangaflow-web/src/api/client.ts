/**
 * Axios 实例与拦截器（文件级注释）。
 *
 * - baseURL '/api'（经 Vite 代理转发至 FastAPI），超时 30s
 * - 请求拦截：自动附加 Bearer Token（localStorage 'mf_token'）
 * - 响应拦截：401 且未重试过 → 用 'mf_refresh' 调 /api/auth/refresh 换新 token
 *   并重放原请求；刷新失败则清空认证并跳转 /login
 * - 业务错误统一 ElMessage.error(detail) 并 reject
 */
import axios, { AxiosError, type AxiosRequestConfig, type InternalAxiosRequestConfig } from 'axios'
import { ElMessage } from 'element-plus'

/** 全局 axios 实例（业务 api 模块统一从此导入） */
const client = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

/**
 * 提取后端返回的 detail 文案（导出供组件错误态复用：拦截器 toast 消失后，
 * 组件内联错误条可展示同一 detail，避免用户看不到失败原因）。
 * @param error axios 拦截器 reject 的错误对象
 * @returns detail 文案（422 数组 detail 降级为通用文案）
 */
export function extractDetail(error: AxiosError): string {
  const data = error.response?.data as { detail?: unknown } | undefined
  const detail = data?.detail
  if (typeof detail === 'string' && detail) return detail
  // 结构化 detail（FastAPI dict detail）：优先取 message 字段（如 P0-1 锁定门禁 409）
  if (detail && typeof detail === 'object' && 'message' in detail) {
    const msg = (detail as { message?: unknown }).message
    if (typeof msg === 'string' && msg) return msg
  }
  return '请求失败，请稍后重试'
}

/** 清空本地认证状态并跳转登录页（内部函数） */
function clearAuthAndRedirect(): void {
  localStorage.removeItem('mf_token')
  localStorage.removeItem('mf_refresh')
  localStorage.removeItem('mf_user')
  // 避免在登录页上循环跳转
  if (!window.location.pathname.startsWith('/login')) {
    window.location.href = '/login'
  }
}

// 进行中的刷新请求（并发 401 时只发起一次刷新）
let refreshingPromise: Promise<string | null> | null = null

/**
 * 用 refresh_token 换取新 access_token（内部函数）。
 * @returns 新 access_token；失败返回 null
 */
async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = localStorage.getItem('mf_refresh')
  if (!refreshToken) return null
  try {
    // 使用独立 axios 实例，避免命中本文件响应拦截器造成递归
    const resp = await axios.post('/api/auth/refresh', { refresh_token: refreshToken })
    const accessToken = resp.data?.access_token as string | undefined
    if (accessToken) {
      localStorage.setItem('mf_token', accessToken)
      return accessToken
    }
    return null
  } catch {
    return null
  }
}

// 请求拦截：附加认证头
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem('mf_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截：401 刷新重放 + 统一业务错误提示
client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as (AxiosRequestConfig & { _retried?: boolean }) | undefined
    const status = error.response?.status
    const url = config?.url ?? ''
    // 认证类接口本身失败不做刷新重放（避免登录失败误触发跳转）
    const isAuthEndpoint = url.includes('/auth/login')
      || url.includes('/auth/register')
      || url.includes('/auth/refresh')

    if (status === 401 && config && !config._retried && !isAuthEndpoint) {
      config._retried = true // 防止重放后仍然 401 导致死循环
      refreshingPromise = refreshingPromise ?? refreshAccessToken()
      const newToken = await refreshingPromise
      refreshingPromise = null
      if (newToken) {
        // 用新 token 重放原请求
        config.headers = { ...(config.headers ?? {}), Authorization: `Bearer ${newToken}` }
        return client.request(config)
      }
      // 刷新失败：清空认证并跳登录页
      clearAuthAndRedirect()
    }

    ElMessage.error(extractDetail(error))
    return Promise.reject(error)
  },
)

export default client
