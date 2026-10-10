<!--
  AI 工作台 W-01（文件级注释）。
  登录后默认落地页：智能体大输入卡为第一屏（SSE 流式对话，可停止），
  意图承接条承接官网带入的创作意图；顶部渠道健康标红条（D-05）；
  对话区支持 HITL 确认卡片（pending_confirm → chat/resume，MA-08）；
  会话侧支持「历史检查点」抽屉回溯（Time Travel，MA-06）；
  下方两栏为「最近生成」（会话回放）与「我的项目」（前 4 个 + 新建项目弹窗）。
  数据分区加载：输入框立即可用，不被下方数据加载阻塞。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { MfButton, MfCard, MfTag } from '@mangaflow/design'
import { useAgentStore } from '@/stores/agent'
import * as agentApi from '@/api/agent'
import * as catalogApi from '@/api/catalog'
import * as projectsApi from '@/api/projects'
import type {
  AgentMessageItem,
  AgentThread,
  HealthSummary,
  Project,
  ThreadCheckpoint,
} from '@/types'
import NewProjectDialog from '@/components/NewProjectDialog.vue'

const router = useRouter()
const agentStore = useAgentStore()

// ---------- 意图承接条 ----------
/** 继续按钮点击：把意图填入输入框并聚焦（同时清空暂存） */
async function acceptIntent(): Promise<void> {
  if (agentStore.pendingIntent) {
    input.value = agentStore.pendingIntent
  }
  agentStore.consumeIntent()
  await nextTick()
  inputAreaRef.value?.focus()
}

/** 忽略按钮点击：清空暂存意图（本会话不再提示） */
function dismissIntent(): void {
  agentStore.consumeIntent()
}

// ---------- Agent 输入与流式对话 ----------
/** 输入框 textarea 引用（继续按钮聚焦用） */
const inputAreaRef = ref<HTMLTextAreaElement>()
/** 用户输入（≤2000 字） */
const input = ref('')
/** 选中的技能（仅上下文标注，不改变请求结构） */
const skill = ref('auto')
/** 参考文件名（[+] 视觉占位，不真实上传） */
const refFiles = ref<string[]>([])
/** 隐藏文件 input 引用 */
const fileInputRef = ref<HTMLInputElement>()
/** 生成中标记（停止按钮显示 / 发送按钮禁用） */
const streaming = ref(false)
/** 当前流式中止控制器 */
const controller = ref<AbortController | null>(null)
/** 当前会话 ID（done 事件返回，多轮对话延续） */
const threadId = ref<string | null>(null)
/** 对话消息列表（渲染区） */
const messages = reactive<AgentMessageItem[]>([])
/** 识别到的阶段意图（stage 事件，展示在回复区顶部） */
const stageHint = ref('')
/** 回复区滚动容器引用（追加文本后自动滚底） */
const chatBodyRef = ref<HTMLDivElement>()

/** 技能下拉选项 */
const SKILL_OPTIONS = [
  { value: 'auto', label: '自动创作' },
  { value: 'script', label: '生成剧本' },
  { value: 'character', label: '角色设计' },
  { value: 'video', label: '视频生成' },
  { value: 'dubbing', label: '配音' },
]

/** 分类快捷 chips（点击填充模板文案并联动技能） */
const QUICK_CHIPS = [
  { label: '漫剧脚本', skill: 'script', text: '帮我生成一个漫剧剧本：' },
  { label: '角色定妆照', skill: 'character', text: '帮我设计一个角色定妆照：' },
  { label: '场景概念图', skill: 'character', text: '帮我生成一张场景概念图：' },
  { label: '分镜拆解', skill: 'script', text: '帮我把这段剧本拆解成分镜：' },
  { label: '视频生成', skill: 'video', text: '帮我生成一段视频：' },
  { label: '配音合成', skill: 'dubbing', text: '帮我把这段台词合成配音：' },
]

/**
 * 点击分类 chip：填充模板文案 + 联动技能下拉。
 * @param chip 被点击的 chip
 */
