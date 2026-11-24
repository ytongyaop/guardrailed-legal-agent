/**
 * 端点封装（规格 006 §2 端点表）。页面只调用这里的函数，不拼 URL、不手写 header。
 *
 * 批次 4 覆盖：setup / auth / enterprise / contracts / settings / admin。
 * 批次 5 补上核对链路、结果、历史、报告与问答。
 */

import { http } from './http'
import type {
  AdminUser,
  AuthState,
  CheckIn,
  ContractDetail,
  ContractPage,
  DeletionOut,
  EnterpriseIn,
  EnterpriseView,
  EvaluationView,
  HistoryPage,
  JobView,
  LogPage,
  LoginIn,
  ModelSettingsView,
  ParseResult,
  PasswordChangeIn,
  RegisterIn,
  RuleCatalogView,
  SampleContract,
  SettingsUiView,
  SetupAdminIn,
  SetupStatus,
  StatutePage,
  TaskDetail,
  TrendView,
  UserView,
} from './types'

// ---------------------------------------------------------------- setup（公开）

export function fetchSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return http.get<SetupStatus>('/setup', { signal })
}

/** 首次向导第 3 步：公开端点，取一份含典型问题的示例合同。 */
export function fetchSampleContract(): Promise<SampleContract> {
  return http.get<SampleContract>('/setup/sample-contract', { redirectOn401: false })
}

export function createFirstAdmin(payload: SetupAdminIn): Promise<AuthState> {
  // 公开接口没有会话也就没有 CSRF token；401 不该被当成会话失效
  return http.post<AuthState>('/setup/admin', payload, { redirectOn401: false })
}

// ---------------------------------------------------------------- auth

export function login(payload: LoginIn): Promise<AuthState> {
  return http.post<AuthState>('/auth/login', payload, { redirectOn401: false })
}

export function register(payload: RegisterIn): Promise<AuthState> {
  return http.post<AuthState>('/auth/register', payload, { redirectOn401: false })
}

/** `/auth/me` 由 session store 在启动时调用；401 表示「未登录」，属正常状态。 */
export function fetchMe(): Promise<AuthState> {
  return http.get<AuthState>('/auth/me', { redirectOn401: false })
}

export function logout(): Promise<void> {
  return http.post<void>('/auth/logout', undefined, { expect: 'none' })
}

export function completeOnboarding(): Promise<UserView> {
  return http.post<UserView>('/auth/onboarding')
}

export function changePassword(payload: PasswordChangeIn): Promise<void> {
  return http.put<void>('/auth/password', payload, { expect: 'none' })
}

// ---------------------------------------------------------------- enterprise

export function fetchEnterprise(): Promise<EnterpriseView | null> {
  return http.get<EnterpriseView | null>('/enterprise')
}

export function saveEnterprise(payload: EnterpriseIn): Promise<EnterpriseView> {
  return http.put<EnterpriseView>('/enterprise', payload)
}

export function deleteEnterprise(): Promise<void> {
  return http.delete<void>('/enterprise', { expect: 'none' })
}

// ---------------------------------------------------------------- contracts

export interface ContractQuery {
  q?: string
  page?: number
  page_size?: number
}

export function fetchContracts(query: ContractQuery = {}): Promise<ContractPage> {
  return http.get<ContractPage>('/contracts', {
    query: { q: query.q ?? '', page: query.page ?? 1, page_size: query.page_size ?? 20 },
  })
}

export function fetchContract(contractId: string): Promise<ContractDetail> {
  return http.get<ContractDetail>(`/contracts/${encodeURIComponent(contractId)}`)
}

export function deleteContract(contractId: string): Promise<void> {
  return http.delete<void>(`/contracts/${encodeURIComponent(contractId)}`, { expect: 'none' })
}

/** 只解析不存档：multipart `file`（txt/docx/文本型 pdf），文件名与正文由服务端规范化。 */
export function parseContractFile(file: File): Promise<ParseResult> {
  const body = new FormData()
  body.append('file', file, file.name)
  // 不手写 Content-Type：浏览器要自己带 multipart 边界
  return http.post<ParseResult>('/contracts/parse', body)
}

// ---------------------------------------------------------------- 核对（异步 job）
//
// 核对一律 202 + 轮询 `/jobs/{id}`：条款审查常需几十秒，长连接会被中间层掐断，
// 而 `queued/running/failed` 必须让用户看得见（006 §2「核对采用异步」）。

export function postCheck(payload: CheckIn): Promise<JobView> {
  return http.post<JobView>('/checks', payload)
}

export function fetchJob(jobId: string): Promise<JobView> {
  return http.get<JobView>(`/jobs/${encodeURIComponent(jobId)}`)
}

