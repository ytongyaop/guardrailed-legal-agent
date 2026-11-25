<script setup lang="ts">
/**
 * 设置（规格 006 §3 `/settings`）：管理员 模型 / 界面 / 账号，普通用户 界面 / 账号。
 *
 * 模型 Key 永远不回显：输入框保存后清空，状态行只说「是否配置 / 上次校验 / 来源」。
 * 清除 Key 要二次确认，默认动作是取消。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import {
  changePassword,
  clearEmbeddingKey,
  clearModelKey,
  fetchModelSettings,
  fetchUiSettings,
  saveEmbeddingKey,
  saveModelKey,
  saveUiSettings,
  testModelKey,
} from '@/api/endpoints'
import type { ModelSettingsView } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { isAdmin, useSession } from '@/stores/session'

const MIN_PASSWORD_LEN = 8
const SHOW_QUERY_PATH_LABEL = '显示查询路径与判定来源'
const SHOW_QUERY_PATH_HELP = '显示耗时、覆盖率、判定来源、补问与运行详情；不影响核对结论。'
const KEY_MISSING_IMPACT =
  '缺 Key 影响：处理建议不生成、生成式问答拒答、条款级审查不可用；确定性规则核对与必备条款缺项检测不依赖模型。'
const KEY_STORAGE_NOTE =
  'DeepSeek Key 以明文保存在当前操作系统用户的凭据文件中，已收紧权限；不要分享该文件。'
const EMBEDDING_IMPACT =
  '这是可选配置：不配也能核对，只是检索退化为关键词匹配；配置后语义相近的法条也能被召回，建议配置。'
const EMBEDDING_STORAGE_NOTE =
  '阿里云百炼 Key 与上面的大模型 Key 存在同一个凭据文件里，同样不会回显。'
const BAILIAN_CONSOLE_URL = 'https://bailian.console.aliyun.com/'

/** 数据与隐私说明（1.0.0 公开分发）：纯展示，不加外链——exe 用户可能离线打开页面。 */
const PRIVACY_LINES: readonly string[] = [
  '合同正文：只在你点击“开始核对”时发送到你配置的模型服务商（如 DeepSeek、阿里云百炼）用于分析。',
  '本地存储：账号、企业、合同与核对记录保存在本机的 %LOCALAPPDATA%\\mla\\；删除该目录即清空。',
  'API Key：只保存在本机的 auth.json，不会上传，界面上也不回显。',
  '使用统计：本工具不收集使用统计，也不上报任何遥测数据。',
]

const session = useSession()
const admin = computed(() => isAdmin())

const uiTask = useAsyncTask()
const modelTask = useAsyncTask()
const embeddingTask = useAsyncTask()
const accountTask = useAsyncTask()

const showQueryPath = ref(false)
const uiFlash = ref('')

const model = ref<ModelSettingsView | null>(null)
const provider = ref('')
const keyInput = ref('')
const keyVisible = ref(false)
const editing = ref(false)
const clearVisible = ref(false)
const modelFlash = ref('')
const keyHint = ref('')

const embeddingInput = ref('')
const embeddingVisible = ref(false)
const embeddingClearVisible = ref(false)
const embeddingFlash = ref('')
const embeddingHint = ref('')

const account = reactive({ current_password: '', new_password: '', confirm_password: '' })
const accountFlash = ref('')
const accountHint = ref('')

const activeProvider = computed(
  () => model.value?.providers.find((item) => item.provider === provider.value) ?? null,
)
const configured = computed(() => activeProvider.value?.configured === true)

async function loadUi(): Promise<void> {
  const settings = await uiTask.run(() => fetchUiSettings())
  if (settings) showQueryPath.value = settings.show_query_path
}

async function loadModel(): Promise<void> {
  if (!admin.value) return
  const settings = await modelTask.run(() => fetchModelSettings())
  if (!settings) return
  model.value = settings
  const first = settings.providers[0]
  provider.value = first ? first.provider : ''
}

async function toggleShowQueryPath(next: boolean | string | number): Promise<void> {
  uiFlash.value = ''
  const wanted = next === true
  const saved = await uiTask.run(() => saveUiSettings(wanted))
  if (!saved) {
    showQueryPath.value = !wanted
    return
  }
  showQueryPath.value = saved.show_query_path
  uiFlash.value = '界面设置已保存。'
}

async function saveKey(): Promise<void> {
  modelFlash.value = ''
  keyHint.value = ''
  if (!keyInput.value.trim()) {
    keyHint.value = '请先粘贴 API Key'
    return
  }
  const updated = await modelTask.run(() => saveModelKey(provider.value, keyInput.value.trim()))
  if (!updated) return
  model.value = updated
  keyInput.value = ''
  keyVisible.value = false
  editing.value = false
  modelFlash.value = '模型 Key 已保存，状态已更新。'
}

