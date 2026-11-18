/**
 * 与后端 DTO 一一对应的类型（规格 006 §2）。
 *
 * 这些类型只描述形状，不含任何计算：计数、分级、排序由服务端给出。
 * 批次 4 只覆盖非结果页用到的部分（结果 DTO 留给批次 5）。
 */

export interface UserView {
  user_id: string
  username: string
  role: string
  enterprise_id: string
  onboarded: boolean
}

export interface AuthState {
  user: UserView
  csrf_token: string
  model_configured: boolean
}

export interface RuntimeInfo {
  copied_from: string | null
  db_path: string
  warnings: string[]
  migration_status: string
  messages: string[]
}

export interface SetupStatus {
  needs_admin: boolean
  runtime: RuntimeInfo
}

export interface LoginIn {
  username: string
  password: string
}

export interface RegisterIn extends LoginIn {
  enterprise_name: string
}

export interface PasswordChangeIn {
  current_password: string
  new_password: string
  confirm_password: string
}

export interface SetupAdminIn {
  username: string
  password: string
  confirm_password: string
  enterprise_name: string
}

export interface EnterpriseIn {
  name: string
  employer_type: string
  staff_size: number
  working_hours: string
  social_insurance: string
}

export interface EnterpriseView extends EnterpriseIn {
  enterprise_id: string
  created_at: string
}

export interface ContractSummary {
  contract_id: string
  filename: string
  char_count: number
  uploaded_at: string
}

