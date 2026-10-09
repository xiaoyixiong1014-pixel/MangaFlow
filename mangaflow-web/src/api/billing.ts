/**
 * 计费 API（文件级注释）。
 *
 * 对应后端 /api/billing：钱包 / 流水 / 套餐 / 订单 / 模拟支付 / 月度汇总。
 */
import client from './client'
import type {
  MonthConsumed,
  Order,
  PageQuery,
  PageResult,
  PayResponse,
  Pkg,
  Wallet,
  WalletTransactionItem,
} from '@/types'

/** 流水列表查询参数 */
export interface TransactionQuery extends PageQuery {
  /** 按类型过滤：freeze/consume/refund/recharge/grant */
  type?: string
}

/**
 * 查询当前用户钱包概要。
 * @returns 可用/冻结积分、低余额标记、版本号
 */
export function wallet(): Promise<Wallet> {
  return client.get('/billing/wallet').then((r) => r.data)
}

/**
 * 查询积分套餐常量列表。
 * @returns 套餐列表
 */
export function packages(): Promise<Pkg[]> {
  return client.get('/billing/packages').then((r) => r.data)
}

/**
 * 查询我的订单分页列表。
 * @param params 分页参数
 * @returns 订单分页列表
 */
export function orders(params?: PageQuery): Promise<PageResult<Order>> {
  return client.get('/billing/orders', { params }).then((r) => r.data)
}

/**
 * 创建充值订单（pending，模拟支付渠道）。
 * @param packageId 套餐标识（p_small/p_mid/p_large/p_sub）
 * @returns 新建订单
 */
export function recharge(packageId: string): Promise<Order> {
  return client.post('/billing/orders', { package_id: packageId }).then((r) => r.data)
}

/**
 * 模拟支付订单（幂等发放积分）。
 * @param orderNo 订单号
 * @returns 支付后订单与最新余额
 */
export function pay(orderNo: string): Promise<PayResponse> {
  return client.post(`/billing/orders/${orderNo}/pay`).then((r) => r.data)
}

/**
 * 分页查询积分流水（倒序，可按类型过滤）。
 * @param params 分页/类型过滤参数
 * @returns 流水分页列表
 */
export function transactions(params?: TransactionQuery): Promise<PageResult<WalletTransactionItem>> {
  return client.get('/billing/transactions', { params }).then((r) => r.data)
}

/**
 * 近 6 个月每月积分消耗汇总（图表用，恒返回 6 个数据点）。
 * @returns 月份与消耗列表
 */
export function summary(): Promise<MonthConsumed[]> {
  return client.get('/billing/wallet/summary').then((r) => r.data)
}
