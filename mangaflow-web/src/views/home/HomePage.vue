<!--
  官网首页 L-01（文件级注释）。
  未登录态为主的公开入口：首屏 Hero 为深色放映厅（真实生成剧照帧带 + 智能体对话框卡片），
  帧带为签名元素——真实平台产出素材直接为产品代言；向下依次为四步流程、双引擎、
  作品展示（真实素材画廊，项目《斗破苍穹》第 1 集产出）、CTA（帧带收束）与页脚。
  主转化链：输入 → 发送 → 未登录弹 AuthDialog（保留输入）→ 登录成功
  → agent.setIntent(输入) → 跳 /agent（W-01 意图承接条）。
  页面主体强制浅色（创作端默认深色）：挂载时临时把 <html data-theme> 切为 light，
  离开时恢复，避免污染全局主题持久化；Hero 深色为区块自有配色，不随主题变化。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MfButton, MfLogo } from '@mangaflow/design'
import { useAgentStore } from '@/stores/agent'
import AuthDialog from '@/components/AuthDialog.vue'

// ---------- 真实作品素材（平台 AI 生成实拍帧/角色定妆，构建期打包） ----------
import frame01 from '@/assets/showcase/frame-01.jpg'
import frame02 from '@/assets/showcase/frame-02.jpg'
import frame03 from '@/assets/showcase/frame-03.jpg'
import frame04 from '@/assets/showcase/frame-04.jpg'
import frame05 from '@/assets/showcase/frame-05.jpg'
import frame06 from '@/assets/showcase/frame-06.jpg'
import charXiaoyan from '@/assets/showcase/char-xiaoyan.jpg'
import charYaolao from '@/assets/showcase/char-yaolao.jpg'
import charXunr from '@/assets/showcase/char-xunr.jpg'
import charNalanyanran from '@/assets/showcase/char-nalanyanran.jpg'
import charXiaozhan from '@/assets/showcase/char-xiaozhan.jpg'

// ============================================================
// 动态星尘背景（canvas 轻粒子：缓慢漂移 + 闪烁）
// prefers-reduced-motion 时仅静态绘制一帧
// ============================================================
/** 星尘 canvas 引用 */
const starfieldRef = ref<HTMLCanvasElement>()
/** 动画帧句柄 */
let starRaf = 0
/** IntersectionObserver（流程区滚动显现） */
let flowObserver: IntersectionObserver | null = null

/** 单颗星尘 */
interface Star {
  x: number
  y: number
  /** 半径 px */
  r: number
  /** 漂移速度（px/s，含方向） */
  vx: number
  vy: number
  /** 闪烁相位与周期 */
  phase: number
  period: number
  /** 亮星（带辉光十字光芒，少数） */
  bright: boolean
}

/**
 * 绘制一帧星尘：清屏后按各自相位计算透明度画点；亮星带辉光。
 * @param ctx 2D 上下文
 * @param stars 星尘集合
 * @param w 画布宽
 * @param h 画布高
 * @param t 当前时间戳（ms）
 * @param dt 距上帧秒数
 */
function drawStars(ctx: CanvasRenderingContext2D, stars: Star[], w: number, h: number, t: number, dt: number): void {
  ctx.clearRect(0, 0, w, h)
  for (const s of stars) {
    s.x += s.vx * dt
    s.y += s.vy * dt
    // 越界回绕，保持密度恒定
    if (s.x < -6) s.x = w + 6
    if (s.x > w + 6) s.x = -6
    if (s.y < -6) s.y = h + 6
    if (s.y > h + 6) s.y = -6
    const tw = 0.4 + 0.6 * (0.5 + 0.5 * Math.sin((t / 1000) * (Math.PI * 2 / s.period) + s.phase))
    ctx.globalAlpha = tw
    if (s.bright) {
      // 亮星：带辉光与十字光芒，占少数
      ctx.shadowColor = 'rgba(123, 232, 220, 0.9)'
      ctx.shadowBlur = 10
      ctx.fillStyle = '#D9FBF4'
      ctx.beginPath()
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2)
      ctx.fill()
      ctx.shadowBlur = 0
      ctx.globalAlpha = tw * 0.5
      ctx.fillRect(s.x - s.r * 4, s.y - 0.5, s.r * 8, 1)
      ctx.fillRect(s.x - 0.5, s.y - s.r * 4, 1, s.r * 8)
    } else {
      ctx.fillStyle = '#9BE8DC'
      ctx.beginPath()
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2)
      ctx.fill()
    }
  }
  ctx.globalAlpha = 1
}

/**
 * 启动星尘背景：生成星点并按设备能力选择动画或静态绘制。
 * @returns 无
 */
function startStarfield(): void {
  const canvas = starfieldRef.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = canvas.clientWidth
  const h = canvas.clientHeight
  canvas.width = w * dpr
  canvas.height = h * dpr
  ctx.scale(dpr, dpr)
  const count = Math.min(110, Math.round((w * h) / 14000))
  const stars: Star[] = Array.from({ length: count }, () => ({
    x: Math.random() * w,
    y: Math.random() * h,
    r: 0.7 + Math.random() * 1.7,
    vx: (Math.random() - 0.5) * 9,
    vy: (Math.random() - 0.5) * 6,
    phase: Math.random() * Math.PI * 2,
    period: 2 + Math.random() * 3.4,
    bright: Math.random() < 0.1,
  }))
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  // 星尘漂移速度极慢（≤9px/s），属氛围层动画；产品要求背景始终动态，
  // reduced-motion 下仅将漂移速度降半，不做完全静止
  const speedScale = reduced ? 0.5 : 1
  for (const s of stars) {
    s.vx *= speedScale
    s.vy *= speedScale
  }
  let last = performance.now()
  const loop = (t: number): void => {
    const dt = Math.min((t - last) / 1000, 0.05)
    last = t
    drawStars(ctx, stars, w, h, t, dt)
    starRaf = requestAnimationFrame(loop)
  }
  starRaf = requestAnimationFrame(loop)
}

/**
 * 流程区滚动显现：进入视口加 in-view 类（CSS 过渡上浮淡入）。
 * 一次性短过渡（0.6s，非循环），不做 reduced-motion 降级——
 * 产品要求动态呈现，且单次位移过渡对动效敏感用户刺激极低。
 * @returns 无
 */
function observeFlow(): void {
  const rows = document.querySelectorAll('.flow-step')
  if (!('IntersectionObserver' in window)) {
    rows.forEach((r) => r.classList.add('flow-step--in'))
    return
  }
  flowObserver = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) {
          e.target.classList.add('flow-step--in')
          flowObserver?.unobserve(e.target)
        }
      }
    },
    { threshold: 0.22 },
  )
  rows.forEach((r) => flowObserver?.observe(r))
}

