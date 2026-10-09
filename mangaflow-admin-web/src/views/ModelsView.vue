<script setup lang="ts">
/**
 * 文件级注释：A-05 模型配置管理。
 * 三个页签：渠道管理（key 脱敏、健康 Tag、新增/编辑/删除/测试连通）、
 * 逻辑模型（默认开关、渠道优先级、价格 JSON 编辑）、健康探测（选渠道批量测试）。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createChannel,
  createLogicalModel,
  deleteChannel,
  getChannels,
  getLogicalModels,
  testChannel,
  updateChannel,
  updateLogicalModel,
} from '@/api/admin'
import type { ChannelTestResult, LogicalModel, ModelChannel } from '@/types'

/** 当前页签 */
const activeTab = ref('channels')

/* ============================ 渠道管理 ============================ */

/** 渠道列表加载状态 */
const chLoading = ref(false)
/** 渠道列表 */
const channels = ref<ModelChannel[]>([])
/** 正在测试连通性的渠道 ID 集合 */
const testingIds = ref<Set<number>>(new Set())
/** 每个渠道最近一次测试结果 */
const testResults = ref<Record<number, ChannelTestResult>>({})

/** 健康状态中文 */
const healthMap: Record<string, string> = {
  healthy: '健康',
  degraded: '降级',
  down: '不可用',
}

/** 健康状态 Tag 类型 */
function healthTag(s: string): 'success' | 'warning' | 'danger' | 'info' {
  if (s === 'healthy') return 'success'
  if (s === 'degraded') return 'warning'
  if (s === 'down') return 'danger'
  return 'info'
}

/**
 * 加载渠道列表。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function fetchChannels() {
  chLoading.value = true
  try {
    channels.value = await getChannels()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    chLoading.value = false
  }
}

/** 新增渠道对话框可见性 */
const createVisible = ref(false)
/** 新增渠道表单（协议固定 openai） */
const createForm = reactive({ protocol: 'openai', base_url: '', api_key: '' })
/** 新增提交 loading */
const creating = ref(false)

/**
 * 提交新增渠道：校验后调接口，Key 传输后由后端加密存储（AES-256）。
 * @returns 无
 * @throws 无（接口错误由拦截器统一提示）
 */
async function submitCreate() {
  if (!createForm.base_url.trim() || !createForm.api_key.trim()) {
    ElMessage.warning('Base URL 与 API Key 均必填')
    return
  }
  creating.value = true
  try {
    await createChannel({
      protocol: createForm.protocol,
      base_url: createForm.base_url.trim(),
      api_key: createForm.api_key.trim(),
    })
    ElMessage.success('渠道已创建')
    createVisible.value = false
    createForm.base_url = ''
    createForm.api_key = ''
    void fetchChannels()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    creating.value = false
  }
}

/**
 * 编辑渠道（Base URL / 启停开关）。
 * @param row 目标渠道
 * @returns 无
 */
async function handleEditChannel(row: ModelChannel) {
  try {
    const { value } = await ElMessageBox.prompt('修改服务地址（Base URL）', `编辑渠道 #${row.id}`, {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputValue: row.base_url,
      inputValidator: (v: string) => (v && v.trim().length > 0) || 'Base URL 必填',
    })
    await updateChannel(row.id, { base_url: value.trim() })
    ElMessage.success('渠道已更新')
    void fetchChannels()
  } catch {
    /* 用户取消或错误已提示 */
  }
}

/**
 * 切换渠道启用状态。
 * @param row 目标渠道
 * @returns 无
 */
