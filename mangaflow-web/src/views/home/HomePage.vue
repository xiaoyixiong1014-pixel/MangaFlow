<!--
  官网首页 L-01（文件级注释）。
  未登录态为主的公开入口：首屏 Hero（大标题 + 智能体对话框卡片）承接创作意图，
  向下依次为四步流程、双引擎、作品展示（假数据画廊）、CTA 与页脚。
  主转化链：输入 → 发送 → 未登录弹 AuthDialog（保留输入）→ 登录成功
  → agent.setIntent(输入) → 跳 /agent（W-01 意图承接条）。
  页面强制浅色（创作端默认深色）：挂载时临时把 <html data-theme> 切为 light，
  离开时恢复，避免污染全局主题持久化。
-->
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MfButton } from '@mangaflow/design'
import { useAgentStore } from '@/stores/agent'
import AuthDialog from '@/components/AuthDialog.vue'

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
  { value: 'visual', label: '视觉大片' },
  { value: 'series', label: '短剧剧集' },
  { value: 'character', label: '角色设计' },
]
/** 画廊占位渐变色组（墨青色系，藤紫仅用于 Canvas 引擎卡） */
const GALLERY_GRADS = [
  'linear-gradient(135deg, #0F766E, #12B3A6)',
  'linear-gradient(135deg, #0E7490, #22D3EE)',
  'linear-gradient(135deg, #166534, #4ADE80)',
  'linear-gradient(135deg, #1E3A8A, #60A5FA)',
  'linear-gradient(135deg, #7C2D12, #F59E0B)',
  'linear-gradient(135deg, #134E4A, #5EEAD4)',
]
/** 假数据画廊：每个 Tab 6 个作品（标题 + 渐变色块占位） */
const GALLERY: Record<string, { title: string }[]> = {
  visual: [
    { title: '霓虹雨巷' }, { title: '山海拾遗' }, { title: '星际漫游指南' },
    { title: '旧城来信' }, { title: '雾中灯塔' }, { title: '深潜' },
  ],
  series: [
    { title: '深夜便利店 · 第一集' }, { title: '修仙从洗碗开始' }, { title: '办公室修罗场' },
    { title: '猫的报恩日记' }, { title: '末班车怪谈' }, { title: '像素小镇' },
  ],
  character: [
    { title: '赛博少女 · 零' }, { title: '剑客 · 白霜' }, { title: '侦探 · 蓝棠' },
    { title: '机甲驾驶员' }, { title: '狐火巫女' }, { title: '老街邮差' },
  ],
}

/** 当前 Tab 的画廊数据（类型安全兜底为空数组） */
const gallery = computed(() => GALLERY[workTab.value] ?? [])

/**
 * 按 index 稳定取画廊渐变色。
 * @param i 作品序号
 * @returns CSS 渐变值
 */
function galleryGrad(i: number): string {
  return GALLERY_GRADS[i % GALLERY_GRADS.length] ?? GALLERY_GRADS[0] ?? '#12B3A6'
}

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

// ---------- 页面级浅色强制：挂载时切 light，离开时恢复 ----------
/** 离开页面时需要恢复的主题属性值 */
let prevTheme: string | null = null
onMounted(() => {
  prevTheme = document.documentElement.getAttribute('data-theme')
  document.documentElement.setAttribute('data-theme', 'light')
})
onBeforeUnmount(() => {
  document.documentElement.setAttribute('data-theme', prevTheme ?? 'dark')
})
</script>

