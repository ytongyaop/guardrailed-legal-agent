/** 权限渲染：普通用户看不到管理端分组与入口，管理员能看到。 */

import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import NavSidebar from '@/components/NavSidebar.vue'
import { applyAuth, resetSession } from '@/stores/session'
import type { UserView } from '@/api/types'

function userView(role: string): UserView {
  return {
    user_id: 'U-1',
    username: 'staff',
    role,
    enterprise_id: 'E-1',
    onboarded: true,
  }
}

async function mountSidebar(): Promise<ReturnType<typeof mount>> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  })
  await router.push('/enterprise')
  await router.isReady()
  return mount(NavSidebar, { global: { plugins: [router] } })
}

beforeEach(() => {
  resetSession()
})

describe('侧栏分组与权限', () => {
  it('普通用户只有工作台与管理两组，看不到管理端入口', async () => {
    applyAuth({ user: userView('用户'), csrf_token: 't', model_configured: false })
    const wrapper = await mountSidebar()
    const text = wrapper.text()

    expect(text).toContain('工作台')
    expect(text).toContain('合同核对')
    expect(text).toContain('管理')
    expect(text).toContain('企业档案')
    expect(text).toContain('合同管理')
    expect(text).toContain('历史记录')
    expect(text).toContain('设置')

    expect(text).not.toContain('管理端')
    expect(text).not.toContain('用户与角色')
    expect(text).not.toContain('法源库')
    expect(text).not.toContain('操作日志')
    expect(text).not.toContain('评测')

    expect(text).toContain('staff · 用户')
  })

  it('管理员额外看到管理端整组', async () => {
    applyAuth({ user: userView('管理员'), csrf_token: 't', model_configured: true })
    const wrapper = await mountSidebar()
    const text = wrapper.text()

    expect(text).toContain('管理端')
    expect(text).toContain('用户与角色')
    expect(text).toContain('法源库')
    expect(text).toContain('规则表')
    expect(text).toContain('操作日志')
    expect(text).toContain('评测')
    expect(text).toContain('staff · 管理员')
  })
})