async function handleToggleEnable(row: ModelChannel) {
  try {
    await updateChannel(row.id, { enabled: !row.enabled })
    ElMessage.success(row.enabled ? '已停用' : '已启用')
    void fetchChannels()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 删除渠道（二次确认）。
 * @param row 目标渠道
 * @returns 无
 */
async function handleDeleteChannel(row: ModelChannel) {
  try {
    await ElMessageBox.confirm(
      `删除后该渠道不可恢复，确认删除渠道 #${row.id}（${row.base_url}）吗？`,
      '删除渠道',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await deleteChannel(row.id)
    ElMessage.success('已删除')
    void fetchChannels()
  } catch {
    /* 错误已由拦截器提示 */
  }
}

/**
 * 测试单个渠道连通性，结果就地展示（ok / latency_ms）。
 * @param row 目标渠道
 * @returns 无
 */
async function handleTest(row: ModelChannel) {
  testingIds.value.add(row.id)
  try {
    const result = await testChannel(row.id)
    testResults.value[row.id] = result
    if (result.ok) {
      ElMessage.success(`渠道 #${row.id} 连通正常，延迟 ${result.latency_ms}ms`)
    } else {
      ElMessage.error(`渠道 #${row.id} 连通失败：${result.error ?? '未知错误'}`)
    }
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    testingIds.value.delete(row.id)
  }
}

/* ============================ 逻辑模型 ============================ */

/** 逻辑模型加载状态 */
const lmLoading = ref(false)
/** 逻辑模型列表 */
const logicals = ref<LogicalModel[]>([])

/** 能力域中文 */
const capMap: Record<string, string> = {
  text: '文本',
  image: '图像',
  video: '视频',
  tts: '配音',
}

/**
 * 加载逻辑模型列表。
 * @returns 无
 */
async function fetchLogicals() {
  lmLoading.value = true
  try {
    logicals.value = await getLogicalModels()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    lmLoading.value = false
  }
}

/** 编辑对话框可见性 */
const editVisible = ref(false)
/** 编辑提交 loading */
const editing = ref(false)
/** 编辑中的逻辑模型表单 */
const editForm = reactive({
  id: 0,
  name: '',
  capability: '',
  channel_priority: [] as number[],
  is_default: false,
  price_per_unit: '',
  scene_desc: '',
  doc_url: '',
})

/**
 * 打开逻辑模型编辑对话框。
 * @param row 目标逻辑模型
 * @returns 无
 */
function openEdit(row: LogicalModel) {
  editForm.id = row.id
  editForm.name = row.name
  editForm.capability = row.capability
  editForm.channel_priority = [...row.channel_priority]
  editForm.is_default = row.is_default
  editForm.price_per_unit = row.price_per_unit
  editForm.scene_desc = row.scene_desc
  editForm.doc_url = row.doc_url
  editVisible.value = true
}

/**
 * 提交逻辑模型编辑：价格字段须为合法 JSON。
 * @returns 无
 * @throws 无（校验失败或接口错误由提示层处理）
 */
async function submitEdit() {
  // 价格 JSON 校验
  try {
    JSON.parse(editForm.price_per_unit || '{}')
  } catch {
    ElMessage.error('单价格式须为合法 JSON，例如 {"image":"0.2"}')
    return
  }
  editing.value = true
  try {
    await updateLogicalModel(editForm.id, {
      channel_priority: editForm.channel_priority,
      is_default: editForm.is_default,
      price_per_unit: editForm.price_per_unit,
      scene_desc: editForm.scene_desc,
      doc_url: editForm.doc_url,
    })
    ElMessage.success('逻辑模型已更新，优先级调整立即生效')
    editVisible.value = false
    void fetchLogicals()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    editing.value = false
  }
}

/** 新增逻辑模型对话框可见性 */
const lmCreateVisible = ref(false)
/** 新增逻辑模型表单 */
const lmCreateForm = reactive({
  capability: 'text',
  name: '',
  channel_priority: [] as number[],
  is_default: false,
  price_per_unit: '{}',
  scene_desc: '',
  doc_url: '',
})
/** 新增提交 loading */
const lmCreating = ref(false)

/**
 * 提交新增逻辑模型。
 * @returns 无
 */
async function submitLmCreate() {
  if (!lmCreateForm.name.trim()) {
    ElMessage.warning('请填写逻辑模型名')
    return
  }
  try {
    JSON.parse(lmCreateForm.price_per_unit || '{}')
  } catch {
    ElMessage.error('单价格式须为合法 JSON')
    return
  }
  lmCreating.value = true
  try {
    await createLogicalModel({
      capability: lmCreateForm.capability,
      name: lmCreateForm.name.trim(),
      channel_priority: lmCreateForm.channel_priority,
      is_default: lmCreateForm.is_default,
      price_per_unit: lmCreateForm.price_per_unit,
      scene_desc: lmCreateForm.scene_desc,
      doc_url: lmCreateForm.doc_url,
    })
    ElMessage.success('逻辑模型已创建')
    lmCreateVisible.value = false
    void fetchLogicals()
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    lmCreating.value = false
  }
}

/* ============================ 健康探测 ============================ */

/** 探测页选中的渠道 ID */
const probeIds = ref<number[]>([])
/** 批量探测进行中 */
const probing = ref(false)
/** 探测结果行 */
const probeRows = ref<Array<{ channel: ModelChannel; result: ChannelTestResult }>>([])

/** 可选渠道选项 */
const channelOptions = computed(() =>
  channels.value.map((c) => ({ label: `#${c.id} ${c.base_url}`, value: c.id })),
)

/**
 * 批量探测所选渠道。
 * @returns 无
 */
async function handleProbe() {
  if (probeIds.value.length === 0) {
    ElMessage.warning('请先选择要探测的渠道')
    return
  }
  probing.value = true
  probeRows.value = []
  try {
    // 顺序探测避免并发压测影响真实服务
    for (const id of probeIds.value) {
      const channel = channels.value.find((c) => c.id === id)
      if (!channel) continue
      try {
        const result = await testChannel(id)
        probeRows.value.push({ channel, result })
      } catch {
        probeRows.value.push({ channel, result: { ok: false, error: '请求失败', latency_ms: -1 } })
      }
    }
    ElMessage.success('探测完成')
  } finally {
    probing.value = false
  }
}

onMounted(() => {
  void fetchChannels()
  void fetchLogicals()
})
</script>

<template>
  <div class="models">
    <div class="models__toolbar">
      <h2 class="page-title">模型配置管理</h2>
      <el-button
        v-if="activeTab === 'channels'"
        type="primary"
        data-test="create-channel"
        @click="createVisible = true"
      >
        + 新增渠道
      </el-button>
      <el-button v-else-if="activeTab === 'logicals'" type="primary" @click="lmCreateVisible = true">
        + 新增逻辑模型
      </el-button>
    </div>

    <el-tabs v-model="activeTab">
      <!-- ==================== 渠道管理 ==================== -->
      <el-tab-pane label="渠道管理" name="channels">
        <el-table :data="channels" v-loading="chLoading" stripe>
          <el-table-column prop="id" label="ID" width="60" />
          <el-table-column prop="protocol" label="协议" width="90" />
          <el-table-column prop="base_url" label="Base URL" min-width="220" show-overflow-tooltip />
          <el-table-column prop="api_key_masked" label="API Key（脱敏）" width="180">
            <template #default="{ row }">
              <code class="models__key">{{ row.api_key_masked || '—' }}</code>
            </template>
          </el-table-column>
          <el-table-column label="健康" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="healthTag(row.health_status)" effect="light">
                {{ healthMap[row.health_status] ?? row.health_status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="模型数" width="80" align="right">
            <template #default="{ row }">{{ row.models?.length ?? 0 }}</template>
          </el-table-column>
          <el-table-column label="启用" width="80">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" @change="handleToggleEnable(row)" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="240" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="handleEditChannel(row)">编辑</el-button>
              <el-button
                link
                type="success"
                size="small"
                :loading="testingIds.has(row.id)"
                @click="handleTest(row)"
              >
                测试连通
              </el-button>
              <el-button link type="danger" size="small" @click="handleDeleteChannel(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="models__hint">Key 加密录入（AES-256），仅调用时解密；探测异常渠道会自动降级并提示用户（RD-50）</p>
      </el-tab-pane>

      <!-- ==================== 逻辑模型 ==================== -->
      <el-tab-pane label="逻辑模型" name="logicals">
        <el-table :data="logicals" v-loading="lmLoading" stripe>
          <el-table-column label="能力" width="90">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ capMap[row.capability] ?? row.capability }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="逻辑模型名" min-width="140" />
          <el-table-column label="渠道优先级" min-width="200">
            <template #default="{ row }">
              <template v-if="row.channel_priority?.length">
                <el-tag
                  v-for="(cid, idx) in row.channel_priority"
                  :key="cid"
                  size="small"
                  effect="plain"
                  class="models__prio"
                >
                  {{ idx + 1 }}. 渠道#{{ cid }}
                </el-tag>
              </template>
              <span v-else class="models__muted">未配置</span>
            </template>
          </el-table-column>
          <el-table-column label="默认" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.is_default" size="small" type="success" effect="light">默认</el-tag>
              <span v-else class="models__muted">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="scene_desc" label="场景说明" min-width="180" show-overflow-tooltip />
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">调整</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ==================== 健康探测 ==================== -->
      <el-tab-pane label="健康探测" name="probe">
        <div class="models__probe-bar">
          <el-select
            v-model="probeIds"
            multiple
            collapse-tags
            placeholder="选择要探测的渠道（可多选）"
            style="min-width: 360px"
          >
            <el-option v-for="o in channelOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-button type="primary" :loading="probing" @click="handleProbe">批量测试</el-button>
        </div>

        <el-empty v-if="probeRows.length === 0" description="选择渠道后点击「批量测试」" />
        <el-table v-else :data="probeRows" stripe>
          <el-table-column label="渠道" min-width="220">
            <template #default="{ row }">{{ row.channel.base_url }}</template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="row.result.ok ? 'success' : 'danger'" effect="light">
                {{ row.result.ok ? '连通' : '失败' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="延迟" width="120">
            <template #default="{ row }">
              {{ row.result.latency_ms >= 0 ? `${row.result.latency_ms}ms` : '—' }}
            </template>
          </el-table-column>
          <el-table-column label="错误原因" min-width="240">
            <template #default="{ row }">{{ row.result.error ?? '—' }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新增渠道对话框 -->
    <el-dialog v-model="createVisible" title="新增模型渠道" width="480px">
      <el-form label-position="top">
        <el-form-item label="协议">
          <el-select v-model="createForm.protocol" disabled>
            <el-option label="OpenAI 兼容" value="openai" />
          </el-select>
        </el-form-item>
        <el-form-item label="Base URL" required>
          <el-input v-model="createForm.base_url" placeholder="https://api.example.com/v1" />
        </el-form-item>
        <el-form-item label="API Key" required>
          <el-input
            v-model="createForm.api_key"
            type="password"
            show-password
            placeholder="加密录入（AES-256），仅调用时解密"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="submitCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 编辑逻辑模型对话框 -->
    <el-dialog v-model="editVisible" :title="`调整逻辑模型：${editForm.name}`" width="560px">
      <el-form label-position="top">
        <el-form-item label="渠道优先级（按顺序，越靠前越优先）">
          <el-select v-model="editForm.channel_priority" multiple style="width: 100%">
            <el-option
              v-for="c in channels"
              :key="c.id"
              :label="`#${c.id} ${c.base_url}`"
              :value="c.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="设为该能力默认模型">
          <el-switch v-model="editForm.is_default" />
        </el-form-item>
        <el-form-item label="单价（JSON）">
          <el-input
            v-model="editForm.price_per_unit"
            type="textarea"
            :rows="3"
            placeholder='例如 {"image": "0.2"}'
          />
        </el-form-item>
        <el-form-item label="场景说明">
          <el-input v-model="editForm.scene_desc" placeholder="该模型的推荐使用场景" />
        </el-form-item>
        <el-form-item label="文档地址">
          <el-input v-model="editForm.doc_url" placeholder="https://docs.example.com" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="editing" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新增逻辑模型对话框 -->
    <el-dialog v-model="lmCreateVisible" title="新增逻辑模型" width="560px">
      <el-form label-position="top">
        <el-form-item label="能力域" required>
          <el-select v-model="lmCreateForm.capability">
            <el-option label="文本" value="text" />
            <el-option label="图像" value="image" />
            <el-option label="视频" value="video" />
            <el-option label="配音" value="tts" />
          </el-select>
        </el-form-item>
        <el-form-item label="逻辑模型名" required>
          <el-input v-model="lmCreateForm.name" placeholder="如：通用图像" />
        </el-form-item>
        <el-form-item label="渠道优先级">
          <el-select v-model="lmCreateForm.channel_priority" multiple style="width: 100%">
            <el-option
              v-for="c in channels"
              :key="c.id"
              :label="`#${c.id} ${c.base_url}`"
              :value="c.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="单价（JSON）">
          <el-input v-model="lmCreateForm.price_per_unit" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="场景说明">
          <el-input v-model="lmCreateForm.scene_desc" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="lmCreateVisible = false">取消</el-button>
        <el-button type="primary" :loading="lmCreating" @click="submitLmCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.models {
  max-width: 1440px;
  margin: 0 auto;
}

.models__toolbar {
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

.models__key {
  font-family: var(--mf-mono);
  font-size: var(--mf-fs-xs);
  background: var(--mf-surface-2);
  padding: 2px 6px;
  border-radius: 6px;
}

.models__prio {
  margin-right: var(--mf-sp-1);
  margin-bottom: var(--mf-sp-1);
}

.models__muted {
  color: var(--mf-text-3);
}

.models__hint {
  margin: var(--mf-sp-3) 0 0;
  font-size: var(--mf-fs-xs);
  color: var(--mf-text-3);
}

.models__probe-bar {
  display: flex;
  gap: var(--mf-sp-3);
  margin-bottom: var(--mf-sp-4);
}
</style>
