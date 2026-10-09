/**
 * 认证与用户中心 API（文件级注释）。
 *
 * 对应后端：/api/auth/*（注册/登录/刷新/当前用户）与 /api/users/me（资料/密码）。
 */
import client from './client'
import type {
  LoginResponse,
  MeResponse,
  MessageResponse,
  RefreshResponse,
  RegisterPayload,
  UpdateUserPayload,
  User,
} from '@/types'

/**
 * 用户注册：成功即建立会话（返回令牌对与用户信息）。
 * @param payload 用户名/邮箱/密码
 * @returns 令牌对与新建用户
 * @throws 400 邮箱格式非法；409 用户名或邮箱已被占用
 */
export function register(payload: RegisterPayload): Promise<LoginResponse> {
  return client.post('/auth/register', payload).then((r) => r.data)
}

/**
 * 登录（username 支持用户名或邮箱）。
 * @param username 用户名或邮箱
 * @param password 密码
 * @returns 令牌对与用户信息
 * @throws 401 用户名或密码错误；403 账号被禁用
 */
export function login(username: string, password: string): Promise<LoginResponse> {
  return client.post('/auth/login', { username, password }).then((r) => r.data)
}

/**
 * 刷新访问令牌（refresh 不换新）。
 * @param refreshToken 刷新令牌
 * @returns 新 access_token 与原 refresh_token
 * @throws 401 刷新令牌非法/过期/用户被禁用
 */
export function refresh(refreshToken: string): Promise<RefreshResponse> {
  return client.post('/auth/refresh', { refresh_token: refreshToken }).then((r) => r.data)
}

/**
 * 获取当前用户信息与钱包汇总。
 * @returns 用户信息 + 钱包摘要
 * @throws 401 未认证；404 钱包缺失
 */
export function me(): Promise<MeResponse> {
  return client.get('/auth/me').then((r) => r.data)
}

/**
 * 更新当前用户资料（头像/邮箱，仅传入字段生效）。
 * @param payload 头像/邮箱
 * @returns 更新后的用户信息
 * @throws 409 新邮箱已被其他用户占用
 */
export function updateMe(payload: UpdateUserPayload): Promise<User> {
  return client.put('/users/me', payload).then((r) => r.data)
}

/**
 * 修改密码（校验旧密码）。
 * @param oldPassword 旧密码
 * @param newPassword 新密码（至少 6 位）
 * @returns 消息响应
 * @throws 400 旧密码不正确
 */
export function updatePassword(oldPassword: string, newPassword: string): Promise<MessageResponse> {
  return client.put('/users/me/password', {
    old_password: oldPassword,
    new_password: newPassword,
  }).then((r) => r.data)
}