export function fetchTask(taskId: string): Promise<TaskDetail> {
  return http.get<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`)
}

export function postAnswers(
  taskId: string,
  payload: { answers: Record<string, string>; skip: boolean },
): Promise<JobView> {
  return http.post<JobView>(`/tasks/${encodeURIComponent(taskId)}/answers`, payload)
}

export function postOptionalFacts(taskId: string, facts: Record<string, string>): Promise<JobView> {
  return http.post<JobView>(`/tasks/${encodeURIComponent(taskId)}/optional-facts`, { facts })
}

export function postQuestion(taskId: string, question: string): Promise<JobView> {
  // 只发问题：上下文由服务端按 `_qa_context` 生成，客户端不得注入合同正文（006 §2）
  return http.post<JobView>(`/tasks/${encodeURIComponent(taskId)}/qa`, { question })
}

export function downloadReport(taskId: string): Promise<Blob> {
  return http.get<Blob>(`/tasks/${encodeURIComponent(taskId)}/report`, { expect: 'blob' })
}

// ---------------------------------------------------------------- 历史

export type HistoryFilter = 'all' | 'risk' | 'degraded'

export function fetchHistory(
  query: { filter?: HistoryFilter; page?: number; page_size?: number } = {},
): Promise<HistoryPage> {
  return http.get<HistoryPage>('/history', {
    query: {
      filter: query.filter ?? 'all',
      page: query.page ?? 1,
      page_size: query.page_size ?? 20,
    },
  })
}

export function fetchTrend(): Promise<TrendView> {
  return http.get<TrendView>('/history/trend')
}

export function deleteHistoryEntry(taskId: string): Promise<void> {
  return http.delete<void>(`/history/${encodeURIComponent(taskId)}`, { expect: 'none' })
}

// ---------------------------------------------------------------- settings

export function fetchUiSettings(): Promise<SettingsUiView> {
  return http.get<SettingsUiView>('/settings/ui')
}

export function saveUiSettings(showQueryPath: boolean): Promise<SettingsUiView> {
  return http.put<SettingsUiView>('/settings/ui', { show_query_path: showQueryPath })
}

export function fetchModelSettings(): Promise<ModelSettingsView> {
  return http.get<ModelSettingsView>('/settings/model')
}

export function saveModelKey(provider: string, key: string): Promise<ModelSettingsView> {
  return http.put<ModelSettingsView>('/settings/model', { provider, key })
}

export function testModelKey(provider: string): Promise<ModelSettingsView> {
  return http.post<ModelSettingsView>(`/settings/model/${encodeURIComponent(provider)}/test`)
}

export function clearModelKey(provider: string): Promise<ModelSettingsView> {
  return http.delete<ModelSettingsView>(`/settings/model/${encodeURIComponent(provider)}`)
}

/** 向量检索 Key（DashScope）：provider 固定，请求体只有 Key；探针通过才落盘。 */
export function saveEmbeddingKey(key: string): Promise<ModelSettingsView> {
  return http.put<ModelSettingsView>('/settings/embedding', { key })
}

export function clearEmbeddingKey(): Promise<ModelSettingsView> {
  return http.delete<ModelSettingsView>('/settings/embedding')
}

// ---------------------------------------------------------------- admin

export function fetchUsers(): Promise<AdminUser[]> {
  return http.get<AdminUser[]>('/admin/users')
}

export function patchUserRole(userId: string, role: string): Promise<AdminUser> {
  return http.patch<AdminUser>(`/admin/users/${encodeURIComponent(userId)}/role`, { role })
}

export function deleteUser(userId: string, confirmUsername: string): Promise<DeletionOut> {
  // 删号要求回填目标用户名：DELETE 带请求体，直接用 request 而不是 delete 简写
  return http.request<DeletionOut>(`/admin/users/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
    body: { confirm_username: confirmUsername },
  })
}

export interface StatuteQuery {
  q?: string
  page?: number
  page_size?: number
}

export function fetchStatutes(query: StatuteQuery = {}): Promise<StatutePage> {
  return http.get<StatutePage>('/admin/statutes', {
    query: { q: query.q ?? '', page: query.page ?? 1, page_size: query.page_size ?? 50 },
  })
}

export function fetchRuleCatalog(): Promise<RuleCatalogView> {
  return http.get<RuleCatalogView>('/admin/rules')
}

export interface LogQuery {
  action?: string
  q?: string
  page?: number
  page_size?: number
}

export function fetchLogs(query: LogQuery = {}): Promise<LogPage> {
  return http.get<LogPage>('/admin/logs', {
    query: {
      action: query.action ?? '全部',
      q: query.q ?? '',
      page: query.page ?? 1,
      page_size: query.page_size ?? 50,
    },
  })
}

export function exportLogs(query: LogQuery = {}): Promise<Blob> {
  return http.get<Blob>('/admin/logs/export', {
    query: { action: query.action ?? '全部', q: query.q ?? '' },
    expect: 'blob',
  })
}

export function fetchEvaluation(): Promise<EvaluationView> {
  return http.get<EvaluationView>('/admin/evaluation')
}