const router = useRouter()
const agentStore = useAgentStore()

/** 登录态标记（进入页面时一次性读取） */
const hasToken = !!localStorage.getItem('mf_token')

/** 登录/注册弹窗可见性 */
const authDialogVisible = ref(false)
/** 弹窗初始面板：login 登录 / register 注册 */
const authMode = ref<'login' | 'register'>('login')

/** 用户输入的创作需求（Hero 对话框） */
const intent = ref('')
/** 对话框模式单选：smart 智能 / script 剧本 / character 角色 / video 视频 / dubbing 配音 */
const chatMode = ref('smart')
/** 参考图文件名列表（仅前端视觉占位，不真实上传） */
const refFiles = ref<string[]>([])
/** 隐藏的文件选择 input 引用 */
const fileInputRef = ref<HTMLInputElement>()

/** 模式选项（切换仅影响提示文案） */
const MODE_OPTIONS = [
  { value: 'smart', label: '智能模式', placeholder: '描述你想创作的漫剧，例如：深夜便利店的相遇…' },
  { value: 'script', label: '剧本', placeholder: '描述剧本创意或粘贴你的故事梗概…' },
  { value: 'character', label: '角色', placeholder: '描述你想设计的角色形象与气质…' },
  { value: 'video', label: '视频', placeholder: '描述你想生成的画面 / 视频效果…' },
  { value: 'dubbing', label: '配音', placeholder: '粘贴需要配音的台词文本…' },
]
/** 当前模式对应的输入占位文案 */
const modePlaceholder = computed(() => MODE_OPTIONS.find((m) => m.value === chatMode.value)?.placeholder ?? '')

/** 快捷示例 chips（点击填充输入框，不直接发送） */
const QUICK_EXAMPLES = [
  '把这句话做成漫剧：深夜便利店的相遇',
  '生成一个赛博朋克少女角色',
  '帮我把这段小说改编成漫剧',
]

/** 输入上限（与原型一致） */
const MAX_LENGTH = 2000

/** 作品展示当前 Tab */
const workTab = ref('visual')
/** 作品展示 Tab 定义 */
const WORK_TABS = [
  { value: 'visual', label: '视觉画面' },
  { value: 'series', label: '剧集分镜' },
  { value: 'character', label: '角色定妆' },
]

/** 画廊作品项：真实平台生成素材（项目「斗破苍穹」第 1 集产出） */
interface WorkItem {
  /** 作品标题（真实项目名 + 素材类型，不虚构剧情） */
  title: string
  /** 类型标签 */
  tag: string
  /** 图片资源 */
  img: string
}

/** 视觉画面：成片关键帧（9:16 竖版剧照） */
const FRAMES: WorkItem[] = [
  { title: '斗破苍穹 · 关键帧', tag: '剧照', img: frame01 },
  { title: '斗破苍穹 · 关键帧', tag: '剧照', img: frame02 },
  { title: '斗破苍穹 · 关键帧', tag: '剧照', img: frame03 },
  { title: '斗破苍穹 · 关键帧', tag: '剧照', img: frame04 },
  { title: '斗破苍穹 · 关键帧', tag: '剧照', img: frame05 },
  { title: '斗破苍穹 · 关键帧', tag: '剧照', img: frame06 },
]

/** 剧集分镜：同一剧集的镜头序列（同素材另一组取景，标注镜号） */
const SERIES: WorkItem[] = [
  { title: '第 1 集 · 镜 04', tag: '分镜帧', img: frame04 },
  { title: '第 1 集 · 镜 06', tag: '分镜帧', img: frame06 },
  { title: '第 1 集 · 镜 02', tag: '分镜帧', img: frame02 },
  { title: '第 1 集 · 镜 05', tag: '分镜帧', img: frame05 },
  { title: '第 1 集 · 镜 03', tag: '分镜帧', img: frame03 },
  { title: '第 1 集 · 镜 01', tag: '分镜帧', img: frame01 },
]

/** 角色定妆：角色资产三视图定妆照 */
const CHARACTERS: WorkItem[] = [
  { title: '萧炎 · 定妆', tag: '三视图', img: charXiaoyan },
  { title: '药老 · 定妆', tag: '三视图', img: charYaolao },
  { title: '萧薰儿 · 定妆', tag: '三视图', img: charXunr },
  { title: '纳兰嫣然 · 定妆', tag: '三视图', img: charNalanyanran },
  { title: '萧战 · 定妆', tag: '三视图', img: charXiaozhan },
]

/** 画廊数据源：三个 Tab 均为真实平台产出素材 */
const GALLERY: Record<string, WorkItem[]> = {
  visual: FRAMES,
  series: SERIES,
  character: CHARACTERS,
}

/** 当前 Tab 的画廊数据（类型安全兜底为空数组） */
const gallery = computed(() => GALLERY[workTab.value] ?? [])

/** Hero 成片帧带：三张代表性竖版剧照（真实生成帧，倾斜胶片排布） */
const HERO_FRAMES = [frame01, frame05, frame03]

/**
 * 锚点滚动：平滑滚动到对应区块。
 * @param id 目标区块元素 id
 */
function scrollTo(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/**
 * 选择参考图（视觉占位）：只记录文件名生成 chip，不发起上传。
 * @param e 文件选择事件
 */
function onFileChosen(e: Event): void {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) refFiles.value.push(file.name)
  input.value = ''
}

/** 移除参考图 chip */
function removeFile(index: number): void {
  refFiles.value.splice(index, 1)
}

/**
 * Hero 对话框发送（主转化链入口）：
 * 空输入抖动提示；已登录 → setIntent 后跳 /agent；
 * 未登录 → 打开 AuthDialog（登录面板，输入保留在本页 ref 中）。
 */
function sendIntent(): void {
  const text = intent.value.trim()
  if (!text) {
    ElMessage.warning('说说你想创作什么')
    return
  }
  if (text.length > MAX_LENGTH) {
    ElMessage.warning(`描述不能超过 ${MAX_LENGTH} 字`)
    return
  }
  if (hasToken) {
    agentStore.setIntent(text)
    router.push('/agent')
  } else {
    authMode.value = 'login'
    authDialogVisible.value = true
  }
}

/**
 * 登录/注册成功回调：把保留的输入写入 agent store 再进工作台。
 */
function onAuthSuccess(): void {
  const text = intent.value.trim()
  if (text) agentStore.setIntent(text)
  router.push('/agent')
}

/** CTA 按钮点击：已登录进工作台；未登录打开注册面板 */
function onCtaClick(): void {
  if (hasToken) {
    router.push('/agent')
  } else {
    authMode.value = 'register'
    authDialogVisible.value = true
  }
}

