/**
 * 剧本工作台「选中文本引用桥」（quoteBridge，文件级注释）。
 *
 * 工作台四个步骤（故事设定/角色设定/分集大纲/剧本正文）中的可编辑文本支持
 * 「选中 → 添加对话中」：QuotableText 包装组件检测容器内 input/textarea 的
 * 非空选区，浮出按钮，点击后经本桥把引用送入容器（ScriptView）持有队列，
 * 右侧 AssistantPanel inject 消费（引用区展示 + 随指令发送 quote-rewrite）。
 *
 * 桥实体由容器 ScriptView provide（单一事实源），QuotableText 与
 * AssistantPanel inject 消费；未提供桥时（组件独立使用）inject 走缺省空实现，
 * 不报错、按钮不可见。
 */
import type { InjectionKey, Ref } from 'vue'
import type { QuoteScope } from '@/api/scriptPipeline'

/** 单条选中文本引用（「添加对话中」入队条目） */
export interface ScriptQuoteItem {
  /** 唯一 ID（自增） */
  id: number
  /** 来源展示标签（如「故事设定 · 创意内容」「剧本正文 · 第 1 集 · 文章体」） */
  source: string
  /** 引用作用域（quote-rewrite 接口语境标注用） */
  scope: QuoteScope
  /** 目标剧集 ID（大纲/正文作用域下携带，可选） */
  targetEpisodeId?: number | null
  /** 选中的原文（strip 后非空） */
  text: string
}

/** 引用桥实体（容器 provide；QuotableText 添加、AssistantPanel 消费） */
export interface ScriptQuoteBridge {
  /** 当前引用队列（AssistantPanel 引用区渲染源） */
  quotes: Ref<ScriptQuoteItem[]>
  /**
   * 添加一条引用（入队；超出上限时提示并忽略）。
   * @param source 来源展示标签
   * @param scope 引用作用域
   * @param text 选中的原文
   * @param targetEpisodeId 目标剧集 ID（可选）
   */
  add: (source: string, scope: QuoteScope, text: string, targetEpisodeId?: number | null) => void
  /**
   * 移除指定引用（AssistantPanel 引用区 × 按钮）。
   * @param id 引用 ID
   */
  remove: (id: number) => void
}

/** 引用桥 inject key（ScriptView provide） */
export const SCRIPT_QUOTE_KEY: InjectionKey<ScriptQuoteBridge> = Symbol('script-quote-bridge')

/** 引用队列上限（超出提示「引用已满」，避免对话区被挤爆） */
export const QUOTE_MAX = 5

/**
 * 缺省空桥（未 provide 时 inject 兜底）：quotes 恒空、add/remove 静默——
 * QuotableText 在无容器环境（独立预览）下按钮点击无效果但不报错。
 */
export const EMPTY_QUOTE_BRIDGE: ScriptQuoteBridge = {
  quotes: { value: [] } as unknown as Ref<ScriptQuoteItem[]>,
  add: () => undefined,
  remove: () => undefined,
}
