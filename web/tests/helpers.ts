/** 测试工具：记录请求、构造响应。不 mock 全局 fetch，避免测试之间互相污染。 */

export interface RecordedCall {
  url: string
  init: RequestInit
}

export interface FetchRecorder {
  calls: RecordedCall[]
  fetchImpl: typeof fetch
}

export function recorder(responses: Response[] | (() => Response)): FetchRecorder {
  const calls: RecordedCall[] = []
  const next = (): Response => {
    if (typeof responses === 'function') return responses()
    const response = responses.shift()
    if (!response) throw new Error('没有更多预置响应')
    return response
  }
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    calls.push({ url: String(input), init: init ?? {} })
    return next()
  }) as typeof fetch
  return { calls, fetchImpl }
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

export function emptyResponse(status = 204): Response {
  return new Response(null, { status })
}

export function headerOf(init: RequestInit, name: string): string | null {
  return new Headers(init.headers).get(name)
}

/** 一次命中的请求（用于 `noUncheckedIndexedAccess` 下取下标）。 */
export function callAt(recorder: FetchRecorder, index: number): RecordedCall {
  const call = recorder.calls[index]
  if (!call) throw new Error(`没有第 ${index} 次请求`)
  return call
}
