/**
 * 文件级注释：管理员认证状态仓库（Pinia）。
 * token 存 localStorage 'mf_admin_token'，用户信息存 'mf_admin_user'；
 * 提供 isOperator getter 判定是否具备管理端准入角色。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { AdminUser } from '@/types'
import { TOKEN_KEY } from '@/api/client'

/** 用户信息在 localStorage 中的键名 */
const USER_KEY = 'mf_admin_user'

/**
 * 认证仓库。
 */
export const useAuthStore = defineStore('auth', () => {
  /** 访问令牌 */
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) ?? '')
  /** 当前管理员信息 */
  const user = ref<AdminUser | null>(parseUser())

  /**
   * 从 localStorage 解析用户信息。
   * @returns 管理员对象；解析失败返回 null
   * @throws 无（JSON 解析异常时吞掉并清空脏数据）
   */
  function parseUser(): AdminUser | null {
    try {
      const raw = localStorage.getItem(USER_KEY)
      return raw ? (JSON.parse(raw) as AdminUser) : null
    } catch {
      localStorage.removeItem(USER_KEY)
      return null
    }
  }

  /** 是否具备管理端准入角色（operator） */
  const isOperator = computed(() => user.value?.role === 'operator')

  /**
   * 写入登录态（token + 用户信息）。
   * @param newToken 访问令牌
   * @param newUser 管理员信息
   */
  function setSession(newToken: string, newUser: AdminUser) {
    token.value = newToken
    user.value = newUser
    localStorage.setItem(TOKEN_KEY, newToken)
    localStorage.setItem(USER_KEY, JSON.stringify(newUser))
  }

  /**
   * 清空登录态并移除本地凭据。
   */
  function clearSession() {
    token.value = ''
    user.value = null
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
  }

  return { token, user, isOperator, setSession, clearSession }
})
