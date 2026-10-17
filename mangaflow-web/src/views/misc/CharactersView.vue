<!--
  角色与场景总览（文件级注释）。
  跨项目角色 + 场景 + 道具联合总览（数据源 studio.overviewCharacters）：
  类型 / 项目 / 关键字三层筛选 + 卡片网格；点击卡片弹窗预览大图与视觉提示词
  （为跨项目复用资产提供预览入口，不做页面跳转）。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import * as studioApi from '@/api/studio'
import * as projectsApi from '@/api/projects'
import type { CrossProjectAsset, Project } from '@/types'

const router = useRouter()

/** 总览原始数据 */
const rows = ref<CrossProjectAsset[]>([])
/** 项目列表（筛选下拉） */
const projects = ref<Project[]>([])
/** 首次加载状态 */
const loading = ref(true)

/** 类型筛选：all / character / scene / prop */
const assetType = ref('all')
/** 项目筛选（空为全部） */
const projectId = ref<number | ''>('')
/** 关键字搜索（名称/描述） */
const keyword = ref('')

/**
 * 过滤后的总览行（类型 + 项目 + 关键字，全部为本地筛选）。
 */
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return rows.value.filter((r) => {
    if (assetType.value !== 'all' && r.asset_type !== assetType.value) return false
    if (projectId.value !== '' && r.project_id !== projectId.value) return false
    if (kw) {
      const hay = `${r.name} ${r.description ?? ''}`.toLowerCase()
      if (!hay.includes(kw)) return false
    }
    return true
  })
})

/** 类型中文文案与占位图标映射（内部常量） */
const TYPE_META: Record<string, { label: string; emoji: string; tag: 'primary' | 'success' | 'warning' }> = {
  character: { label: '角色', emoji: '🧑‍🎨', tag: 'primary' },
  scene: { label: '场景', emoji: '🏞️', tag: 'success' },
  prop: { label: '道具', emoji: '📦', tag: 'warning' },
}

/**
 * 类型中文文案（内部函数）。
 * @param t 资产类型 character / scene / prop
 * @returns 中文标签
 */
function typeLabel(t: string): string {
  return TYPE_META[t]?.label ?? t
}

/** 预览弹窗可见性 */
const previewVisible = ref(false)
/** 预览目标行（大图 + 提示词展示） */
const previewRow = ref<CrossProjectAsset | null>(null)

/**
 * 点击卡片：打开预览弹窗（图片可再点击全屏放大，同时展示视觉提示词）。
 * 总览定位是跨项目复用资产的预览入口，不做页面跳转。
 * @param row 总览行
 */
function openPreview(row: CrossProjectAsset): void {
  previewRow.value = row
  previewVisible.value = true
}

