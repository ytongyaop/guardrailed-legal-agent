<script setup lang="ts">
/**
 * 必备条款完整性（规格 005 第一层）：独立区块、独立计数，**不并入冲突或待确认条数**。
 *
 * 汇总句、三态计数与每条的文案都由服务端 `MissingTermsView` 给出：
 * `written_count` 属覆盖率口径，只有过程开关打开时才非空（这里只用它判断是否显示）。
 * 法条定位写在每条正文里；原文按第二层折叠。
 */
import { computed } from 'vue'

import type { MissingTermItem, MissingTermsView } from '@/api/types'
import FoldBox from './FoldBox.vue'

const props = defineProps<{ terms: MissingTermsView }>()

const absent = computed<MissingTermItem[]>(() => props.terms.items.filter((item) => item.status === '缺失'))
const unseen = computed<MissingTermItem[]>(() => props.terms.items.filter((item) => item.status === '未见'))
const hasContent = computed(
  () =>
    props.terms.summary !== '' ||
    props.terms.short_note !== null ||
    absent.value.length > 0 ||
    unseen.value.length > 0 ||
    props.terms.manual_count > 0,
)
</script>

<template>
  <el-card v-if="hasContent" class="page-card" shadow="never" data-testid="missing-terms">
    <h3 class="section-title">必备条款完整性</h3>
    <p v-if="terms.summary" class="page-caption" data-testid="missing-summary">{{ terms.summary }}</p>
    <p v-else-if="terms.short_note" class="page-caption">{{ terms.short_note }}</p>

    <div v-for="item in absent" :key="item.term_id" class="missing-term">
      <el-alert type="error" :closable="false" show-icon :title="item.text" />
      <fold-box v-if="item.statute.found" :title="`${item.name}：依据条文原文`" testid="missing-fold">
        <p class="fold-text">{{ item.statute.text }}</p>
        <p class="fold-meta">{{ item.statute.label }}</p>
      </fold-box>
    </div>

    <div v-for="item in unseen" :key="item.term_id" class="missing-term">
      <el-alert type="warning" :closable="false" show-icon :title="item.text" />
      <fold-box v-if="item.statute.found" :title="`${item.name}：依据条文原文`" testid="missing-fold">
        <p class="fold-text">{{ item.statute.text }}</p>
        <p class="fold-meta">{{ item.statute.label }}</p>
      </fold-box>
    </div>

    <p v-if="terms.manual_count > 0" class="page-caption" data-testid="missing-manual">
      需人工确认 {{ terms.manual_count }} 类：{{ terms.manual_names.join('、') }}
    </p>
  </el-card>
</template>