// ---------- 挂载：强制浅色主题（主体内容适配）+ 启动星尘与滚动显现 ----------
/** 离开页面时需要恢复的主题属性值 */
let prevTheme: string | null = null
onMounted(() => {
  prevTheme = document.documentElement.getAttribute('data-theme')
  document.documentElement.setAttribute('data-theme', 'light')
  startStarfield()
  observeFlow()
})
onBeforeUnmount(() => {
  document.documentElement.setAttribute('data-theme', prevTheme ?? 'dark')
  if (starRaf) cancelAnimationFrame(starRaf)
  flowObserver?.disconnect()
})
</script>

<template>
  <div class="home-page">
    <!-- ==================== 动态背景层：极光流光 + 星尘 canvas（fixed，全页共享同一舞台） ==================== -->
    <div class="aurora aurora--a" aria-hidden="true" />
    <div class="aurora aurora--b" aria-hidden="true" />
    <canvas ref="starfieldRef" class="starfield" aria-hidden="true" />
    <div class="glow glow--a" aria-hidden="true" />
    <div class="glow glow--b" aria-hidden="true" />

    <!-- ==================== 顶部导航 ==================== -->
    <header class="home-nav">
      <div class="brand">
        <MfLogo :size="22" label="" />
        <span class="brand-name">漫序 MangaFlow</span>
      </div>
      <nav class="nav-anchors">
        <button type="button" class="nav-link" @click="scrollTo('section-idea')">产品理念</button>
        <button type="button" class="nav-link" @click="scrollTo('section-flow')">工作流程</button>
        <button type="button" class="nav-link" @click="scrollTo('section-engines')">双引擎</button>
        <button type="button" class="nav-link" @click="scrollTo('section-works')">作品展示</button>
      </nav>
      <div class="nav-actions">
        <MfButton v-if="!hasToken" variant="ghost" @click="authMode = 'login'; authDialogVisible = true">登录</MfButton>
        <MfButton v-else variant="soft" @click="router.push('/agent')">进入工作台</MfButton>
        <MfButton @click="onCtaClick">免费开始</MfButton>
      </div>
    </header>

    <!-- ==================== Hero：深色放映厅 · 左文案 + 成片帧带 + 智能体对话框 ==================== -->
    <section id="section-idea" class="hero">
      <div class="hero-inner">
        <div class="hero-copy">
          <p class="hero-eyebrow">AI 漫剧一站式生产平台</p>
          <h1 class="hero-title">有序创作，<br />漫无止境</h1>
          <p class="hero-sub">一句话到成片 · 流水线做量产 · 画布做探索</p>
          <ul class="hero-points">
            <li>AI 智能体承接创意，自动进入创作流程</li>
            <li>剧本 → 角色 → 分镜 → 生成 → 发布全链路</li>
            <li>注册即赠初始积分，免费开始第一部漫剧</li>
          </ul>
        </div>

        <!-- 成片帧带（签名元素）：真实生成剧照倾斜排布，暗示"话音落，画面生" -->
        <div class="hero-film" aria-hidden="true">
          <figure v-for="(f, i) in HERO_FRAMES" :key="f" class="film-frame" :class="`film-frame--${i + 1}`">
            <img :src="f" alt="" loading="eager" decoding="async" />
          </figure>
        </div>

        <!-- 智能体对话框卡片（核心转化组件，浮于帧带之上） -->
        <div class="chat-card">
          <div class="chat-card__title">
            <span class="chat-dot" aria-hidden="true" />
            漫序创作 Agent
          </div>
          <textarea
            v-model="intent"
            class="chat-input"
            :maxlength="MAX_LENGTH"
            rows="5"
            :placeholder="modePlaceholder"
            @keydown.enter.exact.prevent="sendIntent"
          />
          <div class="chat-count" :class="{ 'chat-count--over': intent.length >= MAX_LENGTH }">
            {{ intent.length }} / {{ MAX_LENGTH }}
          </div>

          <!-- 参考图 chips（视觉占位） -->
          <div v-if="refFiles.length" class="ref-files">
            <span v-for="(f, i) in refFiles" :key="f + i" class="ref-chip">
              🖼️ {{ f }}
              <button type="button" class="ref-chip__close" aria-label="移除参考图" @click="removeFile(i)">×</button>
            </span>
          </div>

          <div class="chat-toolbar">
            <button type="button" class="tool-chip" title="上传参考图（演示占位）" @click="fileInputRef?.click()">
              📎 上传参考图
            </button>
            <input ref="fileInputRef" type="file" accept="image/*" style="display: none" @change="onFileChosen" />
            <MfButton class="send-btn" pill @click="sendIntent">发送 →</MfButton>
          </div>

          <!-- 模式单选 chips -->
          <div class="mode-row">
            <button
              v-for="m in MODE_OPTIONS"
              :key="m.value"
              type="button"
              class="mode-chip"
              :class="{ 'mode-chip--active': chatMode === m.value }"
              @click="chatMode = m.value"
            >
              {{ m.label }}
            </button>
          </div>

          <!-- 快捷示例 -->
          <div class="example-row">
            <span class="example-label">试试：</span>
            <button
              v-for="ex in QUICK_EXAMPLES"
              :key="ex"
              type="button"
              class="example-chip"
              @click="intent = ex"
            >
              {{ ex }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- ==================== 使用顺序：四步交替区块（真实内容意象 + 滚动显现） ==================== -->
    <section id="section-flow" class="section flow">
      <h2 class="section-title">从一句话到成片</h2>
      <p class="section-sub">四步走完整个创作流程，智能体全程陪伴</p>

      <!-- 步骤 01 · 创意输入：一句话开启（真实示例句的对话意象） -->
      <div class="flow-step">
        <div class="flow-copy">
          <p class="flow-no">STEP 01</p>
          <h3 class="flow-title">创意输入</h3>
          <p class="flow-desc">一句话、一篇小说或一份大纲，多模式进入创作流程。智能体承接你的想法，自动开始工作。</p>
          <ul class="flow-points">
            <li>智能模式 / 剧本 / 角色 / 视频 / 配音五类入口</li>
            <li>支持上传参考图锚定画面风格</li>
          </ul>
        </div>
        <div class="flow-visual">
          <div class="stage stage--chat">
            <div class="stage-chat-head">漫序创作 Agent</div>
            <div class="stage-bubble stage-bubble--user">把这句话做成漫剧：深夜便利店的相遇</div>
            <div class="stage-bubble stage-bubble--ai">收到，正在解析创意并进入创作流程——先从故事设定开始。</div>
            <div class="stage-typing"><span /><span /><span /></div>
          </div>
        </div>
      </div>

      <!-- 步骤 02 · 剧本生成（真实项目《斗破苍穹》第 1 集剧本片段） -->
      <div class="flow-step flow-step--reverse">
        <div class="flow-copy">
          <p class="flow-no">STEP 02</p>
          <h3 class="flow-title">剧本生成</h3>
          <p class="flow-desc">AI 生成结构化剧本，自动体检与角色别名归一，故事设定、角色设定、分集大纲到剧本正文逐段推进。</p>
          <ul class="flow-points">
            <li>版本历史可回溯，剧本随时导出</li>
            <li>AI 助手对话式改写，如「把开场冲突提前」</li>
          </ul>
        </div>
        <div class="flow-visual">
          <div class="stage stage--script">
            <div class="stage-script-head">
              <span>第 1 集 · 废柴觉醒·异火初现</span>
              <span class="stage-script-badge">已有正文</span>
            </div>
            <p class="stage-script-line stage-script-line--action">纳兰嫣然立于萧家大厅中央，将休书掷于地上，殿角微扬。</p>
            <p class="stage-script-line">纳兰嫣然（冷淡）：萧炎，你我婚约，到此为止。</p>
            <p class="stage-script-line">萧炎（沉声）：嫣然，此事可再议——</p>
          </div>
        </div>
      </div>

      <!-- 步骤 03 · 分镜与生成（真实关键帧四宫格） -->
      <div class="flow-step">
        <div class="flow-copy">
          <p class="flow-no">STEP 03</p>
          <h3 class="flow-title">分镜与生成</h3>
          <p class="flow-desc">分镜拆解、角色定妆、关键帧与视频任务流水线，逐镜生成、逐镜替换，先确认风格再全量生产。</p>
          <ul class="flow-points">
            <li>每镜标注景别 / 运镜 / 时长 / 情绪</li>
            <li>关键帧先试后批，积分消耗预估可见</li>
          </ul>
        </div>
        <div class="flow-visual">
          <div class="stage stage--frames">
            <img :src="frame01" alt="关键帧画面" loading="lazy" decoding="async" />
            <img :src="frame04" alt="关键帧画面" loading="lazy" decoding="async" />
            <img :src="frame03" alt="关键帧画面" loading="lazy" decoding="async" />
            <img :src="frame06" alt="关键帧画面" loading="lazy" decoding="async" />
            <span class="stage-frames-badge">16 镜 · 关键帧已完成</span>
          </div>
        </div>
      </div>

      <!-- 步骤 04 · 成片发布（真实成片帧 + 交付要素） -->
      <div class="flow-step flow-step--reverse">
        <div class="flow-copy">
          <p class="flow-no">STEP 04</p>
          <h3 class="flow-title">成片发布</h3>
          <p class="flow-desc">一键合成、隐形水印与带提取码的分享链接，审核通过即刻交付。</p>
          <ul class="flow-points">
            <li>配音 / 字幕 / BGM 在合成中自动对齐</li>
            <li>分享链接带提取码，内容安全可控</li>
          </ul>
        </div>
        <div class="flow-visual">
          <div class="stage stage--publish">
            <img :src="frame05" alt="成片画面" loading="lazy" decoding="async" />
            <span class="stage-play" aria-hidden="true"><i /></span>
            <div class="stage-meta">
              <span>成片 v1 · 9:16</span>
              <span>隐形水印已嵌入 · 提取码 8 位</span>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- ==================== 双引擎 ==================== -->
    <section id="section-engines" class="section section--alt">
      <h2 class="section-title">双引擎，两种创作方式</h2>
      <p class="section-sub">流水线做量产，画布做探索</p>
      <div class="engine-grid">
        <!-- Studio · 墨青引擎 -->
        <div class="engine-card engine-card--studio">
          <div class="engine-tag">Studio 引擎</div>
          <h3>有序创作 · 墨青</h3>
          <p>剧本 → 资产 → 分镜 → 生成 → 发布，五阶段流水线，进度可控、版本可回溯，适合系列化量产。</p>
          <button type="button" class="engine-link" @click="scrollTo('section-flow')">了解流水线 →</button>
        </div>
        <!-- Canvas · 藤紫引擎（#7C6BF0 仅此卡片使用） -->
        <div class="engine-card engine-card--canvas">
          <div class="engine-tag engine-tag--canvas">Canvas 引擎</div>
          <h3>漫无止境 · 藤紫</h3>
          <p>无限画布节点自由编排，画布助手对话式搭建与增量执行，适合风格试验与灵感探索。</p>
          <button type="button" class="engine-link engine-link--canvas" @click="onCtaClick">了解画布 →</button>
        </div>
      </div>
    </section>

    <!-- ==================== 作品展示（真实平台产出画廊） ==================== -->
    <section id="section-works" class="section">
      <div class="works-head">
        <div>
          <h2 class="section-title">灵感作品展示</h2>
          <p class="works-note">以下画面均由漫序平台实际生成 · 项目《斗破苍穹》第 1 集</p>
        </div>
        <div class="works-tabs">
          <button
            v-for="t in WORK_TABS"
            :key="t.value"
            type="button"
            class="works-tab"
            :class="{ 'works-tab--active': workTab === t.value }"
            @click="workTab = t.value"
          >
            {{ t.label }}
          </button>
        </div>
      </div>
      <div class="works-grid">
        <figure v-for="(w, i) in gallery" :key="w.title + i" class="work-card">
          <div class="work-cover">
            <img :src="w.img" :alt="`${w.title}（${w.tag}）`" loading="lazy" decoding="async" />
            <span class="work-tag">{{ w.tag }}</span>
          </div>
          <figcaption class="work-name">{{ w.title }}</figcaption>
        </figure>
      </div>
    </section>

    <!-- ==================== CTA（真实帧带收束） ==================== -->
    <section class="cta">
      <div class="cta-frames" aria-hidden="true">
        <img v-for="f in HERO_FRAMES" :key="f" :src="f" alt="" loading="lazy" decoding="async" />
      </div>
      <div class="cta-body">
        <h2>开启你的 AI 创作工作流</h2>
        <p>现在注册，免费开始你的第一部漫剧</p>
        <MfButton size="lg" pill @click="onCtaClick">免费开始创作</MfButton>
      </div>
    </section>

    <!-- ==================== 页脚 ==================== -->
    <footer class="home-footer">
      <div class="footer-cols">
        <div class="footer-col">
          <div class="footer-brand">
            <MfLogo :size="22" label="" />
            <span>漫序 MangaFlow</span>
          </div>
          <p class="footer-desc">AI 漫剧一站式生产平台</p>
        </div>
        <div class="footer-col">
          <div class="footer-col__title">产品</div>
          <button type="button" class="footer-link" @click="scrollTo('section-engines')">双引擎</button>
          <button type="button" class="footer-link" @click="scrollTo('section-flow')">工作流程</button>
        </div>
        <div class="footer-col">
          <div class="footer-col__title">文档</div>
          <router-link to="/help" class="footer-link">帮助中心</router-link>
          <a href="#" class="footer-link" @click.prevent>使用指南</a>
        </div>
        <div class="footer-col">
          <div class="footer-col__title">关于</div>
          <a href="#" class="footer-link" @click.prevent>关于我们</a>
          <a href="#" class="footer-link" @click.prevent>用户协议</a>
          <a href="#" class="footer-link" @click.prevent>隐私政策</a>
        </div>
      </div>
      <div class="footer-bottom">© 2026 漫序 MangaFlow · AI 漫剧一站式生产平台</div>
    </footer>

    <!-- 登录 / 注册弹窗（主转化链节点） -->
    <AuthDialog v-model="authDialogVisible" :initial-mode="authMode" @success="onAuthSuccess" />
  </div>
</template>

<style scoped>
/* ============================================================
   官网首页 · 全页连续深色放映厅（不随 data-theme 变化，区块自有配色）
   品牌墨青在深底上的可用值：#4FD8CA（文字/强调）、#12B3A6（大面积）
   ============================================================ */
.home-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  position: relative;
  background:
    radial-gradient(100% 60% at 80% 0%, rgba(18, 179, 166, 0.10), transparent 60%),
    #0B1413;
  color: #E8EDEA;
  /* clip 不产生滚动容器：若用 hidden 会使子级 position:sticky 相对本容器而失效（顶栏无法吸附） */
  overflow-x: clip;
}

