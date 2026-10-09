<!--
  剧本创作页（文件级注释）。

  对应原型 S-02，五阶段之「① 剧本」，路由 /projects/:id/script。
  功能：
  - 左栏「原始输入」：已有剧本时只读展示；无剧本时三 Tab（一句话/小说文本/大纲）快速录入
    并调 saveInput(mode, raw_text)；
  - 右栏「AI 生成」：generateStream（POST SSE）增量追加流式文本（打字机光标 + 自动滚动），
    支持 AbortController 中断；error 事件展示警告条；done 后刷新剧本详情；
  - 结构化展示：大纲导航（锚点定位）+ 场景卡列表（场景号/标题/剧情/对白表/时长）；
    content 兼容三种落库形态：字段级 {"value","source"} 包装（AI 生成）、
    整体 {"value","source"} 包装（手动保存）、{"raw"} 纯文本；
  - 场景卡「编辑」：el-dialog 内以 JSON 文本编辑该场景，保存走 saveScript；
  - AI 助手：选粒度 → agent.chatStream 流式给出建议（抽屉展示，标注需手动采纳）；
  - 剧本体检：scriptHealth（修正版 POST）→ 得分 + 问题列表，点击定位到对应场景卡；
  - 底部主按钮「确认剧本，进入实体提取」→ 跳转 /projects/:id/script/extract。
-->
<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Lock } from '@element-plus/icons-vue'
import * as studioApi from '@/api/studio'
import * as agentApi from '@/api/agent'
import { useProjectStore } from '@/stores/project'
import type { ScriptContent, ScriptOut } from '@/types'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

// ============================================================
// 剧本加载状态
// ============================================================

/** 页面首屏加载中（骨架屏） */
const loading = ref(true)
/** 剧本详情（含 content/raw_text/version） */
const script = ref<ScriptOut | null>(null)
/** 项目尚无剧本行（404）时为 true，展示三 Tab 录入 */
const noScript = ref(false)

/** 解析后的场景条目（宽松结构，兼容 LLM 输出） */
interface ParsedScene {
  /** 场景序号 */
  scene_no: number
  /** 场景标题 */
  title: string
  /** 场景剧情描述 */
  plot: string
  /** 对白列表（角色名 + 台词） */
  dialogue: { character: string; line: string }[]
  /** 时长（秒） */
  duration_sec: number | null
}

/** 解析后的大纲条目 */
interface ParsedOutline {
  /** 幕名（第一幕…） */
  act: string
  /** 本幕概要 */
  summary: string
}

/** 剧本内容的统一解析结果 */
interface ParsedScript {
  /** 是否为纯文本形态（content.raw） */
  isRaw: boolean
  /** 纯文本内容（isRaw 时有效） */
  rawText: string
  /** 剧本标题 */
  title: string
  /** 一句话梗概 */
  logline: string
  /** 三幕大纲 */
  outline: ParsedOutline[]
  /** 场景列表 */
  scenes: ParsedScene[]
  /** 解包后的纯结构（编辑保存时整体提交） */
  plain: Record<string, unknown> | null
}

/** 当前解析结果（无剧本时为 null） */
const parsed = ref<ParsedScript | null>(null)

/**
 * 字段级解包：{"value": x, "source": ...} 包装 → x，其余原样返回。
 * @param v 任意字段值
 * @returns 解包后的值
 */
function unwrapField(v: unknown): unknown {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    const rec = v as Record<string, unknown>
    if ('value' in rec && 'source' in rec) return rec.value
  }
  return v
}

/**
 * 把后端 content 统一解析为可渲染结构。
 * 兼容三种形态：字段级包装 / 整体包装（手动保存） / {"raw": 文本} 纯文本。
 * @param content 剧本 content JSONB
 * @returns 解析结果；content 为空时返回 null
 */
