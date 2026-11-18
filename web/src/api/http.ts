/**
 * 进程内唯一的 API client 实例。
 *
 * 这里只做「接线」：CSRF token 从内存 session store 读，401 交给 main.ts 注册的回调。
 * 之所以用回调而不是直接 import store，是为了让 `api/` 不依赖 `stores/`（无循环依赖），
 * 也让单测可以直接 `new ApiClient(...)` 注入假 fetch。
 */

import { ApiClient } from './client'
import { API_BASE } from './client'
import type { ApiError } from './errors'

let csrfTokenReader: () => string = () => ''
let unauthenticatedHandler: ((error: ApiError) => void) | null = null

export function configureHttp(options: {
  getCsrfToken?: () => string
  onUnauthenticated?: (error: ApiError) => void
}): void {
  if (options.getCsrfToken) csrfTokenReader = options.getCsrfToken
  if (options.onUnauthenticated) unauthenticatedHandler = options.onUnauthenticated
}

export const http = new ApiClient({
  baseUrl: API_BASE,
  getCsrfToken: () => csrfTokenReader(),
  onUnauthenticated: (error) => unauthenticatedHandler?.(error),
})
