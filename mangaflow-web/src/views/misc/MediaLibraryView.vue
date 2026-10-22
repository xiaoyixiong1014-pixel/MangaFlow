<!--
  素材库（文件级注释，P-03）。
  四类型 Tab（图片/视频/音频/文本）+ 来源筛选 + 搜索 + 网格预览卡；
  支持真实上传（multipart）、批量选择、删除（被引用 409 由拦截器提示后端 detail）。
  说明：后端 /api/media 列表暂不支持关键字参数，搜索为当前页本地过滤。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { UploadRequestOptions } from 'element-plus'
import * as mediaApi from '@/api/media'
import * as projectsApi from '@/api/projects'
import type { MediaAsset, Project } from '@/types'

// ---------- 状态 ----------
/** 当前类型 Tab */
const mediaType = ref<'image' | 'video' | 'audio' | 'text'>('image')
/** 来源筛选（空为全部） */
const source = ref('')
/** 搜索关键字（当前页本地过滤） */
const keyword = ref('')
/** 列表数据 */
const items = ref<MediaAsset[]>([])
/** 总数 */
const total = ref(0)
/** 页码 */
const page = ref(1)
/** 每页条数 */
const pageSize = ref(12)
/** 加载状态 */
const loading = ref(true)
/** 上传中 */
const uploading = ref(false)
/** 已勾选素材 */
const selected = ref<MediaAsset[]>([])
/** 项目列表（展示项目名） */
const projects = ref<Project[]>([])

/** 类型 Tab 定义 */
const typeTabs = [
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
  { value: 'audio', label: '音频' },
  { value: 'text', label: '文本' },
] as const

/** 来源选项（与后端枚举一致；project=项目产物聚合，2026-10-21 存储方式更新） */
const sourceOptions = [
  { value: 'ai_generated', label: 'AI 生成' },
  { value: 'upload', label: '上传' },
  { value: 'canvas', label: '画布' },
  { value: 'studio', label: 'Studio' },
  { value: 'project', label: '项目产物' },
]

/** 来源中文文案映射 */
const sourceLabels: Record<string, string> = {
  ai_generated: 'AI 生成',
  upload: '上传',
  canvas: '画布',
  studio: 'Studio',
  project: '项目产物',
}

/**
 * 来源 Tag 颜色映射（内部函数，项目产物高亮区分）。
 * @param s 来源标识
 * @returns el-tag type
 */
function sourceTagType(s: string): 'primary' | 'success' | 'warning' | 'info' {
  if (s === 'project') return 'warning'
  if (s === 'ai_generated') return 'primary'
  if (s === 'upload') return 'success'
  return 'info'
}

/**
 * 过滤后的当前页列表（本地关键字过滤，仅对当前页生效）。
 */
const filteredItems = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter((m) => JSON.stringify(m.meta ?? {}).toLowerCase().includes(kw))
})

/**
 * 拼接素材访问地址（内部函数）：相对存储路径补 /storage 前缀。
 * @param url 后端返回的相对路径
 * @returns 可访问 URL
 */
