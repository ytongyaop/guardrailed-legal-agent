/**
 * 三层结果渲染（规格 005 / 006 AC-01、AC-02 的 DOM 侧断言）。
 *
 * 这些用例只挂 `ResultView`，不碰网络：DTO 形状由 `tests/api/test_result_contract.py` 锁，
 * 这里锁的是「前端怎么摆」：
 * 1. 过程开关关闭时，第三层的节点**根本不存在**（不是 CSS 隐藏）；
 * 2. 打开后只多第三层，第一层的结论句/计数/条目/建议与关闭态逐字相同；
 * 3. 条号常驻、原文/事实/类案折叠、建议不折叠、必备条款独立计数、免责只出现一次且在最后。
 */

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ResultView from '@/components/result/ResultView.vue'
import { LABOR_ARTICLE, detail, detailWithProcess, finding } from './fixtures'

function mountResult(overrides: Parameters<typeof detail>[0] = {}) {
  return mount(ResultView, { props: { detail: detail(overrides) } })
}

/** 折叠器**之外**的可见文本：HTMLDetailsElement 的子树要整体排除才算「折叠起来了」。 */
function outsideFolds(element: Element): string {
  const clone = element.cloneNode(true) as Element
  clone.querySelectorAll('details').forEach((node) => node.remove())
  return clone.textContent ?? ''
}

