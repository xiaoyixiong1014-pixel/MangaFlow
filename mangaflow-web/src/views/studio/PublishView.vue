<!--
  审核与发布页（文件级注释）。

  对应原型 S-09，路由 /projects/:id/publish。
  功能：
  - 左侧主播放器：当前成片（video_url 为 mp4 直接播放；SVG 占位则展示图 + 说明）；
    底部分镜条：横向缩略图列表，点击按分镜时长占比 seek 到对应时间点并高亮；
  - 右侧三栏 Tab：
    ① 审核结果：reviews 列表（gate 三级闸门 Tag + result Tag + suggestion 建议；
       violate 红色定位镜头号）；
    ② 版本管理：outputs 列表（版本号 / 分辨率 / 时长 / is_current Tag +「设为当前」
       setCurrent + 选两个版本显示参数 diff 表）；
    ③ 分享设置：当前版本 share_token 存在显示链接 + 复制；有效期 Select + 密码
       Input + 「生成分享链接」publish + 「撤回分享」revokeShare（confirm）；
  - 导出参数卡：分辨率 Select（720P/1080P/2K）+ 比例 Select + 「导出 MP4」
    （演示提示：成片已含水印标识 watermark_id）+ 下载链接（a href download）；
  - 审核不通过时顶部 el-alert 指引修改。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument } from '@element-plus/icons-vue'
import * as productionApi from '@/api/production'
import * as studioApi from '@/api/studio'
import type { FilmOutput, ReviewRecord, Storyboard } from '@/types'

const route = useRoute()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

// ============================================================
// 基础数据
// ============================================================

/** 首屏加载中 */
const loading = ref(true)
/** 成片版本列表 */
const outputs = ref<FilmOutput[]>([])
/** 审核记录列表 */
const reviewList = ref<ReviewRecord[]>([])
/** 分镜列表（分镜条用） */
const boards = ref<Storyboard[]>([])
/** 当前版本 */
const currentOutput = computed(() => outputs.value.find((o) => o.is_current) ?? null)

/**
 * 拉取成片 / 审核 / 分镜数据。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [outs, revs, sb] = await Promise.all([
      productionApi.outputs(projectId.value, { page: 1, page_size: 20 }),
      productionApi.reviews(projectId.value, { page: 1, page_size: 50 }).catch(() => null),
      studioApi.listStoryboard(projectId.value).catch(() => []),
    ])
    outputs.value = outs.items
    reviewList.value = revs?.items ?? []
    boards.value = sb
  } finally {
    loading.value = false
  }
}

// ============================================================
// 播放器与分镜条
// ============================================================

/** video 元素引用 */
const videoRef = ref<HTMLVideoElement | null>(null)
/** 当前高亮的分镜下标 */
const activeShot = ref(0)

/** 播放器地址：mp4 直接播；svg（占位海报）用图展示 */
const isPlayable = computed(
  () => !!currentOutput.value && /\.(mp4|webm|mov)(\?|$)/i.test(currentOutput.value.url),
)

/**
 * 点击分镜条：按分镜时长占比 seek 到该镜起点（成片时长与分镜总时长做比例换算）。
 * @param index 分镜下标
 * @returns 无
 */
function seekToShot(index: number): void {
  activeShot.value = index
  const video = videoRef.value
  if (!video || !isPlayable.value) return
  const totalBoard = boards.value.reduce((s, b) => s + (b.duration || 0), 0)
  if (!totalBoard) return
  const startSec = boards.value
    .slice(0, index)
    .reduce((s, b) => s + (b.duration || 0), 0)
  const ratio = video.duration && isFinite(video.duration) ? video.duration / totalBoard : 1
  video.currentTime = Math.min(video.duration - 0.05, startSec * ratio)
  void video.play().catch(() => undefined)
}

// ============================================================
// 审核结果
// ============================================================