function normalizeScript(content: ScriptContent | null | undefined): ParsedScript | null {
  if (!content) return null
  const c = content as Record<string, unknown>
  let plain: Record<string, unknown>
  if (typeof c.value === 'object' && c.value !== null && !Array.isArray(c.value) && 'source' in c) {
    // 手动保存后的整体包装：{"value": {...}, "source": "user"}
    plain = c.value as Record<string, unknown>
  } else {
    // AI 生成后的字段级包装（或首次录入仅 input_mode）
    plain = {}
    for (const [k, v] of Object.entries(c)) plain[k] = unwrapField(v)
  }
  const empty: ParsedScript = {
    isRaw: false, rawText: '', title: '', logline: '', outline: [], scenes: [], plain,
  }
  // 纯文本形态：JSON 解析失败时后端落库 {"raw": 全文}
  if (typeof plain.raw === 'string') {
    return { ...empty, isRaw: true, rawText: plain.raw }
  }
  const outlineRaw = Array.isArray(plain.outline) ? plain.outline : []
  const scenesRaw = Array.isArray(plain.scenes) ? plain.scenes : []
  return {
    ...empty,
    title: typeof plain.title === 'string' ? plain.title : '',
    logline: typeof plain.logline === 'string' ? plain.logline : '',
    outline: outlineRaw.map((o) => {
      const rec = (o ?? {}) as Record<string, unknown>
      return { act: String(rec.act ?? ''), summary: String(rec.summary ?? '') }
    }),
    scenes: scenesRaw.map((s, i) => {
      const rec = (s ?? {}) as Record<string, unknown>
      const dl = Array.isArray(rec.dialogue) ? rec.dialogue : []
      return {
        scene_no: Number(rec.scene_no ?? i + 1) || i + 1,
        title: String(rec.title ?? `场景 ${i + 1}`),
        plot: String(rec.plot ?? ''),
        dialogue: dl.map((d) => {
          const dr = (d ?? {}) as Record<string, unknown>
          return { character: String(dr.character ?? ''), line: String(dr.line ?? '') }
        }),
        duration_sec: rec.duration_sec != null ? Number(rec.duration_sec) : null,
      }
    }),
  }
}

/**
 * 拉取剧本详情并解析（无剧本行 404 时进入录入空态）。
 * @returns 无
 */
async function loadScript(): Promise<void> {
  loading.value = true
  try {
    script.value = await studioApi.getInput(projectId.value)
    noScript.value = false
    parsed.value = normalizeScript(script.value.content)
  } catch (e) {
    // 404 = 项目尚无剧本行，属正常空态（全局拦截器已提示一次），切录入视图
    const status = (e as { response?: { status?: number } }).response?.status
    if (status === 404) {
      noScript.value = true
      script.value = null
      parsed.value = null
    }
  } finally {
    loading.value = false
  }
}

// ============================================================
// 原始输入（三 Tab 快速录入）
// ============================================================

/** 录入模式：idea 一句话 / novel 小说文本 / outline 大纲 */
const inputMode = ref<'idea' | 'novel' | 'outline'>('idea')
/** 录入文本 */
const inputText = ref('')
/** 录入保存中（防重复提交） */
const savingInput = ref(false)

/**
 * 保存原始输入，成功后进入生成视图。
 * @returns 无
 */
async function saveInput(): Promise<void> {
  if (!inputText.value.trim()) {
    ElMessage.warning('请先输入内容')
    return
  }
  savingInput.value = true
  try {
    script.value = await studioApi.saveInput(projectId.value, {
      mode: inputMode.value,
      raw_text: inputText.value.trim(),
    })
    noScript.value = false
    parsed.value = normalizeScript(script.value.content)
    ElMessage.success('原始输入已保存，可开始 AI 生成')
  } finally {
    savingInput.value = false
  }
}

// ============================================================
// AI 流式生成
// ============================================================

/** 是否生成中（流式进行时禁用按钮、显示光标动画与中断按钮） */
const generating = ref(false)
/** 流式累积文本（打字机区域） */
const streamText = ref('')
/** SSE error 事件文案（警告条） */
const genError = ref('')
/** 生成用的 AbortController（「中断」按钮触发 abort） */
let abortCtrl: AbortController | null = null
/** 流式区滚动容器引用 */
const streamBox = ref<HTMLElement | null>(null)

// 流式文本变化时自动滚动到底部
watch(streamText, () => {
  void nextTick(() => {
    if (streamBox.value) streamBox.value.scrollTop = streamBox.value.scrollHeight
  })
})

/**
 * 开始 AI 流式生成剧本（POST SSE）。
 * 事件：delta 增量追加；error 展示警告条；done 刷新详情。
 * @returns 无
 */