function applyChip(chip: (typeof QUICK_CHIPS)[number]): void {
  input.value = chip.text
  skill.value = chip.skill
  inputAreaRef.value?.focus()
}

/**
 * 选择参考文件（视觉占位）：仅记录文件名 chip。
 * @param e 文件选择事件
 */
function onFileChosen(e: Event): void {
  const inputEl = e.target as HTMLInputElement
  const file = inputEl.files?.[0]
  if (file) refFiles.value.push(file.name)
  inputEl.value = ''
}

/**
 * 点击「优化」：调 /agent/intake 识别意图并提示（不发送）。
 */
async function optimize(): Promise<void> {
  const text = input.value.trim()
  if (!text) {
    ElMessage.warning('请先描述你的创作想法')
    return
  }
  try {
    const result = await agentApi.intake(text)
    const map: Record<string, string> = {
      script: '剧本创作', asset: '角色 / 资产', storyboard: '分镜拆解',
      generation: '视频生成', canvas: '画布编排', billing: '积分查询', chat: '自由对话',
    }
    ElMessage.success(`识别意图：${map[result.intent] ?? result.intent}（置信度 ${(result.confidence * 100).toFixed(0)}%）`)
  } catch {
    // 错误已由拦截器提示 detail
  }
}

/**
 * 发送消息（核心）：SSE 流式对话，事件 message/token / stage / pending_confirm / done。
 * 追加 user + assistant 消息，message/token 帧文本直接追加（打字机效果）；
 * pending_confirm 帧为 HITL 中断：记录确认请求并在对话区渲染确认卡片，
 * 用户确认/取消后调 chat/resume 续跑（Task 10.4）；done 帧记录 thread_id。
 * 生成中可停止（AbortController）。
 */
async function send(): Promise<void> {
  const text = input.value.trim()
  if (!text) {
    ElMessage.warning('请输入你的创作想法')
    return
  }
  if (streaming.value) return
  input.value = ''
  refFiles.value = []
  pendingConfirm.value = null // 新一轮对话清除上一轮未决确认
  messages.push({ role: 'user', content: text })
  const reply = reactive<AgentMessageItem>({ role: 'assistant', content: '' })
  messages.push(reply)
  await runAgentStream({ message: text, thread_id: threadId.value ?? undefined }, reply)
}

/**
 * 消费 Agent 对话 SSE 流（chat/stream 用；resume 的事件处理内联在 confirmHITL）。
 * @param body 请求体（消息 / 会话 ID）
 * @param reply 当前 assistant 回复消息（增量文本追加目标）
 * @returns 无
 */
async function runAgentStream(
  body: { message: string; thread_id?: string },
  reply: AgentMessageItem,
): Promise<void> {
  streaming.value = true
  controller.value = new AbortController()
  try {
    await agentApi.chatStream(body, (event, data) => {
      if (event === 'message' || event === 'token') {
        // message 与 token 均为增量文本（token 为 token 级，拼接语义一致）
        const payload = data as { text?: string }
        reply.content += payload?.text ?? ''
        scrollChatToBottom()
      } else if (event === 'stage') {
        const payload = data as { intent?: string }
        stageHint.value = payload?.intent ?? ''
      } else if (event === 'pending_confirm') {
        // HITL 中断：图在 interrupt() 处暂停，等用户确认后 resume 续跑
        const payload = data as { thread_id?: string; confirm_type?: string; payload?: unknown }
        pendingConfirm.value = {
          threadId: payload?.thread_id ?? threadId.value ?? '',
          confirmType: payload?.confirm_type ?? 'confirm',
          payload: payload?.payload ?? null,
        }
        scrollChatToBottom()
      } else if (event === 'done') {
        const payload = data as { thread_id?: string }
        if (payload?.thread_id) threadId.value = payload.thread_id
      }
    }, controller.value.signal)
  } catch (error) {
    if ((error as Error).name === 'AbortError') {
      reply.content += '\n（已停止生成）'
    } else {
      ElMessage.error((error as Error).message || '生成失败，请稍后重试')
      if (!reply.content) reply.content = '（生成失败）'
    }
  } finally {
    streaming.value = false
    controller.value = null
    refreshThreads()
  }
}

