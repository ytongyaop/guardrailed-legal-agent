/**
 * 历史列表与只读详情（规格 006 §3 `/history`、`/history/{task_id}`，AC-09 / AC-07 的页面侧）。
 *
 * 重点：
 * 1. 列表口径由服务端筛，页面要把「最近 200 条窗口」说清楚（`total_all` 不是全库总量）；
 * 2. 软删要经过确认，删完重新拉列表；
 * 3. 只读详情与核对页同一套三层，但**没有**追问 / 可选补充 / 问答。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/endpoints', () => ({
  fetchHistory: vi.fn(),
  fetchTrend: vi.fn(),
  deleteHistoryEntry: vi.fn(),
  fetchTask: vi.fn(),
  downloadReport: vi.fn(),
}))

import * as endpoints from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import type { AuthState } from '@/api/types'
import HistoryView from '@/views/HistoryView.vue'
import HistoryDetailView from '@/views/HistoryDetailView.vue'
import { applyAuth, resetSession } from '@/stores/session'
import { detail, historyPage, trendView } from './fixtures'

const mocked = vi.mocked(endpoints)

const AUTH: AuthState = {
  user: { user_id: 'U-1', username: 'admin', role: '管理员', enterprise_id: 'E-1', onboarded: true },
  csrf_token: 'csrf',
  model_configured: true,
}

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', name: 'history', component: { template: '<div />' } },
      { path: '/enterprise', name: 'enterprise', component: { template: '<div />' } },
      { path: '/history/:taskId', name: 'history-detail', component: { template: '<div />' } },
    ],
  })
}

async function mountHistory() {
  const router = makeRouter()
  await router.push('/')
  await router.isReady()
  const wrapper = mount(HistoryView, {
    global: { plugins: [router], stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSession()
  applyAuth(AUTH)
  mocked.fetchHistory.mockResolvedValue(historyPage())
  mocked.fetchTrend.mockResolvedValue(trendView())
})

describe('历史列表', () => {
  it('渲染行、状态徽标，并说清筛选窗口口径', async () => {
    const wrapper = await mountHistory()
    expect(mocked.fetchHistory).toHaveBeenCalledWith({ filter: 'all', page: 1, page_size: 20 })
    expect(wrapper.find('[data-testid="history-table"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('劳动合同示例.txt')
    expect(wrapper.text()).toContain('已降级')
    expect(wrapper.text()).toContain('引用可溯源')
    const caption = wrapper.find('[data-testid="history-caption"]').text()
    expect(caption).toContain('已排除 2 条已删除记录')
    expect(caption).toContain('最近 200 次核对')
    expect(caption).toContain('窗口内共 1 条')
  })

  it('切换筛选重新按 filter 请求，并把页码复位', async () => {
    const wrapper = await mountHistory()
    const filters = wrapper.findAll('input[type="radio"]')
    // 全部 / 有风险 / 已降级
    expect(filters).toHaveLength(3)
    await filters[1]?.setValue(true)
    await flushPromises()
    expect(mocked.fetchHistory).toHaveBeenLastCalledWith({ filter: 'risk', page: 1, page_size: 20 })
  })

  it('软删需要确认，确认后调用 DELETE 并重新拉列表', async () => {
    mocked.deleteHistoryEntry.mockResolvedValue(undefined)
    const wrapper = await mountHistory()

    const del = wrapper.findAll('button').find((node) => node.text().includes('删除'))
    await del?.trigger('click')
    await flushPromises()

    const confirm = wrapper.findAll('button').find((node) => node.text().includes('确认删除'))
    expect(confirm).toBeDefined()
    await confirm?.trigger('click')
    await flushPromises()

    expect(mocked.deleteHistoryEntry).toHaveBeenCalledWith('T-1')
    expect(mocked.fetchHistory).toHaveBeenCalledTimes(2)
  })

  it('趋势区展示统计文案，图表默认不加载（展开才拉 ECharts chunk）', async () => {
    const wrapper = await mountHistory()
    const trend = wrapper.find('[data-testid="trend-block"]')
    expect(trend.exists()).toBe(true)
    expect(wrapper.find('[data-testid="trend-note"]').text()).toContain('统计窗口：最近 2 次有效核对')
    expect(wrapper.find('[data-testid="trend-chart"]').exists()).toBe(false)
    expect((trend.element as HTMLDetailsElement).open).toBe(false)
  })

  it('没有企业时只给提示，不发请求', async () => {
    applyAuth({ ...AUTH, user: { ...AUTH.user, enterprise_id: '' } })
    const wrapper = await mountHistory()
    expect(wrapper.text()).toContain('当前账号未绑定企业')
    expect(mocked.fetchHistory).not.toHaveBeenCalled()
  })
})

describe('历史详情（只读）', () => {
  async function mountDetail(taskId = 'T-1') {
    const router = makeRouter()
    await router.push(`/history/${taskId}`)
    await router.isReady()
    const wrapper = mount(HistoryDetailView, { global: { plugins: [router] } })
    await flushPromises()
    return wrapper
  }

  it('用同一套三层渲染结果，但不提供追问 / 可选补充 / 问答', async () => {
    mocked.fetchTask.mockResolvedValue(detail())
    const wrapper = await mountDetail('T-1')

    expect(mocked.fetchTask).toHaveBeenCalledWith('T-1')
    expect(wrapper.find('[data-testid="verdict-headline"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="statute-label"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="disclaimer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="ask-panel"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="optional-facts"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="qa-panel"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="process-layer"]').exists()).toBe(false)
  })

  it('过程开关打开（process 非空）时详情也出现第三层', async () => {
    const { detailWithProcess } = await import('./fixtures')
    mocked.fetchTask.mockResolvedValue(detailWithProcess())
    const wrapper = await mountDetail('T-1')
    expect(wrapper.find('[data-testid="process-layer"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="qa-panel"]').exists()).toBe(false)
  })

  it('记录不存在时显示错误体，不白屏', async () => {
    mocked.fetchTask.mockRejectedValue(
      new ApiError({ status: 404, code: 'NOT_FOUND', message: '任务不存在' }),
    )
    const wrapper = await mountDetail('T-missing')
    const notice = wrapper.find('[data-testid="history-missing"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('任务不存在')
    expect(wrapper.find('[data-testid="verdict-headline"]').exists()).toBe(false)
  })
})
