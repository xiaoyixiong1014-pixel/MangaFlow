<!--
  公开分享预览页（文件级注释，L-04）。
  浅色门户风格：进入依据路由 token 拉取分享数据（免认证），成片播放 +
  标题/分辨率/时长 meta + 水印标识 + 创作引导 CTA。
  - 提取码错误（403）：就地展示密码输入重试（经 query password 传给后端校验）；
  - 链接无效/已撤回/过期（404/410）：展示对应失效态文案 + 返回首页；
  - 挂载期间将根元素切到浅色主题，卸载时恢复用户原主题设置。
-->
<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import * as productionApi from '@/api/production'
import { MfLogo } from '@mangaflow/design'
import type { ShareInfo } from '@/types'

const route = useRoute()
const router = useRouter()

/** 分享令牌（路由参数） */
const token = route.params.token as string

/** 分享页数据 */
const info = ref<ShareInfo | null>(null)
/** 页面加载状态 */
const loading = ref(true)
/** 失败状态：invalid=链接失效（404/410）；password=提取码错误（403）；other=其他异常 */
const failKind = ref<'' | 'invalid' | 'password' | 'other'>('')
/** 失败文案（取后端 detail） */
const errorMsg = ref('')
/** 提取码输入 */
const password = ref('')
/** 校验提取码提交中 */
const checking = ref(false)

/**
 * 拉取分享数据。
 * @param pwd 提取码（可选，仅在 403 重试时携带）
 */
async function load(pwd?: string): Promise<void> {
  loading.value = true
  failKind.value = ''
  errorMsg.value = ''
  checking.value = pwd != null
  try {
    info.value = await productionApi.shareInfo(token, pwd)
  } catch (e) {
    const status = (e as { response?: { status?: number } })?.response?.status
    const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
    if (status === 403) {
      // 提取码错误/需要提取码：进入密码校验态
      failKind.value = 'password'
      errorMsg.value = detail || '此作品受密码保护，请输入提取码'
    } else if (status === 404 || status === 410) {
      // 链接无效 / 已撤销 / 已过期
      failKind.value = 'invalid'
      errorMsg.value = detail || '分享链接已失效或已被作者撤回'
    } else {
      failKind.value = 'other'
      errorMsg.value = detail || '加载失败，请稍后重试'
    }
    info.value = null
  } finally {
    loading.value = false
    checking.value = false
  }
}

/**
 * 提交提取码重试。
 */
function submitPassword(): void {
  if (!password.value) {
    ElMessage.warning('请输入提取码')
    return
  }
  load(password.value)
}

/**
 * 将秒数格式化为 mm:ss（内部函数）。
 * @param seconds 时长秒数
 * @returns 形如 02:30 的字符串
 */
