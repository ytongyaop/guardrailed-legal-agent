/**
 * 首次引导对话框（规格 006 §3「全局首次引导对话框」）。
 *
 * 旧 Streamlit 界面用 `tests/test_ui_setup.py::test_onboarding_dialog_shows_once_then_marks`
 * 钉过这条保证，删旧界面时一起丢了，这里按新实现补回来：
 * 1. 未确认账号渲染出四类要点（用途 / 边界 / 数据 / 范围）与「我知道了」；
 * 2. 点「我知道了」落库后 store 标记 `onboarded=true`，弹窗立即消失，卸载重挂也不再弹；
 * 3. 标记是唯一开关：把它清回 `false`（相当于换一个未确认账号）后重挂会重新弹；
 * 4. 直接关掉弹窗等同未确认——不调接口、重挂后仍会提示。
 *
 * 断言语义（哪段文案出现了、接口是否被调用），不做整段 DOM 快照。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  completeOnboarding: vi.fn(),
}))

import * as endpoints from '@/api/endpoints'
import type { AuthState, UserView } from '@/api/types'
import OnboardingDialog from '@/components/OnboardingDialog.vue'
import { applyAuth, resetSession, useSession } from '@/stores/session'

const mocked = vi.mocked(endpoints)

const TITLE = '开始之前'
const POINT_PURPOSE = '用途：核对劳动合同中的用工风险'
const POINT_BOUNDARY = '边界：只提示与法条存在冲突的地方'
const POINT_DATA = '数据：核对时合同正文会发送给模型服务用于抽取'
const POINT_SCOPE = '范围：只处理劳动用工与劳动合同问题'

function userView(overrides: Partial<UserView> = {}): UserView {
  return {
    user_id: 'U-1',
    username: 'staff',
    role: '用户',
    enterprise_id: 'E-1',
    onboarded: false,
    ...overrides,
  }
}

function authState(user: UserView = userView()): AuthState {
  return { user, csrf_token: 'csrf-in-memory', model_configured: true }
}

function mountDialog() {
  return mount(OnboardingDialog, { global: { stubs: { teleport: true } } })
}

function dialogOf(wrapper: ReturnType<typeof mountDialog>) {
  const dialog = wrapper.findAllComponents({ name: 'ElDialog' })[0]
  if (!dialog) throw new Error('没有渲染出 ElDialog')
  return dialog
}

/**
 * 弹窗是否在展示。
 *
 * Element Plus 关闭后只是隐藏、并不销毁正文（`wrapper.text()` 里文案还在），
 * 所以「弹不弹」看 `modelValue` 这个开关，文案疏密单独在第一个用例里断言。
 */
function showsDialog(wrapper: ReturnType<typeof mountDialog>): boolean {
  return dialogOf(wrapper).props('modelValue') === true
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSession()
  sessionStorage.clear()
  localStorage.clear()
})

describe('首次引导对话框', () => {
  it('未确认账号渲染四类要点与「我知道了」', async () => {
    applyAuth(authState(userView({ onboarded: false })))

    const wrapper = mountDialog()
    await flushPromises()

    const text = wrapper.text()
    expect(showsDialog(wrapper)).toBe(true)
    expect(text).toContain(TITLE)
    expect(text).toContain(POINT_PURPOSE)
    expect(text).toContain(POINT_BOUNDARY)
    expect(text).toContain(POINT_DATA)
    expect(text).toContain(POINT_SCOPE)
    const confirm = wrapper.findAll('button').find((node) => node.text().includes('我知道了'))
    expect(confirm, '弹窗应有「我知道了」').toBeDefined()
  })

  it('点「我知道了」落库后 store 标记生效：弹窗关闭，卸载重挂不再弹', async () => {
    applyAuth(authState(userView({ onboarded: false })))
    mocked.completeOnboarding.mockResolvedValue(userView({ onboarded: true }))

    const wrapper = mountDialog()
    await flushPromises()
    const confirm = wrapper.findAll('button').find((node) => node.text().includes('我知道了'))
    await confirm?.trigger('click')
    await flushPromises()

    expect(mocked.completeOnboarding).toHaveBeenCalledTimes(1)
    expect(useSession().user?.onboarded).toBe(true)
    expect(showsDialog(wrapper)).toBe(false)
    // 引导说明只由后端落库，前端不缓存到任何浏览器存储
    expect(sessionStorage.length).toBe(0)
    expect(localStorage.length).toBe(0)

    wrapper.unmount()
    const remounted = mountDialog()
    await flushPromises()
    expect(showsDialog(remounted)).toBe(false)
  })

  it('标记是唯一开关：清回未确认后重新挂载又弹', async () => {
    applyAuth(authState(userView({ onboarded: true })))

    const confirmed = mountDialog()
    await flushPromises()
    expect(showsDialog(confirmed)).toBe(false)
    confirmed.unmount()

    // 换一个未确认账号（或同账号标记被清）：相当于标记被清掉
    applyAuth(authState(userView({ onboarded: false })))
    const fresh = mountDialog()
    await flushPromises()

    expect(showsDialog(fresh)).toBe(true)
  })

  it('直接关掉等同未确认：不调接口，重挂后仍会提示', async () => {
    applyAuth(authState(userView({ onboarded: false })))

    const wrapper = mountDialog()
    await flushPromises()
    expect(showsDialog(wrapper)).toBe(true)

    const dialog = dialogOf(wrapper)
    dialog.vm.$emit('update:modelValue', false)
    await flushPromises()

    expect(mocked.completeOnboarding).not.toHaveBeenCalled()
    expect(showsDialog(wrapper)).toBe(false)

    wrapper.unmount()
    const remounted = mountDialog()
    await flushPromises()
    expect(showsDialog(remounted)).toBe(true)
  })
})
