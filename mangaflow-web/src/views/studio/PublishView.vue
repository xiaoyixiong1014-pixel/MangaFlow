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
    ③ 分享设置：当前版本 share_token 存在显示链接 + 复制 + 分享二维码（R-06，
       qrcode toDataURL 渲染，撤回/未分享不显示）；有效期 Select + 密码
       Input + 「生成分享链接」publish + 「撤回分享」revokeShare（confirm）；
  - 导出参数卡：分辨率 Select（720P/1080P/2K）+ 比例 Select + 「导出 MP4」
    （演示提示：成片已含水印标识 watermark_id）+ 下载链接（a href download）；
  - R-03 片段重生成直连：分镜条每项附「重生成」小按钮，点击携带 storyboard_id
    query 跳转生成页（生成页自动定位高亮该分镜）；
  - 审核不通过时顶部 el-alert 指引修改。
-->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, RefreshRight } from '@element-plus/icons-vue'
import QRCode from 'qrcode'
import * as productionApi from '@/api/production'
import * as studioApi from '@/api/studio'
import { useProjectStore } from '@/stores/project'
import type { FilmOutput, GenTask, ReviewRecord, Storyboard } from '@/types'

const route = useRoute()
const router = useRouter()
const projectStore = useProjectStore()

/** 当前项目 ID（取路由参数） */
const projectId = computed(() => Number(route.params.id))

/** 当前选中剧集 ID（S-06 剧集切换器；null = 默认剧集，分镜条不传 episode_id） */
const currentEpisodeId = computed(() => projectStore.currentEpisodeId)

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
/** 分镜占位标记映射（分镜 ID → 最新已完成关键帧任务是否为演示占位，D3 角标） */
const sbPlaceholderMap = ref<Record<number, boolean>>({})
/** 当前成片是否为演示占位产物（取最近一条已完成合成任务的 placeholder 标记；
 * FilmOutput 与任务无直接外键关联，按「项目最近合成任务」近似判定） */
const composePlaceholder = ref(false)
/** 当前版本 */
const currentOutput = computed(() => outputs.value.find((o) => o.is_current) ?? null)

/**
 * 依据任务列表构建「分镜 → 演示占位」映射（分镜条缩略图角标用）。
 * @param items 关键帧任务列表（创建时间倒序）
 * @returns 无
 */
function buildPlaceholderMap(items: GenTask[]): void {
  const map: Record<number, boolean> = {}
  for (const t of items) {
    if (t.type !== 'image' || !t.storyboard_id || t.status !== 'done') continue
    if (!(t.storyboard_id in map)) map[t.storyboard_id] = !!t.placeholder
  }
  sbPlaceholderMap.value = map
}

/**
 * 拉取成片 / 审核 / 分镜数据。
 * S-06：分镜条按当前选中剧集过滤（默认剧集 = 不传 episode_id，返回全部）；
 * 成片版本列表不按剧集过滤（outputs 接口未提供剧集维度，保持既有行为）。
 * 另拉取任务列表（关键帧 / 合成）以解析演示占位标记（任务失败静默不影响主流程）。
 * @returns 无
 */
async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [outs, revs, sb, imgTasks, composeTasks] = await Promise.all([
      productionApi.outputs(projectId.value, { page: 1, page_size: 20 }),
      productionApi.reviews(projectId.value, { page: 1, page_size: 50 }).catch(() => null),
      studioApi
        .listStoryboard(projectId.value, currentEpisodeId.value ?? undefined)
        .catch(() => []),
      productionApi
        .taskList(projectId.value, { page: 1, page_size: 100, type: 'image' })
        .catch(() => null),
      productionApi
        .taskList(projectId.value, { page: 1, page_size: 20, type: 'compose' })
        .catch(() => null),
    ])
    outputs.value = outs.items
    reviewList.value = revs?.items ?? []
    boards.value = sb
    buildPlaceholderMap(imgTasks?.items ?? [])
    composePlaceholder.value = !!(composeTasks?.items ?? []).find(
      (t) => t.status === 'done',
    )?.placeholder
  } finally {
    loading.value = false
  }
}

