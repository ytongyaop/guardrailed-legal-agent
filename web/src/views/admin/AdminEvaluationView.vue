<script setup lang="ts">
/**
 * 评测（规格 006 §3 `/admin/evaluation`）：只读最近一次评测结果。
 *
 * 接口不跑评测、不联网；界面只展示服务端给的指标、阈值与达标情况，
 * 不一致样本按原样 JSON 展示（`{{ }}` 插值，不用 v-html）。
 */
import { computed, onMounted, ref } from 'vue'

import { fetchEvaluation } from '@/api/endpoints'
import type { EvaluationView } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { describePassed, formatMetricValue, formatThreshold } from '@/utils/format'

const FULL_VARIANT = 'full'

const { loading, error, run } = useAsyncTask()
const data = ref<EvaluationView | null>(null)
const variantName = ref('')

const variantNames = computed(() => (data.value?.variants ?? []).map((item) => item.variant))
const variant = computed(
  () => (data.value?.variants ?? []).find((item) => item.variant === variantName.value) ?? null,
)
const mismatchText = computed(() =>
  variant.value === null ? '' : JSON.stringify(variant.value.mismatches, null, 2),
)
const historyRows = computed(() => data.value?.history ?? [])

async function load(): Promise<void> {
  const result = await run(() => fetchEvaluation())
  if (!result) return
  data.value = result
  const names = result.variants.map((item) => item.variant)
  variantName.value = names.includes(FULL_VARIANT) ? FULL_VARIANT : (names[0] ?? '')
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="评测" />

    <el-card class="page-card" shadow="never">
      <error-notice v-if="error" :error="error" />
      <el-skeleton v-if="loading && data === null" :rows="4" animated />
      <template v-else-if="data !== null">
        <el-alert
          v-if="!data.available"
          type="info"
          :title="data.message ?? '还没有可展示的评测结果。'"
          :closable="false"
          show-icon
        />
        <template v-else>
          <div class="page-actions">
            <el-select v-model="variantName" style="width: 200px">
              <el-option v-for="name in variantNames" :key="name" :label="name" :value="name" />
            </el-select>
          </div>
          <p class="page-caption">
            样本 {{ data.case_count }} 条 · 生成时间 {{ data.generated_at }} ·
            回归集为官方示范文本加人工植入；留出集与问答消融的标注说明见评测集说明文档。
          </p>

          <el-table :data="variant?.metrics ?? []" style="width: 100%">
            <el-table-column prop="label" label="指标" min-width="220" />
            <el-table-column label="实测" width="120">
              <template #default="{ row }">{{ formatMetricValue(row.value) }}</template>
            </el-table-column>
            <el-table-column label="阈值" width="100">
              <template #default="{ row }">{{ formatThreshold(row.threshold) }}</template>
            </el-table-column>
            <el-table-column label="达标" width="100">
              <template #default="{ row }">{{ describePassed(row.passed) }}</template>
            </el-table-column>
          </el-table>

          <el-collapse class="page-card">
            <el-collapse-item
              :title="`不一致样本（${variant?.mismatches.length ?? 0} 条）`"
              name="mismatches"
            >
              <pre class="mono-block">{{ mismatchText }}</pre>
            </el-collapse-item>
            <el-collapse-item :title="`历史评测记录（${historyRows.length} 行）`" name="history">
              <el-table :data="historyRows" style="width: 100%">
                <el-table-column prop="time" label="时间" width="200" />
                <el-table-column prop="case_count" label="样本" width="90" />
                <el-table-column prop="variant" label="档位" width="130" />
                <el-table-column prop="accuracy" label="准确率" width="110" />
                <el-table-column prop="recall" label="召回率" width="110" />
                <el-table-column prop="passed_text" label="达标项" width="110" />
              </el-table>
            </el-collapse-item>
          </el-collapse>
        </template>
      </template>
    </el-card>
  </section>
</template>
