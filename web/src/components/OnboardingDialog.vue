<script setup lang="ts">
/**
 * 首次引导（规格 006 §3「全局首次引导对话框」）。
 *
 * 未确认前每次登录都会弹：`onboarded` 只由后端落库，前端不缓存到本地。
 * 点关闭等同未确认（与旧界面一致），下次仍会提示。
 */
import { computed, ref } from 'vue'

import { ApiError } from '@/api/errors'
import { confirmOnboarding, useSession } from '@/stores/session'

import ErrorNotice from './ErrorNotice.vue'

const ONBOARDING_TITLE = '开始之前'
const ONBOARDING_POINTS: readonly string[] = [
  '用途：核对劳动合同中的用工风险，每条结论都给出法条依据。',
  '边界：只提示与法条存在冲突的地方，不判定违法或无效，不构成法律意见。',
  '数据：核对时合同正文会发送给模型服务用于抽取；检索、追问与问答只发送结构化事实。',
  '范围：只处理劳动用工与劳动合同问题，其他问题会拒答并建议咨询专业人士。',
]

const session = useSession()
const dismissed = ref(false)
const submitting = ref(false)
const error = ref<ApiError | null>(null)

const visible = computed(
  () => session.user !== null && !session.user.onboarded && !dismissed.value,
)

async function handleConfirm(): Promise<void> {
  submitting.value = true
  error.value = null
  try {
    await confirmOnboarding()
  } catch (failure) {
    error.value = failure instanceof ApiError ? failure : null
  } finally {
    submitting.value = false
  }
}

function handleDismiss(): void {
  dismissed.value = true
}
</script>

<template>
  <el-dialog
    :model-value="visible"
    :title="ONBOARDING_TITLE"
    width="560px"
    :close-on-click-modal="false"
    @update:model-value="handleDismiss"
  >
    <ul class="onboarding-points">
      <li v-for="point in ONBOARDING_POINTS" :key="point">{{ point }}</li>
    </ul>
    <error-notice v-if="error" :error="error" />
    <template #footer>
      <el-button type="primary" :loading="submitting" @click="handleConfirm">我知道了</el-button>
    </template>
  </el-dialog>
</template>