// 切换剧集（S-06）后按新剧集重新加载分镜条
watch(currentEpisodeId, () => {
  void loadAll()
})

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

/**
 * 跳转生成页重生成该分镜（R-03 片段重生成直连）：携带 storyboard_id query，
 * 生成页 onMounted 自动高亮定位该分镜并提示可重新发起生成。
 * @param b 目标分镜
 * @returns 无
 */
function regenShot(b: Storyboard): void {
  void router.push({
    path: `/projects/${projectId.value}/production`,
    query: { storyboard_id: b.id },
  })
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

/** 有效分享链接（未撤回才有值；二维码与链接区共用的展示口径） */
const validShareLink = computed(() =>
  shareLink.value && !currentOutput.value?.share_revoked ? shareLink.value : '',
)

/** 分享二维码 data URL（R-06；无效/撤回/生成失败时为空串隐藏） */
const qrDataUrl = ref('')

// 有效分享链接变化时同步生成二维码（R-06 分享二维码）：
// 生成成功后渲染，撤回/未分享状态清空不显示；失败静默隐藏（复制链接仍可用）
watch(
  validShareLink,
  (link) => {
    if (!link) {
      qrDataUrl.value = ''
      return
    }
    QRCode.toDataURL(link, { width: 132, margin: 1 })
      .then((url) => {
        // 异步回调期间链接可能已被撤回/切换，回写前再校验一次
        if (validShareLink.value === link) qrDataUrl.value = url
      })
      .catch(() => {
        qrDataUrl.value = ''
      })
  },
  { immediate: true },
)

/**
 * 生成分享链接（publish：指定当前版本 + 有效期 + 提取码）。
 * 闸门拦截（400）时把后端 detail 展示为醒目错误条（Task 10.4 S-09）。
 * @returns 无
 */
async function doPublish(): Promise<void> {
  if (!currentOutput.value || publishing.value) return
  publishing.value = true
  gateError.value = ''
  try {
    const res = await productionApi.publish(projectId.value, {
      output_id: currentOutput.value.id,
      share_expires_days: Number(expiresDays.value) || 7,
      share_password: sharePassword.value.trim() || undefined,
    })
    ElMessage.success(`分享链接已生成，有效期至 ${res.expires_at}`)
    await loadAll()
  } catch (e) {
    // 拦截器已 toast；这里额外捕获后端 detail（闸门拦截原因）展示为错误条
    gateError.value = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
      ? String((e as { response: { data: { detail: unknown } } }).response.data.detail)
      : '发布失败，请检查审核记录后重试'
  } finally {
    publishing.value = false
  }
}

/** 闸门拦截错误信息（非空时顶部展示醒目错误条） */
const gateError = ref('')

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
/** 导出比例（展示参数；成片下载按当前版本画幅返回） */
const exportRatio = ref<'9:16' | '16:9' | '1:1'>('9:16')
/** MP4 导出进行中 */
const exportingMp4 = ref(false)

/**
 * 导出 MP4（Task 10.4）：按所选分辨率调 GET /publish/outputs/{id}/download
 * 下载 blob（本机无 ffmpeg 时后端自动降级返回原片）。
 * @returns 无
 */
async function exportMp4(): Promise<void> {
  if (!currentOutput.value || exportingMp4.value) return
  exportingMp4.value = true
  try {
    const res = exportResolution.value.toLowerCase() as '720p' | '1080p' | '2k'
    await productionApi.downloadOutput(currentOutput.value.id, res, currentOutput.value.version_no)
    ElMessage.success(`MP4 导出已开始（${exportResolution.value}，含隐形水印标识 ${currentOutput.value.watermark_id ?? '—'}）`)
  } catch {
    // 错误已由拦截器提示（400 分辨率非法 / 404 成片不可下载）
  } finally {
    exportingMp4.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <div class="page-container pub-view">
    <h2 class="page-title">审核与发布</h2>

    <!-- 闸门拦截错误条（Task 10.4：发布 400 时展示后端 detail） -->
    <el-alert
      v-if="gateError"
      type="error"
      show-icon
      :closable="true"
      class="top-alert gate-error"
      title="发布被审核闸门拦截"
      :description="gateError"
      @close="gateError = ''"
    />

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
              <!-- D3 演示占位角标：最近一次合成任务命中 mock 兜底渠道 -->
              <el-tag v-if="composePlaceholder" size="small" type="warning" effect="light">
                演示占位
              </el-tag>
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
            <!-- D3 演示占位角标：该镜关键帧任务命中 mock 兜底渠道 -->
            <el-tag
              v-if="sbPlaceholderMap[b.id]"
              class="shot-ph-tag"
              size="small"
              type="warning"
              effect="light"
            >
              占位
            </el-tag>
            <span class="shot-no">#{{ String(b.sequence).padStart(2, '0') }}</span>
            <!-- R-03 片段重生成直连：跳生成页并定位该分镜 -->
            <el-button
              class="shot-regen"
              size="small"
              type="primary"
              circle
              :icon="RefreshRight"
              title="到生成页重新生成分镜"
              @click.stop="regenShot(b)"
            />
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
            <el-button type="primary" :loading="exportingMp4" @click="exportMp4">导出 MP4</el-button>
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
                <!-- 人工复核状态（Task 10.4 S-09：增强展示 manual_status） -->
                <el-tag
                  v-if="r.manual_status"
                  size="small"
                  :type="r.manual_status === 'approved' ? 'success' : r.manual_status === 'rejected' ? 'danger' : 'warning'"
                  effect="light"
                >
                  人工·{{ { approved: '已通过', rejected: '已驳回', pending: '待复核' }[r.manual_status] ?? r.manual_status }}
                </el-tag>
                <span class="review-target">
                  {{ r.target_type === 'film' ? '成片' : `${r.target_type} #${r.target_id ?? '—'}` }}
                </span>
                <span class="review-time">{{ r.created_at?.slice(0, 16).replace('T', ' ') }}</span>
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
              <div v-if="validShareLink" class="share-link-box">
                <div class="layer-label">当前分享链接</div>
                <div class="share-link">
                  <span class="share-url">{{ shareLink }}</span>
                  <el-button size="small" :icon="CopyDocument" @click="copyLink">复制</el-button>
                </div>
                <!-- R-06 分享二维码：链接生成成功后渲染，撤回/未分享状态不显示 -->
                <div v-if="qrDataUrl" class="share-qr">
                  <img :src="qrDataUrl" alt="分享链接二维码" class="qr-img" />
                  <span class="qr-tip">手机扫码访问分享页</span>
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
              <div class="share-note">生成成功后将展示访问链接与二维码，可复制或扫码分享</div>
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
/* D3 演示占位角标（分镜条缩略图右上角） */
.shot-ph-tag {
  position: absolute;
  right: 4px;
  top: 4px;
  transform: scale(0.85);
  transform-origin: top right;
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
.review-time {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.gate-error {
  border: 1px solid var(--el-color-danger);
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
/* R-06 分享二维码（链接 + 提示横向排列） */
.share-qr {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-2);
  border: 1px dashed var(--mf-border);
  border-radius: var(--mf-r-ctl);
}
.qr-img {
  width: 132px;
  height: 132px;
  display: block;
  border-radius: var(--mf-r-ctl);
}
.qr-tip {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
/* R-03 分镜条重生成直连按钮（缩略图右下角） */
.shot-regen {
  position: absolute;
  right: 4px;
  bottom: 2px;
}
.share-note {
  margin-top: var(--mf-sp-3);
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
</style>
