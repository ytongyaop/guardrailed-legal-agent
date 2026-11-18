/**
 * 端点封装（规格 006 §2 端点表）。页面只调用这里的函数，不拼 URL、不手写 header。
 *
 * 批次 4 覆盖：setup / auth / enterprise / contracts / settings / admin。
 * 核对、结果、历史、报告与问答属于批次 5，这里不提前封装。
 */

import { http } from './http'
import type {
  AdminUser,
  AuthState,
  ContractDetail,
  ContractPage,
  DeletionOut,
  EnterpriseIn,
  EnterpriseView,
  EvaluationView,
  LogPage,
  LoginIn,
  ModelSettingsView,
  PasswordChangeIn,
  RegisterIn,
  RuleCatalogView,
  SettingsUiView,
  SetupAdminIn,
  SetupStatus,
  StatutePage,
  UserView,
} from './types'

// ---------------------------------------------------------------- setup（公开）

export function fetchSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return http.get<SetupStatus>('/setup', { signal })
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