/**
 * HITL 确认卡片按钮：确认/取消后调 chat/resume 续跑（返回也是 SSE，照常渲染）。
 * @param approved true=确认放行 / false=取消并中止
 * @returns 无
 */
async function confirmHITL(approved: boolean): Promise<void> {
  const confirm = pendingConfirm.value
  if (!confirm || resuming.value) return
  resuming.value = true
  pendingConfirm.value = null
  messages.push({ role: 'user', content: approved ? '（用户已确认继续）' : '（用户已取消该步骤）' })
  const reply = reactive<AgentMessageItem>({ role: 'assistant', content: '' })
  messages.push(reply)
  try {
    // resume 返回同样为 SSE 流：事件处理与 runAgentStream 一致（可能连环 pending_confirm）
    await agentApi.chatResume(
      { thread_id: confirm.threadId, approved },
      (event, data) => {
        if (event === 'message' || event === 'token') {
          const payload = data as { text?: string }
          reply.content += payload?.text ?? ''
          scrollChatToBottom()
        } else if (event === 'pending_confirm') {
          const payload = data as { thread_id?: string; confirm_type?: string; payload?: unknown }
          pendingConfirm.value = {
            threadId: payload?.thread_id ?? confirm.threadId,
            confirmType: payload?.confirm_type ?? 'confirm',
            payload: payload?.payload ?? null,
          }
          scrollChatToBottom()
        } else if (event === 'done') {
          const payload = data as { thread_id?: string }
          if (payload?.thread_id) threadId.value = payload.thread_id
        }
      },
    )
  } catch (error) {
    ElMessage.error((error as Error).message || '恢复执行失败')
    if (!reply.content) reply.content = '（恢复失败）'
  } finally {
    resuming.value = false
    refreshThreads()
  }
}

/** HITL 确认请求（非空时对话区渲染确认卡片） */
const pendingConfirm = ref<{ threadId: string; confirmType: string; payload: unknown } | null>(null)
/** HITL 恢复执行中（防重复点击确认按钮） */
const resuming = ref(false)

/**
 * 确认卡片 payload 摘要文本（JSON 压缩 + 截断）。
 */
const pendingConfirmSummary = computed(() => {
  if (!pendingConfirm.value) return ''
  try {
    const text = JSON.stringify(pendingConfirm.value.payload)
    return text.length > 300 ? `${text.slice(0, 300)}…` : text
  } catch {
    return String(pendingConfirm.value.payload ?? '')
  }
})

/** 停止生成：中止当前 SSE 流 */
function stopStream(): void {
  controller.value?.abort()
}

