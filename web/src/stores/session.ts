/**
 * 内存会话（规格 006 §4）：用户、CSRF token、模型是否已配置。
 *
 * 三条硬约束：
 * 1. **不落 localStorage / sessionStorage**：刷新页面就重新问 `/auth/me`，避免 token 留在磁盘；
 * 2. 权限以后端为准：`role` 只用于「显示哪些入口」，真正的越权由 FastAPI 403/404 拦；
 * 3. 401 由 client 回调统一清会话，页面不需要自己判断「是不是掉线了」。
 *
 * 用 `reactive` 而不是 Pinia：批次 4 只有这一份全局状态，少一个依赖就少一处配置漂移。
 */

import { reactive } from 'vue'

import {
  completeOnboarding as completeOnboardingRequest,
  fetchMe,
  login as loginRequest,
  logout as logoutRequest,
  register as registerRequest,
} from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import { configureHttp } from '@/api/http'
import type { AuthState, LoginIn, RegisterIn, UserView } from '@/api/types'

export const ADMIN_ROLE = '管理员'

export interface SessionState {
  user: UserView | null
  csrfToken: string
  modelConfigured: boolean
  /** 是否已经问过 `/auth/me`；路由守卫靠它避免每次跳转都发请求。 */
  ready: boolean
  /** 自述失败的原因（服务未就绪），登录页用它解释「为什么进不去」。 */
  error: ApiError | null
}

const state = reactive<SessionState>({
  user: null,
  csrfToken: '',
  modelConfigured: false,
  ready: false,
  error: null,
})

let bootstrapTask: Promise<void> | null = null
let signedOutHandler: (() => void) | null = null

configureHttp({
  getCsrfToken: () => state.csrfToken,
  onUnauthenticated: () => {
    clearSession()
    signedOutHandler?.()
  },
})

export function useSession(): SessionState {
  return state
}

export function isAdmin(): boolean {
  return state.user !== null && state.user.role === ADMIN_ROLE
}

/** 401 之后要跳登录页：由 `main.ts` 注入，store 不直接依赖 router。 */
export function setSignedOutHandler(handler: (() => void) | null): void {
  signedOutHandler = handler
}

export function applyAuth(auth: AuthState): void {
  state.user = auth.user
  state.csrfToken = auth.csrf_token
  state.modelConfigured = auth.model_configured
  state.error = null
  state.ready = true
}

export function applyUser(user: UserView): void {
  state.user = user
}

export function clearSession(): void {
  state.user = null
  state.csrfToken = ''
  state.modelConfigured = false
  state.ready = true
}

/** 启动时问一次 `/auth/me`；并发调用共享同一个请求。 */
export function bootstrapSession(): Promise<void> {
  if (state.ready) return Promise.resolve()
  if (bootstrapTask) return bootstrapTask
  bootstrapTask = (async () => {
    try {
      applyAuth(await fetchMe())
    } catch (error) {
      clearSession()
      state.error = error instanceof ApiError ? error : null
    } finally {
      state.ready = true
      bootstrapTask = null
    }
  })()
  return bootstrapTask
}

/** 首次保存企业档案后企业绑定才落库：重新自述一次，让侧栏与页面拿到新状态。 */
export async function reloadSession(): Promise<void> {
  applyAuth(await fetchMe())
}

export async function signIn(payload: LoginIn): Promise<void> {
  applyAuth(await loginRequest(payload))
}

export async function signUp(payload: RegisterIn): Promise<void> {
  applyAuth(await registerRequest(payload))
}

export async function signOut(): Promise<void> {
  try {
    await logoutRequest()
  } catch {
    // 会话可能已过期或 CSRF 已失效：本地照样清干净，不让退出按钮卡住
  } finally {
    clearSession()
  }
}

export async function confirmOnboarding(): Promise<void> {
  const user = await completeOnboardingRequest()
  applyUser(user)
}

/** 退出登录与测试共用：清空内存会话并允许重新自述。 */
export function resetSession(): void {
  clearSession()
  state.ready = false
  state.error = null
  bootstrapTask = null
}
