<script setup lang="ts">
/**
 * 首次初始化向导（规格 006 §3 `/setup`）：建管理员 → 配大模型 Key → 用示例合同试跑一次。
 *
 * 状态机（`step`）不依赖 `needs_admin`：建号成功后 `GET /setup` 会立刻变成
 * `needs_admin=false`，若拿它决定渲染哪一屏，向导会被「已初始化」直接顶掉。
 * 所以 `needs_admin` 只在**进入页面时**用一次（判断要不要显示向导），之后由 `step` 推进；
 * 建号后也不再跳转，而是留在本页进入第 2 步。
 *
 * 第 2、3 步都可跳过：目标是让人先看到界面，Key 与示例随时能补。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import {
  createFirstAdmin,
  fetchJob,
  fetchModelSettings,
  fetchSampleContract,
  fetchSetupStatus,
  postCheck,
  saveModelKey,
} from '@/api/endpoints'
import type { ApiError } from '@/api/errors'
import type {
  AuthState,
  JobView,
  ModelSettingsView,
  ProviderView,
  SampleContract,
  SetupStatus,
} from '@/api/types'
import { isTaskDetail } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import { apiErrorOf, isSettled } from '@/composables/job'
import { toApiError } from '@/composables/asyncTask'
import { applyAuth, reloadSession } from '@/stores/session'

const STEP_ADMIN = 0
const STEP_MODEL = 1
const STEP_SAMPLE = 2
const STEP_COUNT = 3

const MIN_PASSWORD_LEN = 8
const PASSWORD_REMINDER =
  '请记住管理员账号与口令。忘记后只能在本机终端重置：python -m mla admin reset'
const DEEPSEEK_KEY_URL = 'https://platform.deepseek.com/api_keys'
const SAMPLE_CONTRACT_TITLE = '示例劳动合同'
/** 企业名留空时的默认值：向导要在不填任何附加信息的情况下也能跑通核对。 */
const DEFAULT_ENTERPRISE_NAME = '我的企业'
/** 条款审查常要几十秒：每秒问一次状态，两分钟还没完就不再干等。 */
const CHECK_POLL_MS = 1000
const CHECK_TIMEOUT_MS = 120_000

const router = useRouter()

// ---------------------------------------------------------------- 公共
const step = ref(STEP_ADMIN)
const statusLoading = ref(true)
/** 进入页面时库里已有管理员：只显示「已初始化，请去登录」，不显示向导。 */
const initialized = ref(false)
const status = ref<SetupStatus | null>(null)
const statusError = ref<ApiError | null>(null)

const runtimeLines = computed(() => {
  const runtime = status.value?.runtime
  if (!runtime) return []
  return [...runtime.warnings, ...runtime.messages]
})

onMounted(async () => {
  try {
    const loaded = await fetchSetupStatus()
    status.value = loaded
    initialized.value = !loaded.needs_admin
  } catch (failure) {
    statusError.value = toApiError(failure)
  } finally {
    statusLoading.value = false
  }
})

// ---------------------------------------------------------------- 第 1 步：创建管理员
const form = reactive({
  username: '',
  password: '',
  confirm_password: '',
  enterprise_name: '',
})
const submitting = ref(false)
const adminError = ref<ApiError | null>(null)
const hint = ref('')

function validate(): string {
  if (!form.username.trim()) return '请填写管理员账号'
  if (form.password !== form.confirm_password) return '两次输入的密码不一致'
  if (form.password.length < MIN_PASSWORD_LEN) return `密码至少 ${MIN_PASSWORD_LEN} 位`
  return ''
}

async function submit(): Promise<void> {
  hint.value = validate()
  if (hint.value) return
  submitting.value = true
  adminError.value = null
  try {
    const auth: AuthState = await createFirstAdmin({
      ...form,
      // 留空也要有企业：核对结果按企业归档，无企业账号会被 `POST /checks` 直接 409
      enterprise_name: form.enterprise_name.trim() || DEFAULT_ENTERPRISE_NAME,
    })
    applyAuth(auth)
    // 不跳转：同一页进入第 2 步，成功后 GET /setup 会报 needs_admin=false，跳转就回不来
    form.password = ''
    form.confirm_password = ''
    step.value = STEP_MODEL
  } catch (failure) {
    adminError.value = toApiError(failure)
  } finally {
    submitting.value = false
  }
}

