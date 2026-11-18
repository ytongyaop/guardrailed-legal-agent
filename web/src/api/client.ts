/**
 * 统一 API client（规格 006 §1 / §2 / §4）。
 *
 * 五条固定行为，页面不再各自处理：
 * 1. 所有请求走 `/api` 前缀、同源 cookie（`credentials: same-origin`），不开放跨域；
 * 2. 写方法自动带 `X-CSRF-Token`（token 只存在内存里的 session store）；
 * 3. 401 交给 `onUnauthenticated` 统一清会话并跳登录；登录接口自身用 `redirectOn401: false`
 *    关掉这条，避免「密码错误」被当成会话失效；
 * 4. 非 2xx 一律解析成 `ApiError`（`{error:{code,message,details,action}}`），页面只渲染；
 * 5. 204 / 空响应返回 `undefined`，不伪造 `{}`。
 */

import { ApiError, UNEXPECTED_RESPONSE } from './errors'

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export type QueryValue = string | number | boolean | null | undefined

export type QueryParams = Record<string, QueryValue>

export type ResponseKind = 'auto' | 'json' | 'text' | 'blob' | 'none'

export const CSRF_HEADER = 'X-CSRF-Token'
export const API_BASE = '/api'

const SAFE_METHODS: readonly HttpMethod[] = ['GET']

export interface ApiClientOptions {
  baseUrl?: string
  fetchImpl?: typeof fetch
  getCsrfToken?: () => string
  onUnauthenticated?: (error: ApiError) => void
}

export interface RequestOptions {
  method?: HttpMethod
  body?: unknown
  query?: QueryParams
  headers?: Record<string, string>
  /** 默认 true；登录 / 注册 / 初始化这三个公开写接口传 false。 */
  redirectOn401?: boolean
  expect?: ResponseKind
  signal?: AbortSignal
}

function buildQuery(query: QueryParams | undefined): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue
    params.append(key, String(value))
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

async function readPayload(response: Response): Promise<unknown> {
  const contentType = (response.headers.get('content-type') ?? '').toLowerCase()
  if (contentType.includes('json')) {
    try {
      return (await response.json()) as unknown
    } catch {
      return null
    }
  }
  try {
    return await response.text()
  } catch {
    return null
  }
}

async function readSuccess(response: Response, kind: ResponseKind): Promise<unknown> {
  if (kind === 'none' || response.status === 204) return undefined
  if (kind === 'blob') return response.blob()
  if (kind === 'text') return response.text()
  if (kind === 'json') {
    try {
      return (await response.json()) as unknown
    } catch (cause) {
      throw new ApiError({
        status: response.status,
        code: UNEXPECTED_RESPONSE,
        message: '服务返回的数据无法解析，请刷新后重试',
        action: { type: 'retry', label: '重试' },
        cause,
      })
    }
  }
  return readPayload(response)
}

export class ApiClient {
  private readonly baseUrl: string
  private readonly fetchImpl: typeof fetch
  private readonly getCsrfToken: () => string
  private readonly onUnauthenticated: ((error: ApiError) => void) | undefined

  constructor(options: ApiClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? API_BASE).replace(/\/+$/, '')
    // 不在构造时 bind：jsdom 里可能没有 fetch，构造期抛错会让整个模块不可用
    this.fetchImpl = options.fetchImpl ?? ((input, init) => globalThis.fetch(input, init))
    this.getCsrfToken = options.getCsrfToken ?? (() => '')
    this.onUnauthenticated = options.onUnauthenticated
  }

  url(path: string, query?: QueryParams): string {
    const suffix = path.startsWith('/') ? path : `/${path}`
    return `${this.baseUrl}${suffix}${buildQuery(query)}`
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = options.method ?? 'GET'
    const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
    const init: RequestInit = {
      method,
      credentials: 'same-origin',
      headers,
    }
    if (options.signal) init.signal = options.signal

    if (options.body !== undefined) {
      if (options.body instanceof FormData || options.body instanceof Blob) {
        // 交给浏览器自己带 multipart 边界，绝不手写 Content-Type
        init.body = options.body
      } else if (typeof options.body === 'string') {
        init.body = options.body
        headers['Content-Type'] = headers['Content-Type'] ?? 'text/plain;charset=UTF-8'
      } else {
        init.body = JSON.stringify(options.body)
        headers['Content-Type'] = 'application/json'
      }
    }

    if (!SAFE_METHODS.includes(method)) {
      const token = this.getCsrfToken()
      // 公开写接口没有会话也就没有 token，此时不加空头，让服务端按「公开端点」处理
      if (token) headers[CSRF_HEADER] = token
    }

    let response: Response
    try {
      response = await this.fetchImpl(this.url(path, options.query), init)
    } catch (cause) {
      throw ApiError.network(cause)
    }

    if (!response.ok) {
      const error = ApiError.fromResponse(response.status, await readPayload(response))
      if (error.status === 401 && options.redirectOn401 !== false) {
        this.onUnauthenticated?.(error)
      }
      throw error
    }

    return (await readSuccess(response, options.expect ?? 'auto')) as T
  }

  get<T>(path: string, options: Omit<RequestOptions, 'method'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'GET' })
  }

  post<T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'POST', body })
  }

  put<T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PUT', body })
  }

  patch<T>(path: string, body?: unknown, options: Omit<RequestOptions, 'method' | 'body'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PATCH', body })
  }

  delete<T>(path: string, options: Omit<RequestOptions, 'method'> = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'DELETE' })
  }
}
