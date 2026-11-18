/**
 * 核对页的内存会话（等价于旧界面的 `st.session_state`）。
 *
 * 只放「当前这份结果」和输入草稿，不落 localStorage：
 * - 结果详情必须跨路由保留，否则去设置页打开过程开关再回来就看不到同一次核对了；
 * - 但结果的**内容**永远以服务端为准：回到页面时会重新 `GET /tasks/{id}`，
 *   过程开关是全局设置，客户端不得自己拼第三层（006 §2）。
 *
 * 唯一进 sessionStorage 的是「哪些任务的追问已经处理过」（只有任务号，没有结果内容）：
 * 服务端跳过追问后 `questions` 仍在，这条事实只在前端存在；整页刷新要恢复同一次结果，
 * 就必须知道用户已经答过/跳过，否则追问面板会重新挡住已经看过的结果。
 */

import { reactive } from 'vue'

import type { QaResult, TaskDetail } from '@/api/types'

export type InputSource = 'text' | 'file' | 'history'

export interface CheckInputState {
  source: InputSource
  text: string
  contractId: string
  contractName: string
  clauseReview: boolean
  archive: boolean
}

export interface CheckSessionState {
  detail: TaskDetail | null
  qa: QaResult | null
  /**
   * 这份任务的追问是否已经处理过（答完 / 跳过）。
   *
   * 服务端的 `workflow.state` 只要 `task.questions` 非空就是 `awaiting_facts`——跳过追问
   * 只是把首轮结果补存，问题仍在任务里，所以「问过没有」是会话事实，只能由前端记。
   * 旧界面用 `st.session_state["ask_pending"]` 的弹出表达同一件事。
   */
  askDismissed: boolean
  input: CheckInputState
}

export const checkSession = reactive<CheckSessionState>({
  detail: null,
  qa: null,
  askDismissed: false,
  input: {
    source: 'text',
    text: '',
    contractId: '',
    contractName: '',
    clauseReview: true,
    archive: true,
  },
})

/** 追问已处理过的任务号（只存 id，不存结果内容）：整页刷新后仍能认出「这份不用再问」。 */
const ASK_DISMISSED_KEY = 'mla.check.ask-dismissed'

function readDismissedTasks(): Set<string> {
  try {
    const raw = globalThis.sessionStorage?.getItem(ASK_DISMISSED_KEY) ?? ''
    if (raw === '') return new Set<string>()
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set<string>()
    return new Set(parsed.map((item) => String(item)).filter((item) => item !== ''))
  } catch {
    // 隐私模式/存储被禁用：退回「只在内存里记住」，不影响使用
    return new Set<string>()
  }
}

function writeDismissedTasks(taskIds: Set<string>): void {
  try {
    globalThis.sessionStorage?.setItem(ASK_DISMISSED_KEY, JSON.stringify([...taskIds]))
  } catch {
    // 同上：存不下就只保留内存态
  }
}

/** 换一份结果：问答结果跟着清掉，避免上一个任务的问答挂在新结果下面。 */
export function rememberDetail(detail: TaskDetail): void {
  const sameTask = checkSession.detail?.task.task_id === detail.task.task_id
  checkSession.askDismissed =
    (sameTask && checkSession.askDismissed) || readDismissedTasks().has(detail.task.task_id)
  checkSession.detail = detail
  checkSession.qa = null
}

/** 追问已处理（答完或跳过）：之后展示结果，而不是继续挡在追问面板上。 */
export function dismissAsk(): void {
  checkSession.askDismissed = true
  const taskId = checkSession.detail?.task.task_id ?? ''
  if (taskId === '') return
  const dismissed = readDismissedTasks()
  if (dismissed.has(taskId)) return
  dismissed.add(taskId)
  writeDismissedTasks(dismissed)
}

export function rememberQa(result: QaResult): void {
  checkSession.qa = result
}

export function forgetDetail(): void {
  checkSession.detail = null
  checkSession.qa = null
  checkSession.askDismissed = false
}

/** 清空输入草稿（结果保留：用户可能只是想换一份合同再核对）。 */
export function clearInput(): void {
  checkSession.input.text = ''
  checkSession.input.contractId = ''
  checkSession.input.contractName = ''
}