// ---------------------------------------------------------------- 第 2 步：配置大模型 Key
const modelLoading = ref(false)
const modelError = ref<ApiError | null>(null)
const modelWarning = ref('')
const modelFlash = ref('')
const modelStatus = ref('')
const providers = ref<ProviderView[]>([])
const provider = ref('')
const keyInput = ref('')
const keyHint = ref('')

async function loadModel(): Promise<void> {
  modelError.value = null
  try {
    const settings: ModelSettingsView = await fetchModelSettings()
    providers.value = settings.providers
    provider.value = settings.providers[0]?.provider ?? ''
    modelStatus.value = settings.status_line
  } catch (failure) {
    // 读不到状态不影响填 Key：保存时服务端仍会校验
    modelError.value = toApiError(failure)
  }
}

async function saveKey(): Promise<void> {
  modelError.value = null
  modelWarning.value = ''
  modelFlash.value = ''
  keyHint.value = ''
  const key = keyInput.value.trim()
  if (!key) {
    keyHint.value = '请先粘贴 API Key'
    return
  }
  if (!provider.value) {
    keyHint.value = '没有可选的服务商，请到设置页检查配置'
    return
  }
  modelLoading.value = true
  try {
    const updated = await saveModelKey(provider.value, key)
    modelStatus.value = updated.status_line
    keyInput.value = ''
    if (updated.check?.ok === true) {
      modelFlash.value = 'Key 已保存并通过连接测试。'
      // 会话里的「已配置」只在 `/auth/me` 里刷新：不刷新的话核对页会一直显示无模型门禁
      await reloadSession()
      step.value = STEP_SAMPLE
    } else {
      // 以服务端状态行为准，不在前端自己判定「应该能用」
      modelWarning.value = updated.status_line
    }
  } catch (failure) {
    modelError.value = toApiError(failure)
  } finally {
    modelLoading.value = false
  }
}

function skipModel(): void {
  step.value = STEP_SAMPLE
}

// ---------------------------------------------------------------- 第 3 步：试跑示例合同
const sampleLoading = ref(false)
const sample = ref<SampleContract | null>(null)
const sampleError = ref<ApiError | null>(null)
const running = ref(false)
const checkError = ref<ApiError | null>(null)
const noModel = ref(false)
const finishedTaskId = ref('')
const summary = ref<{ risk: number; review: number; awaiting: boolean } | null>(null)

let disposed = false
onBeforeUnmount(() => {
  disposed = true
})

async function loadSample(): Promise<void> {
  sample.value = null
  sampleError.value = null
  sampleLoading.value = true
  try {
    sample.value = await fetchSampleContract()
  } catch (failure) {
    sampleError.value = toApiError(failure)
  } finally {
    sampleLoading.value = false
  }
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms)
  })
}

/** 轮询到 job 结束；离开页面后不再继续发请求。 */
async function waitForSettled(jobId: string): Promise<JobView> {
  const deadline = Date.now() + CHECK_TIMEOUT_MS
  for (;;) {
    const job = await fetchJob(jobId)
    if (isSettled(job) || disposed) return job
    if (Date.now() >= deadline) {
      throw new Error('核对超过两分钟仍未完成，可稍后到历史记录里查看')
    }
    await wait(CHECK_POLL_MS)
  }
}

async function runSample(): Promise<void> {
  if (sample.value === null || running.value) return
  running.value = true
  checkError.value = null
  noModel.value = false
  summary.value = null
  finishedTaskId.value = ''
  try {
    const started = await postCheck({
      text: sample.value.text,
      contract_name: SAMPLE_CONTRACT_TITLE,
      archive: true,
    })
    const job = await waitForSettled(started.job_id)
    if (job.status !== 'succeeded') {
      throw job.error ? apiErrorOf(job.error) : new Error('核对没有完成，请稍后重试')
    }
    if (job.task_id) finishedTaskId.value = job.task_id
    const result = job.result
    if (isTaskDetail(result)) {
      summary.value = {
        risk: result.verdict.risk_count,
        review: result.verdict.review_count,
        awaiting: result.workflow.state === 'awaiting_facts',
      }
    }
  } catch (failure) {
    const error = toApiError(failure)
    checkError.value = error
    // 缺 Key 时服务端回 NO_MODEL：只给一条红条没用，要能一键退回第 2 步
    noModel.value = error.code === 'NO_MODEL'
  } finally {
    running.value = false
  }
}

