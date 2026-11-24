/**
 * 首次向导三步状态机（`/setup`）：
 * 1. 建号成功后停在向导里推进到第 2 步，不跳走（建号后 `GET /setup` 已报 needs_admin=false）；
 * 2. 第 2 步可跳过，第 3 步能取到示例合同；
 * 3. 缺 Key 时后端回 `NO_MODEL`，界面要给「返回步骤 2」的出口，而不是只留一条红条。
 *
 * 降级说明：核对轮询（1 秒一次、最多两分钟）不在这里跑真实计时，只验证状态切换与按钮可见性；
 * 轮询时序由 `job.spec.ts` 的纯函数用例覆盖。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({
  fetchSetupStatus: vi.fn(),
  createFirstAdmin: vi.fn(),
  fetchModelSettings: vi.fn(),
  saveModelKey: vi.fn(),
  fetchSampleContract: vi.fn(),
  postCheck: vi.fn(),
  fetchJob: vi.fn(),
  fetchMe: vi.fn(),
}))

import * as endpoints from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import type { AuthState, ModelSettingsView, SampleContract, SetupStatus } from '@/api/types'
import { resetSession } from '@/stores/session'
import SetupView from '@/views/SetupView.vue'

const mocked = vi.mocked(endpoints)

const SETUP_STATUS: SetupStatus = {
  needs_admin: true,
  runtime: {
    copied_from: null,
    db_path: 'C:/tmp/mla.sqlite3',
    warnings: [],
    migration_status: 'ok',
    messages: [],
  },
}

const ADMIN: AuthState = {
  user: {
    user_id: 'U-1',
    username: 'admin',
    role: '管理员',
    enterprise_id: 'E-1',
    onboarded: false,
  },
  csrf_token: 'csrf',
  model_configured: false,
}

const MODEL_SETTINGS: ModelSettingsView = {
  providers: [
    { provider: 'deepseek', display_name: 'DeepSeek', configured: false, source: 'none', label: '未配置' },
  ],
  model: 'deepseek-chat',
  status_line: '未配置 · 服务商：DeepSeek',
  check: null,
  embedding: { model: 'text-embedding-v3', configured: false, fallback_text: '向量检索：未配置 Key，回退 BM25' },
}

const SAMPLE: SampleContract = {
  name: '示例劳动合同（含典型问题）',
  text: '甲方（用人单位）：某某科技有限公司\n第一 试用期为十二个月。\n第六 公司不为员工缴纳社会保险。',
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/setup', name: 'setup', component: { template: '<div />' } },
      { path: '/login', name: 'login', component: { template: '<div />' } },
      { path: '/check', name: 'check', component: { template: '<div />' } },
      { path: '/enterprise', name: 'enterprise', component: { template: '<div />' } },
    ],
  })
}

async function mountSetup() {
  const router = makeRouter()
  await router.push('/setup')
  await router.isReady()
  const wrapper = mount(SetupView, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

type Mounted = Awaited<ReturnType<typeof mountSetup>>

/** 走完第 1 步：填表 + 提交（后端返回已登录的管理员）。 */
async function createAdmin(wrapper: Mounted['wrapper']): Promise<void> {
  await wrapper.get('[data-testid="setup-username"]').setValue('admin')
  await wrapper.get('[data-testid="setup-password"]').setValue('password123')
  await wrapper.get('[data-testid="setup-confirm"]').setValue('password123')
  await wrapper.get('[data-testid="setup-enterprise"]').setValue('测试企业')
  await wrapper.get('[data-testid="setup-submit"]').trigger('click')
  await flushPromises()
}

/** 到第 3 步：建号 → 跳过 Key。 */
async function reachSampleStep(wrapper: Mounted['wrapper']): Promise<void> {
  await createAdmin(wrapper)
  await wrapper.get('[data-testid="setup-model-skip"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSession()
  mocked.fetchSetupStatus.mockResolvedValue(SETUP_STATUS)
  mocked.createFirstAdmin.mockResolvedValue(ADMIN)
  mocked.fetchModelSettings.mockResolvedValue(MODEL_SETTINGS)
  mocked.fetchSampleContract.mockResolvedValue(SAMPLE)
})

describe('建号后停在向导', () => {
  it('不跳转，直接进入第 2 步「配置大模型 Key」', async () => {
    const { wrapper, router } = await mountSetup()

    await createAdmin(wrapper)

    expect(mocked.createFirstAdmin).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.name).toBe('setup')
    expect(wrapper.find('[data-testid="setup-model-key"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('配置大模型 Key')
    // 第 1 步的表单已经收起来，避免用户以为没提交成功
    expect(wrapper.find('[data-testid="setup-username"]').exists()).toBe(false)
  })

  it('企业名留空时用默认企业名提交，不让第 3 步卡在「未绑定企业」', async () => {
    const { wrapper } = await mountSetup()

    await wrapper.get('[data-testid="setup-username"]').setValue('admin')
    await wrapper.get('[data-testid="setup-password"]').setValue('password123')
    await wrapper.get('[data-testid="setup-confirm"]').setValue('password123')
    // 企业名保持空串
    await wrapper.get('[data-testid="setup-submit"]').trigger('click')
    await flushPromises()

    const payload = mocked.createFirstAdmin.mock.calls[0]?.[0]
    expect(payload?.enterprise_name).toBe('我的企业')
  })
})

describe('步骤 2 可跳过', () => {
  it('点「稍后配置」进入第 3 步并拉到示例合同', async () => {
    const { wrapper } = await mountSetup()

    await reachSampleStep(wrapper)

    expect(mocked.fetchSampleContract).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('试跑一份示例合同')
    expect(wrapper.text()).toContain(SAMPLE.name)
    expect(wrapper.find('[data-testid="setup-sample-run"]').exists()).toBe(true)
  })
})

describe('缺 Key 时给回第 2 步的出口', () => {
  it('NO_MODEL 失败后点「返回步骤 2」，重新露出 Key 输入框', async () => {
    mocked.postCheck.mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'NO_MODEL',
        message: '未配置模型 Key，无法发起核对。请到“设置”页配置 Key 后重试。',
        action: { type: 'open_settings', label: '去设置页配置 API Key' },
      }),
    )
    const { wrapper } = await mountSetup()
    await reachSampleStep(wrapper)

    await wrapper.get('[data-testid="setup-sample-run"]').trigger('click')
    await flushPromises()

    const back = wrapper.find('[data-testid="setup-no-model-back"]')
    expect(back.exists()).toBe(true)
    expect(wrapper.text()).toContain('NO_MODEL')

    await back.trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="setup-model-key"]').exists()).toBe(true)
  })
})