async function startGenerate(): Promise<void> {
  if (generating.value) return // 防重复点击
  streamText.value = ''
  genError.value = ''
  generating.value = true
  abortCtrl = new AbortController()
  try {
    await studioApi.generateStream(
      projectId.value,
      {},
      (event, data) => {
        const d = (data ?? {}) as Record<string, unknown>
        if (event === 'delta' && typeof d.text === 'string') {
          streamText.value += d.text
        } else if (event === 'error') {
          genError.value = typeof d.message === 'string' ? d.message : '生成过程出现异常'
        } else if (event === 'done') {
          // done 负载含最新 content，直接刷新详情视图
          void loadScript()
        }
      },
      abortCtrl.signal,
    )
  } catch (e) {
    // 用户主动中断不提示错误
    if ((e as Error).name !== 'AbortError') {
      ElMessage.error((e as Error).message || '生成失败，请稍后重试')
    }
  } finally {
    generating.value = false
    abortCtrl = null
    // 中断/结束后内容可能已落库（done 前被截断时），兜底刷新一次
    void loadScript()
  }
}

/**
 * 中断当前生成（AbortController；已产出的文本保留在流式区，落库内容不受影响）。
 * @returns 无
 */
function stopGenerate(): void {
  abortCtrl?.abort()
}

// ============================================================
// 场景编辑（JSON 文本编辑单个场景）
// ============================================================

/** 编辑弹窗可见性 */
const editVisible = ref(false)
/** 正在编辑的场景下标（scenes 数组内） */
const editIndex = ref(-1)
/** 编辑弹窗内的 JSON 文本 */
const editJsonText = ref('')
/** 保存编辑中（防重复提交） */
const savingScene = ref(false)

/**
 * 打开某场景的编辑弹窗（该场景序列化为 JSON 供修改）。
 * @param index 场景下标
 * @returns 无
 */
function openSceneEdit(index: number): void {
  if (!parsed.value) return
  editIndex.value = index
  editJsonText.value = JSON.stringify(parsed.value.scenes[index], null, 2)
  editVisible.value = true
}

/**
 * 保存场景编辑：解析 JSON → 回写 scenes → saveScript 整体提交（source=user）。
 * @returns 无
 * @throws JSON 解析失败时提示并保留弹窗
 */
async function saveSceneEdit(): Promise<void> {
  if (!parsed.value || editIndex.value < 0) return
  let sceneObj: unknown
  try {
    sceneObj = JSON.parse(editJsonText.value)
  } catch {
    ElMessage.error('JSON 格式错误，请检查后再保存')
    return
  }
  savingScene.value = true
  try {
    // 提交解包后的纯结构（后端会整体包装为 {"value":…, "source":"user"}）
    const plain = { ...(parsed.value.plain ?? {}) }
    const scenes = [...parsed.value.scenes]
    scenes[editIndex.value] = sceneObj as ParsedScene
    plain.scenes = scenes
    script.value = await studioApi.saveScript(projectId.value, { content: plain as ScriptContent })
    parsed.value = normalizeScript(script.value.content)
    editVisible.value = false
    ElMessage.success('场景已保存')
  } finally {
    savingScene.value = false
  }
}

// ============================================================
// AI 助手（剧本 Agent 建议，抽屉展示）
// ============================================================

/** 助手抽屉可见性 */
const assistantVisible = ref(false)
/** 优化粒度（润色对白/扩写场景/精简全文） */
const granularity = ref('润色对白')
/** 助手补充指令 */
const assistantPrompt = ref('')
/** 助手流式回复文本 */
const assistantReply = ref('')
/** 助手请求进行中（防重复点击） */
const chatting = ref(false)
/** 助手用的 AbortController */
let chatAbort: AbortController | null = null
/** 助手回复区滚动容器 */
const assistantBox = ref<HTMLElement | null>(null)

// 助手回复变化时自动滚动
watch(assistantReply, () => {
  void nextTick(() => {
    if (assistantBox.value) assistantBox.value.scrollTop = assistantBox.value.scrollHeight
  })
})

/**
 * 打开助手抽屉。
 * @returns 无
 */
function openAssistant(): void {
  assistantVisible.value = true
}

/**
 * 让剧本 Agent 生成优化建议（api/agent.chatStream 流式），结果仅作建议展示。
 * @returns 无
 */