/* ---------- 动态背景层 ---------- */
.starfield {
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  z-index: 0;
}
/* 极光流光带：大尺度缓慢流动的色彩层，让"动态"肉眼可感 */
.aurora {
  position: fixed;
  width: 140vmax;
  height: 46vmax;
  pointer-events: none;
  filter: blur(70px);
  opacity: 0.5;
  z-index: 0;
  will-change: transform;
}
.aurora--a {
  left: -30vmax;
  top: -12vmax;
  background: linear-gradient(
    100deg,
    transparent 12%,
    rgba(18, 179, 166, 0.20) 32%,
    rgba(79, 216, 202, 0.12) 46%,
    rgba(18, 179, 166, 0.05) 58%,
    transparent 72%
  );
}
.aurora--b {
  right: -34vmax;
  bottom: -16vmax;
  transform: rotate(-14deg);
  background: linear-gradient(
    80deg,
    transparent 14%,
    rgba(12, 100, 92, 0.30) 38%,
    rgba(18, 179, 166, 0.10) 55%,
    transparent 74%
  );
}
/* 环境层动效（极光/光晕）为产品显式要求的氛围动画：
   大尺度、缓慢、低频位移，不受 prefers-reduced-motion 限制；
   高频小元素动画（typing 闪烁等）仍保留守卫 */
