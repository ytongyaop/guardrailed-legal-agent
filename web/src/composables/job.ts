/**
 * 异步 job 的轮询与错误映射（规格 006 §2：核对 202 + 轮询 `/jobs/{id}`）。
 *
 * 三条约定：
 * 1. 轮询间隔从 250ms 起按 1.5 倍退避到 1.5s：条款审查几十秒，密轮询没有意义；
 * 2. `failed` 的 `error` 与 HTTP 错误体同形（`{code,message,details,action}`），
 *    这里转成同一个 `ApiError`，页面只处理一种错误对象；
 * 3. 组件卸载时通过 `AbortSignal` 停止等待，避免离开页面后还在发请求。
 */

import { fetchJob } from '@/api/endpoints'
import { ApiError, type ErrorAction, type ErrorActionType } from '@/api/errors'
import type { JobErrorView, JobView } from '@/api/types'

const POLL_START_MS = 250
const POLL_MAX_MS = 1500
const ACTION_TYPES: readonly ErrorActionType[] = ['open_settings', 'contact_admin', 'retry', 'none']

/** job 是否已结束（成功或失败）。 */
export function isSettled(job: JobView): boolean {
  return job.status === 'succeeded' || job.status === 'failed'
}

/** job 失败体 → `ApiError`：状态码只用于样式分组，界面读 `code`/`message`/`action`。 */
export function apiErrorOf(body: JobErrorView): ApiError {
  const status = body.code === 'NO_MODEL' ? 409 : body.code === 'NOT_FOUND' ? 404 : 500
  const raw = body.action
  const action: ErrorAction | null =
    raw && raw.label
      ? { type: ACTION_TYPES.find((item) => item === raw.type) ?? 'none', label: raw.label }
      : null
  return new ApiError({
    status,
    code: body.code,
    message: body.message,
    details: body.details,
    action,
  })
}

/** job 状态的中文进度文案：`queued` 与 `running` 必须能分开读。 */
export function jobStatusText(job: JobView | null): string {
  if (job === null) return '正在提交……'
  if (job.status === 'queued') return '已排队，等待开始核对……'
  if (job.status === 'running') return job.kind === 'qa' ? '正在检索并组织回答……' : '正在核对……'
  if (job.status === 'failed') return '核对失败'
  return '核对完成'
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = (): void => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', done)
      resolve()
    }
    const timer = setTimeout(done, ms)
    signal?.addEventListener('abort', done, { once: true })
  })
}

/** 轮询直到 job 结束；`onUpdate` 每次拿到新状态都回调一次（用于进度显示）。 */
export async function pollJob(
  jobId: string,
  options: { signal?: AbortSignal; onUpdate?: (job: JobView) => void } = {},
): Promise<JobView> {
  let delay = POLL_START_MS
  for (;;) {
    const job = await fetchJob(jobId)
    options.onUpdate?.(job)
    if (isSettled(job) || options.signal?.aborted) return job
    await sleep(delay, options.signal)
    delay = Math.min(Math.round(delay * 1.5), POLL_MAX_MS)
  }
}