async function askAssistant(): Promise<void> {
  if (chatting.value) return // 防重复点击
  const digest = parsed.value
    ? `剧本《${parsed.value.title || '未命名'}》：${parsed.value.logline}。共 ${parsed.value.scenes.length} 个场景。`
    : (script.value?.raw_text ?? '')
  assistantReply.value = ''
  chatting.value = true
  assistantVisible.value = true
  chatAbort = new AbortController()
  try {
    await agentApi.chatStream(
      {
        message: `请对以下漫剧剧本进行「${granularity.value}」，并给出建议内容（不要直接改写原剧本）：${digest}。补充要求：${assistantPrompt.value || '无'}`,
        project_id: projectId.value,
      },
      (event, data) => {
        const d = (data ?? {}) as Record<string, unknown>
        if (event === 'delta' && typeof d.text === 'string') {
          assistantReply.value += d.text
        } else if (event === 'error') {
          ElMessage.warning(typeof d.message === 'string' ? d.message : '助手服务异常')
        }
      },
      chatAbort.signal,
    )
  } catch (e) {
    if ((e as Error).name !== 'AbortError') {
      ElMessage.error((e as Error).message || '助手请求失败')
    }
  } finally {
    chatting.value = false
    chatAbort = null
  }
}

// ============================================================
// 剧本体检
// ============================================================

/** 体检请求进行中 */
const healthLoading = ref(false)
/** 体检问题条目（宽松结构） */
interface HealthIssue {
  /** 级别 error/warning */
  level: string
  /** 问题描述 */
  message: string
  /** 定位描述（如「场景2」） */
  location: string
}
/** 体检得分（null 表示未体检） */
const healthScore = ref<number | null>(null)
/** 体检问题列表 */
const healthIssues = ref<HealthIssue[]>([])

/**
 * 发起剧本体检并展示结果面板。
 * @returns 无
 */
async function runHealth(): Promise<void> {
  if (healthLoading.value) return
  healthLoading.value = true
  try {
    const report = await studioApi.scriptHealth(projectId.value)
    healthScore.value = typeof report.score === 'number' ? report.score : null
    healthIssues.value = Array.isArray(report.issues)
      ? (report.issues as Record<string, unknown>[]).map((i) => ({
          level: String(i.level ?? 'warning'),
          message: String(i.message ?? ''),
          location: String(i.location ?? ''),
        }))
      : []
    ElMessage.success(healthIssues.value.length ? '体检完成，存在问题待处理' : '体检完成，未发现问题')
  } finally {
    healthLoading.value = false
  }
}

/**
 * 点击体检问题定位到对应场景卡（location 形如「场景N」时滚动至该卡片）。
 * @param location 体检定位描述
 * @returns 无
 */
