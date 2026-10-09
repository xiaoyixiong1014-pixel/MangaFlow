/**
 * 智能体意图 store（文件级注释）。
 *
 * 承接官网首页输入：未登录用户在官网输入创作需求 → setIntent 暂存 →
 * 登录成功后 W-01 工作台（AgentWorkspace）读取并清空。
 */
import { ref } from 'vue'
import { defineStore } from 'pinia'

/**
 * 智能体意图 store：官网输入承接的暂存区。
 * @returns state/actions 集合
 */
export const useAgentStore = defineStore('agent', () => {
  /** 暂存的用户创作意图（官网输入原文；null 表示无待承接内容） */
  const pendingIntent = ref<string | null>(null)

  /**
   * 记录待承接的创作意图。
   * @param intent 用户输入的需求文本
   */
  function setIntent(intent: string): void {
    pendingIntent.value = intent
  }

  /**
   * 读取并清空待承接意图（登录后的工作台一次性消费）。
   * @returns 暂存的意图文本；无则返回 null
   */
  function consumeIntent(): string | null {
    const intent = pendingIntent.value
    pendingIntent.value = null
    return intent
  }

  return { pendingIntent, setIntent, consumeIntent }
})
