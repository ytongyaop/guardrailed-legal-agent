<script setup lang="ts">
/**
 * 合同核对（规格 006 §3 `/check`，对应旧界面 `_page_check` / `_check_form` / `_ask_panel`）。
 *
 * 三件事按顺序发生：
 * 1. **门禁**：没有模型就不渲染输入/核对/追问（006 AC-04），只给"去哪配 Key"；
 * 2. **发起**：`POST /checks` 拿 job，轮询 `/jobs/{id}` 并把 `queued/running/failed` 显示出来；
 * 3. **呈现**：结果交给 `ResultView`（三层），追问 / 可选补充 / 问答各用一个面板，
 *    过程开关由服务端决定，页面从不自己拼第三层。
 *
 * 结果详情存在内存会话（`stores/checkSession`）里：去设置页打开过程开关再回来，
 * 页面会重新 `GET /tasks/{id}`，看到的是同一次核对（不重跑），第三层按新开关出现。
 *
 * 内存会话扛不过整页刷新，所以每次拿到结果都把任务号写进 URL（`/check?task_id=T-…`，
 * `router.replace` 不污染历史栈）：刷新后按 URL 里的任务号重新拉详情，渲染的还是同一套组件，
 * 追问 / 可选补充 / 问答都还在；任务号不可读（404/403）就清掉 query 并说明原因。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  deleteContract,
  downloadReport,
  fetchContract,
  fetchContracts,
  fetchTask,
  parseContractFile,
  postAnswers,
  postCheck,
  postOptionalFacts,
  postQuestion,
} from '@/api/endpoints'
import { ApiError } from '@/api/errors'
import { isQaResult, isTaskDetail } from '@/api/types'
import type { ContractDetail, ContractSummary, JobView } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import AskPanel from '@/components/result/AskPanel.vue'
import OptionalFactsPanel from '@/components/result/OptionalFactsPanel.vue'
import QaPanel from '@/components/result/QaPanel.vue'
import ResultView from '@/components/result/ResultView.vue'
import { toApiError } from '@/composables/asyncTask'
import { apiErrorOf, jobStatusText, pollJob } from '@/composables/job'
import {
  checkSession,
  clearInput,
  dismissAsk,
  forgetDetail,
  rememberDetail,
  rememberQa,
} from '@/stores/checkSession'
import { isAdmin, useSession } from '@/stores/session'

const session = useSession()
const route = useRoute()
const router = useRouter()

const input = checkSession.input
const detail = computed(() => checkSession.detail)
const qa = computed(() => checkSession.qa)

const modelReady = computed(() => session.modelConfigured)
const admin = computed(() => isAdmin())
const hasEnterprise = computed(() => (session.user?.enterprise_id ?? '') !== '')

const busy = ref(false)
const job = ref<JobView | null>(null)
const jobFailure = ref<ApiError | null>(null)
const formNotice = ref('')
const parseFailure = ref<ApiError | null>(null)
const parsing = ref(false)

/** 从 URL 恢复：`/check?task_id=T-…` 挂载时拉详情，失败时清 query 并说明原因。 */
const restoring = ref(false)
const restoreTarget = ref('')
const restoreFailure = ref<ApiError | null>(null)
const restoreNotice = ref('')

const contracts = ref<ContractSummary[]>([])
const contractsLoading = ref(false)
const contractsFailure = ref<ApiError | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const parsedName = ref('')

const viewing = ref<ContractDetail | null>(null)
const viewingVisible = ref(false)
const pendingDelete = ref<ContractSummary | null>(null)
const deleteVisible = ref(false)

/** 每次发起核对/问答 +1：晚到的旧 job 结果不会覆盖新结果。 */
let runToken = 0
let controller: AbortController | null = null

const awaiting = computed(
  () =>
    detail.value !== null &&
    detail.value.workflow.state === 'awaiting_facts' &&
    !checkSession.askDismissed,
)
const optionalFields = computed(() => detail.value?.workflow.optional_fields ?? [])
const willCheckChars = computed(() => (input.source === 'text' ? input.text.trim().length : 0))

