<script setup lang="ts">
/**
 * 历史详情（规格 006 §3 `/history/{task_id}`）：与核对页同一套三层渲染，**只读**。
 *
 * 与 `/check` 的差别只有两点，都由服务端 DTO 决定：
 * - `workflow.state === 'history'`，服务端不返回可选补充与追问能力，所以这里不渲染它们；
 * - 不提供「就本合同继续提问」（问答需要内存任务上下文）。
 * 过程开关仍是全局设置：历史详情也按 `detail.process` 是否为 null 决定第三层挂不挂。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { downloadReport, fetchTask } from '@/api/endpoints'
import type { ApiError } from '@/api/errors'
import type { TaskDetail } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import ResultView from '@/components/result/ResultView.vue'
import { toApiError } from '@/composables/asyncTask'

const route = useRoute()
const router = useRouter()

const taskId = computed(() => String(route.params['taskId'] ?? ''))
const detail = ref<TaskDetail | null>(null)
const loading = ref(false)
const failure = ref<ApiError | null>(null)
const exporting = ref(false)

async function load(): Promise<void> {
  loading.value = true
  failure.value = null
  try {
    detail.value = await fetchTask(taskId.value)
  } catch (error) {
    detail.value = null
    failure.value = toApiError(error)
  } finally {
    loading.value = false
  }
}

async function exportReport(): Promise<void> {
  const current = detail.value
  if (current === null) return
  exporting.value = true
  failure.value = null
  try {
    const blob = await downloadReport(current.task.task_id)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `核对报告-${current.task.task_id}.docx`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    failure.value = toApiError(error)
  } finally {
    exporting.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="记录详情" />
    <div class="page-actions">
      <el-button text @click="router.push({ name: 'history' })">返回历史记录</el-button>
      <el-button :loading="loading" @click="load">刷新</el-button>
    </div>

    <el-skeleton v-if="loading && detail === null" class="page-card" :rows="6" animated />
    <error-notice v-else-if="failure && failure.status === 404" :error="failure" data-testid="history-missing" />
    <error-notice v-else-if="failure" :error="failure" @retry="load" />
    <result-view
      v-else-if="detail"
      :detail="detail"
      :busy="exporting"
      @download-report="exportReport"
    />
  </section>
</template>