describe('默认态（过程开关关闭：process=null）', () => {
  it('第三层完全不渲染：没有节点、也没有过程文案', () => {
    const wrapper = mountResult()
    expect(wrapper.find('[data-testid="process-layer"]').exists()).toBe(false)
    const text = wrapper.text()
    for (const word of ['查询路径与判定来源', '引用条文', '耗时', '可溯源', '补问 1 轮', 'Token 输入', '运行详情']) {
      expect(text).not.toContain(word)
    }
    // 判定来源与内部等级只在第三层：默认态不出现
    expect(wrapper.find('[data-testid="finding-source"]').exists()).toBe(false)
    expect(text).not.toContain('等级 A')
  })

  it('条号常驻、原文折叠：标签在折叠器外，折叠器默认未展开', () => {
    const wrapper = mountResult()
    const labels = wrapper.findAll('[data-testid="statute-label"]')
    expect(labels.length).toBeGreaterThan(0)
    expect(labels[0]?.text()).toContain('《劳动合同法》')
    const fold = wrapper.find('[data-testid="statute-fold"]')
    expect(fold.exists()).toBe(true)
    const element = fold.element as HTMLDetailsElement
    expect(element.tagName.toLowerCase()).toBe('details')
    expect(element.open).toBe(false)
    // 原文只出现在折叠器内部；条号在折叠器外常驻，不在常驻文本里重复原文
    expect(fold.text()).toContain('劳动合同应当具备以下条款')
    const findings = wrapper.find('[data-testid="findings"]')
    expect(outsideFolds(findings.element)).not.toContain('劳动合同应当具备以下条款')
    expect(outsideFolds(findings.element)).toContain('《劳动合同法》第十七条')
  })

  it('事实与类案折叠、处理建议不折叠', () => {
    const wrapper = mountResult()
    for (const testid of ['facts-fold', 'cases-fold']) {
      const fold = wrapper.find(`[data-testid="${testid}"]`)
      expect(fold.exists()).toBe(true)
      expect((fold.element as HTMLDetailsElement).open).toBe(false)
    }
    const suggestions = wrapper.find('[data-testid="suggestions"]')
    expect(suggestions.exists()).toBe(true)
    expect(suggestions.text()).toContain('把试用期改为不超过法定上限')
    expect(suggestions.find('details').exists()).toBe(false)
  })

  it('必备条款独立计数，不并入冲突/待确认条数', () => {
    const wrapper = mountResult()
    const terms = wrapper.find('[data-testid="missing-terms"]')
    expect(terms.exists()).toBe(true)
    expect(terms.text()).toContain('缺失 1 类')
    expect(terms.text()).toContain('未见 1 类')
    expect(terms.text()).toContain('需人工确认 1 类')
    // 结论行只说冲突/待确认，不把必备条款加进去
    const verdictLine = wrapper.find('[data-testid="verdict-details"]').text()
    expect(verdictLine).toContain('确定冲突 1 条')
    expect(verdictLine).not.toContain('必备条款')
    // 覆盖率口径（已写 x 类）只在服务端开关打开时才有值
    expect(terms.text()).not.toContain('已写')
  })

  it('正文过短：必备条款只给一句合体说明，不再另起「需人工确认 N 类」', () => {
    const wrapper = mountResult({
      missing_terms: {
        summary: '',
        short_note: '正文过短，未做必备条款判定；以下 1 类需人工确认：社会保险。',
        written_count: null,
        absent_count: 0,
        unseen_count: 0,
        manual_count: 1,
        items: [],
        manual_names: ['社会保险'],
      },
    })
    const terms = wrapper.find('[data-testid="missing-terms"]')
    expect(terms.find('[data-testid="missing-short-note"]').text()).toContain(
      '正文过短，未做必备条款判定；以下 1 类需人工确认：社会保险。',
    )
    // 类名已在那句里，再列一行「需人工确认 1 类」会读成自相矛盾
    expect(terms.find('[data-testid="missing-manual"]').exists()).toBe(false)
  })

  it('条号与片段都为空的条目只给灰色说明，不伪造片段', () => {
    const wrapper = mountResult({
      findings: [
        finding({
          item_id: 'RC-1',
          path: 'consensus',
          certainty: '确定冲突',
          clause_ref: '',
          evidence_text: '',
          span_preview: '',
          finding: '数值与第 41 条不一致，请核对合同约定',
        }),
      ],
    })
    const card = wrapper.find('[data-testid="finding-card"]')
    expect(card.find('[data-testid="finding-span-none"]').text()).toBe('（程序数值校验，无对应条款片段）')
    expect(card.text()).not.toContain('条款片段（正文第')
    // 理由与依据照常给，不用灰色说明顶替结论
    expect(card.text()).toContain('数值与第 41 条不一致')
    expect(card.find('[data-testid="finding-statutes"]').exists()).toBe(true)
  })

  it('依据全空时整张「依据」卡片不渲染，不留空壳', () => {
    const wrapper = mountResult({
      facts: [],
      cases: {
        note: '同类情形通常如何认定，不构成本案结论。',
        summary: '共 0 条官方案例，本次匹配 0 条',
        items: [],
      },
    })
    expect(wrapper.find('[data-testid="evidence-layer"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="facts-fold"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="cases-fold"]').exists()).toBe(false)
  })

  it('任务元信息只留存档状态，时间收进「详情」并精确到秒', () => {
    const wrapper = mountResult()
    expect(wrapper.find('[data-testid="task-meta"]').text()).toBe('已存入企业档案')
    const fold = wrapper.find('[data-testid="task-detail-fold"]')
    expect(fold.exists()).toBe(true)
    expect((fold.element as HTMLDetailsElement).open).toBe(false)
    expect(fold.text()).toContain('2026-11-18 09:30:00')
    expect(fold.text()).not.toContain('T09:30')
  })

  it('结论提醒常驻，免责声明只出现一次且在最后', () => {
    const wrapper = mountResult()
    expect(wrapper.find('[data-testid="verdict-caveat"]').text()).toContain('结论不代表合规')
    const disclaimers = wrapper.findAll('[data-testid="disclaimer"]')
    expect(disclaimers).toHaveLength(1)
    const root = wrapper.element as HTMLElement
    const last = root.lastElementChild
    expect(last?.getAttribute('data-testid')).toBe('disclaimer')
  })

  it('运行诊断原样展示：有诊断才出现，失败不静默', () => {
    expect(mountResult().find('[data-testid="result-errors"]').exists()).toBe(false)
    const wrapper = mountResult({
      errors: ['本次结果未能存入历史（OperationalError），历史页看不到这条记录，请重新核对一次'],
    })
    const node = wrapper.find('[data-testid="result-errors"]')
    expect(node.exists()).toBe(true)
    expect(node.text()).toContain('未能存入历史')
    // 诊断不挤占结论层：结论句照常由服务端给
    expect(wrapper.find('[data-testid="verdict-headline"]').exists()).toBe(true)
  })

  it('按影响度分组，组内保持服务端顺序', () => {
    const wrapper = mountResult()
    const cards = wrapper.findAll('[data-testid="finding-card"]')
    expect(cards).toHaveLength(2)
    expect(cards[0]?.attributes('data-impact')).toBe('高')
    expect(cards[1]?.attributes('data-impact')).toBe('中')
    expect(wrapper.find('[data-testid="impact-高"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="impact-中"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="impact-低"]').exists()).toBe(false)
  })

  it('拒答只渲染原因与下一步，不渲染任何结论层', () => {
    const wrapper = mountResult({
      refusal: {
        layer: 'llm',
        reason: '未配置大模型',
        message: '无法生成回答：未配置大模型',
        guidance: '按“无依据不答”原则不给出生成式回答；请在设置页配置 DeepSeek Key 后重试。',
      },
    })
    expect(wrapper.find('[data-testid="refusal"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="findings"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="disclaimer"]').exists()).toBe(false)
  })
})

