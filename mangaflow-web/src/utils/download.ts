/**
 * 浏览器端文件下载工具（文件级注释）。
 *
 * 把 Blob 响应（axios responseType='blob'）转为临时 ObjectURL 并触发
 * <a download> 点击下载，完成后释放 URL，避免内存泄漏。
 */

/**
 * 触发浏览器下载一个 Blob。
 * @param blob 文件二进制内容
 * @param filename 保存到本地的文件名
 * @returns 无
 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // 释放 ObjectURL（延迟到下一帧，避免 Safari 下立即回收导致下载失败）
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/**
 * 从 Content-Disposition 响应头解析 UTF-8 文件名（内部辅助）。
 * 优先取 filename*=UTF-8'' 形式，退回普通 filename 形式。
 * @param disposition Content-Disposition 响应头原始值（可空）
 * @param fallback 解析失败时的兜底文件名
 * @returns 文件名字符串
 */
export function filenameFromDisposition(disposition: string | null | undefined, fallback: string): string {
  if (!disposition) return fallback
  const utf8Match = /filename\*=UTF-8''([^;]+)/i.exec(disposition)
  if (utf8Match?.[1]) {
    try {
      return decodeURIComponent(utf8Match[1])
    } catch {
      /* 解码失败继续走普通匹配 */
    }
  }
  const plainMatch = /filename="?([^";]+)"?/i.exec(disposition)
  return plainMatch?.[1] ?? fallback
}
