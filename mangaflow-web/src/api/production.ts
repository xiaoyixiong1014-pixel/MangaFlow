/**
 * 生成与发布 API（文件级注释）。
 *
 * 对应后端 /api/projects/{id}/storyboards/{sid}/*（关键帧/视频/配音）、
 * /api/tasks/*（任务详情/重试/取消）、/api/projects/{id}/tasks/stream（SSE 进度）、
 * /api/projects/{id}/compose|outputs|reviews|publish|share 与公开分享 /api/share/{token}。
 */
import client from './client'
import { sseGet } from './sse'
import { downloadBlob } from '@/utils/download'
import type {
  ComposePayload,
  FilmOutput,
  GenTask,
  KeyframeCreateResult,
  MessageResponse,
  PageQuery,
  PageResult,
  PublishPayload,
  PublishResult,
  ReviewRecord,
  ShareInfo,
  TaskProgressEvent,
  VideoCreatePayload,
} from '@/types'

// ============================================================
// 生成任务
// ============================================================

/**
 * 生成分镜关键帧（trial 打样 / full 全量）。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID
 * @param payload 阶段与可选用户补充提示词；episode_id 按集工作流（S-06）：
 *        full 全量时限定该剧集的分镜（缺省=项目全部）
 * @returns 新建任务列表与冻结积分总额
 */
export function keyframes(
  projectId: number,
  storyboardId: number,
  payload: { phase: 'trial' | 'full'; prompt_editable?: string; episode_id?: number },
): Promise<KeyframeCreateResult> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/keyframes`, payload)
    .then((r) => r.data)
}

/**
 * 生成分镜视频片段。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID
 * @param payload 视频参数（P-12 分辨率/帧率/运动幅度）与尾帧控制（P-03）
 * @returns 新建任务
 */
export function video(
  projectId: number,
  storyboardId: number,
  payload: VideoCreatePayload = {},
): Promise<GenTask> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/video`, payload)
    .then((r) => r.data)
}

/**
 * 生成分镜尾帧图像（P-03/P-08：任务完成后由后端回写 storyboards.end_frame_url）。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID（须已有关键帧）
 * @returns 新建图像任务
 * @throws 400 分镜尚无关键帧；409 已有进行中的尾帧任务
 */
