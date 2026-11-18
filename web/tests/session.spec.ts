/** 会话 store：内存保存用户与 CSRF、401 清会话、并发自述只发一次请求。 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  completeOnboarding: vi.fn(),
}))

import * as endpoints from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import {
  applyAuth,
  bootstrapSession,
  confirmOnboarding,
  isAdmin,
  resetSession,
  signIn,
  signOut,
  useSession,
} from '@/stores/session'
import type { AuthState, UserView } from '@/api/types'

const mocked = vi.mocked(endpoints)

function userView(overrides: Partial<UserView> = {}): UserView {
  return {
    user_id: 'U-1',
    username: 'staff',
    role: '用户',
    enterprise_id: 'E-1',
    onboarded: true,
    ...overrides,
  }
}

function authState(user: UserView = userView()): AuthState {
  return { user, csrf_token: 'csrf-in-memory', model_configured: true }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSession()
  localStorage.clear()
  sessionStorage.clear()
})

describe('bootstrapSession', () => {
  it('成功自述后把用户与 CSRF 只放内存，不写任何浏览器存储', async () => {
    mocked.fetchMe.mockResolvedValue(authState())

    await bootstrapSession()

    const session = useSession()
    expect(session.user?.username).toBe('staff')
    expect(session.csrfToken).toBe('csrf-in-memory')
    expect(session.modelConfigured).toBe(true)
    expect(session.ready).toBe(true)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    expect(JSON.stringify({ ...localStorage })).not.toContain('csrf-in-memory')
  })

  it('401 视为未登录：不留用户、记录错误并标记已就绪', async () => {
    mocked.fetchMe.mockRejectedValue(
      ApiError.fromResponse(401, {
        error: { code: 'UNAUTHENTICATED', message: '登录状态已失效，请重新登录' },
      }),
    )

    await bootstrapSession()

    const session = useSession()
    expect(session.user).toBeNull()
    expect(session.csrfToken).toBe('')
    expect(session.ready).toBe(true)
    expect(session.error?.isUnauthenticated).toBe(true)
  })

  it('并发调用共享同一次自述请求', async () => {
    mocked.fetchMe.mockResolvedValue(authState())

    await Promise.all([bootstrapSession(), bootstrapSession(), bootstrapSession()])

    expect(mocked.fetchMe).toHaveBeenCalledTimes(1)
  })

  it('已就绪后再次调用不再请求', async () => {
    mocked.fetchMe.mockResolvedValue(authState())
    await bootstrapSession()
    await bootstrapSession()

    expect(mocked.fetchMe).toHaveBeenCalledTimes(1)
  })
})

describe('登录与退出', () => {
  it('登录写入会话，并按服务端角色判断是否管理员', async () => {
    mocked.login.mockResolvedValue(authState(userView({ role: '管理员' })))

    await signIn({ username: 'staff', password: 'secret-123' })

    expect(useSession().user?.role).toBe('管理员')
    expect(isAdmin()).toBe(true)
    expect(mocked.login).toHaveBeenCalledWith({ username: 'staff', password: 'secret-123' })
  })

  it('普通用户不是管理员', async () => {
    applyAuth(authState(userView({ role: '用户' })))
    expect(isAdmin()).toBe(false)
  })

  it('退出即使接口失败也清空本地会话', async () => {
    applyAuth(authState())
    mocked.logout.mockRejectedValue(ApiError.fromStatus(500))

    await signOut()

    expect(useSession().user).toBeNull()
    expect(useSession().csrfToken).toBe('')
  })
})

describe('首次引导', () => {
  it('确认后把 onboarded 更新为 true', async () => {
    applyAuth(authState(userView({ onboarded: false })))
    mocked.completeOnboarding.mockResolvedValue(userView({ onboarded: true }))

    await confirmOnboarding()

    expect(useSession().user?.onboarded).toBe(true)
  })
})
