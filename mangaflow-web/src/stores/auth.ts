/**
 * 认证状态 store（文件级注释）。
 *
 * setup 风格 Pinia：token/refreshToken/user 持久化于 localStorage
 * （'mf_token' / 'mf_refresh' / 'mf_user'），balance 供顶栏展示。
 */
import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import * as authApi from '@/api/auth'
import * as billingApi from '@/api/billing'
import type { LoginResponse, User } from '@/types'

/** localStorage 键名（与 api/client.ts 保持一致） */
const TOKEN_KEY = 'mf_token'
const REFRESH_KEY = 'mf_refresh'
const USER_KEY = 'mf_user'

/** 从 localStorage 安全解析用户对象（内部函数） */
function loadStoredUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

/**
 * 认证 store：会话凭据、当前用户、积分余额。
 * @returns state/getter/actions 集合
 */
export const useAuthStore = defineStore('auth', () => {
  // ---------- state ----------
  /** 访问令牌 */
  const token = ref<string | null>(localStorage.getItem(TOKEN_KEY))
  /** 刷新令牌 */
  const refreshToken = ref<string | null>(localStorage.getItem(REFRESH_KEY))
  /** 当前用户信息 */
  const user = ref<User | null>(loadStoredUser())
  /** 积分余额（顶栏展示，登录后拉取） */
  const balance = ref<number>(0)

  // ---------- getter ----------
  /** 是否已登录（以本地 access_token 存在为准） */
  const isLoggedIn = computed(() => !!token.value)
  /** 是否运营人员角色 */
  const isOperator = computed(() => user.value?.role === 'operator')

  /**
   * 将令牌对与用户写入 state 并持久化（内部函数）。
   * @param data 登录/注册响应
   */
  function applyLoginResponse(data: LoginResponse): void {
    token.value = data.access_token
    refreshToken.value = data.refresh_token
    user.value = data.user
    localStorage.setItem(TOKEN_KEY, data.access_token)
    localStorage.setItem(REFRESH_KEY, data.refresh_token)
    localStorage.setItem(USER_KEY, JSON.stringify(data.user))
  }

  // ---------- actions ----------
  /**
   * 账号密码登录。
   * @param username 用户名或邮箱
   * @param password 密码
   * @throws 401 用户名或密码错误；403 账号被禁用
   */
  async function login(username: string, password: string): Promise<void> {
    const data = await authApi.login(username, password)
    applyLoginResponse(data)
  }

  /**
   * 注册（成功即登录，注册赠送初始积分）。
   * @param payload 用户名/邮箱/密码
   * @throws 409 用户名或邮箱已被占用
   */
  async function register(payload: { username: string; email?: string; password: string }): Promise<void> {
    const data = await authApi.register(payload)
    applyLoginResponse(data)
  }

  /** 退出登录：清空 state 与 localStorage 持久化 */
  function logout(): void {
    token.value = null
    refreshToken.value = null
    user.value = null
    balance.value = 0
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
    localStorage.removeItem(USER_KEY)
  }

  /**
   * 拉取当前用户信息（并同步积分余额到 state.balance）。
   * @throws 401 未认证（由拦截器处理刷新/跳转）
   */
  async function fetchMe(): Promise<void> {
    const data = await authApi.me()
    user.value = data.user
    balance.value = data.wallet.points_balance
    localStorage.setItem(USER_KEY, JSON.stringify(data.user))
  }

  /**
   * 拉取钱包概要（仅更新余额；充值/消耗后调用）。
   * @throws 401 未认证
   */
  async function fetchWallet(): Promise<void> {
    const walletData = await billingApi.wallet()
    balance.value = walletData.points_balance
  }

  return {
    token,
    refreshToken,
    user,
    balance,
    isLoggedIn,
    isOperator,
    login,
    register,
    logout,
    fetchMe,
    fetchWallet,
  }
})
