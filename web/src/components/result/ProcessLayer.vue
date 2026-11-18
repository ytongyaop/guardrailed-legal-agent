<script setup lang="ts">
/**
 * 第三层「查询路径与判定来源」（规格 005）。
 *
 * **这个组件只在 `detail.process !== null` 时被挂载**（见 `ResultView` 的 `v-if`）：
 * 过程开关关闭时服务端根本不返回这些字段，前端也就不渲染——不是渲染完再用 CSS 藏起来。
 * 第一二层的任何内容都不在这里重画，避免同一句话出现两次。
 */
import type { ProcessView } from '@/api/types'

defineProps<{ process: ProcessView }>()
</script>

<template>
  <el-card class="page-card" shadow="never" data-testid="process-layer">
    <h3 class="section-title">查询路径与判定来源</h3>

    <div class="process-metrics">
      <div class="process-metric">
        <span class="process-metric-value">{{ process.cited_statute_count }}</span>
        <span class="process-metric-label">引用条文</span>
      </div>
      <div class="process-metric">
        <span class="process-metric-value">{{ process.latency_text }}</span>
        <span class="process-metric-label">耗时</span>
      </div>
    </div>

    <p v-if="process.details.length" class="page-caption" data-testid="process-details">
      {{ process.details.join(' · ') }}
    </p>
    <p v-if="process.clause_review_note" class="page-caption">{{ process.clause_review_note }}</p>

    <div class="process-badges">
      <el-tag :type="process.verify_ok ? 'success' : 'danger'" size="small" effect="plain">
        {{ process.traceability_text }}
      </el-tag>
      <el-tag :type="process.ask_rounds ? 'primary' : 'info'" size="small" effect="plain">
        {{ process.ask_text }}
      </el-tag>
      <el-tag v-if="process.degraded_visible" type="warning" size="small" effect="plain">已降级</el-tag>
    </div>

    <p v-if="process.extract_note" class="page-caption">{{ process.extract_note }}</p>
    <p class="page-caption">{{ process.run_detail }}</p>

    <el-alert
      v-if="process.questions.length"
      type="info"
      :closable="false"
      show-icon
      :title="`本次提出 ${process.questions.length} 个追问：${process.questions.join('；')}`"
    />
  </el-card>
</template>