export function endFrame(projectId: number, storyboardId: number): Promise<GenTask> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/end-frame`)
    .then((r) => r.data)
}

/**
 * 上传替换生成结果（P-06：multipart 一步上传）。
 * 仅 image 任务且状态 done 可替换；png/jpg/jpeg/webp 且 ≤10MB。
 * @param taskId 目标图像任务 ID
 * @param file 替换图片文件
 * @returns 更新后的任务（result_url 为新地址，关键帧任务已联动分镜回写）
 * @throws 400 任务类型/状态不符或文件非法；403 任务不属于当前用户
 */
export function replaceResult(taskId: number, file: File): Promise<GenTask> {
  const form = new FormData()
  form.append('file', file)
  return client
    .post(`/tasks/${taskId}/replace`, form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    .then((r) => r.data)
}

/**
 * 生成分镜配音（TTS）。
 * @param projectId 项目 ID
 * @param storyboardId 分镜 ID
 * @param payload 音色/语速/音调
 * @returns 新建任务
 */
export function tts(
  projectId: number,
  storyboardId: number,
  payload: { voice?: 'male_01' | 'female_01'; speed?: number; pitch?: number },
): Promise<GenTask> {
  return client
    .post(`/projects/${projectId}/storyboards/${storyboardId}/tts`, payload)
    .then((r) => r.data)
}

/**
 * 查询任务详情。
 * @param taskId 任务 ID
 * @returns 任务输出
 * @throws 404 任务不存在/非本人项目
 */
export function taskDetail(taskId: number): Promise<GenTask> {
  return client.get(`/tasks/${taskId}`).then((r) => r.data)
}

/**
 * 分页查询项目任务列表。
 * @param projectId 项目 ID
 * @param params 分页/类型/状态过滤
 * @returns 任务分页列表
 */
export function taskList(
  projectId: number,
  params?: PageQuery & { type?: string; status?: string },
): Promise<PageResult<GenTask>> {
  return client.get(`/projects/${projectId}/tasks`, { params }).then((r) => r.data)
}

/**
 * 重试失败任务。
 * @param taskId 任务 ID
 * @returns 重试后的任务
 */
export function taskRetry(taskId: number): Promise<GenTask> {
  return client.post(`/tasks/${taskId}/retry`).then((r) => r.data)
}

/**
 * 取消任务（仅 queued 状态可取消，退还冻结积分）。
 * @param taskId 任务 ID
 * @returns 消息响应
 */
export function taskCancel(taskId: number): Promise<MessageResponse> {
  return client.post(`/tasks/${taskId}/cancel`).then((r) => r.data)
}

/**
 * 订阅项目任务进度流（GET SSE）。
 * 事件约定：'progress' 携带 TaskProgressEvent 负载、'done' 批次结束、'error' 失败。
 * @param projectId 项目 ID
 * @param onEvent 事件回调
 * @param signal 中止信号（离开页面/停止监听）
 */
export async function taskStream(
  projectId: number,
  onEvent: (event: string, data: unknown) => void,
  signal?: AbortSignal,
): Promise<void> {
  await sseGet(`/api/projects/${projectId}/tasks/stream`, onEvent, signal)
}

// ============================================================
// 成片与发布
// ============================================================

/**
 * 发起成片合成。
 * @param projectId 项目 ID
 * @param payload 分辨率/画幅/模式
 * @returns 合成任务
 */
export function compose(projectId: number, payload?: ComposePayload): Promise<GenTask> {
  return client.post(`/projects/${projectId}/compose`, payload ?? {}).then((r) => r.data)
}

/**
 * 分页查询项目成片版本列表。
 * @param projectId 项目 ID
 * @param params 分页参数
 * @returns 成片版本分页列表
 */
export function outputs(projectId: number, params?: PageQuery): Promise<PageResult<FilmOutput>> {
  return client.get(`/projects/${projectId}/outputs`, { params }).then((r) => r.data)
}

/**
 * 设为当前成片版本。
 * @param projectId 项目 ID
 * @param outputId 版本记录 ID
 * @returns 更新后的成片版本
 */
export function setCurrent(projectId: number, outputId: number): Promise<FilmOutput> {
  return client
    .post(`/projects/${projectId}/outputs/${outputId}/set-current`)
    .then((r) => r.data)
}

/**
 * 分页查询项目审核记录。
 * @param projectId 项目 ID
 * @param params 分页参数
 * @returns 审核记录分页列表
 */
export function reviews(projectId: number, params?: PageQuery): Promise<PageResult<ReviewRecord>> {
  return client.get(`/projects/${projectId}/reviews`, { params }).then((r) => r.data)
}

/**
 * 发布成片（生成分享链接）。
 * @param projectId 项目 ID
 * @param payload 成片版本/有效期/提取码
 * @returns 分享链接与过期时间
 */
export function publish(projectId: number, payload: PublishPayload): Promise<PublishResult> {
  return client.post(`/projects/${projectId}/publish`, payload).then((r) => r.data)
}

/**
 * 撤销分享链接。
 * @param projectId 项目 ID
 * @returns 消息响应
 */
export function revokeShare(projectId: number): Promise<MessageResponse> {
  return client.post(`/projects/${projectId}/share/revoke`).then((r) => r.data)
}

/**
 * 公开分享页数据（免认证；提取码放在 query，后端校验）。
 * @param token 分享令牌
 * @param password 提取码（可空）
 * @returns 分享页数据
 * @throws 404 链接无效/已撤销/过期；403 提取码错误
 */
export function shareInfo(token: string, password?: string): Promise<ShareInfo> {
  return client
    .get(`/share/${token}`, { params: password ? { password } : undefined })
    .then((r) => r.data)
}

// ============================================================
// 成片导出下载（Task 10.4）
// ============================================================

/**
 * 下载成片 MP4（GET /api/publish/outputs/{id}/download?resolution=）。
 * resolution 传入且本机有 ffmpeg 时后端转码后返回，失败自动降级原片。
 * @param outputId 成片版本记录 ID
 * @param resolution 目标档位（720p/1080p/2k，可空 = 原片）
 * @param versionNo 版本号（用于缺省文件名，可空）
 * @returns 触发浏览器下载（无返回数据）
 * @throws 400 resolution 非法；404 成片不存在/非本人项目
 */
export async function downloadOutput(
  outputId: number,
  resolution?: '720p' | '1080p' | '2k',
  versionNo?: number,
): Promise<void> {
  const resp = await client.get(`/publish/outputs/${outputId}/download`, {
    params: resolution ? { resolution } : undefined,
    responseType: 'blob',
    timeout: 300000, // 转码可能较慢，放宽超时
  })
  const fallback = `mangaflow_film_v${versionNo ?? outputId}${resolution ? `_${resolution}` : ''}.mp4`
  const disposition = resp.headers['content-disposition'] as string | undefined
  const utf8 = disposition ? /filename\*=UTF-8''([^;]+)/i.exec(disposition) : null
  const filename = utf8?.[1] ? decodeURIComponent(utf8[1]) : fallback
  downloadBlob(resp.data as Blob, filename)
}