/** 回复区滚到底部 */
function scrollChatToBottom(): void {
  nextTick(() => {
    const el = chatBodyRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

// ---------- 渠道健康标红条（Task 10.2 D-05） ----------
/** 渠道健康摘要 */
const healthSummary = ref<HealthSummary | null>(null)
/** 异常渠道（down / degraded）列表 */
const unhealthyChannels = computed(() =>
  (healthSummary.value?.channels ?? []).filter(
    (c) => c.health_status === 'down' || c.health_status === 'degraded',
  ),
)
/** 是否存在 down 渠道（决定标红级别：error / warning） */
const hasDownChannel = computed(() =>
  unhealthyChannels.value.some((c) => c.health_status === 'down'),
)

/**
 * 拉取渠道健康摘要（失败静默：健康条非关键路径）。
 * @returns 无
 */
async function fetchHealth(): Promise<void> {
  try {
    healthSummary.value = await catalogApi.healthSummary()
  } catch {
    /* 静默降级 */
  }
}

// ---------- 最近生成（会话列表 + 回放） ----------
/** 会话列表（最近 5 条） */
const threads = ref<AgentThread[]>([])
/** 会话列表加载中 */
const threadsLoading = ref(false)
/** 回放弹窗可见性 */
const replayVisible = ref(false)
/** 回放中的会话消息 */
const replayMessages = ref<AgentMessageItem[]>([])
/** 回放中的会话 ID */
const replayThreadId = ref('')

/**
 * 拉取最近 5 条会话（发送结束后也刷新）。
 */
async function refreshThreads(): Promise<void> {
  threadsLoading.value = true
  try {
    threads.value = (await agentApi.threads()).slice(0, 5)
  } catch {
    // 错误已由拦截器提示，分区降级不阻塞输入
  } finally {
    threadsLoading.value = false
  }
}

/**
 * 点击会话：回放历史消息（弹窗展示）。
 * @param thread 会话项
 */
async function openReplay(thread: AgentThread): Promise<void> {
  replayThreadId.value = thread.thread_id
  replayVisible.value = true
  replayMessages.value = []
  try {
    const detail = await agentApi.threadMessages(thread.thread_id)
    replayMessages.value = detail.messages
  } catch {
    // 错误已由拦截器提示
  }
}

// ---------- 历史检查点抽屉（Time Travel，MA-06 / Task 10.4） ----------
/** 检查点抽屉可见性 */
const historyVisible = ref(false)
/** 检查点历史加载中 */
const historyLoading = ref(false)
/** 当前查看的会话 ID */
const historyThreadId = ref('')
/** 检查点列表（按时间倒序） */
const checkpoints = ref<ThreadCheckpoint[]>([])
/** 回溯进行中的检查点 ID（防重复点击） */
const rewindingId = ref('')

/**
 * 打开历史检查点抽屉并拉取该会话检查点历史。
 * @param thread 会话项
 * @returns 无
 */
async function openHistory(thread: AgentThread): Promise<void> {
  historyThreadId.value = thread.thread_id
  historyVisible.value = true
  historyLoading.value = true
  checkpoints.value = []
  try {
    const resp = await agentApi.threadHistory(thread.thread_id, { page: 1, page_size: 20 })
    checkpoints.value = resp.items.filter((c) => c.checkpoint_id) // 无 ID 的检查点不可回溯
  } catch {
    // 错误已由拦截器提示
  } finally {
    historyLoading.value = false
  }
}

/**
 * 回退到指定检查点：confirm 后调 rewind 写入标记；成功后把当前会话切到该
 * 线程，提示下次对话将从该检查点分叉。
 * @param cp 目标检查点
 * @returns 无
 */
async function rewindTo(cp: ThreadCheckpoint): Promise<void> {
  if (!cp.checkpoint_id || rewindingId.value) return
  try {
    await ElMessageBox.confirm(
      '回退后，下一次对话将从该检查点分叉继续（此标记一次性，对话完成后自动清除）。确认回退？',
      '回退会话',
      { type: 'warning', confirmButtonText: '回退', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  rewindingId.value = cp.checkpoint_id
  try {
    await agentApi.rewindThread(historyThreadId.value, cp.checkpoint_id)
    threadId.value = historyThreadId.value // 后续输入框对话将接在该线程之后（从检查点分叉）
    stageHint.value = cp.stage ?? stageHint.value
    ElMessage.success('已回退，下次对话将从该检查点分叉')
    historyVisible.value = false
  } catch {
    // 错误已由拦截器提示（400 检查点不存在 / 404 会话不存在）
  } finally {
    rewindingId.value = ''
  }
}

// ---------- 我的项目 ----------
/** 项目列表（前 4 个） */
const projects = ref<Project[]>([])
/** 项目加载中 */
const projectsLoading = ref(false)
/** 新建项目弹窗可见性 */
const newProjectVisible = ref(false)

/**
 * 拉取最近 4 个项目（按更新时间倒序）。
 */
async function refreshProjects(): Promise<void> {
  projectsLoading.value = true
  try {
    const page = await projectsApi.list({ page: 1, page_size: 4 })
    projects.value = page.items
  } catch {
    // 错误已由拦截器提示
  } finally {
    projectsLoading.value = false
  }
}

// 页面进入：并发拉取会话与项目（输入区不被阻塞）+ 渠道健康
onMounted(() => {
  refreshThreads()
  refreshProjects()
  void fetchHealth()
})

// 离开页面：中止进行中的流（避免悬挂请求）
onBeforeUnmount(() => {
  controller.value?.abort()
})
</script>

<template>
  <div class="page-container workspace">
    <!-- ==================== 渠道健康标红条（D-05，Task 10.2） ==================== -->
    <el-alert
      v-if="unhealthyChannels.length"
      :type="hasDownChannel ? 'error' : 'warning'"
      show-icon
      :closable="false"
      class="health-alert"
      :title="`模型渠道 ${unhealthyChannels.length} 异常，AI 生成可能降级`"
      :description="unhealthyChannels.map((c) => `#${c.id} ${c.base_url}（${c.health_status === 'down' ? '不可用' : '降级'}）`).join('；')"
    />

    <!-- ==================== 意图承接条（官网带入） ==================== -->
    <el-alert v-if="agentStore.pendingIntent" type="success" show-icon :closable="false" class="intent-alert">
      <template #title>
        <span class="intent-text">继续你刚才的创作：{{ agentStore.pendingIntent }}</span>
        <el-button size="small" type="primary" class="intent-btn" @click="acceptIntent">继续</el-button>
        <el-button size="small" @click="dismissIntent">忽略</el-button>
      </template>
    </el-alert>

    <!-- ==================== 居中 Agent 大输入卡 ==================== -->
    <section class="agent-hero">
      <h2 class="agent-title">漫序 MangaFlow 创作 Agent</h2>
      <p class="agent-sub">从一个想法开始</p>

      <div class="agent-card">
        <textarea
          ref="inputAreaRef"
          v-model="input"
          class="agent-input"
          rows="4"
          maxlength="2000"
          placeholder="输入你的创作想法、剧本要求或画布编排需求…（Enter 发送 / Shift+Enter 换行）"
          @keydown.enter.exact.prevent="send"
        />
        <!-- 参考文件 chips（视觉占位） -->
        <div v-if="refFiles.length" class="ref-files">
          <span v-for="(f, i) in refFiles" :key="f + i" class="ref-chip">📎 {{ f }}</span>
        </div>
        <!-- 底部能力条 -->
        <div class="agent-toolbar">
          <div class="agent-toolbar__left">
            <button type="button" class="tool-btn" title="上传参考素材（演示占位）" @click="fileInputRef?.click()">＋</button>
            <input ref="fileInputRef" type="file" style="display: none" @change="onFileChosen" />
            <el-select v-model="skill" class="skill-select" size="small">
              <el-option v-for="s in SKILL_OPTIONS" :key="s.value" :label="s.label" :value="s.value" />
            </el-select>
            <el-button size="small" text @click="ElMessage.info('角色 / 素材 / 提示词引用即将开放')">@ 引用</el-button>
          </div>
          <div class="agent-toolbar__right">
            <el-button size="small" :disabled="streaming" @click="optimize">优化</el-button>
            <button
              v-if="streaming"
              type="button"
              class="stop-btn"
              title="停止生成"
              @click="stopStream"
            >
              ■ 停止
            </button>
            <button v-else type="button" class="send-btn" title="发送（Enter）" @click="send">↑</button>
          </div>
        </div>
      </div>

      <!-- 分类快捷 chips -->
      <div class="chip-row">
        <button
          v-for="chip in QUICK_CHIPS"
          :key="chip.label"
          type="button"
          class="quick-chip"
          @click="applyChip(chip)"
        >
          {{ chip.label }}
        </button>
      </div>
    </section>

    <!-- ==================== 流式回复区 ==================== -->
    <MfCard v-if="messages.length" class="chat-card">
      <template #header>
        <span>对话{{ stageHint ? ` · 已识别阶段：${stageHint}` : '' }}</span>
      </template>
      <div ref="chatBodyRef" class="chat-body">
        <div
          v-for="(m, i) in messages"
          :key="i"
          class="chat-line"
          :class="m.role === 'user' ? 'chat-line--user' : 'chat-line--assistant'"
        >
          <span class="chat-bubble">{{ m.content }}</span>
        </div>
        <!-- HITL 确认卡片（pending_confirm，Task 10.4） -->
        <div v-if="pendingConfirm" class="chat-line chat-line--assistant">
          <div class="hitl-card">
            <div class="hitl-title">
              <span aria-hidden="true">🤚</span>
              需要你的确认（{{ pendingConfirm.confirmType }}）
            </div>
            <pre v-if="pendingConfirmSummary" class="hitl-payload">{{ pendingConfirmSummary }}</pre>
            <div class="hitl-actions">
              <el-button
                size="small"
                type="primary"
                :loading="resuming"
                @click="confirmHITL(true)"
              >
                确认
              </el-button>
              <el-button size="small" :disabled="resuming" @click="confirmHITL(false)">
                取消
              </el-button>
            </div>
          </div>
        </div>
        <div v-if="streaming" class="chat-typing">▍正在生成…</div>
      </div>
    </MfCard>

    <!-- ==================== 下方两栏：最近生成 / 我的项目 ==================== -->
    <div class="bottom-grid">
      <!-- 最近生成（会话列表，点击回放） -->
      <MfCard class="bottom-card">
        <template #header><span>最近生成</span></template>
        <template #extra>
          <el-button link type="primary" size="small" @click="router.push('/projects')">我的项目</el-button>
        </template>
        <el-skeleton v-if="threadsLoading" :rows="4" animated />
        <el-empty
          v-else-if="threads.length === 0"
          description="完成一次对话后，会话会出现在这里"
          :image-size="80"
        />
        <ul v-else class="thread-list">
          <li v-for="t in threads" :key="t.thread_id" class="thread-item" @click="openReplay(t)">
            <span class="thread-id">{{ t.thread_id }}</span>
            <MfTag v-if="t.stage" variant="brand" size="sm">{{ t.stage }}</MfTag>
            <span class="thread-time">{{ t.updated_at?.slice(5, 16).replace('T', ' ') ?? '' }}</span>
            <!-- 历史检查点入口（Time Travel，Task 10.4） -->
            <el-button
              link
              type="primary"
              size="small"
              class="thread-history-btn"
              @click.stop="openHistory(t)"
            >
              检查点
            </el-button>
          </li>
        </ul>
      </MfCard>

      <!-- 我的项目（前 4 个 + 新建） -->
      <MfCard class="bottom-card">
        <template #header><span>我的项目</span></template>
        <template #extra>
          <MfButton size="sm" @click="newProjectVisible = true">＋ 新建项目</MfButton>
        </template>
        <el-skeleton v-if="projectsLoading" :rows="4" animated />
        <el-empty
          v-else-if="projects.length === 0"
          description="还没有项目，点「新建项目」开始"
          :image-size="80"
        >
          <MfButton @click="newProjectVisible = true">新建项目</MfButton>
        </el-empty>
        <ul v-else class="project-list">
          <li v-for="p in projects" :key="p.id" class="project-item" @click="router.push(`/projects/${p.id}/script`)">
            <span class="project-cover" aria-hidden="true" />
            <span class="project-title">{{ p.title }}</span>
            <MfTag variant="neutral" size="sm">{{ p.status }}</MfTag>
            <span class="project-time">{{ p.updated_at?.slice(5, 16).replace('T', ' ') ?? '' }}</span>
          </li>
        </ul>
      </MfCard>
    </div>

    <!-- 会话回放弹窗 -->
    <el-dialog v-model="replayVisible" :title="`会话回放 · ${replayThreadId}`" width="640px" align-center>
      <el-skeleton v-if="replayVisible && replayMessages.length === 0" :rows="5" animated />
      <div v-else class="replay-body">
        <div
          v-for="(m, i) in replayMessages"
          :key="i"
          class="chat-line"
          :class="m.role === 'user' ? 'chat-line--user' : 'chat-line--assistant'"
        >
          <span class="chat-bubble">{{ m.content }}</span>
        </div>
      </div>
    </el-dialog>

    <!-- 历史检查点抽屉（Time Travel，Task 10.4） -->
    <el-drawer
      v-model="historyVisible"
      :title="`历史检查点 · ${historyThreadId}`"
      size="440px"
      append-to-body
    >
      <el-skeleton v-if="historyLoading" :rows="6" animated />
      <el-empty
        v-else-if="checkpoints.length === 0"
        description="暂无可回溯的检查点（对话后自动生成）"
        :image-size="80"
      />
      <ul v-else class="checkpoint-list">
        <li
          v-for="(cp, i) in checkpoints"
          :key="cp.checkpoint_id ?? `cp-${i}`"
          class="checkpoint-item"
        >
          <div class="checkpoint-meta">
            <el-tag size="small" effect="plain">step {{ cp.step ?? '—' }}</el-tag>
            <span v-if="cp.stage" class="checkpoint-stage">{{ cp.stage }}</span>
            <span class="checkpoint-time">{{ cp.ts?.slice(0, 19).replace('T', ' ') ?? '' }}</span>
          </div>
          <div class="checkpoint-summary">{{ cp.summary || `（${cp.messages_count} 条消息）` }}</div>
          <el-button
            size="small"
            type="warning"
            plain
            :loading="rewindingId === cp.checkpoint_id"
            @click="rewindTo(cp)"
          >
            回退到此
          </el-button>
        </li>
      </ul>
    </el-drawer>

    <!-- 新建项目弹窗（S-01） -->
    <NewProjectDialog v-model="newProjectVisible" @created="refreshProjects" />
  </div>
</template>

<style scoped>
.workspace {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-6);
}

/* ---------- 渠道健康标红条（Task 10.2） ---------- */
.health-alert {
  border-radius: var(--mf-r-ctl);
}

/* ---------- 意图承接条 ---------- */
.intent-alert {
  border-radius: var(--mf-r-ctl);
}
.intent-text {
  margin-right: var(--mf-sp-3);
}
.intent-btn {
  margin-left: var(--mf-sp-3);
}

/* ---------- Agent 首屏 ---------- */
.agent-hero {
  text-align: center;
}
.agent-title {
  margin: var(--mf-sp-4) 0 0;
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
}
.agent-sub {
  margin: var(--mf-sp-2) 0 var(--mf-sp-6);
  color: var(--mf-text-2);
}
.agent-card {
  width: min(760px, 100%);
  margin: 0 auto;
  padding: var(--mf-sp-4);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  box-shadow: var(--mf-shadow);
  text-align: left;
}
.agent-input {
  width: 100%;
  border: none;
  background: transparent;
  color: var(--mf-text);
  font-family: inherit;
  font-size: var(--mf-fs-base);
  line-height: 1.6;
  resize: vertical;
}
.agent-input:focus {
  outline: none;
}
.ref-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: var(--mf-sp-2);
}
.ref-chip {
  padding: 2px 10px;
  border-radius: var(--mf-r-pill);
  background: var(--mf-brand-soft);
  color: var(--mf-brand);
  font-size: var(--mf-fs-xs);
}
.agent-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-3);
  margin-top: var(--mf-sp-2);
}
.agent-toolbar__left,
.agent-toolbar__right {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.skill-select {
  width: 118px;
}
.tool-btn {
  width: 28px;
  height: 28px;
  border: 1px solid var(--mf-border-strong);
  border-radius: var(--mf-r-pill);
  background: transparent;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-lg);
  line-height: 1;
  cursor: pointer;
  transition: all var(--mf-dur) var(--mf-ease);
}
.tool-btn:hover {
  color: var(--mf-brand);
  border-color: var(--mf-brand);
}
.send-btn {
  width: 36px;
  height: 36px;
  border: none;
  border-radius: var(--mf-r-pill);
  background: var(--mf-brand);
  color: var(--mf-brand-contrast);
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-bold);
  cursor: pointer;
  transition: filter var(--mf-dur) var(--mf-ease);
}
.send-btn:hover {
  filter: brightness(1.08);
}
.stop-btn {
  height: 36px;
  padding: 0 var(--mf-sp-4);
  border: 1px solid var(--mf-danger);
  border-radius: var(--mf-r-pill);
  background: var(--mf-danger-soft);
  color: var(--mf-danger);
  font-size: var(--mf-fs-sm);
  cursor: pointer;
  font-family: inherit;
  transition: filter var(--mf-dur) var(--mf-ease);
}
.stop-btn:hover {
  filter: brightness(1.1);
}

/* 分类快捷 chips */
.chip-row {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--mf-sp-2);
  margin-top: var(--mf-sp-5);
}
.quick-chip {
  border: 1px solid var(--mf-border-strong);
  background: var(--mf-surface);
  border-radius: var(--mf-r-pill);
  padding: 7px 16px;
  font-size: var(--mf-fs-sm);
  color: var(--mf-text-2);
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.quick-chip:hover {
  color: var(--mf-brand);
  border-color: var(--mf-brand);
  background: var(--mf-brand-soft);
}

/* ---------- 流式回复区 ---------- */
.chat-card {
  width: min(960px, 100%);
  margin: 0 auto;
}
.chat-body {
  max-height: 320px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.chat-line {
  display: flex;
}
.chat-line--user {
  justify-content: flex-end;
}
.chat-line--assistant {
  justify-content: flex-start;
}
.chat-bubble {
  max-width: 82%;
  padding: 10px 14px;
  border-radius: var(--mf-r-ctl);
  font-size: var(--mf-fs-md);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.chat-line--user .chat-bubble {
  background: var(--mf-brand-soft);
  color: var(--mf-text);
}
.chat-line--assistant .chat-bubble {
  background: var(--mf-surface-2);
  color: var(--mf-text);
}
.chat-typing {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}

/* HITL 确认卡片（Task 10.4） */
.hitl-card {
  max-width: 82%;
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-warn, #d97706);
  background: var(--mf-warn-soft, rgba(217, 119, 6, 0.08));
  border-radius: var(--mf-r-ctl);
}
.hitl-title {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-sm);
  display: flex;
  align-items: center;
  gap: 6px;
}
.hitl-payload {
  margin: var(--mf-sp-2) 0;
  padding: var(--mf-sp-2);
  max-height: 140px;
  overflow-y: auto;
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  white-space: pre-wrap;
  word-break: break-all;
}
.hitl-actions {
  display: flex;
  gap: var(--mf-sp-2);
}

/* ---------- 下方两栏 ---------- */
.bottom-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--mf-sp-5);
  align-items: start;
}
.thread-list,
.project-list {
  margin: 0;
  padding: 0;
  list-style: none;
}
.thread-item,
.project-item {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-3) var(--mf-sp-2);
  border-radius: var(--mf-r-ctl);
  cursor: pointer;
  transition: background var(--mf-dur) var(--mf-ease);
}
.thread-item:hover,
.project-item:hover {
  background: var(--mf-surface-2);
}
.thread-item + .thread-item,
.project-item + .project-item {
  border-top: 1px solid var(--mf-border);
}
.thread-id {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-sm);
}
.thread-time,
.project-time {
  margin-left: auto;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
.project-cover {
  width: 36px;
  height: 36px;
  flex: none;
  border-radius: var(--mf-r-ctl);
  background: linear-gradient(135deg, var(--mf-brand), var(--mf-info));
}
.project-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--mf-fs-md);
}

/* 会话回放弹窗 */
.replay-body {
  max-height: 60vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}

/* 历史检查点抽屉（Task 10.4） */
.thread-history-btn {
  flex: none;
}
.checkpoint-list {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-3);
}
.checkpoint-item {
  padding: var(--mf-sp-3);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
}
.checkpoint-meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.checkpoint-stage {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-xs);
}
.checkpoint-time {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.checkpoint-summary {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.6;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* 窄屏：两栏变一栏 */
@media (max-width: 960px) {
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}
</style>
