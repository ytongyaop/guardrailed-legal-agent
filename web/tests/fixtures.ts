/**
 * 结果 DTO 的测试夹具（形状与 `mla.api.schemas.TaskDetail` 一致）。
 *
 * 刻意手写而不是抓服务端响应：这些用例要钉的是**渲染规则**（三层归属、折叠、计数独立），
 * 数据本身与后端契约由 `tests/api/test_result_contract.py` 负责。内容取一份"丰富任务"：
 * 两条结论（高影响确定冲突 + 中影响待确认）、限制提示、必备条款三态、建议、事实、类案。
 */

import type {
  FindingView,
  HistoryPage,
  JobView,
  ProcessView,
  QaResult,
  StatuteView,
  TaskDetail,
  TrendView,
} from '@/api/types'

export const LABOR_ARTICLE = '《劳动合同法》第十七条'
export const LABOR_TEXT = '劳动合同应当具备以下条款：……'
export const SOCIAL_ARTICLE = '《社会保险法》第五十八条'
export const SOCIAL_TEXT = '用人单位应当自用工之日起三十日内为其职工申请办理社会保险登记。'

export function statute(overrides: Partial<StatuteView> = {}): StatuteView {
  return {
    id: 'LCL-17',
    found: true,
    label: LABOR_ARTICLE,
    text: LABOR_TEXT,
    effective_date: '2013-07-01',
    source_url: 'https://example.gov.cn/lcl.html',
    ...overrides,
  }
}

export function finding(overrides: Partial<FindingView> = {}): FindingView {
  return {
    item_id: 'IT-1',
    path: 'consensus',
    impact: '高',
    certainty: '确定冲突',
    display_marker: null,
    finding: '试用期约定超过法定上限。',
    clause_ref: '3',
    evidence_text: '试用期为六个月，试用期工资为转正工资的百分之七十。',
    span_preview: '试用期为六个月……',
    span_expansible: false,
    need_facts: [],
    statutes: [statute()],
    process: null,
    ...overrides,
  }
}

export function processView(overrides: Partial<ProcessView> = {}): ProcessView {
  return {
    cited_statute_count: 3,
    latency_s: 12.34,
    latency_text: '12.34 秒',
    details: ['规则 18/20 可判定', '条款审查已运行'],
    finding_help: '按影响度分组……',
    clause_review_note: '本次条款级审查未发现可疑条款。',
    verify_ok: true,
    traceability_text: '引用可溯源',
    ask_rounds: 1,
    ask_text: '补问 1 轮',
    degraded_visible: false,
    extract_note: null,
    questions: ['员工月工资是多少？'],
    task_id: 'T-1',
    token_in: 1200,
    token_out: 300,
    run_detail: '任务 T-1 · Token 输入 1200 · 输出 300',
    ...overrides,
  }
}

export function detail(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    task: {
      task_id: 'T-1',
      enterprise_id: 'E-1',
      contract_id: 'C-1',
      contract_name: '劳动合同示例.txt',
      contract_chars: 320,
      created_at: '2026-11-18T09:30:00',
      model: 'deepseek-chat',
      model_first: true,
      clause_review: true,
      shared: true,
    },
    verdict: {
      status: 'risk',
      headline: '发现 1 条确定冲突、1 条待确认',
      risk_count: 1,
      review_count: 1,
      conclusion_details: ['确定冲突 1 条', '待确认 1 条'],
      process_details: [],
      caveat: '结论不代表合规，请结合事实复核。',
    },
    findings: [
      finding(),
      finding({
        item_id: 'IT-2',
        impact: '中',
        certainty: '待确认',
        finding: '竞业限制补偿标准不明确。',
        clause_ref: '',
        evidence_text: '',
        span_preview: '',
        statutes: [statute({ id: 'LCL-23', label: '《劳动合同法》第二十三条', text: '……' })],
        path: 'rule',
      }),
    ],
    limits: {
      unmet_lines: ['未约定试用期期限，无法判断是否满足“试用期不得超过法定上限”（依据 《劳动合同法》第十九条）'],
      field_missing_lines: ['因缺少“员工月工资”，以下 2 条规则无法判定：最低工资标准、加班费基数'],
      premise_missing_lines: [],
      premise_count: 0,
      statutory_na_line: null,
    },
    missing_terms: {
      summary: '本次检查《劳动合同法》第十七条九类必备条款：缺失 1 类，未见 1 类，需人工确认 1 类。（第（九）类为兜底条款，不参与覆盖率）',
      short_note: null,
      written_count: null,
      absent_count: 1,
      unseen_count: 1,
      manual_count: 1,
      items: [
        {
          term_id: 'MT-1',
          name: '工作内容和工作地点',
          status: '缺失',
          text: '缺失：未见工作内容和工作地点的约定（第十七条第一项）',
          statute: statute(),
        },
        {
          term_id: 'MT-2',
          name: '劳动报酬',
          status: '未见',
          text: '未见劳动报酬的约定（第十七条第六项），请人工确认',
          statute: statute({ id: 'LCL-17-6', label: '《劳动合同法》第十七条第六项' }),
        },
        {
          term_id: 'MT-3',
          name: '社会保险',
          status: '需人工确认',
          text: '社会保险（第十七条第七项）',
          statute: statute({ id: 'LCL-17-7', label: '《劳动合同法》第十七条第七项' }),
        },
      ],
      manual_names: ['社会保险'],
    },
    suggestions: [
      {
        text: '把试用期改为不超过法定上限，并保留书面约定。',
        citation_text: '依据 《劳动合同法》第十九条',
        display_disclaimer: null,
        sources: [statute({ id: 'LCL-19', label: '《劳动合同法》第十九条' })],
      },
    ],
    suggestion_empty_text: null,
    facts: [
      { label: '用工主体类型', value: '有限责任公司' },
      { label: '是否书面签订', value: '是' },
    ],
    cases: {
      note: '同类情形通常如何认定，不构成本案结论。',
      summary: '共 2 条官方案例，本次匹配 1 条',
      items: [
        {
          title: '试用期超过法定上限的认定',
          holding: '超出部分按已履行的期限支付赔偿金。',
          source: '最高人民法院指导案例',
          dispute_type: '劳动争议',
          source_url: 'https://example.gov.cn/case/1',
        },
      ],
    },
    process: null,
    refusal: null,
    workflow: {
      state: 'ready',
      effective_contract_id: 'C-1',
      ask_rounds: 0,
      questions: [],
      ask_items: [],
      optional_fields: [],
    },
    disclaimer: '以上为风险提示与依据定位，不构成法律意见；条款效力以仲裁机构或法院裁判为准。',
    ...overrides,
  }
}

