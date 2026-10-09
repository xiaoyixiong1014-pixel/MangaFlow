/**
 * 提示词库 API（文件级注释）。
 *
 * 对应后端 /api/prompts：查询 / 创建 / 更新 / 删除 / 批量导入。
 */
import client from './client'
import type { MessageResponse, PageQuery, PageResult, Prompt, PromptPayload } from '@/types'

/**
 * 分页查询提示词（公共 + 本人私有）。
 * @param params 分页/过滤参数（capability/category/search 可按需扩展）
 * @returns 提示词分页列表
 */
export function list(params?: PageQuery & { capability?: string; category?: string }): Promise<PageResult<Prompt>> {
  return client.get('/prompts', { params }).then((r) => r.data)
}

/**
 * 创建私有提示词。
 * @param payload 标题/内容/能力/分类/标签
 * @returns 新建提示词
 */
export function create(payload: PromptPayload): Promise<Prompt> {
  return client.post('/prompts', payload).then((r) => r.data)
}

/**
 * 更新提示词（仅传入字段生效）。
 * @param id 提示词 ID
 * @param payload 更新内容
 * @returns 更新后的提示词
 * @throws 404 不存在或非本人提示词
 */
export function update(id: number, payload: Partial<PromptPayload>): Promise<Prompt> {
  return client.put(`/prompts/${id}`, payload).then((r) => r.data)
}

/**
 * 删除提示词。
 * @param id 提示词 ID
 * @returns 消息响应
 */
export function remove(id: number): Promise<MessageResponse> {
  return client.delete(`/prompts/${id}`).then((r) => r.data)
}

/**
 * 批量导入提示词（全部导入为当前用户私有）。
 * @param prompts 待导入的提示词列表（1~200 条）
 * @returns 成功导入条数
 */
export function importPrompts(prompts: PromptPayload[]): Promise<{ imported: number }> {
  return client.post('/prompts/import', { prompts }).then((r) => r.data)
}
