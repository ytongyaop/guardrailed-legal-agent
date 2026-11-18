<script setup lang="ts">
/**
 * 规则表与必问事实（规格 006 §3 `/admin/rules`）：只读。
 *
 * 槽位由服务端按规则依赖字段推导，前端只渲染，不改类型判定。
 */
import { onMounted, ref } from 'vue'

import { fetchRuleCatalog } from '@/api/endpoints'
import type { RuleCatalogView } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { describeCondition } from '@/utils/format'

const { loading, error, run } = useAsyncTask()
const catalog = ref<RuleCatalogView | null>(null)

async function load(): Promise<void> {
  const result = await run(() => fetchRuleCatalog())
  if (result) catalog.value = result
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="规则表与必问事实" />

    <el-card class="page-card" shadow="never">
      <error-notice v-if="error" :error="error" />
      <el-skeleton v-if="loading && catalog === null" :rows="4" animated />
      <template v-else>
        <el-table :data="catalog?.rules ?? []" style="width: 100%">
          <el-table-column prop="rule_id" label="规则" width="140" />
          <el-table-column prop="statute_id" label="法条" width="130" />
          <el-table-column prop="field" label="字段" width="150" />
          <el-table-column label="条件" min-width="180">
            <template #default="{ row }">
              {{ describeCondition(row.op, row.threshold, row.unit) }}
            </template>
          </el-table-column>
          <el-table-column prop="evidence_span" label="依据片段" min-width="260" show-overflow-tooltip />
        </el-table>
      </template>
    </el-card>

    <el-card class="page-card" shadow="never">
      <h2 class="page-title">必问事实清单</h2>
      <p class="page-caption">阻断项缺失时必须追问；参考项顺带问，缺了不阻断结论。</p>
      <el-table :data="catalog?.slots ?? []" style="width: 100%">
        <el-table-column prop="slot_id" label="槽位" width="160" />
        <el-table-column prop="name" label="名称" width="140" />
        <el-table-column prop="type" label="类型" width="100" />
        <el-table-column prop="question" label="追问句" min-width="240" show-overflow-tooltip />
        <el-table-column label="被哪些规则使用" min-width="180">
          <template #default="{ row }">{{ (row.needed_by as string[]).join('、') }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </section>
</template>