describe('开关态（process 非空）', () => {
  it('只增加第三层：第一层内容与默认态逐字相同', () => {
    const off = mount(ResultView, { props: { detail: detail() } })
    const on = mount(ResultView, { props: { detail: detailWithProcess() } })

    expect(on.find('[data-testid="process-layer"]').exists()).toBe(true)
    const processText = on.find('[data-testid="process-layer"]').text()
    for (const word of ['查询路径与判定来源', '引用条文', '12.34 秒', '规则 18/20 可判定', '引用可溯源', '补问 1 轮', 'Token 输入']) {
      expect(processText).toContain(word)
    }

    // 第一层：结论句、明细、条目、建议一条不少且一字不改
    expect(on.find('[data-testid="verdict-headline"]').text()).toBe(off.find('[data-testid="verdict-headline"]').text())
    expect(on.find('[data-testid="verdict-details"]').text()).toBe(off.find('[data-testid="verdict-details"]').text())
    expect(on.findAll('[data-testid="finding-card"]')).toHaveLength(off.findAll('[data-testid="finding-card"]').length)
    expect(on.find('[data-testid="suggestions"]').text()).toBe(off.find('[data-testid="suggestions"]').text())
    // 第二层：事实/类案不因开关变化
    expect(on.find('[data-testid="facts-fold"]').text()).toBe(off.find('[data-testid="facts-fold"]').text())
  })

  it('来源与内部等级随第三层出现（每条结论各自叠加）', () => {
    const on = mount(ResultView, { props: { detail: detailWithProcess() } })
    const cards = on.findAll('[data-testid="finding-card"]')
    expect(cards[0]?.find('[data-testid="finding-source"]').text()).toContain('模型审查')
    expect(cards[0]?.text()).toContain('等级 A')
  })

  it('免责声明仍只在最后出现一次', () => {
    const on = mount(ResultView, { props: { detail: detailWithProcess() } })
    expect(on.findAll('[data-testid="disclaimer"]')).toHaveLength(1)
    const root = on.element as HTMLElement
    expect(root.lastElementChild?.getAttribute('data-testid')).toBe('disclaimer')
    expect(on.find('[data-testid="process-layer"]').text()).not.toContain('不构成法律意见')
  })
})

describe('问答插槽', () => {
  it('历史只读详情不传插槽时，免责仍然是最后一行', () => {
    const wrapper = mount(ResultView, { props: { detail: detail() } })
    expect(wrapper.find('[data-testid="qa-panel"]').exists()).toBe(false)
    const root = wrapper.element as HTMLElement
    expect(root.lastElementChild?.getAttribute('data-testid')).toBe('disclaimer')
  })

  it('传入插槽时渲染在免责之前（与旧界面顺序一致）', () => {
    const wrapper = mount(ResultView, {
      props: { detail: detail() },
      slots: { qa: '<div data-testid="qa-panel">问答</div>' },
    })
    const root = wrapper.element as HTMLElement
    expect(root.textContent).toContain(LABOR_ARTICLE)
    const qa = root.querySelector('[data-testid="qa-panel"]')
    const disclaimer = root.querySelector('[data-testid="disclaimer"]')
    expect(qa).not.toBeNull()
    expect(disclaimer?.previousElementSibling).toBe(qa)
  })
})
