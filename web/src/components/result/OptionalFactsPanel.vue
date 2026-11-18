<script setup lang="ts">
/**
 * 可选补充（规格 006 §2 `POST /tasks/{id}/optional-facts`）。
 *
 * 只在 `workflow.optional_fields` 非空时出现——那意味着服务端确实有"补了就能判"的字段。
 * 这是**补充**而不是阻断追问：不填也能看到结论。
 */
import { computed, reactive, watch } from 'vue'

import type { OptionalFieldView } from '@/api/types'

const props = defineProps<{ fields: OptionalFieldView[]; busy: boolean }>()
const emit = defineEmits<{ submit: [facts: Record<string, string>]; empty: [] }>()

const values = reactive<Record<string, string>>({})

watch(
  () => props.fields,
  (next) => {
    for (const key of Object.keys(values)) delete values[key]
    for (const field of next) values[field.field_id] = ''
  },
  { immediate: true },
)

const filled = computed(() => Object.values(values).some((value) => value.trim() !== ''))

function submit(): void {
  if (!filled.value) {
    emit('empty')
    return
  }
  const facts: Record<string, string> = {}
  for (const [key, value] of Object.entries(values)) {
    const text = value.trim()
    if (text !== '') facts[key] = text
  }
  emit('submit', facts)
}
</script>

<template>
  <el-card class="page-card" shadow="never" data-testid="optional-facts">
    <h3 class="section-title">可选补充</h3>
    <p class="page-caption">补上这些事实能减少"因缺少事实无法判定"，不填也能看结论。</p>
    <el-form label-position="top" @submit.prevent>
      <el-form-item v-for="field in fields" :key="field.field_id" :label="field.label">
        <el-input v-model="values[field.field_id]" :placeholder="field.hint ?? '请填写'" />
        <span v-if="field.question" class="form-hint">{{ field.question }}</span>
      </el-form-item>
    </el-form>
    <div class="page-actions">
      <el-button type="primary" :loading="busy" @click="submit">补充并重新核对</el-button>
    </div>
  </el-card>
</template>