function fmtDuration(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

// 主题切换：分享页为公开浅色门户页，进入时切浅色，离开恢复
let prevTheme: string | null = null
onMounted(() => {
  prevTheme = document.documentElement.getAttribute('data-theme')
  document.documentElement.setAttribute('data-theme', 'light')
  load()
})
onUnmounted(() => {
  if (prevTheme == null) document.documentElement.removeAttribute('data-theme')
  else document.documentElement.setAttribute('data-theme', prevTheme)
})
</script>

<template>
  <div class="share-page">
    <!-- 顶栏 -->
    <header class="share-header">
      <div class="brand" @click="router.push('/')">
        <MfLogo :size="22" label="" class="brand-logo" />
        <span class="brand-name">漫序 MangaFlow</span>
      </div>
      <span class="header-tip">分享作品</span>
    </header>

    <main class="share-main">
      <!-- 加载骨架 -->
      <div v-if="loading" class="share-state">
        <el-skeleton animated :rows="5" class="share-skeleton">
          <template #template>
            <div class="sk-video mf-skeleton"></div>
            <div class="mf-skeleton mf-skeleton--text" style="width: 40%; margin: 20px auto"></div>
            <div class="mf-skeleton mf-skeleton--text" style="width: 60%; margin: 0 auto"></div>
          </template>
        </el-skeleton>
      </div>

      <!-- 链接失效 / 已撤回 / 过期 -->
      <div v-else-if="failKind === 'invalid'" class="share-state fail-card">
        <div class="fail-icon" aria-hidden="true">🕊️</div>
        <h1 class="fail-title">链接已失效</h1>
        <p class="fail-desc">{{ errorMsg }}</p>
        <el-button type="primary" size="large" @click="router.push('/')">返回首页</el-button>
      </div>

      <!-- 需要提取码 / 提取码错误 -->
      <div v-else-if="failKind === 'password'" class="share-state pwd-card">
        <div class="fail-icon" aria-hidden="true">🔑</div>
        <h1 class="fail-title">此作品受密码保护</h1>
        <p class="fail-desc">{{ errorMsg }}</p>
        <div class="pwd-row">
          <el-input
            v-model="password"
            placeholder="请输入提取码"
            style="width: 220px"
            clearable
            @keyup.enter="submitPassword"
          />
          <el-button type="primary" :loading="checking" @click="submitPassword">查看</el-button>
        </div>
      </div>

      <!-- 其他异常 -->
      <div v-else-if="failKind === 'other'" class="share-state fail-card">
        <div class="fail-icon" aria-hidden="true">⚠️</div>
        <h1 class="fail-title">加载失败</h1>
        <p class="fail-desc">{{ errorMsg }}</p>
        <el-button type="primary" @click="load()">重试</el-button>
      </div>

      <!-- 正常播放 -->
      <div v-else-if="info" class="share-body">
        <video class="share-video" :src="info.video_url" controls playsinline preload="metadata" />
        <h1 class="share-title">{{ info.title }}</h1>
        <p class="share-meta">
          分辨率 {{ info.resolution }} · 时长 {{ fmtDuration(info.duration) }} ·
          <template v-if="info.published_at">发布于 {{ info.published_at.slice(0, 10) }}</template>
        </p>
        <p class="share-watermark" v-if="info.watermark_id">隐形水印 {{ info.watermark_id }}</p>

        <div class="share-cta">
          <p class="cta-text">使用漫序 MangaFlow 创作你的漫剧</p>
          <el-button type="primary" size="large" round @click="router.push('/')">
            免费开始创作
          </el-button>
        </div>
      </div>
    </main>

    <footer class="share-foot">
      由 漫序 MangaFlow 生成 · 本页面内容由创作者发布
    </footer>
  </div>
</template>

<style scoped>
.share-page {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  background:
    radial-gradient(1000px 500px at 85% -10%, rgba(18, 179, 166, 0.08), transparent 60%),
    radial-gradient(800px 400px at 0% 100%, rgba(124, 107, 240, 0.08), transparent 60%),
    #f5f7f6;
  color: var(--mf-text);
}

/* ---------- 顶栏 ---------- */
.share-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--mf-sp-7);
  height: 60px;
  background: rgba(255, 255, 255, 0.86);
  border-bottom: 1px solid var(--mf-border);
  position: sticky;
  top: 0;
  z-index: 10;
}
.brand {
  display: inline-flex;
  align-items: center;
  gap: var(--mf-sp-2);
  cursor: pointer;
}
.brand-logo {
  display: inline-flex;
  border-radius: 10px;
}
.brand-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
}
.header-tip {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}

/* ---------- 主体 ---------- */
.share-main {
  flex: 1;
  width: min(760px, 94vw);
  margin: 0 auto;
  padding: var(--mf-sp-8) 0 var(--mf-sp-9);
}
.share-state,
.share-body {
  text-align: center;
}
.share-skeleton,
.sk-video {
  width: 100%;
}
.sk-video {
  height: 380px;
  border-radius: var(--mf-r-card);
}

/* 失效/密码态卡片 */
.fail-card,
.pwd-card {
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-card);
  box-shadow: var(--mf-shadow);
  padding: var(--mf-sp-10) var(--mf-sp-6);
}
.fail-icon {
  font-size: 46px;
  line-height: 1;
}
.fail-title {
  margin: var(--mf-sp-4) 0 var(--mf-sp-2);
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
}
.fail-desc {
  margin: 0 0 var(--mf-sp-6);
  color: var(--mf-text-2);
}
.pwd-row {
  display: flex;
  justify-content: center;
  gap: var(--mf-sp-2);
}

/* 播放主体 */
.share-video {
  width: 100%;
  border-radius: var(--mf-r-card);
  background: #000;
  box-shadow: var(--mf-shadow-pop);
  outline: none;
}
.share-title {
  margin: var(--mf-sp-6) 0 var(--mf-sp-2);
  font-size: var(--mf-fs-2xl);
  font-weight: var(--mf-fw-bold);
}
.share-meta {
  margin: 0;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.share-watermark {
  margin: var(--mf-sp-1) 0 0;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.share-cta {
  margin-top: var(--mf-sp-7);
  padding: var(--mf-sp-6);
  border-radius: var(--mf-r-card);
  background: linear-gradient(135deg, var(--mf-brand-soft), var(--mf-brand2-soft));
  border: 1px solid var(--mf-border);
}
.cta-text {
  margin: 0 0 var(--mf-sp-3);
  font-size: var(--mf-fs-lg);
  font-weight: var(--mf-fw-semibold);
}

/* ---------- 底栏 ---------- */
.share-foot {
  text-align: center;
  padding: var(--mf-sp-4);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  border-top: 1px solid var(--mf-border);
}
</style>
