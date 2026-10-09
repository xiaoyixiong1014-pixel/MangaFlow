<script setup lang="ts">
/**
 * 文件级注释：A-07 内容运营（公告 / 公共提示词）。
 * 公告：置顶开关（即时同步用户工作台，D-05）、发布/下线、新建/编辑对话框；
 * 提示词：上下架、新建对话框、删除。
 */
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createAnnouncement,
  createPrompt,
  deleteAnnouncement,
  deletePrompt,
  getAnnouncements,
  getPrompts,
  updateAnnouncement,
  updatePromptStatus,
} from '@/api/admin'
import type { Announcement, PromptTemplate } from '@/types'

/** 当前页签 */
const activeTab = ref('announcements')

/* ============================ 公告管理 ============================ */

/** 公告加载状态 */
const annLoading = ref(false)
/** 公告列表 */
const annItems = ref<Announcement[]>([])
/** 公告总数 */
const annTotal = ref(0)
/** 公告分页 */
const annPage = ref(1)

/** 公告状态中文 */
const annStatusMap: Record<string, string> = {
  published: '已发布',
  draft: '草稿',
  offline: '已下线',
}

/**
 * 加载公告列表。
 * @returns 无
 */
async function fetchAnnouncements() {
  annLoading.value = true
  try {
    const data = await getAnnouncements({ page: annPage.value, page_size: 10 })
    annItems.value = data.items
    annTotal.value = data.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    annLoading.value = false
  }
}

/** 公告编辑对话框可见性 */
const annDialogVisible = ref(false)
/** 是否为编辑模式 */
const annEditing = ref(false)
/** 公告表单 */
const annForm = reactive({
  id: 0,
  title: '',
  content: '',
  is_pinned: false,
  status: 'draft',
})
/** 提交 loading */
const annSaving = ref(false)

/**
 * 打开新建公告对话框。
 * @returns 无
 */
function openAnnCreate() {
  annEditing.value = false
  annForm.id = 0
  annForm.title = ''
  annForm.content = ''
  annForm.is_pinned = false
  annForm.status = 'draft'
  annDialogVisible.value = true
}

/**
 * 打开编辑公告对话框。
 * @param row 目标公告
 * @returns 无
 */
function openAnnEdit(row: Announcement) {
  annEditing.value = true
  annForm.id = row.id
  annForm.title = row.title
  annForm.content = row.content
  annForm.is_pinned = row.is_pinned
  annForm.status = row.status
  annDialogVisible.value = true
}

/**
 * 提交公告（新建或保存）。
 * @returns 无
 */
async function submitAnnouncement() {
  if (!annForm.title.trim()) {
    ElMessage.warning('请填写公告标题')
    return
  }
  annSaving.value = true
  const payload = {
    title: annForm.title.trim(),
    content: annForm.content,
    is_pinned: annForm.is_pinned,
    status: annForm.status,
  }
  try {
    if (annEditing.value) {
      await updateAnnouncement(annForm.id, payload)
      ElMessage.success('公告已保存')
    } else {
      await createAnnouncement(payload)
      ElMessage.success('公告已创建')
    }
    annDialogVisible.value = false
    void fetchAnnouncements()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    annSaving.value = false
  }
}

/**
 * 切换置顶（即时同步用户工作台，D-05）。
 * @param row 目标公告
 * @returns 无
 */
