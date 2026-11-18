<script setup lang="ts">
/**
 * 追问面板（规格 005 第一层里的交互，006 §2 `POST /tasks/{id}/answers`）。
 *
 * 一轮问全：每项按 `ask_items[].slot_id` 回填，避免"按问句原文反查"把答案写错字段；
 * 答不出可以跳过——跳过会把首轮结果补存并留下降级痕迹，不阻断看结论。
 */
import { computed, reactive, watch } from 'vue'

import type { TaskDetail } from '@/api/types'

const props = defineProps<{ detail: TaskDetail; busy: boolean }>()
const emit = defineEmits<{ submit: [answers: Record<string, string>]; skip: [] }>()

interface AskRow {
  key: string
  question: string
  hint: string
}

const rows = computed<AskRow[]>(() => {
  const workflow = props.detail.workflow
  if (workflow.ask_items.length > 0) {
    return workflow.ask_items.map((item, index) => ({
      key: item.slot_id || `未识别${index}`,
      question: item.question || workflow.questions[index] || '',
      hint: item.hint ?? '请填写',
    }))
  }
  return workflow.questions.map((question, index) => ({
    key: `未识别${index}`,
    question,
    hint: '请填写',
  }))
})

const answers = reactive<Record<string, string>>({})

watch(
  rows,
  (next) => {
    for (const key of Object.keys(answers)) delete answers[key]
    for (const row of next) answers[row.key] = ''
  },
  { immediate: true },
)

function submit(): void {
  const payload: Record<string, string> = {}
  for (const [key, value] of Object.entries(answers)) {
    const text = value.trim()
    // 没识别到字段的答案不带回服务端：服务端会按"未识别"前缀丢弃，这里提前挡掉
    if (text !== '' && !key.startsWith('未识别')) payload[key] = text
  }
  emit('submit', payload)
}
</script>

<template>
  <el-card class="page-card" shadow="never" data-testid="ask-panel">
    <h3 class="section-title">补充 {{ rows.length }} 项事实</h3>
    <p class="page-caption">答完自动重新核对；答不出可以跳过，跳过会在结果里标注降级。</p>
    <el-form label-position="top" @submit.prevent>
      <el-form-item v-for="row in rows" :key="row.key" :label="row.question">
        <el-input v-model="answers[row.key]" :placeholder="row.hint" />
      </el-form-item>
    </el-form>
    <div class="page-actions">
      <el-button type="primary" :loading="busy" @click="submit">提交并重新核对</el-button>
      <el-button :disabled="busy" @click="emit('skip')">跳过，直接看结论</el-button>
    </div>
  </el-card>
</template>
