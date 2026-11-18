/**
 * 核对页（规格 006 §3 `/check` + AC-04 无模型门禁 + AC-07 发起/追问/问答链路）。
 *
 * 只 mock 端点层，不 mock 组件：要验证的是「页面在什么状态下渲染什么」。
 * 轮询本身（`composables/job`）另有纯函数用例，这里让 job 一步到位，避免用真实计时等待。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({
  fetchContracts: vi.fn(),
  fetchContract: vi.fn(),
  deleteContract: vi.fn(),
  parseContractFile: vi.fn(),
  postCheck: vi.fn(),
  fetchJob: vi.fn(),
  fetchTask: vi.fn(),
  postAnswers: vi.fn(),
  postOptionalFacts: vi.fn(),
  postQuestion: vi.fn(),
  downloadReport: vi.fn(),
}))

import * as endpoints from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import type { AuthState, JobView, TaskDetail } from '@/api/types'
import CheckView from '@/views/CheckView.vue'
import { checkSession, clearInput, forgetDetail } from '@/stores/checkSession'
import { applyAuth, resetSession } from '@/stores/session'
import { detail, qaResult, taskJob } from './fixtures'

const mocked = vi.mocked(endpoints)

const ADMIN: AuthState = {
  user: { user_id: 'U-1', username: 'admin', role: '管理员', enterprise_id: 'E-1', onboarded: true },
  csrf_token: 'csrf',
  model_configured: true,
}
const STAFF_NO_MODEL: AuthState = {
  user: { user_id: 'U-2', username: 'staff', role: '用户', enterprise_id: 'E-1', onboarded: true },
  csrf_token: 'csrf',
  model_configured: false,
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'check', component: { template: '<div />' } },
      { path: '/settings', name: 'settings', component: { template: '<div />' } },
      { path: '/enterprise', name: 'enterprise', component: { template: '<div />' } },
    ],
  })
}

async function mountView() {
  const router = makeRouter()
  await router.push('/')
  await router.isReady()
  const wrapper = mount(CheckView, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

function failedJob(code: string, message: string, label: string): JobView {
  return {
    job_id: 'J-9',
    kind: 'check',
    status: 'failed',
    task_id: null,
    result: null,
    error: { code, message, details: null, action: { type: 'open_settings', label } },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSession()
  forgetDetail()
  clearInput()
  // 勾选项不随 clearInput 复位（那是输入草稿），用例之间要显式还原
  checkSession.input.source = 'text'
  checkSession.input.clauseReview = true
  checkSession.input.archive = true
  mocked.fetchContracts.mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100, total_all: 0 })
})

describe('无模型门禁', () => {
  it('普通用户：不渲染输入区，只给"联系管理员"的说明', async () => {
    applyAuth(STAFF_NO_MODEL)
    const wrapper = await mountView()

    expect(wrapper.find('[data-testid="no-model-gate"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="check-form"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ask-panel"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('未配置模型，合同核对不可用')
    expect(wrapper.text()).toContain('请联系管理员在设置页配置模型')
    expect(mocked.postCheck).not.toHaveBeenCalled()
  })

  it('管理员看到"去设置页配置 API Key"', async () => {
    applyAuth({ ...ADMIN, model_configured: false })
    const wrapper = await mountView()
    expect(wrapper.text()).toContain('去设置页配置 API Key')
  })
})

describe('发起核对', () => {
  beforeEach(() => {
    applyAuth(ADMIN)
    mocked.fetchTask.mockResolvedValue(detail())
  })

  it('POST /checks 后轮询 job，成功即渲染三层结果（默认无第三层）', async () => {
    const result: TaskDetail = detail()
    mocked.postCheck.mockResolvedValue({ ...taskJob(result), status: 'queued', result: null })
    mocked.fetchJob.mockResolvedValue(taskJob(result))

    const wrapper = await mountView()
    await wrapper.find('textarea').setValue('第一条 试用期六个月。')
    await wrapper.find('[data-testid="start-check"]').trigger('click')
    await flushPromises()

    expect(mocked.postCheck).toHaveBeenCalledTimes(1)
    const payload = mocked.postCheck.mock.calls[0]?.[0]
    expect(payload?.text).toBe('第一条 试用期六个月。')
    expect(payload?.contract_name).toBe('粘贴合同')
    expect(payload?.archive).toBe(true) // 默认存入企业档案
    expect(payload?.clause_review).toBe(true) // 有模型时默认开启条款审查

    expect(wrapper.find('[data-testid="verdict-headline"]').text()).toContain('发现 1 条确定冲突')
    expect(wrapper.find('[data-testid="statute-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="process-layer"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="disclaimer"]').exists()).toBe(true)
  })

  it('取消"存入企业档案"后按 archive=false 提交', async () => {
    mocked.postCheck.mockResolvedValue(taskJob(detail()))
    mocked.fetchJob.mockResolvedValue(taskJob(detail()))

    const wrapper = await mountView()
    await wrapper.find('textarea').setValue('合同正文')
    await wrapper.find('[data-testid="archive-checkbox"] input').setValue(false)
    await flushPromises()
    await wrapper.find('[data-testid="start-check"]').trigger('click')
    await flushPromises()

    expect(mocked.postCheck.mock.calls[0]?.[0]?.archive).toBe(false)
  })

  it('409 NO_MODEL：显示服务端文案与可操作按钮，且不落结果', async () => {
    mocked.postCheck.mockRejectedValue(
      new ApiError({
        status: 409,
        code: 'NO_MODEL',
        message: '未配置模型，无法核对；请先在设置页配置 API Key。',
        action: { type: 'open_settings', label: '去设置页配置 API Key' },
      }),
    )

    const wrapper = await mountView()
    await wrapper.find('textarea').setValue('合同正文')
    await wrapper.find('[data-testid="start-check"]').trigger('click')
    await flushPromises()

    const error = wrapper.find('[data-testid="job-error"]')
    expect(error.exists()).toBe(true)
    expect(error.text()).toContain('未配置模型，无法核对')
    expect(error.text()).toContain('去设置页配置 API Key')
    expect(wrapper.find('[data-testid="verdict-headline"]').exists()).toBe(false)
  })

  it('job 失败（worker 内拦到的 NO_MODEL）也按错误体展示', async () => {
    mocked.postCheck.mockResolvedValue({ ...taskJob(detail()), status: 'queued', result: null })
    mocked.fetchJob.mockResolvedValue(failedJob('NO_MODEL', '未配置模型，无法核对。', '去设置页配置 API Key'))

    const wrapper = await mountView()
    await wrapper.find('textarea').setValue('合同正文')
    await wrapper.find('[data-testid="start-check"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="job-error"]').text()).toContain('未配置模型，无法核对。')
  })

  it('上传文件：调 /contracts/parse，解析结果填进正文并带文件名核对', async () => {
    mocked.parseContractFile.mockResolvedValue({
      filename: '劳动合同A',
      text: '解析出的合同正文',
      char_count: 8,
    })
    mocked.postCheck.mockResolvedValue(taskJob(detail()))
    mocked.fetchJob.mockResolvedValue(taskJob(detail()))

    const wrapper = await mountView()
    await wrapper.findAll('input[type="radio"]')[1]?.setValue(true)
    await flushPromises()

    const fileInput = wrapper.find('[data-testid="contract-file"]')
    const file = new File(['正文'], '劳动合同A.txt', { type: 'text/plain' })
    Object.defineProperty(fileInput.element, 'files', { value: [file], configurable: true })
    await fileInput.trigger('change')
    await flushPromises()

    expect(mocked.parseContractFile).toHaveBeenCalledTimes(1)
    const parsed = wrapper.find('textarea').element as HTMLTextAreaElement
    expect(parsed.value).toBe('解析出的合同正文')

    await wrapper.find('[data-testid="start-check"]').trigger('click')
    await flushPromises()
    expect(mocked.postCheck.mock.calls[0]?.[0]).toMatchObject({
      text: '解析出的合同正文',
      contract_name: '劳动合同A',
    })
  })

  it('从历史合同选择：只发 contract_id，不把正文再传一遍', async () => {
    mocked.fetchContracts.mockResolvedValue({
      items: [{ contract_id: 'C-9', filename: '旧合同.txt', char_count: 20, uploaded_at: '2026-11-01' }],
      total: 1,
      page: 1,
      page_size: 100,
      total_all: 1,
    })
    mocked.postCheck.mockResolvedValue(taskJob(detail()))
    mocked.fetchJob.mockResolvedValue(taskJob(detail()))

    const wrapper = await mountView()
    await wrapper.findAll('input[type="radio"]')[2]?.setValue(true)
    await flushPromises()
    expect(mocked.fetchContracts).toHaveBeenCalledTimes(1)

    const { checkSession } = await import('@/stores/checkSession')
    checkSession.input.contractId = 'C-9'
    await flushPromises()

    await wrapper.find('[data-testid="start-check"]').trigger('click')
    await flushPromises()
    expect(mocked.postCheck.mock.calls[0]?.[0]).toEqual({
      contract_id: 'C-9',
      clause_review: true,
      archive: true,
    })
  })
})

describe('追问 / 可选补充 / 问答', () => {
  beforeEach(() => {
    applyAuth(ADMIN)
  })

  it('awaiting_facts 时左列换成追问面板，提交按 slot_id 回填', async () => {
    const awaiting: TaskDetail = {
      ...detail(),
      workflow: {
        state: 'awaiting_facts',
        effective_contract_id: 'C-1',
        ask_rounds: 1,
        questions: ['员工月工资是多少？'],
        ask_items: [{ slot_id: 'monthly_wage', question: '员工月工资是多少？', source: 'rule', hint: '例如 6000' }],
        optional_fields: [],
      },
    }
    mocked.fetchTask.mockResolvedValue(awaiting)
    mocked.postAnswers.mockResolvedValue(taskJob(detail()))
    mocked.fetchJob.mockResolvedValue(taskJob(detail()))

    const wrapper = await mountView()
    // 通过 job 拿到 awaiting 结果：直接写会话再重挂更贴近真实路径，但这里验证面板行为
    const { rememberDetail } = await import('@/stores/checkSession')
    rememberDetail(awaiting)
    const view = await mountView()

    expect(view.find('[data-testid="ask-panel"]').exists()).toBe(true)
    expect(view.find('[data-testid="check-form"]').exists()).toBe(false)
    expect(view.text()).toContain('补充 1 项事实')

    await view.find('[data-testid="ask-panel"] input').setValue('6000')
    const submit = view.findAll('[data-testid="ask-panel"] button').find((node) => node.text().includes('提交'))
    await submit?.trigger('click')
    await flushPromises()

    expect(mocked.postAnswers).toHaveBeenCalledWith('T-1', {
      answers: { monthly_wage: '6000' },
      skip: false,
    })
    wrapper.unmount()
  })

  it('跳过追问后直接展示结果（服务端 questions 仍在，不重复挡在追问面板）', async () => {
    const awaiting: TaskDetail = {
      ...detail(),
      workflow: {
        state: 'awaiting_facts',
        effective_contract_id: 'C-1',
        ask_rounds: 0,
        questions: ['员工月工资是多少？'],
        ask_items: [{ slot_id: 'monthly_wage', question: '员工月工资是多少？', source: 'rule', hint: null }],
        optional_fields: [],
      },
    }
    mocked.fetchTask.mockResolvedValue(awaiting)
    mocked.postAnswers.mockResolvedValue(taskJob(awaiting))
    mocked.fetchJob.mockResolvedValue(taskJob(awaiting))

    const { rememberDetail } = await import('@/stores/checkSession')
    rememberDetail(awaiting)
    const view = await mountView()

    expect(view.find('[data-testid="ask-panel"]').exists()).toBe(true)
    const skip = view.findAll('[data-testid="ask-panel"] button').find((node) => node.text().includes('跳过'))
    await skip?.trigger('click')
    await flushPromises()

    expect(mocked.postAnswers).toHaveBeenCalledWith('T-1', { answers: {}, skip: true })
    expect(view.find('[data-testid="ask-panel"]').exists()).toBe(false)
    expect(view.find('[data-testid="verdict-headline"]').exists()).toBe(true)
  })

  it('可选补充只在 workflow.optional_fields 非空时出现，并按字段 id 提交', async () => {
    mocked.fetchTask.mockResolvedValue(
      detail({
        workflow: {
          state: 'ready',
          effective_contract_id: 'C-1',
          ask_rounds: 0,
          questions: [],
          ask_items: [],
          optional_fields: [{ field_id: 'monthly_wage', label: '员工月工资', hint: '例如 6000', question: '月工资是多少？' }],
        },
      }),
    )
    mocked.postOptionalFacts.mockResolvedValue(taskJob(detail()))
    mocked.fetchJob.mockResolvedValue(taskJob(detail()))

    const { rememberDetail } = await import('@/stores/checkSession')
    rememberDetail(
      detail({
        workflow: {
          state: 'ready',
          effective_contract_id: 'C-1',
          ask_rounds: 0,
          questions: [],
          ask_items: [],
          optional_fields: [{ field_id: 'monthly_wage', label: '员工月工资', hint: '例如 6000', question: null }],
        },
      }),
    )

    const view = await mountView()
    expect(view.find('[data-testid="optional-facts"]').exists()).toBe(true)
    await view.find('[data-testid="optional-facts"] input').setValue('8000')
    const submit = view.findAll('[data-testid="optional-facts"] button').find((node) => node.text().includes('补充'))
    await submit?.trigger('click')
    await flushPromises()
    expect(mocked.postOptionalFacts).toHaveBeenCalledWith('T-1', { monthly_wage: '8000' })
  })

  it('没有可选字段时不渲染可选补充面板', async () => {
    mocked.fetchTask.mockResolvedValue(detail())
    const { rememberDetail } = await import('@/stores/checkSession')
    rememberDetail(detail())
    const view = await mountView()
    expect(view.find('[data-testid="optional-facts"]').exists()).toBe(false)
  })

  it('提问走 /qa 并渲染问答结果（无过程开关时无运行详情）', async () => {
    mocked.fetchTask.mockResolvedValue(detail())
    mocked.postQuestion.mockResolvedValue({
      job_id: 'J-QA',
      kind: 'qa',
      status: 'succeeded',
      task_id: 'Q-1',
      result: qaResult(),
      error: null,
    })
    mocked.fetchJob.mockResolvedValue({
      job_id: 'J-QA',
      kind: 'qa',
      status: 'succeeded',
      task_id: 'Q-1',
      result: qaResult(),
      error: null,
    })

    const { rememberDetail } = await import('@/stores/checkSession')
    rememberDetail(detail())
    const view = await mountView()

    await view.find('[data-testid="qa-panel"] textarea').setValue('试用期的风险？')
    const ask = view.findAll('[data-testid="qa-panel"] button').find((node) => node.text().includes('提问'))
    await ask?.trigger('click')
    await flushPromises()

    expect(mocked.postQuestion).toHaveBeenCalledWith('T-1', '试用期的风险？')
    expect(view.find('[data-testid="qa-sources"]').exists()).toBe(true)
    expect(view.find('[data-testid="qa-process"]').exists()).toBe(false)
  })
})