function backToModel(): void {
  checkError.value = null
  noModel.value = false
  step.value = STEP_MODEL
}

function openResult(): void {
  void router.push({
    name: 'check',
    query: finishedTaskId.value ? { task_id: finishedTaskId.value } : {},
  })
}

function enterSystem(): void {
  void router.push({ name: 'check' })
}

// ---------------------------------------------------------------- 步骤切换时取数据
watch(step, (current) => {
  if (current === STEP_MODEL) {
    modelFlash.value = ''
    void loadModel()
  }
  if (current === STEP_SAMPLE) {
    void loadSample()
  }
})
</script>

<template>
  <div class="bare-page">
    <div class="bare-panel">
      <div>
        <div class="bare-brand">
          <img class="bare-logo" src="/logo.svg" alt="系统标识" />
          <h1 class="bare-title">首次使用</h1>
        </div>
        <p class="bare-subtitle">
          三步走完就能看到一次完整核对：创建管理员、配置大模型 Key、拿示例合同试跑一遍。
        </p>
        <el-steps
          v-if="!statusLoading && !initialized"
          :active="step"
          :total="STEP_COUNT"
          direction="vertical"
          finish-status="success"
          class="setup-steps"
        >
          <el-step title="创建账号" description="本机第一个管理员" />
          <el-step title="配置 Key" description="核对需要大模型" />
          <el-step title="试跑示例" description="看一次完整结果" />
        </el-steps>
      </div>

      <div class="bare-card">
        <template v-if="statusLoading">
          <el-skeleton :rows="4" animated />
        </template>

        <template v-else-if="initialized">
          <h2>已初始化</h2>
          <p class="form-hint">管理员账号已存在，请直接登录。</p>
          <el-button type="primary" @click="router.push({ name: 'login' })">去登录</el-button>
        </template>

        <template v-else>
          <!-- 第 1 步：创建管理员 -->
          <template v-if="step === STEP_ADMIN">
            <h2>创建管理员</h2>
            <error-notice v-if="statusError" :error="statusError" />
            <error-notice v-if="adminError" :error="adminError" />
            <el-form label-position="top" @submit.prevent="submit">
              <el-form-item label="管理员账号">
                <el-input
                  v-model="form.username"
                  placeholder="请输入管理员账号"
                  autocomplete="username"
                  data-testid="setup-username"
                />
              </el-form-item>
              <el-form-item label="登录密码">
                <el-input
                  v-model="form.password"
                  type="password"
                  :placeholder="`至少 ${MIN_PASSWORD_LEN} 位`"
                  autocomplete="new-password"
                  data-testid="setup-password"
                />
              </el-form-item>
              <el-form-item label="确认密码">
                <el-input
                  v-model="form.confirm_password"
                  type="password"
                  placeholder="再输入一次"
                  autocomplete="new-password"
                  data-testid="setup-confirm"
                />
              </el-form-item>
              <el-form-item label="企业名称">
                <el-input
                  v-model="form.enterprise_name"
                  placeholder="可留空，留空将自动创建“我的企业”"
                  data-testid="setup-enterprise"
                />
              </el-form-item>
              <el-alert v-if="hint" class="error-notice" type="error" :title="hint" :closable="false" />
              <el-button
                type="primary"
                :loading="submitting"
                data-testid="setup-submit"
                @click="submit"
              >
                创建并继续
              </el-button>
            </el-form>
            <p class="form-hint">企业名用于归档核对结果，之后可在“企业档案”里改名。</p>
            <p class="form-hint">{{ PASSWORD_REMINDER }}</p>
          </template>

          <!-- 第 2 步：配置大模型 Key -->
          <template v-else-if="step === STEP_MODEL">
            <h2>配置大模型 Key</h2>
            <p class="form-hint">
              核对要靠大模型判定风险，没有 Key 只能浏览界面、出不了结果。Key 只保存在本机，
              保存后输入框清空、不再回显。
            </p>
            <error-notice v-if="modelError" :error="modelError" />
            <el-alert
              v-if="modelFlash"
              class="error-notice"
              type="success"
              :title="modelFlash"
              :closable="false"
            />
            <el-alert
              v-if="modelWarning"
              class="error-notice"
              type="warning"
              :title="modelWarning"
              :closable="false"
            />
            <p v-if="modelStatus" class="form-hint">当前状态：{{ modelStatus }}</p>

            <el-form v-if="providers.length > 1" label-position="top">
              <el-form-item label="服务商">
                <el-select v-model="provider" placeholder="请选择服务商">
                  <el-option
                    v-for="item in providers"
                    :key="item.provider"
                    :label="item.display_name"
                    :value="item.provider"
                  />
                </el-select>
              </el-form-item>
            </el-form>

            <el-form label-position="top" @submit.prevent="saveKey">
              <el-form-item label="大模型 API Key">
                <el-input
                  v-model="keyInput"
                  type="password"
                  placeholder="粘贴 DeepSeek 等大模型的 API Key"
                  autocomplete="off"
                  data-testid="setup-model-key"
                />
              </el-form-item>
              <el-alert
                v-if="keyHint"
                class="error-notice"
                type="error"
                :title="keyHint"
                :closable="false"
              />
              <div class="page-actions">
                <el-button
                  type="primary"
                  :loading="modelLoading"
                  data-testid="setup-model-save"
                  @click="saveKey"
                >
                  测试并保存
                </el-button>
                <el-button data-testid="setup-model-skip" @click="skipModel">
                  稍后配置，先给我看看界面
                </el-button>
              </div>
            </el-form>
            <p class="form-hint">
              获取 Key：
              <a :href="DEEPSEEK_KEY_URL" target="_blank" rel="noopener">platform.deepseek.com</a>
            </p>
            <p class="form-hint">
              向量检索 Key 可选，不配也能核对（退化为关键词检索），之后可在设置页补上。
            </p>
          </template>

          <!-- 第 3 步：试跑示例合同 -->
          <template v-else>
            <h2>试跑一份示例合同</h2>
            <p class="form-hint">这是一份故意埋了问题的示例，用来验证整条链路能跑通。</p>

            <el-skeleton v-if="sampleLoading" :rows="3" animated />
            <template v-else-if="sampleError">
              <error-notice :error="sampleError" />
              <div class="page-actions">
                <el-button type="primary" data-testid="setup-sample-skip" @click="enterSystem">
                  跳过，直接进入系统
                </el-button>
              </div>
            </template>
            <template v-else-if="sample">
              <p class="form-hint">示例：{{ sample.name }}</p>
              <pre class="mono-block sample-preview">{{ sample.text }}</pre>

              <div class="page-actions">
                <el-button
                  v-if="summary === null"
                  type="primary"
                  :loading="running"
                  data-testid="setup-sample-run"
                  @click="runSample"
                >
                  开始核对
                </el-button>
                <el-button
                  v-if="summary === null && !running"
                  data-testid="setup-sample-skip"
                  @click="enterSystem"
                >
                  跳过，直接进入系统
                </el-button>
              </div>

              <p v-if="running" class="form-hint">正在核对，条款审查常需几十秒，请稍候。</p>

              <error-notice v-if="checkError" :error="checkError" @retry="runSample" />
              <div v-if="noModel" class="page-actions">
                <el-button type="primary" data-testid="setup-no-model-back" @click="backToModel">
                  返回步骤 2 配置 Key
                </el-button>
              </div>

              <template v-if="summary">
                <el-alert
                  class="error-notice"
                  type="success"
                  :closable="false"
                  show-icon
                  :title="`核对完成：确定冲突 ${summary.risk} 条，待确认 ${summary.review} 条`"
                />
                <p v-if="summary.awaiting" class="form-hint">
                  还有几个事实需要你确认，完整结果页会逐条询问，回答后结论会更准。
                </p>
                <div class="page-actions">
                  <el-button
                    v-if="finishedTaskId"
                    type="primary"
                    data-testid="setup-open-result"
                    @click="openResult"
                  >
                    查看完整结果
                  </el-button>
                  <el-button data-testid="setup-enter" @click="enterSystem">进入系统</el-button>
                </div>
              </template>
            </template>
          </template>
        </template>

        <div v-if="runtimeLines.length" class="runtime-notice">
          <el-alert
            v-for="line in runtimeLines"
            :key="line"
            type="warning"
            :title="line"
            :closable="false"
            show-icon
          />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setup-steps {
  margin-top: 20px;
  max-width: 280px;
}

.sample-preview {
  max-height: 180px;
  margin-bottom: 12px;
}
</style>