/** 是否存在未通过的审核记录（顶部警示条） */
const hasViolate = computed(() =>
  reviewList.value.some((r) => r.result === 'violate' || r.result === 'suspect'),
)
/** 闸门 → 中文文案 */
const gateLabel: Record<string, string> = {
  generate: '生成级',
  compose: '合成级',
  publish: '导出版',
}
/** 审核结果 → Tag 类型与文案 */
const resultMap: Record<string, { label: string; type: 'success' | 'warning' | 'danger' }> = {
  pass: { label: '通过', type: 'success' },
  suspect: { label: '疑似', type: 'warning' },
  violate: { label: '违规', type: 'danger' },
}

// ============================================================
// 版本管理与对比
// ============================================================

/** 设为当前版本进行中的版本 ID */
const setCurrentBusyId = ref<number | null>(null)

/**
 * 设为当前成片版本（版本回退）。
 * @param o 目标版本
 * @returns 无
 */
async function setCurrentVersion(o: FilmOutput): Promise<void> {
  if (setCurrentBusyId.value) return
  setCurrentBusyId.value = o.id
  try {
    await productionApi.setCurrent(projectId.value, o.id)
    ElMessage.success(`已将 v${o.version_no} 设为当前版本`)
    await loadAll()
  } finally {
    setCurrentBusyId.value = null
  }
}

/** 对比选择的两个版本 ID */
const diffA = ref<number | null>(null)
const diffB = ref<number | null>(null)

/**
 * 参数 diff 行定义（两个版本逐项对比）。
 */
const diffRows = computed(() => {
  const a = outputs.value.find((o) => o.id === diffA.value)
  const b = outputs.value.find((o) => o.id === diffB.value)
  if (!a || !b) return []
  return [
    { key: '版本号', va: `v${a.version_no}`, vb: `v${b.version_no}` },
    { key: '分辨率', va: a.resolution, vb: b.resolution },
    { key: '画幅比例', va: a.aspect_ratio, vb: b.aspect_ratio },
    { key: '时长', va: `${a.duration}s`, vb: `${b.duration}s` },
    { key: '水印标识', va: a.watermark_id || '—', vb: b.watermark_id || '—' },
    { key: '创建时间', va: a.created_at ?? '—', vb: b.created_at ?? '—' },
  ]
})

// ============================================================
// 分享设置
// ============================================================

/** 分享有效期（天） */
const expiresDays = ref<number | 7>(7)
/** 提取码 */
const sharePassword = ref('')
/** 发布进行中（防重复点击） */
const publishing = ref(false)

/** 分享完整链接（当前版本 share_token 拼接） */
const shareLink = computed(() =>
  currentOutput.value?.share_token
    ? `${window.location.origin}/share/${currentOutput.value.share_token}`
    : '',
)

/**
 * 生成分享链接（publish：指定当前版本 + 有效期 + 提取码）。
 * @returns 无
 */
async function doPublish(): Promise<void> {
  if (!currentOutput.value || publishing.value) return
  publishing.value = true
  try {
    const res = await productionApi.publish(projectId.value, {
      output_id: currentOutput.value.id,
      share_expires_days: Number(expiresDays.value) || 7,
      share_password: sharePassword.value.trim() || undefined,
    })
    ElMessage.success(`分享链接已生成，有效期至 ${res.expires_at}`)
    await loadAll()
  } finally {
    publishing.value = false
  }
}

/**
 * 撤回分享（confirm 二次确认）。
 * @returns 无
 */
async function doRevoke(): Promise<void> {
  try {
    await ElMessageBox.confirm('撤回后分享链接立即失效，确认撤回？', '撤回分享', {
      type: 'warning',
    })
  } catch {
    return
  }
  await productionApi.revokeShare(projectId.value)
  ElMessage.success('分享已撤回')
  await loadAll()
}

/**
 * 复制分享链接到剪贴板。
 * @returns 无
 */
async function copyLink(): Promise<void> {
  if (!shareLink.value) return
  try {
    await navigator.clipboard.writeText(shareLink.value)
    ElMessage.success('链接已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择链接复制')
  }
}

// ============================================================
// 导出参数卡
// ============================================================

