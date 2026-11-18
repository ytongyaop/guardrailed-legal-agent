<script setup lang="ts">
/**
 * 「就本合同继续提问」（006 §2 `POST /tasks/{id}/qa`）。
 *
 * - 只发问题：上下文由服务端按 `_qa_context` 生成，客户端不拼合同正文；
 * - 拒答 / 空结果都是成功结果，按服务端给的 `refusal` / `empty_text` 原样渲染；
 * - 问答的运行详情与结果页共用同一个过程开关：`result.process === null` 时整段不渲染。
 */
import { computed, ref } from 'vue'

import type { QaResult } from '@/api/types'
import StatuteFold from './StatuteFold.vue'

const props = defineProps<{ result: QaResult | null; busy: boolean }>()
const emit = defineEmits<{ ask: [question: string] }>()

const question = ref('')

const canAsk = computed(() => question.value.trim() !== '' && !props.busy)

function ask(): void {
  if (!canAsk.value) return
  emit('ask', question.value.trim())
}
</script>

<template>
  <el-card class="page-card" shadow="never" data-testid="qa-panel">
    <h3 class="section-title">就本合同继续提问</h3>
    <p class="page-caption">回答基于本次核对的结构化事实与命中法条，不发送合同正文。</p>
    <el-input
      v-model="question"
      type="textarea"
      :rows="3"
      placeholder="例如：这份合同的试用期约定有什么风险？"
    />
    <div class="page-actions">
      <el-button type="primary" :loading="busy" :disabled="!canAsk" @click="ask">提问</el-button>
    </div>

    <template v-if="result">
      <el-alert
        v-if="result.refusal"
        class="page-card"
        type="error"
        :closable="false"
        show-icon
        :title="result.refusal.message"
        data-testid="qa-refusal"
      >
        <p>{{ result.refusal.guidance }}</p>
      </el-alert>
      <el-alert
        v-else-if="result.empty_text"
        class="page-card"
        type="warning"
        :closable="false"
        show-icon
        :title="result.empty_text"
        data-testid="qa-empty"
      />
      <template v-else>
        <div v-if="result.sources.length" class="qa-sources" data-testid="qa-sources">
          <h4 class="impact-title">结论与依据（法条原文）</h4>
          <statute-fold
            v-for="statute in result.sources"
            :key="statute.id"
            :statute="statute"
            :title="statute.label"
          />
        </div>
        <div v-if="result.suggestions.length" data-testid="qa-suggestions">
          <h4 class="impact-title">处理建议（非法律意见）</h4>
          <ul class="suggestion-list">
            <li v-for="(row, index) in result.suggestions" :key="index" class="suggestion">
              <p class="suggestion-text">{{ row.text }}</p>
              <p class="page-caption">
                {{ row.citation_text }}<template v-if="row.display_disclaimer"> · {{ row.display_disclaimer }}</template>
              </p>
            </li>
          </ul>
        </div>
        <p v-if="result.errors.length" class="page-caption">{{ result.errors.join(' · ') }}</p>
        <p v-if="result.process" class="page-caption" data-testid="qa-process">{{ result.process.detail_text }}</p>
      </template>
    </template>
  </el-card>
</template>
