<script setup lang="ts">
/**
 * 一条依据（法条）的第二层形态：**条号在折叠器外常驻，原文默认折叠**（规格 005 第二层）。
 *
 * 内部 `statute.id` 只作为 key，永远不显示；查不到法源时服务端已给「法源库未找到该条文」，
 * 这里只显示标签、不伪造原文。
 */
import { computed } from 'vue'

import type { StatuteView } from '@/api/types'
import { safeSourceUrl } from '@/utils/format'
import FoldBox from './FoldBox.vue'

const props = defineProps<{ statute: StatuteView; title?: string }>()

const hasText = computed(() => props.statute.found && (props.statute.text ?? '') !== '')
const heading = computed(() => props.title ?? `${props.statute.label} 原文`)
const meta = computed(() => {
  const date = props.statute.effective_date ? `施行日期：${props.statute.effective_date}` : '施行日期：未记录'
  return props.statute.source_url ? `${date} · 来源：${props.statute.source_url}` : date
})
const link = computed(() => safeSourceUrl(props.statute.source_url))
</script>

<template>
  <fold-box v-if="hasText" :title="heading" testid="statute-fold">
    <p class="fold-text">{{ statute.text }}</p>
    <p class="fold-meta">
      {{ meta }}
      <a v-if="link" class="fold-link" :href="link" target="_blank" rel="noopener noreferrer">打开来源</a>
    </p>
  </fold-box>
</template>
