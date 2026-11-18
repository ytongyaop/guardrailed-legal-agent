<script setup lang="ts">
/**
 * 单条核对结论（规格 005 第一层 + 第三层叠加）。
 *
 * 固定归属：
 * - **常驻**：确定性徽标、`display_marker`（模型自提提示）、条款片段与理由、法名条号、原文折叠入口；
 * - **第三层**（`finding.process` 非空才出现）：来源徽标、内部 A/B/C 等级、共识 `note`。
 *
 * 不在这里数条数、不在这里排序：条目顺序与 `impact` / `certainty` 全部来自服务端。
 */
import { computed } from 'vue'

import type { FindingView } from '@/api/types'
import StatuteFold from './StatuteFold.vue'
import FoldBox from './FoldBox.vue'

const props = defineProps<{ finding: FindingView }>()

const certainty = computed(() => props.finding.certainty ?? '')
const isConflict = computed(() => certainty.value === '确定冲突')
const isRule = computed(() => props.finding.path === 'rule')
const showSpan = computed(
  () => props.finding.evidence_text !== '' && props.finding.clause_ref !== '',
)
const note = computed(() => {
  const process = props.finding.process
  if (process === null) return ''
  return process.note !== '' && process.note !== props.finding.finding ? process.note : ''
})
</script>

<template>
  <article
    class="finding-card"
    data-testid="finding-card"
    :data-path="finding.path"
    :data-certainty="certainty"
    :data-impact="finding.impact ?? ''"
  >
    <header class="finding-head">
      <el-tag :type="isConflict ? 'danger' : 'info'" size="small" effect="dark">
        {{ certainty || '待确认' }}
      </el-tag>
      <el-tag v-if="finding.display_marker" type="warning" size="small" effect="plain" data-testid="finding-marker">
        {{ finding.display_marker }}
      </el-tag>
      <!-- 第三层：来源与内部等级随 process 一起出现/消失 -->
      <template v-if="finding.process">
        <el-tag type="info" size="small" effect="plain" data-testid="finding-source">
          来源：{{ finding.process.source_label }}
        </el-tag>
        <el-tag v-if="finding.process.grade" type="info" size="small" effect="plain">等级 {{ finding.process.grade }}</el-tag>
      </template>
    </header>

    <p v-if="isRule" class="finding-claim"><strong>{{ finding.finding }}</strong></p>
    <template v-else>
      <p v-if="showSpan" class="finding-span">
        条款片段（正文第 {{ finding.clause_ref }} 段）：{{ finding.span_preview }}
      </p>
      <fold-box v-if="finding.span_expansible" title="查看条款原文" testid="span-fold">
        <p class="fold-text">{{ finding.evidence_text }}</p>
      </fold-box>
      <p v-if="finding.finding" class="finding-reason"><strong>理由</strong>：{{ finding.finding }}</p>
    </template>

    <p v-if="finding.statutes.length" class="finding-statutes" data-testid="finding-statutes">
      <span>依据：</span>
      <span
        v-for="(row, index) in finding.statutes"
        :key="row.id"
        class="statute-label"
        data-testid="statute-label"
        >{{ row.label }}<template v-if="index < finding.statutes.length - 1">、</template></span
      >
    </p>
    <statute-fold v-for="row in finding.statutes" :key="`${row.id}-text`" :statute="row" />

    <p v-if="finding.need_facts.length" class="finding-caption">需补充事实：{{ finding.need_facts.join('、') }}</p>
    <p v-if="note" class="finding-caption">{{ note }}</p>
  </article>
</template>