.aurora--a { animation: aurora-a 26s ease-in-out infinite alternate; }
.aurora--b { animation: aurora-b 32s ease-in-out infinite alternate; }
@keyframes aurora-a {
  from { transform: translate3d(-6vmax, 2vmax, 0) rotate(-4deg); }
  to { transform: translate3d(8vmax, 5vmax, 0) rotate(5deg); }
}
@keyframes aurora-b {
  from { transform: rotate(-10deg) translate3d(4vmax, -2vmax, 0); }
  to { transform: rotate(-18deg) translate3d(-6vmax, 4vmax, 0); }
}
.glow {
  position: fixed;
  border-radius: 50%;
  filter: blur(90px);
  pointer-events: none;
  z-index: 0;
}
.glow--a {
  width: 560px;
  height: 560px;
  left: -160px;
  top: 12%;
  background: rgba(18, 179, 166, 0.14);
}
.glow--b {
  width: 640px;
  height: 640px;
  right: -200px;
  bottom: 6%;
  background: rgba(12, 100, 92, 0.20);
}
.glow--a { animation: glow-drift 18s ease-in-out infinite alternate; }
.glow--b { animation: glow-drift 22s ease-in-out 3s infinite alternate-reverse; }
@keyframes glow-drift {
  from { transform: translate3d(0, 0, 0) scale(1); }
  to { transform: translate3d(110px, 90px, 0) scale(1.16); }
}
/* 内容全部置于背景层之上 */
.home-nav,
.hero,
.section,
.cta,
.home-footer {
  position: relative;
  z-index: 1;
}