function resolveUrl(url: string): string {
  if (!url) return ''
  if (/^https?:\/\//.test(url) || url.startsWith('/')) return url
  return `/storage/${url}`
}

/**
 * 从 meta 中取展示摘录（内部函数）：文本类型取 excerpt/content，否则取文件名。
 * @param m 素材记录
 * @returns 摘录文案
 */
function excerptOf(m: MediaAsset): string {
  const meta = (m.meta ?? {}) as Record<string, unknown>
  const cand = (meta.excerpt ?? meta.content ?? meta.filename ?? '') as string
  return typeof cand === 'string' ? cand.slice(0, 80) : ''
}

/**
 * 拉取素材列表。
 */
async function fetchList(): Promise<void> {
  loading.value = true
  try {
    const resp = await mediaApi.list({
      page: page.value,
      page_size: pageSize.value,
      media_type: mediaType.value,
      source: source.value || undefined,
    })
    items.value = resp.items
    total.value = resp.total
    selected.value = []
  } catch {
    // 错误已由拦截器 ElMessage(detail) 提示
  } finally {
    loading.value = false
  }
}

/**
 * 类型/来源切换：重置页码并重新查询。
 */
function onFilterChange(): void {
  page.value = 1
  fetchList()
}

/**
 * 自定义上传：走真实 media.upload（FormData multipart）。
 * @param options el-upload 上传上下文（file 为原始 File 对象）
 */
async function doUpload(options: UploadRequestOptions): Promise<void> {
  const file = options.file as File
  // 依据 MIME 推断媒体类型（缺省 image）
  let mtype: 'image' | 'video' | 'audio' | 'text' = 'image'
  if (file.type.startsWith('video/')) mtype = 'video'
  else if (file.type.startsWith('audio/')) mtype = 'audio'
  else if (file.type.startsWith('text/') || /\.(txt|md|json)$/i.test(file.name)) mtype = 'text'
  uploading.value = true
  try {
    await mediaApi.upload(file, { media_type: mtype })
    ElMessage.success('上传成功')
    // 上传后切到对应类型便于看到新素材
    mediaType.value = mtype
    page.value = 1
    await fetchList()
  } catch {
    // 错误已由拦截器提示
  } finally {
    uploading.value = false
  }
}

/**
 * 删除素材：二次确认；被引用（409）时拦截器已展示后端 detail。
 * @param m 素材记录
 */
async function removeItem(m: MediaAsset): Promise<void> {
  try {
    await ElMessageBox.confirm('确定删除该素材吗？被引用的素材无法删除。', '删除素材', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  try {
    await mediaApi.remove(m.id)
    ElMessage.success('已删除')
    await fetchList()
  } catch {
    // 409 引用保护等错误已由拦截器 ElMessage(detail) 提示
  }
}

/**
 * 批量"发送到画布"（演示级）：复制内容引用到剪贴板并提示在画布中使用。
 */
async function sendToCanvas(): Promise<void> {
  if (selected.value.length === 0) return
  const refs = selected.value.map((m) => resolveUrl(m.url)).join('\n')
  try {
    await navigator.clipboard.writeText(refs)
  } catch {
    /* 剪贴板权限失败不阻断提示 */
  }
  ElMessage.success(`已复制 ${selected.value.length} 条内容引用，请在画布中使用`)
  selected.value = []
}

// 首次进入：并行拉取素材与项目名
onMounted(async () => {
  fetchList()
  try {
    const resp = await projectsApi.list({ page: 1, page_size: 100 })
    projects.value = resp.items
  } catch {
    /* 项目名仅用于展示，失败不阻断 */
  }
})
</script>

<template>
  <div class="page-container">
    <h2 class="page-title">素材库</h2>

    <!-- 类型 Tab + 筛选 + 操作 -->
    <div class="toolbar">
      <el-tabs v-model="mediaType" class="type-tabs" @tab-change="onFilterChange">
        <el-tab-pane v-for="t in typeTabs" :key="t.value" :label="t.label" :name="t.value" />
      </el-tabs>
      <div class="toolbar-right">
        <el-select
          v-model="source"
          style="width: 130px"
          placeholder="全部来源"
          clearable
          @change="onFilterChange"
        >
          <el-option v-for="s in sourceOptions" :key="s.value" :label="s.label" :value="s.value" />
        </el-select>
        <el-input
          v-model="keyword"
          style="width: 200px"
          placeholder="搜索当前页"
          clearable
        />
        <el-upload
          :show-file-list="false"
          :http-request="doUpload"
          accept="image/*,video/*,audio/*,.txt,.md,.json"
        >
          <el-button type="primary" :loading="uploading">上传素材</el-button>
        </el-upload>
      </div>
    </div>

    <!-- 批量操作条 -->
    <div v-if="selected.length > 0" class="bulk-bar">
      <span>已选 {{ selected.length }} 项</span>
      <el-button size="small" type="primary" plain @click="sendToCanvas">发送到画布</el-button>
    </div>

    <!-- 加载骨架 -->
    <div v-if="loading" class="grid">
      <div v-for="i in 6" :key="i" class="media-card">
        <div class="mf-skeleton sk-preview"></div>
        <div style="padding: 10px">
          <div class="mf-skeleton mf-skeleton--text" style="width: 70%"></div>
        </div>
      </div>
    </div>

    <!-- 空态引导 -->
    <el-empty
      v-else-if="filteredItems.length === 0"
      :description="keyword ? '没有匹配的素材' : '还没有素材，去创作或上传一个吧'"
    >
      <el-upload :show-file-list="false" :http-request="doUpload" accept="image/*,video/*,audio/*,.txt,.md,.json">
        <el-button type="primary" :loading="uploading">上传素材</el-button>
      </el-upload>
    </el-empty>

    <!-- 素材网格 -->
    <template v-else>
      <div class="grid">
        <div v-for="m in filteredItems" :key="m.id" class="media-card">
          <!-- 预览区 -->
          <div class="preview">
            <el-checkbox
              class="pick"
              :model-value="selected.includes(m)"
              @change="(v: unknown) => (v ? selected.push(m) : (selected = selected.filter((x) => x.id !== m.id)))"
            />
            <el-image
              v-if="m.media_type === 'image'"
              :src="resolveUrl(m.url)"
              fit="cover"
              class="preview-inner"
              :preview-src-list="[resolveUrl(m.url)]"
              preview-teleported
            >
              <template #error>
                <div class="preview-fallback">🖼️ 预览失败</div>
              </template>
            </el-image>
            <video
              v-else-if="m.media_type === 'video'"
              :src="resolveUrl(m.url)"
              class="preview-inner"
              controls
              preload="metadata"
            ></video>
            <audio
              v-else-if="m.media_type === 'audio'"
              :src="resolveUrl(m.url)"
              class="audio-ctl"
              controls
              preload="metadata"
            ></audio>
            <div v-else class="text-preview">{{ excerptOf(m) || '（无摘录）' }}</div>
          </div>
          <!-- 信息区 -->
          <div class="info">
            <div class="info-row">
              <el-tag size="small" :type="sourceTagType(m.source)">
                {{ sourceLabels[m.source] ?? m.source }}
              </el-tag>
              <span class="time">{{ (m.created_at ?? '').slice(0, 16).replace('T', ' ') }}</span>
            </div>
            <div class="info-row">
              <span class="project" :title="projects.find((p) => p.id === m.project_id)?.title ?? '独立素材'">
                {{ projects.find((p) => p.id === m.project_id)?.title ?? '独立素材' }}
              </span>
              <!-- 项目产物（关键帧/成片/试音/剧本正文）随项目存续，不提供删除 -->
              <el-button v-if="m.source !== 'project'" link type="danger" size="small" @click="removeItem(m)">删除</el-button>
            </div>
          </div>
        </div>
      </div>

      <!-- 分页 -->
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[12, 24, 48]"
          layout="total, sizes, prev, pager, next"
          @current-change="fetchList"
          @size-change="onFilterChange"
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--mf-sp-4);
  margin-bottom: var(--mf-sp-4);
  flex-wrap: wrap;
}
.type-tabs {
  flex: 1;
  min-width: 280px;
}
.type-tabs :deep(.el-tabs__header) {
  margin-bottom: 0;
}
.toolbar-right {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  padding-bottom: 4px;
}
.bulk-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  padding: var(--mf-sp-2) var(--mf-sp-4);
  margin-bottom: var(--mf-sp-4);
  border-radius: var(--mf-r-ctl);
  background: var(--mf-brand-soft);
  color: var(--mf-brand);
  font-size: var(--mf-fs-sm);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: var(--mf-sp-4);
}
.media-card {
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  overflow: hidden;
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.media-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.preview {
  position: relative;
  height: 150px;
  background: var(--mf-surface-2);
  display: flex;
  align-items: center;
  justify-content: center;
}
.pick {
  position: absolute;
  top: 6px;
  left: 6px;
  z-index: 2;
}
.preview-inner {
  width: 100%;
  height: 100%;
  display: block;
}
.audio-ctl {
  width: 90%;
}
.text-preview {
  padding: var(--mf-sp-3);
  width: 100%;
  height: 100%;
  overflow: hidden;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-all;
}
.preview-fallback {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--mf-text-3);
}
.info {
  padding: var(--mf-sp-2) var(--mf-sp-3) var(--mf-sp-3);
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.info-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-2);
}
.time {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
}
.project {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-5);
}
</style>