/** 导出分辨率 */
const exportResolution = ref<'720P' | '1080P' | '2K'>('1080P')
/** 导出比例 */
const exportRatio = ref<'9:16' | '16:9' | '1:1'>('9:16')

/**
 * 导出 MP4（演示提示：演示环境已含成片与隐形水印，给出下载链接）。
 * @returns 无
 */
function exportMp4(): void {
  const wm = currentOutput.value?.watermark_id
  ElMessage.info(
    `演示环境已含成片（分辨率选择 ${exportResolution.value} / 比例 ${exportRatio.value} 仅作参数演示），隐形水印标识：${wm ?? '—'}，可点击下方链接下载`,
  )
}

onMounted(loadAll)
</script>

<template>
  <div class="page-container pub-view">
    <h2 class="page-title">审核与发布</h2>

    <!-- 审核未通过顶部警示 -->
    <el-alert
      v-if="hasViolate"
      type="error"
      show-icon
      :closable="false"
      class="top-alert"
      title="存在疑似 / 违规审核记录：请在右侧「审核结果」查看定位镜头与修改建议，处理后重新合成"
    />

    <!-- 空态：还没有成片 -->
    <el-card v-if="!loading && !outputs.length" shadow="never">
      <el-empty description="还没有成片，请先到「生成」页完成合成">
        <el-button type="primary" @click="$router.push(`/projects/${projectId}/production`)">
          去合成成片
        </el-button>
      </el-empty>
    </el-card>

    <!-- 首屏骨架 -->
    <el-skeleton v-else-if="loading" :rows="8" animated />

    <div v-else class="pub-layout">
      <!-- ============ 左：播放器 + 分镜条 ============ -->
      <div class="pub-left">
        <el-card shadow="never" class="player-card">
          <template #header>
            <div class="player-head">
              <span>当前成片 v{{ currentOutput?.version_no ?? '—' }}</span>
              <el-tag v-if="currentOutput?.is_current" size="small" type="success">当前版本</el-tag>
            </div>
          </template>
          <template v-if="currentOutput">
            <!-- mp4 直接播放 -->
            <video
              v-if="isPlayable"
              ref="videoRef"
              :key="currentOutput.id"
              :src="currentOutput.url"
              controls
              class="player"
            />
            <!-- SVG 占位：图 + 说明 -->
            <div v-else class="player player--placeholder">
              <img :src="currentOutput.url" :alt="`成片 v${currentOutput.version_no} 预览`" class="poster" />
              <div class="poster-tip">
                该版本为演示合成结果（占位海报，非 mp4 流），实际播放请在导出 MP4 后进行
              </div>
            </div>
          </template>
          <el-empty v-else description="暂无当前版本" :image-size="80" />
        </el-card>

        <!-- 分镜条（点击 seek + 高亮联动） -->
        <div v-if="boards.length" class="shot-strip">
          <div
            v-for="(b, i) in boards"
            :key="b.id"
            class="shot-item"
            :class="{ 'shot-item--active': i === activeShot }"
            role="button"
            @click="seekToShot(i)"
          >
            <el-image
              v-if="b.keyframe_url"
              :src="b.keyframe_url"
              fit="cover"
              class="shot-img"
            />
            <div v-else class="shot-img shot-img--empty">#{{ String(b.sequence).padStart(2, '0') }}</div>
            <span class="shot-no">#{{ String(b.sequence).padStart(2, '0') }}</span>
          </div>
        </div>

        <!-- 导出参数卡 -->
        <el-card shadow="never" class="export-card">
          <template #header><span>导出与分享</span></template>
          <div class="export-form">
            <span>分辨率</span>
            <el-select v-model="exportResolution" style="width: 120px">
              <el-option label="720P" value="720P" />
              <el-option label="1080P" value="1080P" />
              <el-option label="2K" value="2K" />
            </el-select>
            <span>比例</span>
            <el-select v-model="exportRatio" style="width: 110px">
              <el-option label="9:16" value="9:16" />
              <el-option label="16:9" value="16:9" />
              <el-option label="1:1" value="1:1" />
            </el-select>
            <el-button type="primary" @click="exportMp4">导出 MP4</el-button>
            <a
              v-if="currentOutput"
              :href="currentOutput.url"
              :download="`mangaflow-v${currentOutput.version_no}.mp4`"
              class="download-link"
            >
              下载成片文件
            </a>
          </div>
        </el-card>
      </div>

      <!-- ============ 右：审核 / 版本 / 分享 ============ -->
      <div class="pub-right">
        <el-tabs type="border-card" class="pub-tabs">
          <!-- 审核结果 -->
          <el-tab-pane :label="`审核结果 (${reviewList.length})`">
            <el-empty v-if="!reviewList.length" description="暂无审核记录" />
            <div v-for="r in reviewList" :key="r.id" class="review-item">
              <div class="review-line">
                <el-tag size="small" effect="plain">{{ gateLabel[r.gate] ?? r.gate }}</el-tag>
                <el-tag size="small" :type="resultMap[r.result]?.type ?? 'info'">
                  {{ resultMap[r.result]?.label ?? r.result }}
                </el-tag>
                <span class="review-target">
                  {{ r.target_type === 'film' ? '成片' : `${r.target_type} #${r.target_id ?? '—'}` }}
                </span>
              </div>
              <div v-if="r.violation_type" class="review-violate">
                违规类型：{{ r.violation_type }}
                <template v-if="r.target_id">（定位镜头 #{{ r.target_id }}）</template>
              </div>
              <div v-if="r.suggestion" class="review-tip">建议：{{ r.suggestion }}</div>
            </div>
          </el-tab-pane>

          <!-- 版本管理 -->
          <el-tab-pane :label="`成片版本 (${outputs.length})`">
            <div v-for="o in outputs" :key="o.id" class="ver-item">
              <div class="ver-line">
                <span class="ver-no">v{{ o.version_no }}</span>
                <el-tag v-if="o.is_current" size="small" type="success">当前</el-tag>
                <span class="ver-meta">{{ o.resolution }} · {{ o.duration }}s</span>
                <span class="ver-time">{{ o.created_at?.slice(0, 16).replace('T', ' ') }}</span>
                <el-button
                  v-if="!o.is_current"
                  size="small"
                  text
                  type="primary"
                  :loading="setCurrentBusyId === o.id"
                  @click="setCurrentVersion(o)"
                >
                  设为当前
                </el-button>
              </div>
            </div>

            <el-divider>版本对比</el-divider>
            <div class="diff-select">
              <el-select v-model="diffA" placeholder="版本 A" size="small" style="width: 120px">
                <el-option v-for="o in outputs" :key="o.id" :label="`v${o.version_no}`" :value="o.id" />
              </el-select>
              <span>对比</span>
              <el-select v-model="diffB" placeholder="版本 B" size="small" style="width: 120px">
                <el-option v-for="o in outputs" :key="o.id" :label="`v${o.version_no}`" :value="o.id" />
              </el-select>
            </div>
            <el-table v-if="diffRows.length" :data="diffRows" size="small" class="diff-table">
              <el-table-column prop="key" label="参数" width="100" />
              <el-table-column prop="va" label="版本 A" />
              <el-table-column prop="vb" label="版本 B" />
            </el-table>
          </el-tab-pane>

          <!-- 分享设置 -->
          <el-tab-pane label="分享设置">
            <template v-if="currentOutput">
              <!-- 已有分享链接 -->
              <div v-if="shareLink && !currentOutput.share_revoked" class="share-link-box">
                <div class="layer-label">当前分享链接</div>
                <div class="share-link">
                  <span class="share-url">{{ shareLink }}</span>
                  <el-button size="small" :icon="CopyDocument" @click="copyLink">复制</el-button>
                </div>
                <div class="share-meta">
                  <el-tag
                    v-if="currentOutput.has_password"
                    size="small"
                    type="warning"
                    effect="plain"
                  >
                    已设提取码
                  </el-tag>
                  <span v-if="currentOutput.share_expires_at">
                    有效期至 {{ currentOutput.share_expires_at.slice(0, 16).replace('T', ' ') }}
                  </span>
                </div>
                <el-button type="danger" plain size="small" @click="doRevoke">撤回分享</el-button>
              </div>
              <el-divider>{{ shareLink ? '重新生成链接' : '生成分享链接' }}</el-divider>

              <el-form label-width="80px">
                <el-form-item label="有效期">
                  <el-select v-model="expiresDays" style="width: 140px">
                    <el-option label="1 天" :value="1" />
                    <el-option label="7 天" :value="7" />
                    <el-option label="30 天" :value="30" />
                    <el-option label="永久" :value="36500" />
                  </el-select>
                </el-form-item>
                <el-form-item label="提取码">
                  <el-input
                    v-model="sharePassword"
                    placeholder="留空则无需提取码"
                    maxlength="12"
                    style="width: 180px"
                  />
                </el-form-item>
              </el-form>
              <el-button type="primary" :loading="publishing" @click="doPublish">
                生成分享链接
              </el-button>
              <div class="share-note">二维码：发布后可在浏览器地址栏直接访问分享链接</div>
            </template>
            <el-empty v-else description="暂无当前版本可分享" />
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pub-view {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-5);
}
.pub-view .page-title {
  margin: 0;
}
.top-alert {
  margin-bottom: var(--mf-sp-4);
}
.pub-layout {
  display: grid;
  grid-template-columns: minmax(380px, 1.4fr) minmax(340px, 1fr);
  gap: var(--mf-sp-5);
  align-items: start;
}
.pub-left {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-4);
}
.player-head {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.player {
  width: 100%;
  border-radius: var(--mf-r-ctl);
  background: #000;
  max-height: 480px;
}
.player--placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--mf-sp-3);
  background: var(--mf-surface-2);
  padding: var(--mf-sp-4);
}
.poster {
  max-width: 100%;
  max-height: 360px;
  border-radius: var(--mf-r-ctl);
}
.poster-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.shot-strip {
  display: flex;
  gap: var(--mf-sp-2);
  overflow-x: auto;
  padding-bottom: 4px;
}
.shot-item {
  position: relative;
  flex-shrink: 0;
  width: 88px;
  cursor: pointer;
  border: 2px solid transparent;
  border-radius: var(--mf-r-ctl);
  overflow: hidden;
  transition: border-color var(--mf-dur) var(--mf-ease);
}
.shot-item--active {
  border-color: var(--mf-brand);
}
.shot-img {
  width: 88px;
  height: 60px;
  display: block;
}
.shot-img--empty {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--mf-surface-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.shot-no {
  position: absolute;
  left: 4px;
  bottom: 2px;
  font-size: 10px;
  color: #fff;
  background: rgba(0, 0, 0, 0.5);
  border-radius: 4px;
  padding: 0 4px;
}
.export-form {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  flex-wrap: wrap;
}
.download-link {
  color: var(--mf-brand);
  font-size: var(--mf-fs-sm);
}
.review-item {
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.review-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.review-target {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.review-violate {
  margin-top: 4px;
  color: var(--el-color-danger);
  font-size: var(--mf-fs-sm);
}
.review-tip {
  margin-top: 4px;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.ver-item {
  padding: var(--mf-sp-2) 0;
  border-bottom: 1px dashed var(--mf-border);
}
.ver-line {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
}
.ver-no {
  font-weight: var(--mf-fw-semibold);
}
.ver-meta {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.ver-time {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
  margin-left: auto;
}
.diff-select {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.diff-table {
  width: 100%;
}
.share-link-box {
  display: flex;
  flex-direction: column;
  gap: var(--mf-sp-2);
  align-items: flex-start;
}
.layer-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.share-link {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  width: 100%;
}
.share-url {
  flex: 1;
  word-break: break-all;
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-ctl);
  padding: 6px 10px;
  font-size: var(--mf-fs-sm);
}
.share-meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.share-note {
  margin-top: var(--mf-sp-3);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
</style>