/* ---------- 顶部导航（深色玻璃） ---------- */
.home-nav {
  position: sticky;
  top: 0;
  z-index: var(--mf-z-sticky);
  display: flex;
  align-items: center;
  gap: 40px;
  padding: 0 48px;
  height: 64px;
  background: rgba(11, 20, 19, 0.78);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-name {
  font-weight: 700;
  color: #FFFFFF;
}
.nav-anchors {
  display: flex;
  gap: 4px;
  flex: 1;
}
.nav-link {
  border: none;
  background: transparent;
  padding: 8px 14px;
  border-radius: var(--mf-r-pill);
  color: rgba(232, 237, 234, 0.66);
  font-size: 14px;
  font-family: inherit;
  cursor: pointer;
  transition: color var(--mf-dur) var(--mf-ease), background var(--mf-dur) var(--mf-ease);
}
.nav-link:hover {
  color: #4FD8CA;
  background: rgba(79, 216, 202, 0.10);
}
.nav-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* ============================================================
   Hero · 深色放映厅（签名区块）
   底色半透明：fixed 极光与星尘可透出，动态背景全页连续；
   Agent 深色玻璃卡以墨青辉光点睛，是首屏转化焦点。
   ============================================================ */
.hero {
  background:
    radial-gradient(120% 90% at 78% 10%, rgba(18, 179, 166, 0.14), transparent 55%),
    radial-gradient(80% 70% at 12% 88%, rgba(12, 66, 60, 0.35), transparent 60%),
    rgba(9, 20, 18, 0.5);
  color: #E8EDEA;
  overflow: hidden;
}
.hero-inner {
  display: grid;
  grid-template-columns: minmax(0, 0.92fr) minmax(0, 1.08fr);
  align-items: center;
  gap: 24px;
  width: min(1160px, 100%);
  margin: 0 auto;
  padding: 88px 24px 96px;
  position: relative;
}
.hero-eyebrow {
  margin: 0 0 18px;
  font-family: var(--mf-mono, ui-monospace, Menlo, Consolas, monospace);
  font-size: 12px;
  letter-spacing: 0.14em;
  color: #4FD8CA;
}
.hero-title {
  margin: 0;
  font-size: 56px;
  font-weight: 700;
  line-height: 1.14;
  letter-spacing: 0.01em;
  color: #FFFFFF;
}
.hero-sub {
  margin: 18px 0 26px;
  font-size: 18px;
  color: rgba(232, 237, 234, 0.72);
}
.hero-points {
  margin: 0;
  padding: 0;
  list-style: none;
  color: rgba(232, 237, 234, 0.62);
  font-size: 14px;
  line-height: 2.1;
}
.hero-points li::before {
  content: '✦';
  color: #4FD8CA;
  margin-right: 8px;
}

/* ---------- 成片帧带（签名元素）：真实剧照倾斜胶片排布 ---------- */
.hero-film {
  position: relative;
  height: 460px;
}
.film-frame {
  position: absolute;
  margin: 0;
  width: 236px;
  border-radius: 14px;
  padding: 8px;
  background: rgba(255, 255, 255, 0.92);
  box-shadow: 0 24px 60px -18px rgba(0, 0, 0, 0.65);
  transition: transform var(--mf-dur) var(--mf-ease);
}
.film-frame img {
  display: block;
  width: 100%;
  aspect-ratio: 9 / 14;
  object-fit: cover;
  border-radius: 8px;
}
.film-frame--1 {
  left: 2%;
  top: 8%;
  transform: rotate(-6deg);
  z-index: 1;
}
.film-frame--2 {
  left: 34%;
  top: 0;
  transform: rotate(2.5deg);
  z-index: 2;
}
.film-frame--3 {
  left: 62%;
  top: 14%;
  transform: rotate(7deg);
  z-index: 1;
}
/* 帧带轻微浮动：缓慢大位移氛围动画，产品要求始终动态 */
.film-frame--1 { animation: film-drift 7s ease-in-out infinite; }
.film-frame--2 { animation: film-drift 8.5s ease-in-out 0.8s infinite; }
.film-frame--3 { animation: film-drift 7.8s ease-in-out 1.6s infinite; }
@keyframes film-drift {
  0%, 100% { translate: 0 0; }
  50% { translate: 0 -10px; }
}

/* ---------- 智能体对话框卡片（深色玻璃，与放映厅融为一体；转化核心以墨青辉光点睛） ---------- */
.chat-card {
  position: absolute;
  right: 24px;
  bottom: 64px;
  width: min(420px, 42%);
  padding: 20px;
  color: #E8EDEA;
  background: linear-gradient(160deg, rgba(23, 36, 34, 0.86), rgba(15, 26, 25, 0.78));
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: var(--mf-r-card);
  box-shadow:
    inset 0 1px 0 rgba(255, 255, 255, 0.10),
    0 32px 80px -24px rgba(0, 0, 0, 0.65),
    0 0 90px -30px rgba(18, 179, 166, 0.55);
  backdrop-filter: blur(18px) saturate(140%);
  -webkit-backdrop-filter: blur(18px) saturate(140%);
  z-index: 3;
}
.chat-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  margin-bottom: 14px;
  color: #FFFFFF;
}
.chat-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4FD8CA;
  box-shadow: 0 0 10px rgba(79, 216, 202, 0.9);
}
.chat-input {
  width: 100%;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 12px;
  padding: 12px;
  font-family: inherit;
  font-size: 14px;
  color: #E8EDEA;
  background: rgba(255, 255, 255, 0.05);
  resize: vertical;
  transition: border-color var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease), background var(--mf-dur) var(--mf-ease);
}
.chat-input::placeholder {
  color: rgba(232, 237, 234, 0.42);
}
.chat-input:focus {
  outline: none;
  border-color: rgba(79, 216, 202, 0.65);
  background: rgba(79, 216, 202, 0.05);
  box-shadow: 0 0 0 3px rgba(79, 216, 202, 0.18);
}
.chat-count {
  text-align: right;
  font-size: 12px;
  color: rgba(232, 237, 234, 0.42);
  margin: 4px 0 8px;
}
.chat-count--over {
  color: #F0736F;
  font-weight: 600;
}
.ref-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.ref-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: var(--mf-r-pill);
  background: rgba(79, 216, 202, 0.14);
  color: #9BE8DC;
  font-size: 12px;
}
.ref-chip__close {
  border: none;
  background: transparent;
  cursor: pointer;
  color: inherit;
  font-size: 13px;
  line-height: 1;
}
.chat-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.tool-chip {
  border: 1px dashed rgba(255, 255, 255, 0.24);
  background: transparent;
  border-radius: var(--mf-r-pill);
  padding: 5px 12px;
  font-size: 13px;
  color: rgba(232, 237, 234, 0.62);
  cursor: pointer;
  font-family: inherit;
  transition: color var(--mf-dur) var(--mf-ease), border-color var(--mf-dur) var(--mf-ease);
}
.tool-chip:hover {
  color: #4FD8CA;
  border-color: rgba(79, 216, 202, 0.6);
}
.send-btn {
  min-width: 96px;
}
.mode-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.mode-chip {
  border: 1px solid rgba(255, 255, 255, 0.16);
  background: rgba(255, 255, 255, 0.04);
  border-radius: var(--mf-r-pill);
  padding: 5px 14px;
  font-size: 13px;
  color: rgba(232, 237, 234, 0.72);
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.mode-chip--active {
  background: rgba(79, 216, 202, 0.18);
  border-color: rgba(79, 216, 202, 0.7);
  color: #9BE8DC;
  font-weight: 600;
}
.example-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
}
.example-label {
  font-size: 13px;
  color: rgba(232, 237, 234, 0.42);
}
.example-chip {
  border: 1px solid rgba(255, 255, 255, 0.1);
  background: rgba(255, 255, 255, 0.03);
  border-radius: var(--mf-r-pill);
  padding: 5px 12px;
  font-size: 12px;
  color: rgba(232, 237, 234, 0.62);
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.example-chip:hover {
  background: rgba(79, 216, 202, 0.12);
  border-color: rgba(79, 216, 202, 0.4);
  color: #9BE8DC;
}

/* ---------- 通用区块（全页连续深色，区块间以细线分隔） ---------- */
.section {
  width: min(1160px, 100%);
  margin: 0 auto;
  padding: 88px 24px;
}
.section--alt {
  width: 100%;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}
.section--alt > .section-title,
.section--alt > .section-sub {
  width: min(1160px, 100%);
  margin-left: auto;
  margin-right: auto;
}
.section-title {
  margin: 0;
  text-align: center;
  font-size: 32px;
  font-weight: 700;
  color: #FFFFFF;
}
.section-sub {
  margin: 12px 0 40px;
  text-align: center;
  color: rgba(232, 237, 234, 0.6);
}

/* ============================================================
   使用顺序 · 四步交替区块（参考舞台式叙事：左文右图 ↔ 右文左图）
   stage = 发光容器（辉光边框 + 深色玻璃），承载真实内容意象
   ============================================================ */
.flow-step {
  display: grid;
  grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
  align-items: center;
  gap: 56px;
  padding: 56px 0;
}
.flow-step + .flow-step {
  border-top: 1px solid rgba(255, 255, 255, 0.05);
}
.flow-step--reverse .flow-copy {
  order: 2;
}
.flow-step--reverse .flow-visual {
  order: 1;
}
/* 滚动显现：进入视口上浮淡入（reduced-motion 时直接显示） */
.flow-step {
  opacity: 0;
  transform: translateY(28px);
  transition: opacity 0.6s var(--mf-ease), transform 0.6s var(--mf-ease);
}
.flow-step--in {
  opacity: 1;
  transform: none;
}

/* ---------- 左侧文案 ---------- */
.flow-no {
  margin: 0 0 14px;
  font-family: var(--mf-mono, ui-monospace, Menlo, Consolas, monospace);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.2em;
  color: #4FD8CA;
}
.flow-title {
  margin: 0 0 14px;
  font-size: 30px;
  font-weight: 700;
  color: #FFFFFF;
}
.flow-desc {
  margin: 0 0 18px;
  font-size: 15px;
  line-height: 1.9;
  color: rgba(232, 237, 234, 0.68);
}
.flow-points {
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 13px;
  line-height: 2;
  color: rgba(232, 237, 234, 0.55);
}
.flow-points li::before {
  content: '—';
  color: rgba(79, 216, 202, 0.6);
  margin-right: 8px;
}

/* ---------- 右侧发光容器（stage 通用） ---------- */
.flow-visual {
  min-width: 0;
}
.stage {
  position: relative;
  border-radius: 20px;
  background: linear-gradient(160deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.015));
  border: 1px solid rgba(79, 216, 202, 0.28);
  box-shadow:
    0 0 0 1px rgba(12, 26, 24, 0.6),
    0 24px 70px -28px rgba(18, 179, 166, 0.4),
    inset 0 1px 0 rgba(255, 255, 255, 0.06);
  overflow: hidden;
}

