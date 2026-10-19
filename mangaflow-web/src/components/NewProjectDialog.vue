<!--
  新建项目弹窗（文件级注释，对应原型 S-01）。
  三 Tab 输入（一句话创意 / 上传小说 / 粘贴大纲）+ 元信息表单；
  创建成功后立即把原始输入写入剧本（studio.saveInput）再跳转剧本页。
  被 W-01 工作台与 W-02 项目列表复用。
-->
<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { FormInstance, FormRules, UploadFile } from 'element-plus'
import * as projectsApi from '@/api/projects'
import * as studioApi from '@/api/studio'
import type { Project } from '@/types'

/** 弹窗可见性（v-model） */
const modelValue = defineModel<boolean>({ default: false })

/** 组件事件：created = 项目创建（含输入保存）成功 */
const emit = defineEmits<{ created: [project: Project] }>()

const router = useRouter()

/** 输入模式 Tab：idea 一句话创意 / novel 上传小说 / outline 粘贴大纲 */
const inputTab = ref<'idea' | 'novel' | 'outline'>('idea')
/** 提交中标记 */
const loading = ref(false)
/** 表单实例（元信息校验） */
const formRef = ref<FormInstance>()
/** 标题是否被用户手动编辑过（true 后不再自动生成） */
const titleTouched = ref(false)
/** 忠实度是否被用户手动改过（上传小说时自动切严格忠实） */
const fidelityTouched = ref(false)
/** 上传的小说文件名（视觉展示） */
const novelFileName = ref('')

/** 三种输入的原始文本 */
const ideaText = ref('')
const novelText = ref('')
const outlineText = ref('')

/** 元信息表单数据 */
const metaForm = reactive({
  /** 项目标题 */
  title: '',
  /** 体裁 */
  genre: '都市',
  /** 视觉风格 */
  style: '日系动漫',
  /** 忠实度档位：free/outline/faithful */
  fidelity: 'free',
  /** 输出比例 */
  aspectRatio: '9:16',
  /** 目标时长（秒） */
  targetDuration: 180,
})

/** 体裁选项 */
const GENRES = ['科幻', '悬疑', '都市', '古风', '奇幻', '喜剧', '其他']
/** 视觉风格选项（后 3 项为真人写实系：提示词映射见后端 style_prompts.py） */
const STYLES = [
  '国风水墨', '日系动漫', '美式漫画', '赛博朋克', '写实厚涂', '简约扁平',
  '影视级写实', '现代都市写实', '复古/年代写实',
]
/** 忠实度选项（value 对齐后端档位） */
const FIDELITIES = [
  { value: 'free', label: '自由发挥' },
  { value: 'outline', label: '遵循大纲' },
  { value: 'faithful', label: '严格忠实' },
]
/** 输出比例选项 */
const ASPECT_RATIOS = ['9:16', '16:9', '1:1']

/** 元信息校验规则（按输入模式动态）：标题仅小说/大纲模式必填——
    创意模式下标题由首句自动生成（输入框置灰），不设必填避免自相矛盾 */
const metaRules = computed<FormRules>(() => ({
  title: inputTab.value === 'idea'
    ? [{ max: 100, message: '标题不超过 100 字', trigger: 'blur' }]
    : [
        { required: true, message: '请输入项目标题', trigger: 'blur' },
        { max: 100, message: '标题不超过 100 字', trigger: 'blur' },
      ],
}))

/** 单集时长旁注文案：换算分钟 + 说明整部集数的推定方式（消解"总时长/单集"歧义） */
const durationHint = computed(
  () => `单集约 ${Math.max(1, Math.round(metaForm.targetDuration / 60))} 分钟 · 整部集数由 AI 按内容推定，可在分集大纲步调整`,
)

/** 当前 Tab 对应的原始输入文本（创建时写入剧本 raw_text） */
const rawText = computed(() =>
  inputTab.value === 'idea' ? ideaText.value : inputTab.value === 'novel' ? novelText.value : outlineText.value,
)

/**
 * 从创意文本提取默认标题：取第一句（按句末标点/换行切分）前 20 字。
 * @param text 创意原文
 * @returns 默认标题（可能为空字符串）
 */
function pickTitle(text: string): string {
  const firstSentence = text.split(/[。！？!?\n]/).find((s) => s.trim())
  return (firstSentence ?? '').trim().slice(0, 20)
}

// 弹窗打开：重置全部输入与表单
watch(modelValue, (visible) => {
  if (visible) {
    inputTab.value = 'idea'
    ideaText.value = ''
    novelText.value = ''
    outlineText.value = ''
    novelFileName.value = ''
    titleTouched.value = false
    fidelityTouched.value = false
    Object.assign(metaForm, {
      title: '',
      genre: '都市',
      style: '日系动漫',
      fidelity: 'free',
      aspectRatio: '9:16',
      targetDuration: 180,
    })
  }
})

