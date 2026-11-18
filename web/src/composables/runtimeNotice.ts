/**
 * 运行目录引导 / 凭据迁移提示（规格 006 §2「运行/迁移提示由前端每会话只显示一次」）。
 *
 * 与旧界面同一口径：复制旧库、引导告警、迁移结果各提示一次；
 * 迁移失败降级为警告，其余为提示。这些文案全部来自服务端，前端不改写。
 */

import { reactive } from 'vue'

import { fetchSetupStatus } from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import type { SetupStatus } from '@/api/types'

export type NoticeLevel = 'info' | 'warning'

export interface RuntimeNoticeLine {
  level: NoticeLevel
  text: string
}

export interface RuntimeNoticeState {
  lines: RuntimeNoticeLine[]
  loaded: boolean
  error: ApiError | null
}

const MIGRATION_FAILED = 'failed'

const state = reactive<RuntimeNoticeState>({ lines: [], loaded: false, error: null })

export function useRuntimeNotice(): RuntimeNoticeState {
  return state
}

/** 每会话只取一次；内存标记随刷新重置。 */
export async function loadRuntimeNotice(): Promise<void> {
  if (state.loaded) return
  state.loaded = true
  try {
    const status = await fetchSetupStatus()
    state.lines = describeRuntime(status)
  } catch (error) {
    state.error = error instanceof ApiError ? error : null
  }
}

export function describeRuntime(status: SetupStatus): RuntimeNoticeLine[] {
  const lines: RuntimeNoticeLine[] = []
  const runtime = status.runtime
  if (runtime.copied_from) {
    lines.push({
      level: 'info',
      text: `已从旧数据库复制业务数据到 ${runtime.db_path}；旧库保留在 ${runtime.copied_from}`,
    })
  }
  for (const warning of runtime.warnings) {
    lines.push({ level: 'warning', text: warning })
  }
  const failed = runtime.migration_status === MIGRATION_FAILED
  for (const message of runtime.messages) {
    lines.push({ level: failed ? 'warning' : 'info', text: message })
  }
  return lines
}

export function dismissRuntimeNotice(): void {
  state.lines = []
}

/** 测试用：清空已加载标记，让下一轮重新取。 */
export function resetRuntimeNotice(): void {
  state.lines = []
  state.loaded = false
  state.error = null
}