/* stage 01 · 对话意象 */
.stage--chat {
  padding: 22px;
}
.stage-chat-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: rgba(232, 237, 234, 0.85);
  margin-bottom: 16px;
}
.stage-chat-head::before {
  content: '';
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #4FD8CA;
  box-shadow: 0 0 10px rgba(79, 216, 202, 0.9);
}
.stage-bubble {
  max-width: 86%;
  padding: 10px 14px;
  border-radius: 14px;
  font-size: 13px;
  line-height: 1.7;
  margin-bottom: 10px;
}
.stage-bubble--user {
  margin-left: auto;
  background: rgba(79, 216, 202, 0.16);
  border: 1px solid rgba(79, 216, 202, 0.35);
  color: #DFF7F2;
}
.stage-bubble--ai {
  background: rgba(255, 255, 255, 0.06);
  color: rgba(232, 237, 234, 0.85);
}
.stage-typing {
  display: inline-flex;
  gap: 5px;
  padding: 8px 12px;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.06);
}
.stage-typing span {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: rgba(232, 237, 234, 0.6);
}
@media (prefers-reduced-motion: no-preference) {
  .stage-typing span { animation: typing 1.2s ease-in-out infinite; }
  .stage-typing span:nth-child(2) { animation-delay: 0.15s; }
  .stage-typing span:nth-child(3) { animation-delay: 0.3s; }
  @keyframes typing {
    0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
    30% { transform: translateY(-4px); opacity: 1; }
  }
}

/* stage 02 · 剧本卡意象（真实剧本片段） */
.stage--script {
  padding: 22px 24px;
  font-size: 13px;
}
.stage-script-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  margin-bottom: 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  color: rgba(232, 237, 234, 0.85);
  font-weight: 600;
}
.stage-script-badge {
  padding: 2px 10px;
  border-radius: var(--mf-r-pill);
  background: rgba(79, 216, 202, 0.14);
  color: #4FD8CA;
  font-size: 12px;
  font-weight: 500;
}
.stage-script-line {
  margin: 0 0 10px;
  line-height: 1.9;
  color: rgba(232, 237, 234, 0.78);
}
.stage-script-line--action {
  color: rgba(232, 237, 234, 0.5);
  font-style: normal;
}

/* stage 03 · 关键帧四宫格（真实生成帧） */
.stage--frames {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  padding: 14px;
}
.stage--frames img {
  width: 100%;
  aspect-ratio: 3 / 4;
  object-fit: cover;
  border-radius: 10px;
  display: block;
}
.stage-frames-badge {
  position: absolute;
  right: 14px;
  bottom: 14px;
  padding: 4px 12px;
  border-radius: var(--mf-r-pill);
  background: rgba(11, 20, 19, 0.82);
  border: 1px solid rgba(79, 216, 202, 0.4);
  color: #4FD8CA;
  font-size: 12px;
  font-family: var(--mf-mono, ui-monospace, Menlo, Consolas, monospace);
  letter-spacing: 0.04em;
  backdrop-filter: blur(6px);
}

/* stage 04 · 成片意象（真实帧 + 播放 + 交付要素） */
.stage--publish img {
  display: block;
  width: 100%;
  aspect-ratio: 16 / 9;
  object-fit: cover;
  object-position: top;
}
.stage-play {
  position: absolute;
  left: 50%;
  top: 38%;
  transform: translate(-50%, -50%);
  width: 64px;
  height: 64px;
  border-radius: 50%;
  background: rgba(11, 20, 19, 0.62);
  border: 1px solid rgba(255, 255, 255, 0.35);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
}
.stage-play i {
  width: 0;
  height: 0;
  border-style: solid;
  border-width: 11px 0 11px 18px;
  border-color: transparent transparent transparent #FFFFFF;
  margin-left: 5px;
}
.stage-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  font-size: 12px;
  font-family: var(--mf-mono, ui-monospace, Menlo, Consolas, monospace);
  color: rgba(232, 237, 234, 0.6);
  border-top: 1px solid rgba(255, 255, 255, 0.07);
}

/* ---------- 双引擎 ---------- */
.engine-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  width: min(1120px, 100%);
  margin: 0 auto;
  padding: 0 24px 72px;
}
.engine-card {
  padding: 36px 32px;
  border-radius: var(--mf-r-card);
  border: 1px solid transparent;
}
.engine-card--studio {
  background: linear-gradient(150deg, rgba(18, 179, 166, 0.16), rgba(18, 179, 166, 0.04));
  border-color: rgba(79, 216, 202, 0.35);
}
.engine-card--canvas {
  /* 藤紫 #7C6BF0：全站仅 Canvas 引擎卡可用 */
  background: linear-gradient(150deg, rgba(124, 107, 240, 0.18), rgba(124, 107, 240, 0.05));
  border-color: rgba(154, 140, 245, 0.45);
}
.engine-tag {
  display: inline-block;
  padding: 4px 12px;
  border-radius: var(--mf-r-pill);
  background: rgba(79, 216, 202, 0.16);
  color: #4FD8CA;
  font-size: 12px;
  font-weight: 600;
}
.engine-tag--canvas {
  background: rgba(154, 140, 245, 0.18);
  color: #B4A9F7;
}
.engine-card h3 {
  margin: 16px 0 10px;
  font-size: 24px;
  color: #FFFFFF;
}
.engine-card p {
  margin: 0 0 20px;
  font-size: 14px;
  color: rgba(232, 237, 234, 0.66);
  line-height: 1.8;
}
.engine-link {
  border: none;
  background: transparent;
  color: #4FD8CA;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  padding: 0;
}
.engine-link--canvas {
  color: #B4A9F7;
}