async function testKey(): Promise<void> {
  modelFlash.value = ''
  const updated = await modelTask.run(() => testModelKey(provider.value))
  if (!updated) return
  model.value = updated
  modelFlash.value = '连接测试已完成，状态已更新。'
}

async function clearKey(): Promise<void> {
  modelFlash.value = ''
  const updated = await modelTask.run(() => clearModelKey(provider.value))
  if (!updated) return
  model.value = updated
  clearVisible.value = false
  editing.value = false
  keyInput.value = ''
  modelFlash.value = '已清除该 Key。'
}

function cancelEdit(): void {
  keyInput.value = ''
  keyVisible.value = false
  editing.value = false
  keyHint.value = ''
}

async function saveEmbedding(): Promise<void> {
  embeddingFlash.value = ''
  embeddingHint.value = ''
  if (!embeddingInput.value.trim()) {
    embeddingHint.value = '请先粘贴 API Key'
    return
  }
  const updated = await embeddingTask.run(() => saveEmbeddingKey(embeddingInput.value.trim()))
  if (!updated) return
  model.value = updated
  embeddingInput.value = ''
  embeddingVisible.value = false
  embeddingFlash.value = '向量检索 Key 已保存，状态已更新。'
}

async function clearEmbedding(): Promise<void> {
  embeddingFlash.value = ''
  const updated = await embeddingTask.run(() => clearEmbeddingKey())
  if (!updated) return
  model.value = updated
  embeddingClearVisible.value = false
  embeddingInput.value = ''
  embeddingFlash.value = '已清除向量检索 Key，检索退化为关键词匹配。'
}

async function submitPassword(): Promise<void> {
  accountFlash.value = ''
  accountHint.value = ''
  if (account.new_password !== account.confirm_password) {
    accountHint.value = '两次输入的新密码不一致'
    return
  }
  const done = await accountTask.run(async () => {
    await changePassword({ ...account })
    return true
  })
  if (!done) return
  account.current_password = ''
  account.new_password = ''
  account.confirm_password = ''
  accountFlash.value = '密码已修改，下次登录请使用新密码。'
}

onMounted(() => {
  void loadUi()
  void loadModel()
})
</script>

