/**
 * 项目管理 API（文件级注释）。
 *
 * 对应后端 /api/projects：CRUD / 软删除 / 复制 / 版本快照（均校验归属）。
 */
import client from './client'
import type {
  MessageResponse,
  PageQuery,
  PageResult,
  Project,
  ProjectCreatePayload,
  ProjectDetail,
  ProjectUpdatePayload,
  VersionSnapshot,
} from '@/types'

/** 项目列表查询参数 */
export interface ProjectListQuery extends PageQuery {
  /** 标题模糊搜索关键字 */
  search?: string
  /** 项目状态过滤 */
  status?: string
  /** 排序字段：updated_at / created_at */
  sort?: 'updated_at' | 'created_at'
  /** 排序方向 */
  order?: 'desc' | 'asc'
}

/**
 * 分页查询当前用户项目列表。
 * @param params 分页/搜索/排序参数
 * @returns 项目分页列表
 */
export function list(params?: ProjectListQuery): Promise<PageResult<Project>> {
  return client.get('/projects', { params }).then((r) => r.data)
}

/**
 * 创建项目（初始状态 draft）。
 * @param payload 项目标题/体裁/风格等
 * @returns 新建项目
 */
export function create(payload: ProjectCreatePayload): Promise<Project> {
  return client.post('/projects', payload).then((r) => r.data)
}

/**
 * 查询项目详情（含剧集/剧本计数）。
 * @param id 项目 ID
 * @returns 项目详情
 * @throws 404 项目不存在/已删除/非本人
 */
export function get(id: number): Promise<ProjectDetail> {
  return client.get(`/projects/${id}`).then((r) => r.data)
}

/**
 * 更新项目字段（仅传入字段生效）。
 * @param id 项目 ID
 * @param payload 更新内容
 * @returns 更新后的项目
 */
export function update(id: number, payload: ProjectUpdatePayload): Promise<Project> {
  return client.put(`/projects/${id}`, payload).then((r) => r.data)
}

/**
 * 软删除项目（可从回收站恢复）。
 * @param id 项目 ID
 * @returns 消息响应
 */
export function remove(id: number): Promise<MessageResponse> {
  return client.delete(`/projects/${id}`).then((r) => r.data)
}

/**
 * 复制项目（仅复制项目行，标题追加"(副本)"，状态回到 draft）。
 * @param id 源项目 ID
 * @returns 新副本项目
 */
export function duplicate(id: number): Promise<Project> {
  return client.post(`/projects/${id}/duplicate`).then((r) => r.data)
}

/**
 * 分页查询项目版本快照（按创建时间倒序）。
 * @param id 项目 ID
 * @param params 分页参数
 * @returns 快照分页列表
 */
export function snapshots(id: number, params?: PageQuery): Promise<PageResult<VersionSnapshot>> {
  return client.get(`/projects/${id}/snapshots`, { params }).then((r) => r.data)
}

/**
 * 为项目手动创建版本快照。
 * @param id 项目 ID
 * @param payload 阶段标识与摘要
 * @returns 新建快照
 */
export function createSnapshot(
  id: number,
  payload: { stage: string; summary?: unknown },
): Promise<VersionSnapshot> {
  return client.post(`/projects/${id}/snapshots`, payload).then((r) => r.data)
}