/* ---------- 作品展示（真实素材画廊） ---------- */
.works-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 32px;
}
.works-head .section-title {
  text-align: left;
}
.works-note {
  margin: 10px 0 0;
  font-size: 13px;
  color: rgba(232, 237, 234, 0.5);
}
.works-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: var(--mf-r-pill);
}
.works-tab {
  border: none;
  background: transparent;
  padding: 7px 18px;
  border-radius: var(--mf-r-pill);
  font-size: 13px;
  color: rgba(232, 237, 234, 0.62);
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.works-tab--active {
  background: #12B3A6;
  color: #06332F;
  font-weight: 600;
}
.works-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.work-card {
  margin: 0;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(255, 255, 255, 0.09);
  border-radius: var(--mf-r-card);
  overflow: hidden;
  transition: box-shadow var(--mf-dur) var(--mf-ease), transform var(--mf-dur) var(--mf-ease), border-color var(--mf-dur) var(--mf-ease);
}
.work-card:hover {
  border-color: rgba(79, 216, 202, 0.4);
  box-shadow: 0 24px 60px -24px rgba(18, 179, 166, 0.45);
  transform: translateY(-2px);
}
.work-cover {
  position: relative;
  aspect-ratio: 3 / 4;
  overflow: hidden;
  background: #0C1A18;
}
.work-cover img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform var(--mf-dur) var(--mf-ease);
}
.work-card:hover .work-cover img {
  transform: scale(1.03);
}
.work-tag {
  position: absolute;
  left: 12px;
  top: 12px;
  padding: 3px 10px;
  border-radius: var(--mf-r-pill);
  background: rgba(12, 26, 24, 0.66);
  color: #E8EDEA;
  font-size: 12px;
  font-family: var(--mf-mono, ui-monospace, Menlo, Consolas, monospace);
  letter-spacing: 0.06em;
  backdrop-filter: blur(4px);
}
.work-name {
  padding: 12px 16px;
  font-size: 14px;
  font-weight: 500;
  color: rgba(232, 237, 234, 0.88);
}

/* ---------- CTA（真实帧带横贯收束"画面"主题） ---------- */
.cta {
  position: relative;
  width: 100%;
  padding: 88px 24px;
  text-align: center;
  background: linear-gradient(160deg, rgba(18, 179, 166, 0.16), rgba(18, 179, 166, 0.02));
  border-top: 1px solid rgba(255, 255, 255, 0.06);
  overflow: hidden;
}
.cta-frames {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 18px;
  opacity: 0.16;
  filter: saturate(0.85);
  pointer-events: none;
}
.cta-frames img {
  width: 190px;
  aspect-ratio: 9 / 14;
  object-fit: cover;
  border-radius: 10px;
  flex: none;
}
.cta-body {
  position: relative;
}
.cta h2 {
  margin: 0;
  font-size: 32px;
  color: #FFFFFF;
}
.cta p {
  margin: 12px 0 28px;
  color: rgba(232, 237, 234, 0.66);
}

/* ---------- 页脚 ---------- */
.home-footer {
  background: rgba(255, 255, 255, 0.02);
  border-top: 1px solid rgba(255, 255, 255, 0.07);
  padding: 48px 48px 24px;
}
.footer-cols {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr 1fr;
  gap: 32px;
  width: min(1120px, 100%);
  margin: 0 auto;
}
.footer-brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 700;
}
.footer-desc {
  margin: 10px 0 0;
  font-size: 13px;
  color: rgba(232, 237, 234, 0.45);
}
.footer-col__title {
  font-weight: 600;
  margin-bottom: 12px;
  font-size: 14px;
  color: rgba(232, 237, 234, 0.85);
}
.footer-link {
  display: block;
  border: none;
  background: transparent;
  padding: 4px 0;
  color: rgba(232, 237, 234, 0.55);
  font-size: 13px;
  text-decoration: none;
  text-align: left;
  cursor: pointer;
  font-family: inherit;
  transition: color var(--mf-dur) var(--mf-ease);
}
.footer-link:hover {
  color: #4FD8CA;
}
.footer-bottom {
  margin-top: 40px;
  padding-top: 20px;
  border-top: 1px solid rgba(255, 255, 255, 0.07);
  text-align: center;
  color: rgba(232, 237, 234, 0.4);
  font-size: 13px;
}

/* ---------- 窄屏适配 ---------- */
@media (max-width: 960px) {
  .flow-step,
  .flow-step--reverse {
    grid-template-columns: 1fr;
    gap: 28px;
    padding: 44px 0;
  }
  /* 移动端统一"文案在上、画面在下"，取消桌面端交替 order */
  .flow-step--reverse .flow-copy {
    order: 1;
  }
  .flow-step--reverse .flow-visual {
    order: 2;
  }
  .flow-title {
    font-size: 24px;
  }
  .stage-play {
    width: 48px;
    height: 48px;
  }
  .stage-play i {
    border-width: 8px 0 8px 13px;
  }
  .hero-inner {
    grid-template-columns: 1fr;
    padding-top: 48px;
    gap: 40px;
  }
  .hero-title {
    font-size: 38px;
  }
  /* 帧带降为一横排缩略（保留"画面"暗示，不再倾斜漂浮） */
  .hero-film {
    height: auto;
    display: flex;
    gap: 12px;
    justify-content: center;
  }
  .film-frame {
    position: static;
    width: 132px;
    transform: none;
    animation: none;
  }
  .film-frame--2,
  .film-frame--3 {
    display: none;
  }
  /* 对话框卡回到文档流，铺满宽度 */
  .chat-card {
    position: static;
    width: 100%;
  }
  .nav-anchors {
    display: none;
  }
  .flow-grid,
  .works-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  .engine-grid {
    grid-template-columns: 1fr;
  }
  .footer-cols {
    grid-template-columns: 1fr 1fr;
  }
  .cta-frames img {
    width: 120px;
  }
}
@media (max-width: 560px) {
  .works-grid {
    grid-template-columns: 1fr;
  }
  .work-cover {
    aspect-ratio: 4 / 5;
  }
  .hero-film {
    display: none;
  }
}
</style>
