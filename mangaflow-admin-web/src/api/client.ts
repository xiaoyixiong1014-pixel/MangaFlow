/**
 * 文件级注释：axios 实例与全局拦截器。
 * 与用户端同模式：baseURL '/api'，token 存 localStorage 'mf_admin_token'；
 * 401 统一跳转 /login；业务错误统一 ElMessage.error 提示后端 detail。
 */
import axios from 'axios'
import { ElMessage } from 'element-plus'

/** token 在 localStorage 中的键名 */
export const TOKEN_KEY = 'mf_admin_token'

/**
 * 创建管理端 axios 实例。
 * @returns 配置好 baseURL 与超时的 axios 实例
 */
const client = axios.create({
  baseURL: '/api',
  timeout: 15000,
})

/**
 * 请求拦截器：自动附加管理员访问令牌。
 */
client.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** 是否正在跳转登录页（防止 401 多次触发重复跳转与重复报错） */
let redirecting = false

/**
 * 响应拦截器：统一错误处理。
 * - 401：清除 token 并跳转 /login；
 * - 其他错误：读取后端 {detail} 并 ElMessage.error 提示。
 * @returns 成功时返回响应本体；失败时抛出错误供调用方继续处理
 */
client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    const detail = error?.response?.data?.detail

    if (status === 401) {
      if (!redirecting) {
        redirecting = true
        localStorage.removeItem(TOKEN_KEY)
        ElMessage.error(detail || '登录已失效，请重新登录')
        // 延迟跳转避免与当前渲染冲突
        setTimeout(() => {
          redirecting = false
          window.location.href = '/login'
        }, 300)
      }
    } else {
      const msg = typeof detail === 'string' ? detail : '请求失败，请稍后重试'
      ElMessage.error(msg)
    }
    return Promise.reject(error)
  },
)

export default client
