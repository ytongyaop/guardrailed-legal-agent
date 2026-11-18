/**
 * 登录页的「忘记密码」说明（规格 006 §3 `/login`）。
 *
 * 本机应用没有邮箱找回，只给可复制的本地命令；这段说明的取值在旧 Streamlit
 * 界面的测试里钉过（`tests/test_ui_setup.py`），删旧界面时一起丢了，这里补回来：
 * 1. 三条命令必须真的渲染出来（用户能照着复制）；
 * 2. 说明里不得出现任何明文口令，密码框也不预填。
 *
 * 断言语义（命令文本是否存在、有没有泄露口令），不做整段 DOM 快照。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({
  fetchSetupStatus: vi.fn(),
  fetchMe: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  completeOnboarding: vi.fn(),
}))

import * as endpoints from '@/api/endpoints'
import type { SetupStatus } from '@/api/types'
import { resetSession } from '@/stores/session'
import LoginView from '@/views/LoginView.vue'

const mocked = vi.mocked(endpoints)

const SETUP_STATUS: SetupStatus = {
  needs_admin: false,
  runtime: {
    copied_from: null,
    db_path: 'C:/tmp/mla.sqlite3',
    warnings: [],
    migration_status: 'ok',
    messages: [],
  },
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/login', name: 'login', component: { template: '<div />' } },
      { path: '/setup', name: 'setup', component: { template: '<div />' } },
      { path: '/check', name: 'check', component: { template: '<div />' } },
    ],
  })
}

async function mountLogin() {
  const router = makeRouter()
  await router.push('/login')
  await router.isReady()
  const wrapper = mount(LoginView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

/** 展开「忘记密码」说明：默认收起，点一次才出现。 */
async function openForgotHelp(wrapper: Awaited<ReturnType<typeof mountLogin>>) {
  const toggle = wrapper.findAll('button').find((node) => node.text().includes('忘记密码'))
  expect(toggle, '登录页应有「忘记密码」入口').toBeDefined()
  await toggle?.trigger('click')
  await flushPromises()
  return wrapper.text()
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSession()
  mocked.fetchSetupStatus.mockResolvedValue(SETUP_STATUS)
})

describe('忘记密码说明', () => {
  it('点开「忘记密码」后给出三条本地重置命令', async () => {
    const wrapper = await mountLogin()
    // 收起状态不预先灌命令，避免用户误以为要直接跑
    expect(wrapper.text()).not.toContain('python -m mla admin reset')

    const text = await openForgotHelp(wrapper)

    expect(text).toContain('本地应用无法通过邮箱找回')
    expect(text).toContain('python -m mla admin reset --username 你的账号')
    expect(text).toContain('python -m mla admin list')
    expect(text).toContain('python -m mla admin promote --username 你的账号')
    // 两条命令分开写：先列出账号，再把在职员工提成管理员
    expect(text).toContain('若所有管理员账号都忘记了')
  })

  it('说明里不带任何明文口令，密码框也不预填', async () => {
    const wrapper = await mountLogin()
    const text = await openForgotHelp(wrapper)

    // 命令只接受用户名；出现 --password / password= 之类就是把口令写进了界面
    expect(text).not.toMatch(/--password\b/i)
    // 「密码：abc」「口令=abc」这类赋值形态一律不该出现（「新口令并显示一次」不算赋值）
    expect(text).not.toMatch(/(?:密码|口令|password)\s*[:：=]\s*\S/i)
    expect(text).not.toMatch(/\bsecret/i)

    const passwordInputs = wrapper.findAll('input[type="password"]')
    expect(passwordInputs.length).toBeGreaterThan(0)
    for (const input of passwordInputs) {
      expect((input.element as HTMLInputElement).value).toBe('')
    }
  })
})