<template>
  <div class="home-page">
    <!-- ==================== 顶部导航 ==================== -->
    <header class="home-nav">
      <div class="brand">
        <span class="brand-logo" aria-hidden="true" />
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

    <!-- ==================== Hero：左文案 + 右智能体对话框 ==================== -->
    <section id="section-idea" class="hero">
      <div class="hero-copy">
        <h1 class="hero-title">有序创作，漫无止境</h1>
        <p class="hero-sub">一句话到成片 · 流水线做量产 · 画布做探索</p>
        <ul class="hero-points">
          <li>AI 智能体承接创意，自动进入创作流程</li>
          <li>剧本 → 角色 → 分镜 → 生成 → 发布全链路</li>
          <li>注册即赠初始积分，免费开始第一部漫剧</li>
        </ul>
      </div>

      <!-- 智能体对话框卡片（核心转化组件） -->
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
    </section>

    <!-- ==================== 四步流程 ==================== -->
    <section id="section-flow" class="section">
      <h2 class="section-title">简单四步，创意即刻落地</h2>
      <p class="section-sub">从一个想法到可发布的漫剧成片，智能体全程陪伴</p>
      <div class="flow-grid">
        <div class="flow-card">
          <span class="flow-no">01</span>
          <h3>创意输入</h3>
          <p>一句话、一篇小说或一份大纲，多模式进入创作流程</p>
        </div>
        <div class="flow-card">
          <span class="flow-no">02</span>
          <h3>剧本生成</h3>
          <p>AI 生成结构化剧本，自动体检与角色别名归一</p>
        </div>
        <div class="flow-card">
          <span class="flow-no">03</span>
          <h3>分镜与生成</h3>
          <p>分镜拆解、角色定妆、关键帧与视频任务流水线</p>
        </div>
        <div class="flow-card">
          <span class="flow-no">04</span>
          <h3>成片发布</h3>
          <p>一键合成、隐形水印与带提取码的分享链接</p>
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

    <!-- ==================== 作品展示（假数据画廊） ==================== -->
    <section id="section-works" class="section">
      <div class="works-head">
        <h2 class="section-title">灵感作品展示</h2>
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
        <div v-for="(w, i) in gallery" :key="w.title" class="work-card">
          <div class="work-cover" :style="{ background: galleryGrad(i) }" />
          <div class="work-name">{{ w.title }}</div>
        </div>
      </div>
    </section>

    <!-- ==================== CTA ==================== -->
    <section class="cta">
      <h2>开启你的 AI 创作工作流</h2>
      <p>现在注册，免费开始你的第一部漫剧</p>
      <MfButton size="lg" pill @click="onCtaClick">免费开始创作</MfButton>
    </section>

    <!-- ==================== 页脚 ==================== -->
    <footer class="home-footer">
      <div class="footer-cols">
        <div class="footer-col">
          <div class="footer-brand">
            <span class="brand-logo" aria-hidden="true" />
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
   官网固定浅色（挂载时 <html data-theme> 已切 light，
   此处变量随 light 组生效；少量兜底色保证不依赖主题）
   ============================================================ */