function locateIssue(location: string): void {
  const m = location.match(/(\d+)/)
  if (!m) return
  const target = document.getElementById(`scene-card-${m[1]}`)
  target?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

// ============================================================
// 确认剧本
// ============================================================

/**
 * 确认剧本，进入实体提取页。
 * @returns 无
 */
async function confirmScript(): Promise<void> {
  if (!parsed.value || parsed.value.isRaw || parsed.value.scenes.length === 0) {
    // 无结构化场景时二次确认，提示下游拆解依赖结构化内容
    try {
      await ElMessageBox.confirm(
        '当前剧本暂无结构化场景数据，实体提取与分镜拆解效果可能受限。是否继续？',
        '确认剧本',
        { type: 'warning', confirmButtonText: '仍要继续', cancelButtonText: '返回编辑' },
      )
    } catch {
      return
    }
  }
  await router.push(`/projects/${projectId.value}/script/extract`)
}

// 剧本已生成标记（有结构化场景或纯文本均视为已有内容）
const hasContent = computed(
  () => !!parsed.value && (parsed.value.isRaw || parsed.value.scenes.length > 0),
)

onMounted(loadScript)
onBeforeUnmount(() => {
  // 离开页面中止进行中的流式请求
  abortCtrl?.abort()
  chatAbort?.abort()
})
</script>

<template>
  <div class="page-container script-view">
    <div class="view-head">
      <h2 class="page-title">剧本创作</h2>
      <span v-if="script" class="ver-tag">v{{ script.version }}</span>
      <span v-if="projectStore.currentProject?.target_duration" class="dur-tag">
        目标时长 {{ projectStore.currentProject.target_duration }}s
      </span>
    </div>

    <!-- 首屏骨架 -->
    <el-skeleton v-if="loading" :rows="8" animated />

    <template v-else>
      <!-- ========== 无剧本：三 Tab 快速录入 ========== -->
      <el-card v-if="noScript" shadow="never" class="input-card">
        <template #header>
          <div class="card-head">
            <span>原始输入</span>
            <span class="head-tip">尚未创建剧本，请选择一种方式录入创作素材</span>
          </div>
        </template>
        <el-tabs v-model="inputMode">
          <el-tab-pane label="一句话灵感" name="idea">
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="4"
              maxlength="500"
              show-word-limit
              placeholder="例：深夜便利店，失眠的都市女孩与值夜班店员的一场温暖相遇"
            />
          </el-tab-pane>
          <el-tab-pane label="小说文本" name="novel">
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="8"
              maxlength="8000"
              show-word-limit
              placeholder="粘贴小说片段或短篇故事原文，AI 将据此改编为漫剧剧本"
            />
          </el-tab-pane>
          <el-tab-pane label="故事大纲" name="outline">
            <el-input
              v-model="inputText"
              type="textarea"
              :rows="8"
              maxlength="4000"
              show-word-limit
              placeholder="按幕/按场罗列你的故事大纲，AI 将扩写为完整剧本"
            />
          </el-tab-pane>
        </el-tabs>
        <div class="input-actions">
          <el-button type="primary" :loading="savingInput" @click="saveInput">保存输入</el-button>
        </div>
      </el-card>

      <!-- ========== 双栏：左原始输入 / 右 AI 生成 ========== -->
      <div v-else class="two-col">
        <!-- 左栏：原始输入 -->
        <el-card shadow="never" class="col-left">
          <template #header>
            <div class="card-head"><span>原始输入</span></div>
          </template>
          <div class="raw-text">{{ script?.raw_text || '（无原始输入）' }}</div>
          <div class="col-left__ops">
            <el-button text type="primary" @click="noScript = true; inputText = script?.raw_text ?? ''">
              修改输入
            </el-button>
          </div>
        </el-card>

        <!-- 右栏：AI 生成 -->
        <el-card shadow="never" class="col-right">
          <template #header>
            <div class="card-head">
              <span>AI 生成</span>
              <div class="head-ops">
                <template v-if="generating">
                  <el-button type="danger" plain size="small" @click="stopGenerate">中断</el-button>
                </template>
                <el-button
                  v-else
                  type="primary"
                  size="small"
                  :loading="generating"
                  @click="startGenerate"
                >
                  {{ hasContent ? '重新生成' : '开始生成' }}
                </el-button>
              </div>
            </div>
          </template>

          <!-- 生成失败/降级警告条 -->
          <el-alert
            v-if="genError"
            :title="genError"
            type="warning"
            show-icon
            closable
            class="gen-alert"
            @close="genError = ''"
          />

          <!-- 流式输出区（生成中可见，含打字机光标） -->
          <div v-if="generating || streamText" ref="streamBox" class="stream-box">
            <span class="stream-text">{{ streamText || '正在连接模型…' }}</span><span
              v-if="generating"
              class="cursor"
            >▍</span>
          </div>
          <el-empty
            v-else-if="!hasContent"
            description="还没有剧本内容，点击右上角「开始生成」，AI 将根据原始输入流式创作"
          />
        </el-card>
      </div>

      <!-- ========== 结构化剧本展示 ========== -->
      <template v-if="parsed && parsed.isRaw">
        <el-alert
          class="raw-alert"
          type="info"
          show-icon
          :closable="false"
          title="当前剧本为纯文本形态（结构化解析未通过），请重新生成以获得场景结构"
        />
        <el-card shadow="never"><pre class="raw-pre">{{ parsed.rawText }}</pre></el-card>
      </template>

      <div v-if="parsed && !parsed.isRaw" class="structure">
        <!-- 左：大纲导航 -->
        <el-card shadow="never" class="nav-card">
          <template #header>
            <div class="card-head"><span>大纲导航</span></div>
          </template>
          <el-empty v-if="!parsed.outline.length && !parsed.scenes.length" description="暂无大纲" />
          <template v-else>
            <div v-if="parsed.title" class="nav-title">《{{ parsed.title }}》</div>
            <div v-if="parsed.logline" class="nav-logline">{{ parsed.logline }}</div>
            <div v-for="(o, i) in parsed.outline" :key="`o-${i}`" class="nav-item">
              <span class="nav-act">{{ o.act }}</span>
              <span class="nav-summary">{{ o.summary }}</span>
            </div>
            <el-divider>场景</el-divider>
            <a
              v-for="(s, i) in parsed.scenes"
              :key="`sn-${i}`"
              class="nav-scene"
              :href="`#scene-card-${s.scene_no}`"
            >
              第{{ s.scene_no }}场 · {{ s.title }}
            </a>
          </template>
        </el-card>

        <!-- 右：场景卡列表 -->
        <div class="scenes-col">
          <el-card
            v-for="(s, i) in parsed.scenes"
            :id="`scene-card-${s.scene_no}`"
            :key="i"
            shadow="never"
            class="scene-card"
          >
            <template #header>
              <div class="card-head">
                <span class="scene-no">场景 {{ s.scene_no }}</span>
                <span class="scene-title">{{ s.title }}</span>
                <el-tag v-if="s.duration_sec" size="small" type="info" effect="plain">
                  约 {{ s.duration_sec }}s
                </el-tag>
                <el-button size="small" text type="primary" @click="openSceneEdit(i)">编辑</el-button>
              </div>
            </template>
            <p class="scene-plot">{{ s.plot || '（无剧情描述）' }}</p>
            <el-table v-if="s.dialogue.length" :data="s.dialogue" size="small" class="dlg-table">
              <el-table-column prop="character" label="角色" width="120" />
              <el-table-column prop="line" label="台词" />
            </el-table>
            <el-empty v-else description="本场景暂无对白" :image-size="48" />
          </el-card>
          <el-empty v-if="!parsed.scenes.length" description="剧本中暂无场景数据" />
        </div>
      </div>

      <!-- ========== 体检结果面板 ========== -->
      <el-card v-if="healthScore !== null" shadow="never" class="health-card">
        <template #header>
          <div class="card-head">
            <span>剧本体检</span>
            <el-tag
              :type="healthScore >= 80 ? 'success' : healthScore >= 60 ? 'warning' : 'danger'"
              effect="dark"
            >
              得分 {{ healthScore }}
            </el-tag>
          </div>
        </template>
        <el-empty v-if="!healthIssues.length" description="未发现问题" :image-size="60" />
        <div
          v-for="(iss, i) in healthIssues"
          :key="i"
          class="issue-row"
          role="button"
          @click="locateIssue(iss.location)"
        >
          <el-tag size="small" :type="iss.level === 'error' ? 'danger' : 'warning'">
            {{ iss.level === 'error' ? '错误' : '提醒' }}
          </el-tag>
          <span class="issue-msg">{{ iss.message }}</span>
          <span class="issue-loc">{{ iss.location }}</span>
          <el-button size="small" text type="primary">定位</el-button>
        </div>
      </el-card>

      <!-- ========== 底部操作条 ========== -->
      <div class="footer-bar">
        <div class="footer-left">
          <el-button :loading="healthLoading" @click="runHealth">剧本体检</el-button>
          <el-button :disabled="!hasContent" @click="openAssistant">AI 助手</el-button>
        </div>
        <el-button
          size="large"
          class="confirm-btn"
          :loading="generating"
          @click="confirmScript"
        >
          <el-icon v-if="false"><Lock /></el-icon>
          确认剧本，进入实体提取
        </el-button>
      </div>
    </template>

    <!-- ========== 场景编辑弹窗 ========== -->
    <el-dialog v-model="editVisible" title="编辑场景（JSON）" width="640px" append-to-body>
      <el-input
        v-model="editJsonText"
        type="textarea"
        :rows="16"
        spellcheck="false"
        placeholder='{"scene_no":1,"title":"…","plot":"…","dialogue":[{"character":"…","line":"…"}],"duration_sec":30}'
      />
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingScene" @click="saveSceneEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- ========== AI 助手抽屉 ========== -->
    <el-drawer v-model="assistantVisible" title="AI 剧本助手" size="420px" append-to-body>
      <div class="assistant-grain">
        <span>优化粒度：</span>
        <el-select v-model="granularity" style="width: 160px">
          <el-option label="润色对白" value="润色对白" />
          <el-option label="扩写场景" value="扩写场景" />
          <el-option label="精简全文" value="精简全文" />
        </el-select>
      </div>
      <el-input
        v-model="assistantPrompt"
        type="textarea"
        :rows="3"
        placeholder="补充优化指令（可空），例：对白更口语化"
        class="assistant-prompt"
      />
      <el-button type="primary" :loading="chatting" @click="askAssistant">生成建议</el-button>
      <el-divider />
      <div ref="assistantBox" class="assistant-box">
        <el-empty v-if="!assistantReply && !chatting" description="点击「生成建议」获取 AI 优化建议" />
        <span v-else>{{ assistantReply }}<span v-if="chatting" class="cursor">▍</span></span>
      </div>
      <el-alert
        class="assistant-tip"
        type="info"
        :closable="false"
        title="以上为建议内容，请手动采纳到剧本中（本助手不会直接改写剧本）"
      />
    </el-drawer>
  </div>
</template>

<style scoped>
.script-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.view-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.page-title {
  margin: 0;
}
.ver-tag,
.dur-tag {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.card-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
}
.head-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.head-ops {
  margin-left: auto;
}
.input-actions {
  margin-top: var(--mf-sp-4);
  text-align: right;
}
.two-col {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.6fr);
  gap: var(--mf-sp-5);
  align-items: start;
}
.raw-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--mf-text-2);
  min-height: 96px;
  max-height: 300px;
  overflow-y: auto;
}
.col-left__ops {
  margin-top: var(--mf-sp-3);
  text-align: right;
}
.gen-alert {
  margin-bottom: var(--mf-sp-3);
}
.stream-box {
  min-height: 160px;
  max-height: 420px;
  overflow-y: auto;
  padding: var(--mf-sp-3);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-surface-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
}
.cursor {
  display: inline-block;
  color: var(--mf-brand);
  animation: mf-blink 0.9s step-end infinite;
}
@keyframes mf-blink {
  50% {
    opacity: 0;
  }
}
.raw-alert {
  margin-bottom: var(--mf-sp-3);
}
.raw-pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--mf-font);
}
.structure {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: var(--mf-sp-5);
  align-items: start;
}
.nav-title {
  font-weight: var(--mf-fw-semibold);
  margin-bottom: var(--mf-sp-1);
}
.nav-logline {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin-bottom: var(--mf-sp-3);
}
.nav-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.nav-act {
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-brand);
  font-size: var(--mf-fs-sm);
}
.nav-summary {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.nav-scene {
  display: block;
  padding: var(--mf-sp-2) 0;
  color: var(--mf-text-2);
  text-decoration: none;
  font-size: var(--mf-fs-sm);
  border-bottom: 1px dashed var(--mf-border);
}
.nav-scene:hover {
  color: var(--mf-brand);
}
.scenes-col {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.scene-no {
  font-weight: var(--mf-fw-semibold);
  color: var(--mf-brand);
}
.scene-title {
  font-weight: var(--mf-fw-semibold);
}
.scene-card .card-head .el-button {
  margin-left: auto;
}
.scene-plot {
  margin: 0 0 var(--mf-sp-3);
  color: var(--mf-text-2);
  line-height: 1.8;
}
.dlg-table {
  width: 100%;
}
.health-card .issue-row {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
  cursor: pointer;
}
.issue-row:hover {
  color: var(--mf-brand);
}
.issue-msg {
  flex: 1;
}
.issue-loc {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.footer-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  position: sticky;
  bottom: 0;
  padding: var(--mf-sp-3);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.confirm-btn {
  background: var(--mf-brand);
  border-color: var(--mf-brand);
  color: var(--mf-bg);
}
.confirm-btn:hover {
  opacity: 0.9;
}
.assistant-grain {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-3);
}
.assistant-prompt {
  margin-bottom: var(--mf-sp-3);
}
.assistant-box {
  min-height: 180px;
  max-height: 400px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.8;
}
.assistant-tip {
  margin-top: var(--mf-sp-3);
}
</style>