// 创意模式：标题未手动编辑时，随创意文本自动生成
watch(ideaText, (text) => {
  if (inputTab.value === 'idea' && !titleTouched.value) {
    metaForm.title = pickTitle(text)
  }
})

// 切到小说 Tab 且已解析出正文：默认切「严格忠实」（原型 S-01 行为），用户已手动改过则不覆盖
watch([inputTab, novelText], ([tab, text]) => {
  if (tab === 'novel' && text && !fidelityTouched.value) {
    metaForm.fidelity = 'faithful'
  }
})

/**
 * 标题输入：标记为手动编辑，停止自动生成。
 */
function onTitleInput(): void {
  titleTouched.value = true
}

/**
 * 选择小说文件：读取 .txt 文本内容进 textarea（FileReader，前端解析不落盘）。
 * @param uploadFile el-upload 变更文件对象（raw 为原生 File）
 */
function onNovelFileChange(uploadFile: UploadFile): void {
  const file = uploadFile.raw
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.txt')) {
    ElMessage.warning('仅支持 .txt 文本文件')
    return
  }
  if (file.size > 20 * 1024 * 1024) {
    ElMessage.warning('文件大小不能超过 20MB')
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    novelText.value = String(reader.result ?? '')
    novelFileName.value = file.name
    ElMessage.success(`已读取 ${file.name}（${novelText.value.length} 字）`)
  }
  reader.onerror = () => {
    ElMessage.error('文件读取失败，请重试或改为粘贴大纲')
  }
  reader.readAsText(file, 'utf-8')
}

/**
 * 提交创建：
 * 1. 校验标题与当前 Tab 非空；
 * 2. projects.create 建项目（draft）；
 * 3. 立即 studio.saveInput 写入原始输入（mode + raw_text）；
 * 4. 关闭弹窗、emit('created') 并跳转 /projects/{id}/script（故事设定步骤
 *    展示创作输入，用户查看/修改后保存；生成分集大纲与剧本正文均基于该输入）。
 */
