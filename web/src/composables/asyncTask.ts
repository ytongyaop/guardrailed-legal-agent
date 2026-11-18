/** 轻量异步状态封装：`loading` / `error` / `run`，页面不再各写一套 try-catch。 */

import { ref, type Ref } from 'vue'

import { ApiError } from '@/api/errors'

export interface AsyncTask {
  loading: Ref<boolean>
  error: Ref<ApiError | null>
  run: <T>(task: () => Promise<T>) => Promise<T | undefined>
}

/** 任意异常收敛成 `ApiError`：界面只认这一种错误对象。 */
export function toApiError(value: unknown): ApiError {
  if (value instanceof ApiError) return value
  const message = value instanceof Error ? value.message : '请求失败，请稍后重试'
  return new ApiError({
    status: 0,
    code: 'INTERNAL_ERROR',
    message,
    action: { type: 'retry', label: '重试' },
    cause: value,
  })
}

export function useAsyncTask(): AsyncTask {
  const loading = ref(false)
  const error = ref<ApiError | null>(null)

  async function run<T>(task: () => Promise<T>): Promise<T | undefined> {
    loading.value = true
    error.value = null
    try {
      return await task()
    } catch (failure) {
      error.value = toApiError(failure)
      return undefined
    } finally {
      loading.value = false
    }
  }

  return { loading, error, run }
}
