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