async function submitCreate(): Promise<void> {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  if (!rawText.value.trim()) {
    ElMessage.warning(inputTab.value === 'idea' ? '请描述你的漫剧创意' : inputTab.value === 'novel' ? '请上传小说文本' : '请粘贴故事大纲')
    return
  }
  loading.value = true
  try {
    // 1. 创建项目（初始 draft）；创意模式下标题为自动生成值，
    //    极端情况（首句切分为空）时以原文前 20 字兜底，避免空标题被后端 422
    const finalTitle = metaForm.title.trim()
      || (inputTab.value === 'idea' ? pickTitle(rawText.value) || '未命名项目' : '')
    const project = await projectsApi.create({
      title: finalTitle,
      genre: metaForm.genre,
      style: metaForm.style,
      fidelity: metaForm.fidelity,
      aspect_ratio: metaForm.aspectRatio,
      target_duration: metaForm.targetDuration,
    })
    // 2. 原始输入写入剧本（失败不阻塞跳转，剧本页可重新保存）
    try {
      await studioApi.saveInput(project.id, { mode: inputTab.value, raw_text: rawText.value })
    } catch {
      ElMessage.warning('原始输入保存失败，可在剧本页重新粘贴')
    }
    ElMessage.success('项目创建成功')
    modelValue.value = false
    emit('created', project)
    // 3. 进入剧本创作页（故事设定步骤展示创作输入，用户查看/修改后保存，
    //    生成分集大纲与剧本正文均基于该输入；不再携带 auto 自动生成标记）
    router.push(`/projects/${project.id}/script`)
  } catch {
    // 创建失败（校验/网络）已由拦截器提示，弹窗保留表单
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <el-dialog v-model="modelValue" title="新建项目" width="560px" align-center class="np-dialog">
    <!-- 三 Tab 输入：创意 / 小说 / 大纲（切换互不影响其他表单） -->
    <el-tabs v-model="inputTab" stretch>
      <el-tab-pane label="一句话创意" name="idea">
        <el-input
          v-model="ideaText"
          type="textarea"
          :rows="5"
          maxlength="2000"
          show-word-limit
          placeholder="说说你想做一个什么样的漫剧…"
        />
      </el-tab-pane>
      <el-tab-pane label="上传小说" name="novel">
        <el-upload
          drag
          accept=".txt"
          :auto-upload="false"
          :show-file-list="false"
          :on-change="onNovelFileChange"
          class="novel-upload"
        >
          <div class="novel-upload__inner">
            <span class="novel-upload__icon" aria-hidden="true">📄</span>
            <div>拖拽 .txt 文件到此处，或 <em>点击选择文件</em></div>
            <div class="novel-upload__hint">支持 .txt，≤ 20MB，解析在前端完成</div>
          </div>
        </el-upload>
        <el-tag v-if="novelFileName" closable type="success" class="novel-chip" @close="novelFileName = ''; novelText = ''">
          {{ novelFileName }}
        </el-tag>
        <el-input
          v-model="novelText"
          type="textarea"
          :rows="4"
          placeholder="小说正文将读取到这里，可手动调整"
        />
      </el-tab-pane>
      <el-tab-pane label="粘贴大纲" name="outline">
        <el-input
          v-model="outlineText"
          type="textarea"
          :rows="7"
          maxlength="20000"
          show-word-limit
          placeholder="粘贴你的故事大纲 / 分集梗概…"
        />
      </el-tab-pane>
    </el-tabs>

    <!-- 项目元信息 -->
    <el-divider>项目信息</el-divider>
    <el-form ref="formRef" :model="metaForm" :rules="metaRules" label-position="top" size="large" @submit.prevent>
      <!-- 标题：三种模式均可填写；创意模式下留空则自动取首句前 20 字
           （watch 预填，用户手动编辑后停止跟随），非必填 -->
      <el-form-item label="标题" prop="title" :required="inputTab !== 'idea'">
        <el-input
          v-model="metaForm.title"
          :placeholder="inputTab === 'idea' ? '不填则自动取创意首句前 20 字' : '请输入项目标题'"
          maxlength="100"
          @input="onTitleInput"
        />
      </el-form-item>
      <div class="meta-row">
        <el-form-item label="体裁" class="meta-item">
          <el-select v-model="metaForm.genre">
            <el-option v-for="g in GENRES" :key="g" :label="g" :value="g" />
          </el-select>
        </el-form-item>
        <el-form-item label="视觉风格" class="meta-item">
          <el-select v-model="metaForm.style">
            <el-option v-for="s in STYLES" :key="s" :label="s" :value="s" />
          </el-select>
        </el-form-item>
      </div>
      <!-- 忠实度：仅小说 / 大纲模式显示——忠实的前提是"已有大纲/原著"，
           一句话创意模式下大纲尚未生成，档位语义不成立（用户建议采纳） -->
      <el-form-item v-if="inputTab !== 'idea'" label="忠实度">
        <el-radio-group v-model="metaForm.fidelity" @change="fidelityTouched = true">
          <el-radio-button v-for="f in FIDELITIES" :key="f.value" :value="f.value">
            {{ f.label }}
          </el-radio-button>
        </el-radio-group>
      </el-form-item>
      <div class="meta-row">
        <el-form-item label="输出比例" class="meta-item">
          <el-select v-model="metaForm.aspectRatio">
            <el-option v-for="r in ASPECT_RATIOS" :key="r" :label="r" :value="r" />
          </el-select>
        </el-form-item>
        <!-- 单集时长：label 明确"单集"口径 + 旁注换算分钟与集数推定方式 -->
        <el-form-item label="单集时长（秒）" class="meta-item">
          <el-input-number
            v-model="metaForm.targetDuration"
            :min="30"
            :max="600"
            :step="30"
            controls-position="right"
          />
          <div class="meta-hint">{{ durationHint }}</div>
        </el-form-item>
      </div>
    </el-form>

    <!-- 底部：预估积分 + 操作 -->
    <template #footer>
      <div class="np-footer">
        <span class="np-cost">预估消耗：约 100 积分（以实际生成任务为准）</span>
        <div class="np-actions">
          <el-button @click="modelValue = false">取消</el-button>
          <el-button type="primary" :loading="loading" @click="submitCreate">创建并进入剧本</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 小说上传区拖拽样式 */
.novel-upload {
  width: 100%;
  margin-bottom: var(--mf-sp-3);
}
.novel-upload__inner {
  padding: var(--mf-sp-4);
  text-align: center;
  color: var(--mf-text-2);
  font-size: var(--mf-fs-sm);
}
.novel-upload__icon {
  font-size: 24px;
  display: block;
  margin-bottom: var(--mf-sp-2);
}
.novel-upload__hint {
  margin-top: var(--mf-sp-1);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}
.novel-upload :deep(.el-upload-dragger) {
  padding: 0;
}

/* 已解析文件 chip */
.novel-chip {
  margin-bottom: var(--mf-sp-2);
}

/* 元信息两列行 */
.meta-row {
  display: flex;
  gap: var(--mf-sp-4);
}
.meta-item {
  flex: 1;
}
/* 单集时长旁注（换算分钟 + 集数推定说明） */
.meta-hint {
  margin-top: var(--mf-sp-1);
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
  line-height: 1.5;
}

/* 底部成本提示与操作 */
.np-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--mf-sp-4);
}
.np-cost {
  font-size: var(--mf-fs-sm);
  color: var(--mf-warn);
}
.np-actions {
  display: flex;
  gap: var(--mf-sp-2);
}
</style>
