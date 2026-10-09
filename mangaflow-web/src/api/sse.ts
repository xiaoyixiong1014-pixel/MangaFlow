/**
 * SSE（Server-Sent Events）请求工具（文件级注释）。
 *
 * 后端以 POST（少量 GET）返回 text/event-stream；axios 不便处理流式响应，
 * 这里用 fetch + ReadableStream 手动解析。
 *
 * 解析规则：
 * - 事件帧之间以"空行"分隔（\n\n）；
 * - 帧内 `event:` 行为事件名（缺省 'message'），`data:` 行为负载，
 *   多行 data 逐行累积后以 \n 连接；
 * - data 约定为 JSON，解析失败时按原始字符串透传。
 */

/** SSE 事件回调签名 */
export type SseEventHandler = (event: string, data: unknown) => void

/**
 * 构造带认证头的请求 headers（内部函数）。
 * @returns 含 Authorization（若本地有 token）与 Accept 的请求头对象
 */
function buildHeaders(): Record<string, string> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  const token = localStorage.getItem('mf_token')
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

/**
 * 从一段已按帧切分出的原始文本解析出事件帧并触发回调（内部函数）。
 * @param rawFrame 单个事件帧原始文本（不含结尾空行）
 * @param onEvent 事件回调
 */
function dispatchFrame(rawFrame: string, onEvent: SseEventHandler): void {
  let eventName = ''
  const dataLines: string[] = []
  for (const line of rawFrame.split('\n')) {
    if (line.startsWith('event:')) {
      eventName = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      // data: 后可能有一个空格，约定 JSON 负载
      dataLines.push(line.slice(5).replace(/^ /, ''))
    }
    // 其余行（注释 ":"、id:、retry:）忽略
  }
  if (!eventName && dataLines.length === 0) return
  const raw = dataLines.join('\n')
  let data: unknown = raw
  try {
    data = JSON.parse(raw) // 约定 data 为 JSON；失败则按原始字符串透传
  } catch {
    /* 非 JSON 负载，保持字符串 */
  }
  onEvent(eventName || 'message', data)
}

/**
 * 通用 SSE 流式读取（内部函数，POST/GET 共用）。
 * @param response 已建立的 fetch Response（须 200 且有 body）
 * @param onEvent 每个事件帧的回调
 * @throws Error 网络流读取失败时抛出
 */
async function consumeStream(response: Response, onEvent: SseEventHandler): Promise<void> {
  const reader = response.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
    // 按"空行"（帧分隔符）切分，最后一段可能是半帧，留在 buffer
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      if (frame.trim()) dispatchFrame(frame, onEvent)
    }
  }
  // 流结束：处理残余的最后一帧（服务端可能省略结尾空行）
  buffer += decoder.decode()
  if (buffer.trim()) dispatchFrame(buffer, onEvent)
}

/**
 * 发起 POST SSE 请求并逐帧解析。
 * @param url 完整路径（以 /api 开头，经 Vite 代理转发）
 * @param body JSON 请求体
 * @param onEvent 事件回调（event 为事件名，data 为解析后的 JSON）
 * @param signal 可选中止信号（页面离开/用户停止生成时传入 AbortSignal）
 * @throws Error 响应非 2xx 或无 body 时抛出（携带后端 detail 文案）
 */
export async function ssePost(
  url: string,
  body: unknown,
  onEvent: SseEventHandler,
  signal?: AbortSignal,
): Promise<void> {
  const headers = buildHeaders()
  headers['Content-Type'] = 'application/json'
  const response = await fetch(url, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
    signal,
  })
  if (!response.ok || !response.body) {
    let detail = `请求失败（HTTP ${response.status}）`
    try {
      const data = await response.json()
      if (typeof data?.detail === 'string') detail = data.detail
    } catch {
      /* 无 JSON 响应体时使用默认文案 */
    }
    throw new Error(detail)
  }
  await consumeStream(response, onEvent)
}

/**
 * 发起 GET SSE 请求并逐帧解析（任务进度/画布执行流等）。
 * @param url 完整路径（以 /api 开头）
 * @param onEvent 事件回调
 * @param signal 可选中止信号
 * @throws Error 响应非 2xx 或无 body 时抛出
 */
export async function sseGet(
  url: string,
  onEvent: SseEventHandler,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(url, { method: 'GET', headers: buildHeaders(), signal })
  if (!response.ok || !response.body) {
    throw new Error(`请求失败（HTTP ${response.status}）`)
  }
  await consumeStream(response, onEvent)
}
