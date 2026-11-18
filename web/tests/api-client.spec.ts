/** API client：前缀、同源凭证、CSRF 注入、401 跳转、错误体解析、204 与网络失败。 */

import { describe, expect, it, vi } from 'vitest'

import { ApiClient, CSRF_HEADER } from '@/api/client'
import { ApiError, NETWORK_ERROR } from '@/api/errors'

import { callAt, emptyResponse, headerOf, jsonResponse, recorder } from './helpers'

function clientWith(
  responses: Response[] | (() => Response),
  token = 'csrf-token-1',
  onUnauthenticated?: (error: ApiError) => void,
): { client: ApiClient; rec: ReturnType<typeof recorder> } {
  const rec = recorder(responses)
  const client = new ApiClient({
    fetchImpl: rec.fetchImpl,
    getCsrfToken: () => token,
    onUnauthenticated,
  })
  return { client, rec }
}

describe('ApiClient 请求构造', () => {
  it('GET 走 /api 前缀、同源凭证，且不带 CSRF 头', async () => {
    const { client, rec } = clientWith([jsonResponse({ show_query_path: true })])
    const body = await client.get<{ show_query_path: boolean }>('/settings/ui', {
      query: { q: '试用期', page: 2, missing: null, blank: undefined },
    })

    expect(body.show_query_path).toBe(true)
    const call = callAt(rec, 0)
    expect(call.url).toBe('/api/settings/ui?q=%E8%AF%95%E7%94%A8%E6%9C%9F&page=2')
    expect(call.init.method).toBe('GET')
    expect(call.init.credentials).toBe('same-origin')
    expect(headerOf(call.init, CSRF_HEADER)).toBeNull()
  })

  it('写方法注入 X-CSRF-Token 并序列化 JSON', async () => {
    const { client, rec } = clientWith([jsonResponse({ show_query_path: false })])
    await client.put('/settings/ui', { show_query_path: false })

    const call = callAt(rec, 0)
    expect(call.init.method).toBe('PUT')
    expect(headerOf(call.init, CSRF_HEADER)).toBe('csrf-token-1')
    expect(headerOf(call.init, 'Content-Type')).toBe('application/json')
    expect(call.init.body).toBe('{"show_query_path":false}')
  })

  it('没有 token 时不写空 CSRF 头（公开写接口没有会话）', async () => {
    const { client, rec } = clientWith(
      [jsonResponse({ needs_admin: true, runtime: {} }, 200)],
      '',
    )
    await client.post('/setup/admin', { username: 'admin' })

    expect(headerOf(callAt(rec, 0).init, CSRF_HEADER)).toBeNull()
  })

  it('FormData 不手写 Content-Type，交给浏览器带边界', async () => {
    const { client, rec } = clientWith([jsonResponse({ filename: 'a.txt', text: '', char_count: 0 })])
    const form = new FormData()
    form.append('file', new Blob(['正文']), 'a.txt')
    await client.post('/contracts/parse', form)

    expect(headerOf(callAt(rec, 0).init, 'Content-Type')).toBeNull()
  })

  it('204 返回 undefined，而不是空对象', async () => {
    const { client } = clientWith([emptyResponse()])
    await expect(client.delete('/contracts/C-1', { expect: 'none' })).resolves.toBeUndefined()
  })
})

describe('ApiClient 错误处理', () => {
  it('401 触发 onUnauthenticated，并抛出按错误体解析的 ApiError', async () => {
    const onUnauthenticated = vi.fn()
    const { client } = clientWith(
      [
        jsonResponse(
          {
            error: {
              code: 'UNAUTHENTICATED',
              message: '登录状态已失效，请重新登录',
              details: null,
              action: null,
            },
            request_id: 'req_abc123',
          },
          401,
        ),
      ],
      'csrf-token-1',
      onUnauthenticated,
    )

    const failure = await client.get('/enterprise').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    const error = failure as ApiError
    expect(error.status).toBe(401)
    expect(error.code).toBe('UNAUTHENTICATED')
    expect(error.message).toBe('登录状态已失效，请重新登录')
    expect(error.requestId).toBe('req_abc123')
    expect(error.isUnauthenticated).toBe(true)
    expect(onUnauthenticated).toHaveBeenCalledTimes(1)
    expect(onUnauthenticated.mock.calls[0]?.[0]).toBe(error)
  })

  it('登录接口用 redirectOn401:false：密码错误不当作会话失效', async () => {
    const onUnauthenticated = vi.fn()
    const { client } = clientWith(
      [
        jsonResponse(
          { error: { code: 'UNAUTHENTICATED', message: '用户名或密码不正确' }, request_id: 'r1' },
          401,
        ),
      ],
      '',
      onUnauthenticated,
    )

    await expect(
      client.post('/auth/login', { username: 'a', password: 'b' }, { redirectOn401: false }),
    ).rejects.toThrow(ApiError)
    expect(onUnauthenticated).not.toHaveBeenCalled()
  })

  it('400 的 details.fields 与 action 原样带给页面', async () => {
    const { client } = clientWith([
      jsonResponse(
        {
          error: {
            code: 'VALIDATION_ERROR',
            message: '保存前请先选择：工时制度',
            details: { fields: ['working_hours'] },
            action: { type: 'none', label: '修改后重试' },
          },
          request_id: 'req_400',
        },
        400,
      ),
    ])

    const failure = (await client.put('/enterprise', {}).catch((error: unknown) => error)) as ApiError
    expect(failure.code).toBe('VALIDATION_ERROR')
    expect(failure.fieldNames).toEqual(['working_hours'])
    expect(failure.action).toEqual({ type: 'none', label: '修改后重试' })
  })

  it('没有错误体时按状态码兜底，界面仍有可读文案', async () => {
    const { client } = clientWith([new Response('<html>502</html>', { status: 502 })])
    const failure = (await client.get('/settings/model').catch((error: unknown) => error)) as ApiError
    expect(failure.status).toBe(502)
    expect(failure.code).toBe('MODEL_PROBE_FAILED')
    expect(failure.message).toBe('上游服务暂时不可用，请稍后重试')
  })

  it('网络层失败映射为 status 0 的 NETWORK_ERROR，而不是伪装成 500', async () => {
    const client = new ApiClient({
      fetchImpl: (async () => {
        throw new TypeError('fetch failed')
      }) as typeof fetch,
    })

    const failure = (await client.get('/setup').catch((error: unknown) => error)) as ApiError
    expect(failure.status).toBe(0)
    expect(failure.code).toBe(NETWORK_ERROR)
    expect(failure.action?.type).toBe('retry')
  })
})
