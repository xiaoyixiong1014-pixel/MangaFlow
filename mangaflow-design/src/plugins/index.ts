/**
 * @mangaflow/design · 画布插件类型模块
 *
 * 画布插件市场（CV-20/21/22）的前后端共享类型（UI 单一事实源 RD-58）：
 * PluginManifest 与后端 canvas_plugins 接口出参（PluginOut）字段对齐，
 * 端口定义与后端 PluginPortIn / init_db 内置插件种子结构一致。
 * 前端插件面板 / 节点检查器 import type 消费，避免各自重复声明漂移。
 */

/** 插件端口数据类型（与后端 PluginPortIn 校验规则一致） */
export type PluginPortType = 'text' | 'image' | 'json'

/** 插件端口定义（输入/输出端口） */
export interface PluginPortDef {
  /** 端口名 */
  name: string
  /** 端口数据类型：text/image/json */
  type: PluginPortType
}

/** 插件清单（插件市场条目，对应后端 PluginOut） */
export interface PluginManifest {
  /** 插件 ID */
  id: number
  /** 安装者用户 ID */
  user_id: number
  /** 插件名 */
  name: string
  /** 版本号 */
  version?: string | null
  /** 描述 */
  description?: string | null
  /** 插件地址（"builtin:" 前缀 = 内置实现，否则 HTTP 端点） */
  endpoint_url: string
  /** 输入端口定义 */
  inputs: PluginPortDef[]
  /** 输出端口定义 */
  outputs: PluginPortDef[]
  /** 权限清单（RD-46） */
  permissions: string[]
  /** 用户是否已显式授权 */
  granted: boolean
  /** 是否启用 */
  enabled: boolean
  /** 是否通过市场审核（true = approved / false = pending） */
  reviewed: boolean
}

/** 插件调用结果（对应后端 invoke 响应：插件返回的 JSON 对象） */
export interface PluginInvokeResult {
  /** 插件输出负载：约定内置文本类插件返回 { text: string }，其余由插件自定义 */
  text?: string
  /** 允许插件返回任意附加键 */
  [key: string]: unknown
}
