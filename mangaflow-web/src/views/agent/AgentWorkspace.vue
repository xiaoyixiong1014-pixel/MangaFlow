<!--
  AI 工作台 W-01（文件级注释）。
  登录后默认落地页：智能体大输入卡为第一屏（SSE 流式对话，可停止），
  意图承接条承接官网带入的创作意图；下方两栏为「最近生成」（会话回放）
  与「我的项目」（前 4 个 + 新建项目弹窗）。
  数据分区加载：输入框立即可用，不被下方数据加载阻塞。
-->
<script setup lang="ts">
import { nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MfButton, MfCard, MfTag } from '@mangaflow/design'
import { useAgentStore } from '@/stores/agent'
import * as agentApi from '@/api/agent'
import * as projectsApi from '@/api/projects'
import type { AgentMessageItem, AgentThread, Project } from '@/types'
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
 * 发送消息（核心）：SSE 流式对话，事件 stage / message / done。
 * 追加 user + assistant 消息，message 帧文本直接追加（打字机效果），
 * done 帧记录 thread_id 支持多轮；生成中可停止（AbortController）。
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
  messages.push({ role: 'user', content: text })
  const reply = reactive<AgentMessageItem>({ role: 'assistant', content: '' })
  messages.push(reply)
  streaming.value = true
  controller.value = new AbortController()
  try {
    await agentApi.chatStream(
      { message: text, thread_id: threadId.value ?? undefined },
      (event, data) => {
        if (event === 'message') {
          const payload = data as { text?: string }
          reply.content += payload?.text ?? ''
          scrollChatToBottom()
        } else if (event === 'stage') {
          const payload = data as { intent?: string }
          stageHint.value = payload?.intent ?? ''
        } else if (event === 'done') {
          const payload = data as { thread_id?: string }
          if (payload?.thread_id) threadId.value = payload.thread_id
        }
      },
      controller.value.signal,
    )
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

// 页面进入：并发拉取会话与项目（输入区不被阻塞）
onMounted(() => {
  refreshThreads()
  refreshProjects()
})
</script>

<template>
  <div class="page-container workspace">
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

/* 窄屏：两栏变一栏 */
@media (max-width: 960px) {
  .bottom-grid {
    grid-template-columns: 1fr;
  }
}
</style>