// 首次进入：并行拉取总览与项目列表
onMounted(async () => {
  try {
    const [chars, projs] = await Promise.all([
      studioApi.overviewCharacters(),
      projectsApi.list({ page: 1, page_size: 100 }),
    ])
    rows.value = chars
    projects.value = projs.items
  } catch {
    // 错误已由拦截器 ElMessage(detail) 提示
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="page-container">
    <h2 class="page-title">角色与场景</h2>

    <!-- 筛选区 -->
    <div class="filter-bar">
      <el-select v-model="assetType" class="filter-item" style="width: 130px">
        <el-option label="全部类型" value="all" />
        <el-option label="角色" value="character" />
        <el-option label="场景" value="scene" />
        <el-option label="道具" value="prop" />
      </el-select>
      <el-select
        v-model="projectId"
        class="filter-item"
        style="width: 200px"
        placeholder="全部项目"
        clearable
      >
        <el-option
          v-for="p in projects"
          :key="p.id"
          :label="p.title"
          :value="p.id"
        />
      </el-select>
      <el-input
        v-model="keyword"
        class="filter-item"
        style="width: 240px"
        placeholder="搜索名称 / 描述"
        clearable
      />
      <span class="count-tip">共 {{ filtered.length }} 项</span>
    </div>

    <!-- 加载骨架 -->
    <div v-if="loading" class="grid">
      <div v-for="i in 8" :key="i" class="asset-card">
        <div class="mf-skeleton sk-cover"></div>
        <div style="padding: 12px">
          <div class="mf-skeleton mf-skeleton--text" style="width: 60%"></div>
        </div>
      </div>
    </div>

    <!-- 空态引导 -->
    <el-empty
      v-else-if="filtered.length === 0"
      description="还没有角色 / 场景 / 道具资产，去项目创作工作区生成吧"
    >
      <el-button type="primary" @click="router.push('/projects')">前往项目</el-button>
    </el-empty>

    <!-- 卡片网格 -->
    <div v-else class="grid">
      <div
        v-for="row in filtered"
        :key="`${row.asset_type}-${row.id}`"
        class="asset-card"
        :title="`预览「${row.name}」`"
        @click="openPreview(row)"
      >
        <div class="cover">
          <el-image
            v-if="row.asset_url"
            :src="row.asset_url"
            fit="cover"
            class="cover-img"
          >
            <template #error>
              <div class="cover-placeholder">🖼️ 加载失败</div>
            </template>
          </el-image>
          <div v-else class="cover-placeholder">{{ TYPE_META[row.asset_type]?.emoji ?? '🖼️' }}</div>
          <el-tag class="type-tag" :type="TYPE_META[row.asset_type]?.tag ?? 'info'" size="small">
            {{ typeLabel(row.asset_type) }}
          </el-tag>
        </div>
        <div class="card-body">
          <div class="card-name" :title="row.name">{{ row.name }}</div>
          <div class="card-project" :title="row.project_title">所属项目：{{ row.project_title }}</div>
        </div>
      </div>
    </div>

    <!-- 预览弹窗：大图（可全屏放大）+ 名称/类型/所属项目 + 视觉提示词 -->
    <el-dialog
      v-model="previewVisible"
      :title="previewRow ? `${typeLabel(previewRow.asset_type)} · ${previewRow.name}` : '资产预览'"
      width="560px"
      append-to-body
    >
      <template v-if="previewRow">
        <div class="preview-cover">
          <el-image
            v-if="previewRow.asset_url"
            :src="previewRow.asset_url"
            :preview-src-list="[previewRow.asset_url]"
            fit="contain"
            class="preview-img"
            preview-teleported
          >
            <template #error>
              <div class="cover-placeholder">🖼️ 加载失败</div>
            </template>
          </el-image>
          <div v-else class="cover-placeholder preview-placeholder">
            {{ TYPE_META[previewRow.asset_type]?.emoji ?? '🖼️' }}
          </div>
        </div>
        <div class="preview-hint" v-if="previewRow.asset_url">点击图片可全屏放大查看</div>
        <div class="preview-meta">
          <el-tag :type="TYPE_META[previewRow.asset_type]?.tag ?? 'info'" size="small">
            {{ typeLabel(previewRow.asset_type) }}
          </el-tag>
          <span class="preview-project">所属项目：{{ previewRow.project_title }}</span>
        </div>
        <div v-if="previewRow.description" class="preview-desc">{{ previewRow.description }}</div>
        <div class="layer-label">视觉提示词（生成该图片所用）</div>
        <div class="prompt-block">{{ previewRow.visual_prompt || '（空）' }}</div>
      </template>
      <template #footer>
        <el-button @click="previewVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-5);
  flex-wrap: wrap;
}
.count-tip {
  margin-left: auto;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: var(--mf-sp-4);
}
.asset-card {
  border-radius: var(--mf-r-card);
  background: var(--mf-surface);
  border: 1px solid var(--mf-border);
  overflow: hidden;
  cursor: pointer;
  transition: transform var(--mf-dur) var(--mf-ease), box-shadow var(--mf-dur) var(--mf-ease);
}
.asset-card:hover {
  transform: translateY(-3px);
  box-shadow: var(--mf-shadow-pop);
}
.cover {
  position: relative;
  height: 150px;
  background: var(--mf-surface-2);
}
.cover-img {
  width: 100%;
  height: 100%;
  display: block;
}
.cover-placeholder {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 34px;
  color: var(--mf-text-3);
}
.type-tag {
  position: absolute;
  top: 8px;
  left: 8px;
}
.card-body {
  padding: var(--mf-sp-3) var(--mf-sp-3) var(--mf-sp-4);
}
.card-name {
  font-weight: var(--mf-fw-semibold);
  font-size: var(--mf-fs-md);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.card-project {
  margin-top: 2px;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 预览弹窗 */
.preview-cover {
  display: flex;
  justify-content: center;
  background: var(--mf-surface-2);
  border-radius: var(--mf-r-card);
  overflow: hidden;
  margin-bottom: var(--mf-sp-3);
}
.preview-img {
  width: 100%;
  max-height: 340px;
  cursor: zoom-in;
}
.preview-placeholder {
  width: 100%;
  height: 220px;
}
.preview-hint {
  text-align: center;
  color: var(--mf-text-3);
  font-size: var(--mf-fs-xs);
  margin-bottom: var(--mf-sp-2);
}
.preview-meta {
  display: flex;
  align-items: center;
  gap: var(--mf-sp-2);
  margin-bottom: var(--mf-sp-2);
}
.preview-project {
  color: var(--mf-text-3);
  font-size: var(--mf-fs-sm);
}
.preview-desc {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  line-height: 1.7;
  margin-bottom: var(--mf-sp-2);
}
.layer-label {
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
  margin: var(--mf-sp-2) 0 4px;
}
.prompt-block {
  background: var(--mf-surface-2);
  border: 1px solid var(--mf-border);
  border-radius: var(--mf-r-ctl, 6px);
  padding: var(--mf-sp-3);
  color: var(--mf-text);
  font-size: var(--mf-fs-sm);
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow-y: auto;
}
</style>
