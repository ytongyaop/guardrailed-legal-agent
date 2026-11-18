/** 路由守卫：未登录去登录页；普通用户直接访问管理端被导向统一 403 提示页。 */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { router } from '@/router'
import { resetSession } from '@/stores/session'

const ME_OK = {
  user: {
    user_id: 'U-1',
    username: 'staff',
    role: '用户',
    enterprise_id: 'E-1',
    onboarded: true,
  },
  csrf_token: 'csrf-1',
  model_configured: true,
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(meResponse: () => Response): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/auth/me')) return meResponse()
      return jsonResponse({ error: { code: 'NOT_FOUND', message: '请求的资源不存在' } }, 404)
    }),
  )
}

afterEach(() => {
  vi.unstubAllGlobals()
  resetSession()
})

describe('路由守卫', () => {
  it('未登录访问受保护页时跳到登录页并记住原路径', async () => {
    stubFetch(() =>
      jsonResponse({ error: { code: 'UNAUTHENTICATED', message: '登录状态已失效' } }, 401),
    )
    resetSession()

    await router.push('/enterprise')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.redirect).toBe('/enterprise')
  })

  it('普通用户直接访问管理端时落到 403 提示页，而不是崩掉或空白', async () => {
    stubFetch(() => jsonResponse(ME_OK))
    resetSession()

    await router.push('/admin/users')

    expect(router.currentRoute.value.name).toBe('forbidden')
  })
})