export interface ContractDetail {
  contract_id: string
  enterprise_id: string
  filename: string
  text: string
  uploaded_at: string
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface ContractPage extends Page<ContractSummary> {
  total_all: number
}

export interface SettingsUiView {
  show_query_path: boolean
}

export interface ProviderView {
  provider: string
  display_name: string
  configured: boolean
  source: string
  label: string
}

export interface ModelCheckView {
  ok: boolean
  checked_at: string
  kind: string
  stale: boolean
}

export interface EmbeddingView {
  model: string
  configured: boolean
  fallback_text: string
}

export interface ModelSettingsView {
  providers: ProviderView[]
  model: string
  status_line: string
  check: ModelCheckView | null
  embedding: EmbeddingView
}

export interface AdminUser {
  user_id: string
  username: string
  role: string
  enterprise_id: string
  created_at: string
  onboarded: boolean
}

export interface DeletionOut {
  deleted: boolean
  username: string
  contracts: number
  tasks: number
  logs: number
  reason: string
}

export interface StatuteRow {
  statute_id: string
  law_name: string
  article_no: string
  text: string
  issuing_body: string
  doc_no: string
  effective_date: string | null
  expiry_date: string | null
  status: string
  superseded_by: string | null
  level: string
  region: string
  source_url: string
  note: string
}

export interface StatutePage extends Page<StatuteRow> {
  total_all: number
}

export interface RuleRow {
  rule_id: string
  name: string
  field: string
  op: string
  threshold: number | string
  statute_id: string
  unit: string
  severity: string
  effective_date: string | null
  note: string
  when_field: string
  when_op: string
  when_value: number | string
  when_field2: string
  when_op2: string
  when_value2: number | string
  when_mode: string
  evidence_span: string
}

export interface SlotRow {
  slot_id: string
  name: string
  type: string
  question: string
  needed_by: string[]
}

export interface RuleCatalogView {
  rules: RuleRow[]
  slots: SlotRow[]
}

export interface LogRow {
  log_id: string
  task_id: string
  user_id: string
  action: string
  detail: string
  created_at: string
}

export interface LogPage extends Page<LogRow> {
  total_all: number
}

export interface MetricRow {
  key: string
  label: string
  value: number
  threshold: number | null
  passed: boolean | null
}

export interface EvalVariant {
  variant: string
  metrics: MetricRow[]
  mismatches: Record<string, unknown>[]
}

export interface EvalHistoryRow {
  time: string
  case_count: number
  variant: string
  accuracy: number | null
  recall: number | null
  passed_text: string
}

export interface EvaluationView {
  available: boolean
  message: string | null
  case_count: number
  generated_at: string
  variants: EvalVariant[]
  history: EvalHistoryRow[]
}

// ---------------------------------------------------------------- 核对结果（006「结果 DTO」）
//
// 结果页的**唯一展示真源**：计数、分级、排序、分组键都在服务端算好，这里只描述形状。
// 前端不得自己数「几条风险」或重排条目（005 第 6 节 / 006 非目标）。

export interface StatuteView {
  id: string
  found: boolean
  /** 常驻可读法名条号（如《劳动合同法》第十七条）；查不到时是固定文案，绝不是内部 ID。 */
  label: string
  text: string | null
  effective_date: string | null
  source_url: string | null
}

export interface FindingProcessView {
  source_label: string
  grade: string
  note: string
}

export type FindingPath = 'consensus' | 'rule' | 'clause'

export interface FindingView {
  item_id: string
  path: FindingPath
  impact: string | null
  certainty: string | null
  /** 常驻提示（如「模型自提，待人工确认」），属结论而非过程。 */
  display_marker: string | null
  finding: string
  clause_ref: string
  evidence_text: string
  span_preview: string
  span_expansible: boolean
  need_facts: string[]
  statutes: StatuteView[]
  /** 第三层：过程开关关闭时必为 null（服务端不返回，前端不补算）。 */
  process: FindingProcessView | null
}

export interface VerdictView {
  status: string
  headline: string
  risk_count: number
  review_count: number
  conclusion_details: string[]
  process_details: string[]
  caveat: string
}

export interface LimitsView {
  unmet_lines: string[]
  field_missing_lines: string[]
  premise_missing_lines: string[]
  premise_count: number
  statutory_na_line: string | null
}

export interface MissingTermItem {
  term_id: string
  name: string
  status: string
  text: string
  statute: StatuteView
}

export interface MissingTermsView {
  summary: string
  short_note: string | null
  /** 覆盖率口径，仅过程开关开启时非空。 */
  written_count: number | null
  absent_count: number
  unseen_count: number
  manual_count: number
  items: MissingTermItem[]
  manual_names: string[]
}

export interface SuggestionView {
  text: string
  citation_text: string
  display_disclaimer: string | null
  sources: StatuteView[]
}

export interface ProcessView {
  cited_statute_count: number
  /** 旧记录没有耗时就是 null；前端不得显示成 0。 */
  latency_s: number | null
  latency_text: string
  details: string[]
  finding_help: string
  clause_review_note: string | null
  verify_ok: boolean
  traceability_text: string
  ask_rounds: number
  ask_text: string
  degraded_visible: boolean
  extract_note: string | null
  questions: string[]
  task_id: string
  token_in: number
  token_out: number
  run_detail: string
}

export interface TaskHead {
  task_id: string
  enterprise_id: string
  contract_id: string
  contract_name: string
  contract_chars: number
  created_at: string | null
  model: string
  model_first: boolean
  clause_review: boolean
  /** true=已存入企业档案；false=仅自己可见（历史页不显示）。 */
  shared: boolean
}

export interface AskItemView {
  slot_id: string
  question: string
  source: string
  hint: string | null
}

export interface OptionalFieldView {
  field_id: string
  label: string
  hint: string | null
  question: string | null
}

export interface WorkflowView {
  state: 'awaiting_facts' | 'ready' | 'history'
  effective_contract_id: string
  ask_rounds: number
  questions: string[]
  ask_items: AskItemView[]
  optional_fields: OptionalFieldView[]
}

export interface RefusalView {
  layer: string
  reason: string
  message: string
  guidance: string
}

export interface FactRow {
  label: string
  value: string
}

export interface CaseItemView {
  title: string
  holding: string
  source: string
  dispute_type: string
  source_url: string | null
}

export interface CaseBlockView {
  note: string
  summary: string
  items: CaseItemView[]
}

export interface TaskDetail {
  task: TaskHead
  verdict: VerdictView
  findings: FindingView[]
  limits: LimitsView
  missing_terms: MissingTermsView
  suggestions: SuggestionView[]
  suggestion_empty_text: string | null
  facts: FactRow[]
  cases: CaseBlockView
  process: ProcessView | null
  refusal: RefusalView | null
  workflow: WorkflowView
  /** 本次运行的诊断（含「结果未能存入历史」）：原样展示，前端不补算。 */
  errors: string[]
  disclaimer: string
}

export interface QaProcessView {
  agent_trace: string[]
  token_in: number
  token_out: number
  detail_text: string
}

export interface QaResult {
  task_id: string
  parent_task_id: string
  refusal: RefusalView | null
  empty_text: string | null
  errors: string[]
  sources: StatuteView[]
  suggestions: SuggestionView[]
  process: QaProcessView | null
}

export interface HistorySummary {
  task_id: string
  contract_name: string
  created_at: string | null
  verdict: VerdictView
  suggestion_count: number
  latency_s: number | null
  latency_text: string
  refused: boolean
  verify_ok: boolean
  degraded_visible: boolean
  ask_rounds: number
}

export interface HistoryPage extends Page<HistorySummary> {
  /** 200 条窗口内的条数，不是全库总量。 */
  total_all: number
  deleted_count: number
  window_size: number
}

export interface TrendPoint {
  order: number
  label: string
  date: string
  risk_item_count: number
  suggestion_count: number
}

export interface TrendView {
  recent: number
  average: number
  max: number
  window_size: number
  note: string
  points: TrendPoint[]
}

// ---------------------------------------------------------------- 异步核对（/checks 与 /jobs）

export interface CheckIn {
  text?: string | null
  contract_name?: string | null
  contract_id?: string | null
  clause_review?: boolean | null
  archive: boolean
}

export interface ParseResult {
  filename: string
  text: string
  char_count: number
}

export interface JobErrorView {
  code: string
  message: string
  details: Record<string, unknown> | null
  action: { type: string; label: string } | null
}

export interface JobView {
  job_id: string
  kind: string
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  task_id: string | null
  /** 形状按 kind 区分：check/recheck 是 TaskDetail，qa 是 QaResult。 */
  result: TaskDetail | QaResult | null
  error: JobErrorView | null
}

/** 判别 job.result 是哪种结果：结果页与问答面板共用，避免每处各写一份断言。 */
export function isTaskDetail(result: TaskDetail | QaResult | null): result is TaskDetail {
  return result !== null && typeof result === 'object' && 'verdict' in result
}

export function isQaResult(result: TaskDetail | QaResult | null): result is QaResult {
  return result !== null && typeof result === 'object' && 'parent_task_id' in result
}

