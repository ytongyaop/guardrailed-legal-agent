<script setup lang="ts">
/**
 * 错误提示：只渲染服务端给的 `{code,message,action}`（规格 006 §2）。
 *
 * - `message` 直接用，前端不编业务文案；
 * - `action` 决定给不给按钮：open_settings 跳设置页，retry 交给页面重跑，none 不给按钮；
 * - 底部小字是 code 与 request_id，便于把界面报错对上服务端日志。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import type { ApiError } from '@/api/errors'

const props = defineProps<{ error: ApiError }>()
const emit = defineEmits<{ retry: [] }>()
const router = useRouter()

const action = computed(() => props.error.action)
const actionable = computed(() => action.value !== null && action.value.type !== 'none')
const hint = computed(() => {
  const type = action.value?.type
  if (type === 'open_settings') return '可以到设置页检查模型配置。'
  if (type === 'contact_admin') return '请联系管理员处理。'
  if (type === 'retry') return '稍后重试即可，本次操作没有生效。'
  return ''
})
const meta = computed(() =>
  [props.error.code, props.error.requestId].filter((item) => item !== '').join(' · '),
)

function handleAction(): void {
  const type = action.value?.type
  if (type === 'open_settings') {
    void router.push({ name: 'settings' })
    return
  }
  if (type === 'retry') emit('retry')
}
</script>

<template>
  <el-alert class="error-notice" type="error" :closable="false" show-icon>
    <template #title>{{ error.message }}</template>
    <div class="error-notice-body">
      <span v-if="hint" class="error-notice-hint">{{ hint }}</span>
      <el-button v-if="actionable" size="small" @click="handleAction">
        {{ action?.label }}
      </el-button>
      <span v-if="meta" class="error-notice-meta">{{ meta }}</span>
    </div>
  </el-alert>
</template>
