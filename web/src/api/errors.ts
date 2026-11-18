/**
 * 错误体映射（规格 006 §2「错误约定」）。
 *
 * 服务端非 2xx 一律 `{error:{code,message,details,action},request_id}`；这里把响应体解析成
 * 一个 `ApiError`，前端只按 `code` / `message` / `action` 渲染，不自己编文案。
 * 响应体缺失或形状不对时（反向代理的 HTML 502、进程崩溃）退回状态码对应的通用文案，
 * 保证界面永远有可读的错误提示，不会因为解析失败而白屏。
 */

export type ErrorActionType = 'open_settings' | 'contact_admin' | 'retry' | 'none'

export interface ErrorAction {
  type: ErrorActionType
  label: string
}

export interface ErrorBody {
  code: string
  message: string
  details: Record<string, unknown> | null
  action: ErrorAction | null
}

export interface ApiErrorInit {
  status: number
  code: string
  message: string
  details?: Record<string, unknown> | null
  action?: ErrorAction | null
  requestId?: string
  cause?: unknown
}

/** 前端自造的两个 code：只用于「服务端没有回错误体」的情形，不写进后端契约。 */
export const NETWORK_ERROR = 'NETWORK_ERROR'
export const UNEXPECTED_RESPONSE = 'UNEXPECTED_RESPONSE'

const ACTION_TYPES: readonly ErrorActionType[] = ['open_settings', 'contact_admin', 'retry', 'none']

interface StatusFallback {
  code: string
  message: string
}

const STATUS_FALLBACK: Record<number, StatusFallback> = {
  400: { code: 'VALIDATION_ERROR', message: '请求参数不合法，请检查后重试' },
  401: { code: 'UNAUTHENTICATED', message: '登录状态已失效，请重新登录' },
  403: { code: 'FORBIDDEN', message: '当前账号没有访问该功能的权限' },
  404: { code: 'NOT_FOUND', message: '请求的资源不存在' },
  409: { code: 'STATE_CONFLICT', message: '当前状态不允许该操作，请刷新后重试' },
  413: { code: 'PAYLOAD_TOO_LARGE', message: '提交的内容超过大小上限' },
  415: { code: 'UNSUPPORTED_FILE_TYPE', message: '不支持该文件类型' },
  422: { code: 'FILE_PARSE_ERROR', message: '提交的内容无法处理，请检查后重试' },
  502: { code: 'MODEL_PROBE_FAILED', message: '上游服务暂时不可用，请稍后重试' },
}

const DEFAULT_FALLBACK: StatusFallback = {
  code: 'INTERNAL_ERROR',
  message: '服务器内部错误，请稍后重试',
}

export function fallbackFor(status: number): StatusFallback {
  return STATUS_FALLBACK[status] ?? DEFAULT_FALLBACK
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function parseAction(value: unknown): ErrorAction | null {
  if (!isRecord(value)) return null
  const type = typeof value['type'] === 'string' ? value['type'] : ''
  const label = typeof value['label'] === 'string' ? value['label'] : ''
  if (!label) return null
  const known = ACTION_TYPES.find((item) => item === type)
  return { type: known ?? 'none', label }
}

export interface ParsedErrorBody extends ErrorBody {
  requestId: string
}

/** 把任意响应体解析成错误体；缺字段时用状态码兜底，`message` 永远非空。 */
export function parseErrorBody(status: number, payload: unknown): ParsedErrorBody {
  const fallback = fallbackFor(status)
  const requestId = isRecord(payload) && typeof payload['request_id'] === 'string' ? payload['request_id'] : ''
  const envelope = isRecord(payload) && isRecord(payload['error']) ? payload['error'] : payload
  const candidate = isRecord(envelope) ? envelope : null
  const rawCode = candidate && typeof candidate['code'] === 'string' ? candidate['code'] : ''
  const rawMessage = candidate && typeof candidate['message'] === 'string' ? candidate['message'].trim() : ''
  const details = candidate && isRecord(candidate['details']) ? candidate['details'] : null
  const action = candidate ? parseAction(candidate['action']) : null
  return {
    code: rawCode || fallback.code,
    message: rawMessage || fallback.message,
    details,
    action: action ?? null,
    requestId,
  }
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: Record<string, unknown> | null
  readonly action: ErrorAction | null
  readonly requestId: string
  override readonly cause: unknown

  constructor(init: ApiErrorInit) {
    super(init.message)
    this.name = 'ApiError'
    this.status = init.status
    this.code = init.code
    this.details = init.details ?? null
    this.action = init.action ?? null
    this.requestId = init.requestId ?? ''
    this.cause = init.cause
  }

  /** 服务端回的错误体；反向代理/断网时按状态码兜底。 */
  static fromResponse(status: number, payload: unknown): ApiError {
    const parsed = parseErrorBody(status, payload)
    return new ApiError({
      status,
      code: parsed.code,
      message: parsed.message,
      details: parsed.details,
      action: parsed.action,
      requestId: parsed.requestId,
    })
  }

  /** 完全拿不到响应体时（如状态码没有对应的 JSON）。 */
  static fromStatus(status: number, message?: string): ApiError {
    const fallback = fallbackFor(status)
    return new ApiError({
      status,
      code: fallback.code,
      message: message ?? fallback.message,
      action: status >= 500 ? { type: 'retry', label: '重试' } : null,
    })
  }

  /** 网络层失败（后端未启动、连接被重置）：`status = 0`，不冒充任何 HTTP 状态。 */
  static network(cause: unknown): ApiError {
    return new ApiError({
      status: 0,
      code: NETWORK_ERROR,
      message: '无法连接服务，请确认服务已启动后重试',
      action: { type: 'retry', label: '重试' },
      cause,
    })
  }

  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  get isForbidden(): boolean {
    return this.status === 403
  }

  get isNotFound(): boolean {
    return this.status === 404
  }

  /** 400 的 `details.fields`：后端用它指明缺哪一项，前端只做提示。 */
  get fieldNames(): string[] {
    const fields = this.details?.['fields']
    return Array.isArray(fields) ? fields.filter((item): item is string => typeof item === 'string') : []
  }
}