<template>
  <section class="page">
    <page-header title="设置" />

    <el-card v-if="admin" class="page-card" shadow="never">
      <h2 class="page-title">模型设置</h2>
      <el-skeleton v-if="modelTask.loading.value && model === null" :rows="3" animated />
      <template v-else>
        <error-notice v-if="modelTask.error.value" :error="modelTask.error.value" />
        <el-alert
          v-if="modelFlash"
          class="error-notice"
          type="success"
          :title="modelFlash"
          :closable="false"
        />
        <p class="page-caption">{{ model?.status_line ?? '尚未读取到模型状态' }}</p>

        <el-form label-width="120px" class="form-narrow">
          <el-form-item label="服务商">
            <el-select v-model="provider" placeholder="请选择服务商">
              <el-option
                v-for="item in model?.providers ?? []"
                :key="item.provider"
                :label="item.display_name"
                :value="item.provider"
              />
            </el-select>
          </el-form-item>
        </el-form>

        <template v-if="!configured || editing">
          <el-form label-width="120px" class="form-narrow" @submit.prevent="saveKey">
            <el-form-item :label="`${activeProvider?.display_name ?? 'DeepSeek'} API Key`">
              <el-input
                v-model="keyInput"
                :type="keyVisible ? 'text' : 'password'"
                placeholder="粘贴 DeepSeek API Key"
                autocomplete="off"
              />
            </el-form-item>
            <el-form-item>
              <el-checkbox v-model="keyVisible">显示</el-checkbox>
            </el-form-item>
            <el-alert v-if="keyHint" class="error-notice" type="error" :title="keyHint" :closable="false" />
            <div class="page-actions">
              <el-button type="primary" :loading="modelTask.loading.value" @click="saveKey">
                测试并保存
              </el-button>
              <el-button v-if="editing" @click="cancelEdit">取消</el-button>
              <el-button v-if="configured" text type="danger" @click="clearVisible = true">
                清除此 Key
              </el-button>
            </div>
          </el-form>
          <p class="form-hint">{{ KEY_MISSING_IMPACT }}</p>
        </template>

        <template v-else>
          <div class="page-actions">
            <el-button :loading="modelTask.loading.value" @click="editing = true">更改 Key</el-button>
            <el-button :loading="modelTask.loading.value" @click="testKey">测试连接</el-button>
          </div>
        </template>

        <p class="form-hint">{{ KEY_STORAGE_NOTE }}</p>

        <h3 class="section-title">向量检索 Key（可选）</h3>
        <p class="page-caption" data-testid="embedding-status">
          {{ model?.embedding.fallback_text ?? '尚未读取到向量检索状态' }}
        </p>
        <p class="form-hint">{{ EMBEDDING_IMPACT }}</p>
        <p class="form-hint">
          获取地址：
          <a :href="BAILIAN_CONSOLE_URL" target="_blank" rel="noopener">阿里云百炼控制台</a>
        </p>
        <error-notice v-if="embeddingTask.error.value" :error="embeddingTask.error.value" />
        <el-alert
          v-if="embeddingFlash"
          class="error-notice"
          type="success"
          :title="embeddingFlash"
          :closable="false"
        />
        <el-alert
          v-if="embeddingHint"
          class="error-notice"
          type="error"
          :title="embeddingHint"
          :closable="false"
        />
        <el-form label-width="160px" class="form-narrow" @submit.prevent="saveEmbedding">
          <el-form-item label="阿里云百炼 API Key">
            <el-input
              v-model="embeddingInput"
              :type="embeddingVisible ? 'text' : 'password'"
              placeholder="粘贴阿里云百炼 API Key"
              autocomplete="off"
              data-testid="embedding-key"
            />
          </el-form-item>
          <el-form-item>
            <el-checkbox v-model="embeddingVisible">显示</el-checkbox>
          </el-form-item>
          <div class="page-actions">
            <el-button
              type="primary"
              :loading="embeddingTask.loading.value"
              data-testid="save-embedding"
              @click="saveEmbedding"
            >
              测试并保存
            </el-button>
            <el-button
              v-if="model?.embedding.configured"
              text
              type="danger"
              @click="embeddingClearVisible = true"
            >
              清除此 Key
            </el-button>
          </div>
        </el-form>
        <p class="form-hint">{{ EMBEDDING_STORAGE_NOTE }}</p>
      </template>
    </el-card>

    <el-card class="page-card" shadow="never">
      <h2 class="page-title">界面设置</h2>
      <error-notice v-if="uiTask.error.value" :error="uiTask.error.value" />
      <el-alert v-if="uiFlash" class="error-notice" type="success" :title="uiFlash" :closable="false" />
      <el-form label-width="200px">
        <el-form-item :label="SHOW_QUERY_PATH_LABEL">
          <el-switch v-model="showQueryPath" @change="toggleShowQueryPath" />
        </el-form-item>
      </el-form>
      <p class="form-hint">{{ SHOW_QUERY_PATH_HELP }}</p>
    </el-card>

    <el-card class="page-card" shadow="never">
      <h2 class="page-title">账号设置</h2>
      <el-form label-width="120px" class="form-narrow">
        <el-form-item label="用户名">
          <el-input :model-value="session.user?.username ?? ''" disabled />
        </el-form-item>
      </el-form>
      <error-notice v-if="accountTask.error.value" :error="accountTask.error.value" />
      <el-alert
        v-if="accountFlash"
        class="error-notice"
        type="success"
        :title="accountFlash"
        :closable="false"
      />
      <el-alert
        v-if="accountHint"
        class="error-notice"
        type="error"
        :title="accountHint"
        :closable="false"
      />
      <el-form label-width="120px" class="form-narrow" @submit.prevent="submitPassword">
        <el-form-item label="当前密码">
          <el-input
            v-model="account.current_password"
            type="password"
            autocomplete="current-password"
            show-password
          />
        </el-form-item>
        <el-form-item :label="`新密码（至少 ${MIN_PASSWORD_LEN} 位）`">
          <el-input
            v-model="account.new_password"
            type="password"
            autocomplete="new-password"
            show-password
          />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input
            v-model="account.confirm_password"
            type="password"
            autocomplete="new-password"
            show-password
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="accountTask.loading.value" @click="submitPassword">
            修改密码
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="page-card" shadow="never">
      <h2 class="page-title">数据与隐私</h2>
      <p v-for="line in PRIVACY_LINES" :key="line" class="privacy-line">{{ line }}</p>
    </el-card>

    <el-dialog v-model="clearVisible" title="确认清除" width="460px">
      <p>
        确认清除 {{ activeProvider?.display_name ?? 'DeepSeek' }} 的 Key？清除后：处理建议生成、生成式问答、条款级审查不可用；确定性规则核对与必备条款缺项检测不受影响。
      </p>
      <template #footer>
        <el-button type="primary" @click="clearVisible = false">取消清除</el-button>
        <el-button @click="clearKey">确认清除</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="embeddingClearVisible" title="确认清除" width="460px">
      <p>
        确认清除向量检索 Key？清除后检索退化为关键词匹配，核对仍可正常进行；需要语义检索时再填一次即可。
      </p>
      <template #footer>
        <el-button type="primary" @click="embeddingClearVisible = false">取消清除</el-button>
        <el-button @click="clearEmbedding">确认清除</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.privacy-line {
  margin: 0 0 8px;
  font-size: 13px;
  line-height: 1.7;
}

.privacy-line:last-child {
  margin-bottom: 0;
}
</style>