/** 带第三层的同一份任务：只多过程字段，第一二层逐字段不变（由用例断言）。 */
export function detailWithProcess(): TaskDetail {
  const base = detail()
  return {
    ...base,
    verdict: { ...base.verdict, process_details: ['规则 18/20 可判定', '条款审查已运行'] },
    findings: base.findings.map((row) => ({
      ...row,
      process: { source_label: '模型审查', grade: 'A', note: '与第十九条明文冲突' },
    })),
    missing_terms: { ...base.missing_terms, written_count: 5 },
    process: processView(),
  }
}

export function taskJob(result: TaskDetail): JobView {
  return {
    job_id: 'J-1',
    kind: 'check',
    status: 'succeeded',
    task_id: result.task.task_id,
    result,
    error: null,
  }
}

export function qaResult(overrides: Partial<QaResult> = {}): QaResult {
  return {
    task_id: 'Q-1',
    parent_task_id: 'T-1',
    refusal: null,
    empty_text: null,
    errors: [],
    sources: [statute({ id: 'LCL-19', label: '《劳动合同法》第十九条', text: '试用期不得超过……' })],
    suggestions: [
      {
        text: '按法定上限重签试用期条款。',
        citation_text: '依据 《劳动合同法》第十九条',
        display_disclaimer: null,
        sources: [statute({ id: 'LCL-19', label: '《劳动合同法》第十九条' })],
      },
    ],
    process: null,
    ...overrides,
  }
}

export function historyPage(): HistoryPage {
  return {
    items: [
      {
        task_id: 'T-1',
        contract_name: '劳动合同示例.txt',
        created_at: '2026-11-18T09:30:00',
        verdict: {
          status: 'risk',
          headline: '发现 1 条确定冲突',
          risk_count: 1,
          review_count: 0,
          conclusion_details: [],
          process_details: [],
          caveat: '结论不代表合规，请结合事实复核。',
        },
        suggestion_count: 1,
        latency_s: 12.34,
        latency_text: '12.34 秒',
        refused: false,
        verify_ok: true,
        degraded_visible: true,
        ask_rounds: 0,
      },
    ],
    total: 1,
    page: 1,
    page_size: 20,
    total_all: 1,
    deleted_count: 2,
    window_size: 200,
  }
}

export function trendView(): TrendView {
  return {
    recent: 1,
    average: 1.5,
    max: 3,
    window_size: 200,
    note: '统计窗口：最近 2 次有效核对；平均值含真实 0 风险记录，分母为 2。图内“风险条目”只统计规则条目，不含条款审查结论与覆盖状态。',
    points: [
      { order: 1, label: '11-17 09:00', date: '11-17 09:00', risk_item_count: 3, suggestion_count: 2 },
      { order: 2, label: '11-18 09:30', date: '11-18 09:30', risk_item_count: 1, suggestion_count: 1 },
    ],
  }
}
