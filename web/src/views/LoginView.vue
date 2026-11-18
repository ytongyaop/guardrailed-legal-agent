<script setup lang="ts">
/**
 * 登录 / 注册（规格 006 §3 `/login`）+ 忘记密码说明。
 *
 * 登录页只留品牌、页签与表单；用途、边界、数据去向都在首次登录的引导弹窗里说一次。
 * 忘记密码只给可复制的命令：本机应用没有邮箱找回，这是有意的设计。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchSetupStatus } from '@/api/endpoints'
import type { ApiError } from '@/api/errors'
import ErrorNotice from '@/components/ErrorNotice.vue'
import { toApiError } from '@/composables/asyncTask'
import { signIn, signUp, useSession } from '@/stores/session'

const MIN_PASSWORD_LEN = 8
const FORGOT_PASSWORD_HEAD = '本地应用无法通过邮箱找回。请在装有本程序的机器上打开终端执行：'
const FORGOT_PASSWORD_TAIL = '会生成一个新口令并显示一次。若所有管理员账号都忘记了，执行：'
const FORGOT_PASSWORD_RESET_CMD = 'python -m mla admin reset --username 你的账号'
const FORGOT_PASSWORD_LIST_CMD = 'python -m mla admin list'
const FORGOT_PASSWORD_PROMOTE_CMD = 'python -m mla admin promote --username 你的账号'

const route = useRoute()
const router = useRouter()
const session = useSession()

const activeTab = ref('login')
const showForgot = ref(false)
const needsAdmin = ref(false)
const submitting = ref(false)
const loginError = ref<ApiError | null>(null)
const registerError = ref<ApiError | null>(null)

const loginForm = reactive({ username: '', password: '' })
const registerForm = reactive({ username: '', password: '', enterprise_name: '' })

const bootstrapError = computed(() => session.error)

onMounted(async () => {
  try {
    const status = await fetchSetupStatus()
    needsAdmin.value = status.needs_admin
  } catch {
    // 初始化状态拿不到不影响登录：真的要初始化时登录会返回 401，引导页仍可从地址栏进入
    needsAdmin.value = false
  }
})

function redirectTarget(): string {
  const raw = route.query.redirect
  return typeof raw === 'string' && raw.startsWith('/') ? raw : ''
}

async function submitLogin(): Promise<void> {
  loginError.value = null
  submitting.value = true
  try {
    await signIn({ username: loginForm.username, password: loginForm.password })
    await router.push(redirectTarget() || { name: 'check' })
  } catch (failure) {
    loginError.value = toApiError(failure)
  } finally {
    submitting.value = false
  }
}

async function submitRegister(): Promise<void> {
  registerError.value = null
  if (registerForm.password.length < MIN_PASSWORD_LEN) {
    registerError.value = toApiError(new Error(`密码至少 ${MIN_PASSWORD_LEN} 位`))
    return
  }
  submitting.value = true
  try {
    await signUp({ ...registerForm })
    await router.push({ name: 'check' })
  } catch (failure) {
    registerError.value = toApiError(failure)
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
          <h1 class="bare-title">小微企业用工风险辅助系统</h1>
        </div>
        <p class="bare-subtitle">
          核对劳动合同中的用工风险：只提示与法条存在冲突的地方，不判定违法或无效。
        </p>
        <p v-if="needsAdmin" class="form-hint">本机还没有管理员账号，请先完成首次初始化。</p>
        <el-button v-if="needsAdmin" text @click="router.push({ name: 'setup' })">
          去初始化
        </el-button>
      </div>

      <div class="bare-card">
        <error-notice v-if="bootstrapError" :error="bootstrapError" />
        <el-tabs v-model="activeTab">
          <el-tab-pane label="登录" name="login">
            <el-form label-position="top" @submit.prevent="submitLogin">
              <el-form-item label="用户名">
                <el-input
                  v-model="loginForm.username"
                  placeholder="请输入用户名"
                  autocomplete="username"
                />
              </el-form-item>
              <el-form-item label="密码">
                <el-input
                  v-model="loginForm.password"
                  type="password"
                  placeholder="请输入密码"
                  autocomplete="current-password"
                  show-password
                />
              </el-form-item>
              <error-notice v-if="loginError" :error="loginError" />
              <el-button type="primary" :loading="submitting" @click="submitLogin">
                登录
              </el-button>
            </el-form>
            <el-button text @click="showForgot = !showForgot">忘记密码</el-button>
            <div v-if="showForgot">
              <p class="form-hint">{{ FORGOT_PASSWORD_HEAD }}</p>
              <pre class="mono-block">{{ FORGOT_PASSWORD_RESET_CMD }}</pre>
              <p class="form-hint">{{ FORGOT_PASSWORD_TAIL }}</p>
              <pre class="mono-block">{{ FORGOT_PASSWORD_LIST_CMD }}
{{ FORGOT_PASSWORD_PROMOTE_CMD }}</pre>
            </div>
          </el-tab-pane>

          <el-tab-pane label="注册" name="register">
            <el-form label-position="top" @submit.prevent="submitRegister">
              <el-form-item label="用户名（至少 3 个字符）">
                <el-input v-model="registerForm.username" autocomplete="username" />
              </el-form-item>
              <el-form-item :label="`密码（至少 ${MIN_PASSWORD_LEN} 位）`">
                <el-input
                  v-model="registerForm.password"
                  type="password"
                  autocomplete="new-password"
                  show-password
                />
              </el-form-item>
              <el-form-item label="企业名称（可选）">
                <el-input v-model="registerForm.enterprise_name" />
              </el-form-item>
              <error-notice v-if="registerError" :error="registerError" />
              <el-button :loading="submitting" @click="submitRegister">注册</el-button>
            </el-form>
          </el-tab-pane>
        </el-tabs>
      </div>
    </div>
  </div>
</template>
