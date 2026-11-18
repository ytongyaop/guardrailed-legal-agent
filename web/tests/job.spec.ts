/**
 * 异步 job 的小工具（规格 006 §2「核对采用异步」）。
 *
 * 只测纯逻辑与轮询时序：失败体如何映射成 `ApiError`、状态文案、`queued → running → succeeded`
 * 的推进与提前停止。真实链路的行为由 E2E 覆盖。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({ fetchJob: vi.fn() }))

import * as endpoints from '@/api/endpoints'
import type { JobView } from '@/api/types'
import { apiErrorOf, isSettled, jobStatusText, pollJob } from '@/composables/job'

const mocked = vi.mocked(endpoints)

function job(overrides: Partial<JobView> = {}): JobView {
  return {
    job_id: 'J-1',
    kind: 'check',
    status: 'queued',
    task_id: null,
    result: null,
    error: null,
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('失败体映射', () => {
  it('NO_MODEL 映射成 409，并保留服务端的 action', () => {
    const error = apiErrorOf({
      code: 'NO_MODEL',
      message: '未配置模型，无法核对。',
      details: null,
      action: { type: 'open_settings', label: '去设置页配置 API Key' },
    })
    expect(error.status).toBe(409)
    expect(error.code).toBe('NO_MODEL')
    expect(error.action).toEqual({ type: 'open_settings', label: '去设置页配置 API Key' })
  })

  it('未知 action 类型退化成 none，不把坏数据塞进按钮', () => {
    const error = apiErrorOf({
      code: 'INTERNAL_ERROR',
      message: '服务器内部错误',
      details: null,
      action: { type: 'launch_missiles', label: '点我' },
    })
    expect(error.status).toBe(500)
    expect(error.action).toEqual({ type: 'none', label: '点我' })
  })

  it('没有 action 时保持 null：界面不凭空造一个按钮', () => {
    const error = apiErrorOf({ code: 'NOT_FOUND', message: '任务不存在', details: null, action: null })
    expect(error.status).toBe(404)
    expect(error.action).toBeNull()
  })
})

describe('状态文案', () => {
  it('queued / running / failed / succeeded 各有说法', () => {
    expect(jobStatusText(null)).toContain('提交')
    expect(jobStatusText(job({ status: 'queued' }))).toContain('排队')
    expect(jobStatusText(job({ status: 'running' }))).toContain('核对')
    expect(jobStatusText(job({ status: 'running', kind: 'qa' }))).toContain('回答')
    expect(jobStatusText(job({ status: 'failed' }))).toContain('失败')
    expect(jobStatusText(job({ status: 'succeeded' }))).toContain('完成')
  })

  it('isSettled 只认 succeeded / failed', () => {
    expect(isSettled(job({ status: 'queued' }))).toBe(false)
    expect(isSettled(job({ status: 'running' }))).toBe(false)
    expect(isSettled(job({ status: 'succeeded' }))).toBe(true)
    expect(isSettled(job({ status: 'failed' }))).toBe(true)
  })
})

describe('pollJob', () => {
  it('按 queued → running → succeeded 推进，每步回调一次', async () => {
    vi.useFakeTimers()
    const updates: string[] = []
    mocked.fetchJob
      .mockResolvedValueOnce(job({ status: 'queued' }))
      .mockResolvedValueOnce(job({ status: 'running' }))
      .mockResolvedValueOnce(job({ status: 'succeeded', task_id: 'T-1' }))

    const pending = pollJob('J-1', { onUpdate: (next) => updates.push(next.status) })
    await vi.advanceTimersByTimeAsync(5_000)
    const settled = await pending

    expect(settled.status).toBe('succeeded')
    expect(updates).toEqual(['queued', 'running', 'succeeded'])
    expect(mocked.fetchJob).toHaveBeenCalledTimes(3)
  })

  it('失败即停，不再继续轮询', async () => {
    vi.useFakeTimers()
    mocked.fetchJob.mockResolvedValue(job({ status: 'failed', error: null }))

    const settled = await pollJob('J-1')

    expect(settled.status).toBe('failed')
    expect(mocked.fetchJob).toHaveBeenCalledTimes(1)
  })

  it('abort 后停止等待（离开页面不该继续发请求）', async () => {
    vi.useFakeTimers()
    mocked.fetchJob.mockResolvedValue(job({ status: 'queued' }))
    const controller = new AbortController()

    const pending = pollJob('J-1', { signal: controller.signal })
    await vi.advanceTimersByTimeAsync(0)
    controller.abort()
    const settled = await pending

    expect(settled.status).toBe('queued')
    expect(mocked.fetchJob.mock.calls.length).toBeLessThan(3)
  })
})