function abortRunning(): void {
  runToken += 1
  controller?.abort()
  controller = null
  busy.value = false
}

/** URL 里的任务号（不合法的 query 一律当没有）。 */
function queryTaskId(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * 把任务号写进 URL：`router.replace` 不新增历史记录，浏览器后退仍是「上一步」而不是上一份结果。
 * 传 null 表示清掉这一项（其余 query 原样保留）。
 */
async function syncTaskId(taskId: string | null): Promise<void> {
  const next = taskId ?? ''
  if (queryTaskId(route.query['task_id']) === next) return
  await router.replace({ query: { ...route.query, task_id: next === '' ? undefined : next } })
}

/** 按 URL 拉一份详情：成功后会话、URL 都是同一个任务号；读不到就清 query 并说明。 */
async function loadFromUrl(taskId: string): Promise<void> {
  restoreTarget.value = taskId
  restoring.value = true
  restoreFailure.value = null
  restoreNotice.value = ''
  try {
    const loaded = await fetchTask(taskId)
    rememberDetail(loaded)
    await syncTaskId(loaded.task.task_id)
  } catch (error) {
    const failure = toApiError(error)
    // 越权与不存在服务端都回 404（不区分，避免探测他人任务）；会话失效另有 403
    if (failure.status === 404 || failure.status === 403) {
      forgetDetail()
      await syncTaskId(null)
      restoreNotice.value = '该结果不可访问或已删除'
      return
    }
    restoreFailure.value = failure
  } finally {
    restoring.value = false
  }
}

function retryRestore(): void {
  if (restoreTarget.value === '') return
  void loadFromUrl(restoreTarget.value)
}

/** 发起一次异步操作并轮询到结束；返回结束时的 job（被新操作取代时返回 null）。 */
async function runJob(start: () => Promise<JobView>): Promise<JobView | null> {
  abortRunning()
  const token = runToken
  const local = new AbortController()
  controller = local
  busy.value = true
  jobFailure.value = null
  job.value = null
  try {
    const queued = await start()
    if (token !== runToken) return null
    job.value = queued
    const settled = await pollJob(queued.job_id, {
      signal: local.signal,
      onUpdate: (next) => {
        if (token === runToken) job.value = next
      },
    })
    if (token !== runToken) return null
    job.value = settled
    if (settled.status === 'failed') {
      jobFailure.value = settled.error ? apiErrorOf(settled.error) : ApiError.fromStatus(500)
      return settled
    }
    if (isTaskDetail(settled.result)) {
      rememberDetail(settled.result)
      // 追问/补充事实重跑会换新任务号：URL 跟着换，刷新后还是这份新结果
      await syncTaskId(settled.result.task.task_id)
    } else if (isQaResult(settled.result)) {
      rememberQa(settled.result)
      // 问答挂在本份核对下面：URL 保持父任务号
      await syncTaskId(settled.result.parent_task_id)
    }
    return settled
  } catch (error) {
    if (token === runToken) jobFailure.value = toApiError(error)
    return null
  } finally {
    if (token === runToken) {
      busy.value = false
      if (controller === local) controller = null
    }
  }
}

async function loadContracts(): Promise<void> {
  contractsLoading.value = true
  contractsFailure.value = null
  try {
    const page = await fetchContracts({ page: 1, page_size: 100 })
    contracts.value = page.items
  } catch (error) {
    contractsFailure.value = toApiError(error)
  } finally {
    contractsLoading.value = false
  }
}

async function onFilePicked(event: Event): Promise<void> {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  target.value = '' // 允许再次选择同一个文件
  formNotice.value = ''
  if (!file) return
  parseFailure.value = null
  parsing.value = true
  try {
    const parsed = await parseContractFile(file)
    input.text = parsed.text
    parsedName.value = parsed.filename
    formNotice.value = `已解析 ${parsed.filename}（${parsed.char_count} 字），确认内容后开始核对。`
  } catch (error) {
    parseFailure.value = toApiError(error)
  } finally {
    parsing.value = false
  }
}

async function startCheck(): Promise<void> {
  formNotice.value = ''
  jobFailure.value = null
  restoreNotice.value = ''
  if (input.source === 'history') {
    if (input.contractId === '') {
      formNotice.value = '请先选择一份历史合同。'
      return
    }
    await runJob(() =>
      postCheck({
        contract_id: input.contractId,
        clause_review: input.clauseReview,
        archive: input.archive,
      }),
    )
    return
  }
  const text = input.text.trim()
  if (text === '') {
    formNotice.value = '请先粘贴合同文本或上传文件。'
    return
  }
  const name = input.source === 'file' && parsedName.value !== '' ? parsedName.value : '粘贴合同'
  await runJob(() =>
    postCheck({ text, contract_name: name, clause_review: input.clauseReview, archive: input.archive }),
  )
}

function submitAnswers(answers: Record<string, string>): void {
  const current = detail.value
  if (current === null) return
  void (async () => {
    const finished = await runJob(() => postAnswers(current.task.task_id, { answers, skip: false }))
    // 一轮问全：答完就不再挡在追问面板上，结果直接展示
    if (finished !== null && finished.status === 'succeeded') dismissAsk()
  })()
}

function skipAnswers(): void {
  const current = detail.value
  if (current === null) return
  void (async () => {
    const finished = await runJob(() => postAnswers(current.task.task_id, { answers: {}, skip: true }))
    // 跳过只补存首轮结果，服务端任务里的 questions 仍在：由会话记住「已经问过了」
    if (finished !== null && finished.status === 'succeeded') dismissAsk()
  })()
}

function submitOptionalFacts(facts: Record<string, string>): void {
  const current = detail.value
  if (current === null) return
  void runJob(() => postOptionalFacts(current.task.task_id, facts))
}

function askQuestion(question: string): void {
  const current = detail.value
  if (current === null) return
  void runJob(() => postQuestion(current.task.task_id, question))
}

async function exportReport(): Promise<void> {
  const current = detail.value
  if (current === null) return
  jobFailure.value = null
  try {
    const blob = await downloadReport(current.task.task_id)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `核对报告-${current.task.task_id}.docx`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    jobFailure.value = toApiError(error)
  }
}

async function openContract(row: ContractSummary | null): Promise<void> {
  if (row === null) return
  try {
    viewing.value = await fetchContract(row.contract_id)
    viewingVisible.value = true
  } catch (error) {
    contractsFailure.value = toApiError(error)
  }
}

function selectedContract(): ContractSummary | null {
  return contracts.value.find((row) => row.contract_id === input.contractId) ?? null
}

function askDeleteSelected(): void {
  const row = selectedContract()
  if (row === null) return
  pendingDelete.value = row
  deleteVisible.value = true
}

function onSourceChange(): void {
  if (input.source === 'history') void loadContracts()
}

async function confirmDeleteContract(): Promise<void> {
  const target = pendingDelete.value
  if (target === null) return
  try {
    await deleteContract(target.contract_id)
    deleteVisible.value = false
    pendingDelete.value = null
    viewingVisible.value = false
    if (input.contractId === target.contract_id) clearInput()
    await loadContracts()
  } catch (error) {
    contractsFailure.value = toApiError(error)
  }
}

/** 回到页面时用服务端最新详情刷新：过程开关是全局设置，只有服务端知道当前值。 */
async function refreshDetail(): Promise<void> {
  const current = detail.value
  if (current === null) return
  try {
    const latest = await fetchTask(current.task.task_id)
    rememberDetail(latest)
    // 从侧栏回来时 URL 已被清成 `/check`：把任务号补回去，刷新才有东西可恢复
    await syncTaskId(latest.task.task_id)
  } catch (error) {
    const failure = toApiError(error)
    if (failure.status === 404 || failure.status === 403) {
      forgetDetail()
      await syncTaskId(null)
      formNotice.value = '上一次的核对结果已失效（服务进程重启或该任务未存档），请重新提交合同。'
      return
    }
    jobFailure.value = failure
  }
}

onMounted(async () => {
  if (input.source === 'history') void loadContracts()
  const urlTaskId = queryTaskId(route.query['task_id'])
  if (urlTaskId !== '') {
    // 刷新恢复：URL 里有任务号就以它为准（同一份结果只刷新，换一份就重新拉）
    if (detail.value?.task.task_id === urlTaskId) await refreshDetail()
    else await loadFromUrl(urlTaskId)
    return
  }
  await refreshDetail()
})

onBeforeUnmount(() => {
  abortRunning()
})
</script>

<template>
  <section class="page check-page">
    <page-header title="合同核对" />

    <!-- URL 里的结果读不到（404/403）：已经清掉任务号，这里说明原因 -->
    <el-alert
      v-if="restoreNotice"
      class="page-card"
      type="warning"
      :closable="false"
      show-icon
      :title="restoreNotice"
      data-testid="restore-notice"
    />

    <!-- 门禁 1：没有模型就什么都不渲染（不给残缺结论，也不给点了会失败的按钮） -->
    <el-card v-if="!modelReady" class="page-card" shadow="never" data-testid="no-model-gate">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="未配置模型，合同核对不可用；配置后才能给出结论。"
      />
      <p class="page-caption">
        缺 Key 时：处理建议不生成、生成式问答拒答、条款级审查不可用；确定性规则核对与必备条款缺项检测不依赖模型。
      </p>
      <el-button v-if="admin" type="primary" @click="router.push({ name: 'settings' })">
        去设置页配置 API Key
      </el-button>
      <p v-else class="page-caption">当前账号不是管理员，请联系管理员在设置页配置模型。</p>
    </el-card>

    <!-- 门禁 2：没有企业档案就不发起核对（结果按企业归档） -->
    <el-card v-else-if="!hasEnterprise" class="page-card" shadow="never" data-testid="no-enterprise-gate">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="当前账号未绑定企业。核对结果按企业归档，请先完善企业档案。"
      />
      <el-button type="primary" @click="router.push({ name: 'enterprise' })">去完善企业档案</el-button>
    </el-card>

    <div v-else class="check-layout">
      <div class="check-side">
        <ask-panel
          v-if="awaiting && detail"
          :detail="detail"
          :busy="busy"
          @submit="submitAnswers"
          @skip="skipAnswers"
        />
        <el-card v-else class="page-card" shadow="never" data-testid="check-form">
          <h3 class="section-title">合同输入</h3>
          <el-radio-group v-model="input.source" class="source-switch" @change="onSourceChange">
            <el-radio-button value="text">粘贴文本</el-radio-button>
            <el-radio-button value="file">上传文件</el-radio-button>
            <el-radio-button value="history">从历史合同选择</el-radio-button>
          </el-radio-group>

          <template v-if="input.source === 'text'">
            <el-input
              v-model="input.text"
              type="textarea"
              :rows="9"
              placeholder="把劳动合同正文粘贴到这里……"
              data-testid="contract-text"
            />
            <p v-if="willCheckChars" class="page-caption">将核对 {{ willCheckChars }} 字</p>
          </template>

          <template v-else-if="input.source === 'file'">
            <input
              ref="fileInput"
              class="hidden-file"
              type="file"
              accept=".txt,.docx,.pdf"
              data-testid="contract-file"
              @change="onFilePicked"
            />
            <div class="page-actions">
              <el-button :loading="parsing" @click="fileInput?.click()">选择文件（.txt / .docx / 文本型 .pdf）</el-button>
            </div>
            <error-notice v-if="parseFailure" :error="parseFailure" />
            <p v-if="formNotice" class="page-caption">{{ formNotice }}</p>
            <el-input
              v-if="input.text"
              v-model="input.text"
              type="textarea"
              :rows="9"
              data-testid="parsed-text"
            />
          </template>

          <template v-else>
            <error-notice v-if="contractsFailure" :error="contractsFailure" />
            <el-select
              v-model="input.contractId"
              :loading="contractsLoading"
              placeholder="选择一份已保存的合同"
              filterable
              style="width: 100%"
              data-testid="contract-select"
            >
              <el-option
                v-for="row in contracts"
                :key="row.contract_id"
                :value="row.contract_id"
                :label="`${row.filename}（${row.char_count} 字）`"
              />
            </el-select>
            <p v-if="!contractsLoading && contracts.length === 0" class="page-caption">还没有保存过合同。</p>
            <div v-if="input.contractId" class="page-actions">
              <el-button text type="primary" @click="openContract(selectedContract())">查看原文</el-button>
              <el-button text type="danger" @click="askDeleteSelected">删除这份合同</el-button>
            </div>
          </template>

          <div class="check-options">
            <el-checkbox v-model="input.archive" data-testid="archive-checkbox">存入企业档案</el-checkbox>
            <p class="form-hint">勾选后同企业同事可在历史记录里看到这份结果；不勾选则只有你自己能打开。</p>
            <el-checkbox v-model="input.clauseReview" data-testid="clause-review-checkbox">
              启用条款审查（模型辅助）
            </el-checkbox>
            <p class="form-hint">逐条审查可疑条款并按 A/B/C 分级，约需 20–30 秒并消耗模型额度。</p>
          </div>

          <p class="page-caption">核对时会把合同正文发送至模型服务用于抽取关键条款；合同可能含姓名等信息，请确认已获授权。</p>
          <p v-if="formNotice" class="page-caption" data-testid="form-notice">{{ formNotice }}</p>
          <div class="page-actions">
            <el-button type="primary" :loading="busy" data-testid="start-check" @click="startCheck">
              开始核对
            </el-button>
          </div>
        </el-card>

        <optional-facts-panel
          v-if="detail && !awaiting && optionalFields.length > 0"
          :fields="optionalFields"
          :busy="busy"
          @submit="submitOptionalFacts"
          @empty="formNotice = '未填写任何内容，本次未重新核对。'"
        />
      </div>

      <div class="check-main">
        <el-card class="page-card result-card" shadow="never">
          <div v-if="restoring && detail === null" class="job-progress" data-testid="restore-loading">
            <p class="job-status">正在读取这次核对的结果……</p>
            <el-skeleton :rows="5" animated />
          </div>
          <error-notice
            v-else-if="restoreFailure"
            :error="restoreFailure"
            data-testid="restore-error"
            @retry="retryRestore"
          />
          <div v-else-if="busy" class="job-progress" data-testid="job-progress">
            <p class="job-status">{{ jobStatusText(job) }}</p>
            <el-skeleton :rows="5" animated />
          </div>
          <error-notice v-else-if="jobFailure" :error="jobFailure" data-testid="job-error" @retry="startCheck" />
          <result-view
            v-else-if="detail && !awaiting"
            :detail="detail"
            :busy="busy"
            @download-report="exportReport"
          >
            <template #qa>
              <qa-panel :result="qa" :busy="busy" @ask="askQuestion" />
            </template>
          </result-view>
          <p v-else-if="awaiting" class="page-caption">先在左侧补齐追问事项，答完自动重新核对，结果显示在这里。</p>
          <p v-else class="page-caption" data-testid="result-placeholder">
            左侧粘贴或选择一份合同，点击开始核对，结果显示在这里。
          </p>
        </el-card>
      </div>
    </div>

    <el-dialog v-model="viewingVisible" :title="viewing?.filename ?? '合同原文'" width="760px">
      <pre class="mono-block contract-text">{{ viewing?.text ?? '' }}</pre>
      <template #footer>
        <el-button @click="viewingVisible = false">收起原文</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="deleteVisible" title="确认删除" width="420px">
      <p>确认删除“{{ pendingDelete?.filename ?? '该合同' }}”？删除后不可在“从历史合同选择”里找到。</p>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" @click="confirmDeleteContract">确认删除</el-button>
      </template>
    </el-dialog>
  </section>
</template>