async function handleTogglePin(row: Announcement) {
  try {
    await updateAnnouncement(row.id, { is_pinned: !row.is_pinned })
    ElMessage.success(row.is_pinned ? '已取消置顶' : '已置顶，即时展示在用户工作台顶部')
    void fetchAnnouncements()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 发布 / 下线公告。
 * @param row 目标公告
 * @param target 目标状态：published / offline
 * @returns 无
 */
async function handleAnnStatus(row: Announcement, target: 'published' | 'offline') {
  try {
    await updateAnnouncement(row.id, { status: target })
    ElMessage.success(target === 'published' ? '公告已发布' : '公告已下线')
    void fetchAnnouncements()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 删除公告（二次确认）。
 * @param row 目标公告
 * @returns 无
 */
async function handleAnnDelete(row: Announcement) {
  try {
    await ElMessageBox.confirm(`确认删除公告「${row.title}」吗？删除后不可恢复`, '删除公告', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await deleteAnnouncement(row.id)
    ElMessage.success('公告已删除')
    void fetchAnnouncements()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/* ============================ 公共提示词 ============================ */

/** 提示词加载状态 */
const prLoading = ref(false)
/** 提示词列表 */
const prItems = ref<PromptTemplate[]>([])
/** 提示词总数 */
const prTotal = ref(0)
/** 提示词分页 */
const prPage = ref(1)

/**
 * 加载提示词列表。
 * @returns 无
 */
async function fetchPrompts() {
  prLoading.value = true
  try {
    const data = await getPrompts({ page: prPage.value, page_size: 10 })
    prItems.value = data.items
    prTotal.value = data.total
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    prLoading.value = false
  }
}

/** 提示词新建对话框可见性 */
const prDialogVisible = ref(false)
/** 提示词表单 */
const prForm = reactive({
  title: '',
  capability: 'image',
  content: '',
  status: 'published',
})
/** 提交 loading */
const prSaving = ref(false)

/** 能力域中文 */
const capMap: Record<string, string> = {
  text: '文本',
  image: '角色/图像',
  video: '分镜/视频',
  tts: '配音',
}

/**
 * 提交新建提示词。
 * @returns 无
 */
async function submitPrompt() {
  if (!prForm.title.trim()) {
    ElMessage.warning('请填写提示词标题')
    return
  }
  prSaving.value = true
  try {
    await createPrompt({
      title: prForm.title.trim(),
      capability: prForm.capability,
      content: prForm.content,
      status: prForm.status,
    })
    ElMessage.success('提示词已创建')
    prDialogVisible.value = false
    prForm.title = ''
    prForm.content = ''
    void fetchPrompts()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    prSaving.value = false
  }
}

/**
 * 上架 / 下架提示词。
 * @param row 目标提示词
 * @returns 无
 */
async function handlePrToggle(row: PromptTemplate) {
  const target: 'published' | 'offline' = row.status === 'published' ? 'offline' : 'published'
  try {
    await updatePromptStatus(row.id, { status: target })
    ElMessage.success(target === 'published' ? '已上架' : '已下架')
    void fetchPrompts()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 删除提示词（二次确认）。
 * @param row 目标提示词
 * @returns 无
 */
async function handlePrDelete(row: PromptTemplate) {
  try {
    await ElMessageBox.confirm(`确认删除提示词「${row.title}」吗？`, '删除提示词', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await deletePrompt(row.id)
    ElMessage.success('提示词已删除')
    void fetchPrompts()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

onMounted(() => {
  void fetchAnnouncements()
  void fetchPrompts()
})
</script>

<template>
  <div class="content">
    <div class="content__toolbar">
      <h2 class="page-title">内容运营</h2>
      <el-button
        v-if="activeTab === 'announcements'"
        type="primary"
        data-test="create-announcement"
        @click="openAnnCreate"
      >
        + 新建公告
      </el-button>
      <el-button v-else type="primary" @click="prDialogVisible = true">+ 新建提示词</el-button>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ==================== 公告管理 ==================== -->
      <el-tab-pane label="公告管理" name="announcements">
        <el-table :data="annItems" v-loading="annLoading" stripe>
          <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip />
          <el-table-column label="置顶" width="90">
            <template #default="{ row }">
              <el-switch :model-value="row.is_pinned" @change="handleTogglePin(row)" />
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.status === 'published' ? 'success' : row.status === 'draft' ? 'info' : 'warning'"
                effect="light"
              >
                {{ annStatusMap[row.status] ?? row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="published_at" label="发布时间" width="170" show-overflow-tooltip />
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openAnnEdit(row)">编辑</el-button>
              <el-button
                v-if="row.status !== 'published'"
                link
                type="success"
                size="small"
                @click="handleAnnStatus(row, 'published')"
              >
                发布
              </el-button>
              <el-button
                v-else
                link
                type="warning"
                size="small"
                @click="handleAnnStatus(row, 'offline')"
              >
                下线
              </el-button>
              <el-button link type="danger" size="small" @click="handleAnnDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="content__pager">
          <el-pagination
            v-model:current-page="annPage"
            :page-size="10"
            :total="annTotal"
            layout="total, prev, pager, next"
            background
            @current-change="fetchAnnouncements"
          />
        </div>
        <p class="content__hint">公告置顶后将即时展示在用户工作台顶部（D-05）</p>
      </el-tab-pane>

      <!-- ==================== 公共提示词 ==================== -->
      <el-tab-pane label="公共提示词" name="prompts">
        <el-table :data="prItems" v-loading="prLoading" stripe>
          <el-table-column prop="title" label="标题" min-width="200" show-overflow-tooltip />
          <el-table-column label="能力" width="110">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ capMap[row.capability] ?? row.capability }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag
                size="small"
                :type="row.status === 'published' ? 'success' : 'info'"
                effect="light"
              >
                {{ row.status === 'published' ? '已上架' : '已下架' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="使用次数" width="110" align="right">
            <template #default="{ row }">{{ (row.usage_count ?? 0).toLocaleString('zh-CN') }}</template>
          </el-table-column>
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button link :type="row.status === 'published' ? 'warning' : 'success'" size="small" @click="handlePrToggle(row)">
                {{ row.status === 'published' ? '下架' : '上架' }}
              </el-button>
              <el-button link type="danger" size="small" @click="handlePrDelete(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="content__pager">
          <el-pagination
            v-model:current-page="prPage"
            :page-size="10"
            :total="prTotal"
            layout="total, prev, pager, next"
            background
            @current-change="fetchPrompts"
          />
        </div>
      </el-tab-pane>
    </el-tabs>

    <!-- 公告新建/编辑对话框 -->
    <el-dialog
      v-model="annDialogVisible"
      :title="annEditing ? '编辑公告' : '新建公告'"
      width="560px"
    >
      <el-form label-position="top">
        <el-form-item label="标题" required>
          <el-input v-model="annForm.title" placeholder="如：模型维护通知" maxlength="60" show-word-limit />
        </el-form-item>
        <el-form-item label="正文">
          <el-input
            v-model="annForm.content"
            type="textarea"
            :rows="5"
            placeholder="公告正文内容"
            maxlength="2000"
            show-word-limit
          />
        </el-form-item>
        <el-form-item label="置顶">
          <el-switch v-model="annForm.is_pinned" />
          <span class="content__form-hint">置顶后即时展示在用户工作台顶部</span>
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="annForm.status">
            <el-radio value="draft">草稿</el-radio>
            <el-radio value="published">发布</el-radio>
            <el-radio value="offline">下线</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="annDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="annSaving" @click="submitAnnouncement">保存</el-button>
      </template>
    </el-dialog>

    <!-- 提示词新建对话框 -->
    <el-dialog v-model="prDialogVisible" title="新建公共提示词" width="560px">
      <el-form label-position="top">
        <el-form-item label="标题" required>
          <el-input v-model="prForm.title" placeholder="如：角色定妆 · 清冷" maxlength="60" show-word-limit />
        </el-form-item>
        <el-form-item label="能力分类">
          <el-select v-model="prForm.capability">
            <el-option label="角色/图像" value="image" />
            <el-option label="分镜/视频" value="video" />
            <el-option label="配音" value="tts" />
            <el-option label="文本" value="text" />
          </el-select>
        </el-form-item>
        <el-form-item label="内容">
          <el-input v-model="prForm.content" type="textarea" :rows="5" placeholder="提示词正文" />
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="prForm.status">
            <el-radio value="published">上架</el-radio>
            <el-radio value="offline">下架</el-radio>
          </el-radio-group>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="prDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="prSaving" @click="submitPrompt">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.content {
  max-width: 1440px;
  margin: 0 auto;
}

.content__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--mf-sp-3);
}

.page-title {
  margin: 0;
  font-size: var(--mf-fs-xl);
  font-weight: var(--mf-fw-semibold);
}

.content__pager {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--mf-sp-4);
}

.content__hint {
  margin: var(--mf-sp-3) 0 0;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}

.content__form-hint {
  margin-left: var(--mf-sp-2);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
</style>
