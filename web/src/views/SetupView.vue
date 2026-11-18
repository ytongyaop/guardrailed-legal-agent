<script setup lang="ts">
/**
 * 首次初始化（规格 006 §3 `/setup`）：库里还没有管理员时先建号，建完直接进主界面。
 *
 * 已有管理员时页面只提示去登录，不再显示表单——后端也会用 409 拒绝，界面提前避免误导。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { createFirstAdmin, fetchSetupStatus } from '@/api/endpoints'
import type { SetupStatus } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import { toApiError, useAsyncTask } from '@/composables/asyncTask'
import { applyAuth } from '@/stores/session'

const MIN_PASSWORD_LEN = 8
const PASSWORD_REMINDER =
  '请记住管理员账号与口令。忘记后只能在本机终端重置：python -m mla admin reset'

const router = useRouter()
const { loading, error, run } = useAsyncTask()

const form = reactive({
  username: '',
  password: '',
  confirm_password: '',
  enterprise_name: '',
})
const status = ref<SetupStatus | null>(null)
const submitting = ref(false)
const hint = ref('')

const needsAdmin = computed(() => status.value === null || status.value.needs_admin)
const runtimeLines = computed(() => {
  const runtime = status.value?.runtime
  if (!runtime) return []
  return [...runtime.warnings, ...runtime.messages]
})

onMounted(async () => {
  const loaded = await run(() => fetchSetupStatus())
  if (loaded) status.value = loaded
})

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
  error.value = null
  try {
    applyAuth(await createFirstAdmin({ ...form }))
    await router.push({ name: 'check' })
  } catch (failure) {
    error.value = toApiError(failure)
  } finally {
    submitting.value = false
  }
}
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
          本机还没有管理员账号。先创建管理员，之后的账号在该账号的“用户与角色”里管理。
        </p>
      </div>

      <div class="bare-card">
        <template v-if="loading">
          <el-skeleton :rows="4" animated />
        </template>

        <template v-else-if="needsAdmin">
          <h2>创建管理员</h2>
          <error-notice v-if="error" :error="error" />
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
                placeholder="可留空，稍后在企业档案里补"
                data-testid="setup-enterprise"
              />
            </el-form-item>
            <el-alert v-if="hint" class="error-notice" type="error" :title="hint" :closable="false" />
            <el-button type="primary" :loading="submitting" data-testid="setup-submit" @click="submit">
              创建并进入
            </el-button>
          </el-form>
          <p class="form-hint">{{ PASSWORD_REMINDER }}</p>
        </template>

        <template v-else>
          <h2>已初始化</h2>
          <p class="form-hint">管理员账号已存在，请直接登录。</p>
          <el-button type="primary" @click="router.push({ name: 'login' })">去登录</el-button>
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