.home-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background: var(--mf-bg, #F5F7F6);
  color: var(--mf-text, #14181A);
}

/* ---------- 顶部导航 ---------- */
.home-nav {
  position: sticky;
  top: 0;
  z-index: var(--mf-z-sticky);
  display: flex;
  align-items: center;
  gap: 40px;
  padding: 0 48px;
  height: 64px;
  background: rgba(255, 255, 255, 0.86);
  backdrop-filter: blur(10px);
  border-bottom: 1px solid rgba(0, 0, 0, 0.06);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
}
.brand-logo {
  width: 22px;
  height: 22px;
  border-radius: 8px;
  background: linear-gradient(135deg, #12B3A6, #3B82F6);
}
.brand-name {
  font-weight: 700;
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
  color: #5F6B68;
  font-size: 14px;
  font-family: inherit;
  cursor: pointer;
  transition: color var(--mf-dur) var(--mf-ease), background var(--mf-dur) var(--mf-ease);
}
.nav-link:hover {
  color: #12B3A6;
  background: rgba(18, 179, 166, 0.08);
}
.nav-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* ---------- Hero ---------- */
.hero {
  display: grid;
  grid-template-columns: 1fr 1fr;
  align-items: center;
  gap: 48px;
  width: min(1120px, 100%);
  margin: 0 auto;
  padding: 88px 24px 72px;
}
.hero-title {
  margin: 0;
  font-size: 52px;
  font-weight: 700;
  line-height: 1.18;
  letter-spacing: 0.01em;
}
.hero-sub {
  margin: 16px 0 24px;
  font-size: 19px;
  color: #5F6B68;
}
.hero-points {
  margin: 0;
  padding: 0;
  list-style: none;
  color: #5F6B68;
  font-size: 14px;
  line-height: 2;
}
.hero-points li::before {
  content: '✦';
  color: #12B3A6;
  margin-right: 8px;
}

/* 智能体对话框卡片 */
.chat-card {
  padding: 20px;
  background: #FFFFFF;
  border: 1px solid rgba(0, 0, 0, 0.09);
  border-radius: var(--mf-r-card);
  box-shadow: var(--mf-shadow-pop);
}
.chat-card__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  margin-bottom: 14px;
}
.chat-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #12B3A6;
}
.chat-input {
  width: 100%;
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 12px;
  padding: 12px;
  font-family: inherit;
  font-size: 14px;
  color: #14181A;
  background: #FAFAF8;
  resize: vertical;
  transition: border-color var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.chat-input:focus {
  outline: none;
  border-color: #12B3A6;
  box-shadow: 0 0 0 3px rgba(18, 179, 166, 0.18);
}
.chat-count {
  text-align: right;
  font-size: 12px;
  color: #8E9A97;
  margin: 4px 0 8px;
}
.chat-count--over {
  color: #CE4343;
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
  background: rgba(18, 179, 166, 0.10);
  color: #0F766E;
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
  border: 1px dashed rgba(0, 0, 0, 0.16);
  background: transparent;
  border-radius: var(--mf-r-pill);
  padding: 5px 12px;
  font-size: 13px;
  color: #5F6B68;
  cursor: pointer;
  font-family: inherit;
  transition: color var(--mf-dur) var(--mf-ease), border-color var(--mf-dur) var(--mf-ease);
}
.tool-chip:hover {
  color: #12B3A6;
  border-color: #12B3A6;
}
.send-btn {
  min-width: 96px;
}
.mode-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
}
.mode-chip {
  border: 1px solid rgba(0, 0, 0, 0.12);
  background: #FFFFFF;
  border-radius: var(--mf-r-pill);
  padding: 5px 14px;
  font-size: 13px;
  color: #5F6B68;
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.mode-chip--active {
  background: rgba(18, 179, 166, 0.12);
  border-color: #12B3A6;
  color: #0F766E;
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
  color: #8E9A97;
}
.example-chip {
  border: none;
  background: #F0F4F3;
  border-radius: var(--mf-r-pill);
  padding: 5px 12px;
  font-size: 12px;
  color: #5F6B68;
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.example-chip:hover {
  background: rgba(18, 179, 166, 0.10);
  color: #0F766E;
}

/* ---------- 通用区块 ---------- */
.section {
  width: min(1120px, 100%);
  margin: 0 auto;
  padding: 72px 24px;
}
.section--alt {
  width: 100%;
  background: #FFFFFF;
  border-top: 1px solid rgba(0, 0, 0, 0.05);
  border-bottom: 1px solid rgba(0, 0, 0, 0.05);
}
.section--alt > .section-title,
.section--alt > .section-sub {
  width: min(1120px, 100%);
  margin-left: auto;
  margin-right: auto;
}
.section-title {
  margin: 0;
  text-align: center;
  font-size: 32px;
  font-weight: 700;
}
.section-sub {
  margin: 12px 0 40px;
  text-align: center;
  color: #5F6B68;
}

/* ---------- 四步流程 ---------- */
.flow-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
}
.flow-card {
  position: relative;
  padding: 28px 24px;
  background: #FFFFFF;
  border: 1px solid rgba(0, 0, 0, 0.09);
  border-radius: var(--mf-r-card);
  transition: box-shadow var(--mf-dur) var(--mf-ease), transform var(--mf-dur) var(--mf-ease);
}
.flow-card:hover {
  box-shadow: var(--mf-shadow-pop);
  transform: translateY(-2px);
}
.flow-no {
  font-size: 28px;
  font-weight: 700;
  color: rgba(18, 179, 166, 0.35);
}
.flow-card h3 {
  margin: 10px 0 8px;
  font-size: 17px;
}
.flow-card p {
  margin: 0;
  font-size: 13px;
  color: #5F6B68;
  line-height: 1.7;
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
  background: linear-gradient(150deg, rgba(18, 179, 166, 0.12), rgba(18, 179, 166, 0.03));
  border-color: rgba(18, 179, 166, 0.35);
}
.engine-card--canvas {
  /* 藤紫 #7C6BF0：全站仅 Canvas 引擎卡可用 */
  background: linear-gradient(150deg, rgba(124, 107, 240, 0.14), rgba(124, 107, 240, 0.03));
  border-color: rgba(124, 107, 240, 0.40);
}
.engine-tag {
  display: inline-block;
  padding: 4px 12px;
  border-radius: var(--mf-r-pill);
  background: rgba(18, 179, 166, 0.15);
  color: #0F766E;
  font-size: 12px;
  font-weight: 600;
}
.engine-tag--canvas {
  background: rgba(124, 107, 240, 0.16);
  color: #5B4BD4;
}
.engine-card h3 {
  margin: 16px 0 10px;
  font-size: 24px;
}
.engine-card p {
  margin: 0 0 20px;
  font-size: 14px;
  color: #5F6B68;
  line-height: 1.8;
}
.engine-link {
  border: none;
  background: transparent;
  color: #12B3A6;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  padding: 0;
}
.engine-link--canvas {
  color: #7C6BF0;
}

/* ---------- 作品展示 ---------- */
.works-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 32px;
}
.works-head .section-title {
  text-align: left;
}
.works-tabs {
  display: flex;
  gap: 6px;
  padding: 4px;
  background: #FFFFFF;
  border: 1px solid rgba(0, 0, 0, 0.09);
  border-radius: var(--mf-r-pill);
}
.works-tab {
  border: none;
  background: transparent;
  padding: 7px 18px;
  border-radius: var(--mf-r-pill);
  font-size: 13px;
  color: #5F6B68;
  cursor: pointer;
  font-family: inherit;
  transition: all var(--mf-dur) var(--mf-ease);
}
.works-tab--active {
  background: #12B3A6;
  color: #FFFFFF;
  font-weight: 600;
}
.works-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.work-card {
  background: #FFFFFF;
  border: 1px solid rgba(0, 0, 0, 0.09);
  border-radius: var(--mf-r-card);
  overflow: hidden;
  transition: box-shadow var(--mf-dur) var(--mf-ease), transform var(--mf-dur) var(--mf-ease);
}
.work-card:hover {
  box-shadow: var(--mf-shadow-pop);
  transform: translateY(-2px);
}
.work-cover {
  aspect-ratio: 16 / 10;
}
.work-name {
  padding: 12px 16px;
  font-size: 14px;
  font-weight: 500;
}

/* ---------- CTA ---------- */
.cta {
  width: 100%;
  padding: 80px 24px;
  text-align: center;
  background: linear-gradient(160deg, rgba(18, 179, 166, 0.14), rgba(18, 179, 166, 0.02));
  border-top: 1px solid rgba(0, 0, 0, 0.05);
}
.cta h2 {
  margin: 0;
  font-size: 32px;
}
.cta p {
  margin: 12px 0 28px;
  color: #5F6B68;
}

/* ---------- 页脚 ---------- */
.home-footer {
  background: #FFFFFF;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
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
  color: #8E9A97;
}
.footer-col__title {
  font-weight: 600;
  margin-bottom: 12px;
  font-size: 14px;
}
.footer-link {
  display: block;
  border: none;
  background: transparent;
  padding: 4px 0;
  color: #5F6B68;
  font-size: 13px;
  text-decoration: none;
  text-align: left;
  cursor: pointer;
  font-family: inherit;
  transition: color var(--mf-dur) var(--mf-ease);
}
.footer-link:hover {
  color: #12B3A6;
}
.footer-bottom {
  margin-top: 40px;
  padding-top: 20px;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
  text-align: center;
  color: #8E9A97;
  font-size: 13px;
}

/* ---------- 窄屏适配 ---------- */
@media (max-width: 960px) {
  .hero {
    grid-template-columns: 1fr;
    padding-top: 48px;
  }
  .hero-title {
    font-size: 38px;
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
}
</style>
