<script setup lang="ts">
/**
 * 法源库（规格 006 §3 `/admin/statutes`）：只读，按法名 / 条号 / 正文过滤。
 *
 * `total` 是筛选后的条数，`total_all` 是法源总数；来源链接只允许 http(s) 且带 rel=noopener。
 */
import { computed, onMounted, ref } from 'vue'

import { fetchStatutes } from '@/api/endpoints'
import type { StatutePage, StatuteRow } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { safeSourceUrl } from '@/utils/format'

const PAGE_SIZE = 50

const { loading, error, run } = useAsyncTask()
const keyword = ref('')
const page = ref(1)
const data = ref<StatutePage | null>(null)

const rows = computed<StatuteRow[]>(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const totalAll = computed(() => data.value?.total_all ?? 0)

async function load(): Promise<void> {
  const result = await run(() =>
    fetchStatutes({ q: keyword.value, page: page.value, page_size: PAGE_SIZE }),
  )
  if (result) data.value = result
}

function search(): void {
  page.value = 1
  void load()
}

function changePage(next: number): void {
  page.value = next
  void load()
}

function sourceHref(row: StatuteRow): string | null {
  return safeSourceUrl(row.source_url)
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="法源库" />

    <el-card class="page-card" shadow="never">
      <div class="page-actions">
        <el-input
          v-model="keyword"
          placeholder="按关键词过滤（法名 / 条号 / 正文），例如：试用期"
          clearable
          style="max-width: 360px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </div>
      <p class="page-caption">共 {{ totalAll }} 条 · 筛出 {{ total }} 条</p>

      <error-notice v-if="error" :error="error" />
      <el-skeleton v-if="loading && rows.length === 0" :rows="4" animated />
      <template v-else>
        <el-table :data="rows" style="width: 100%">
          <el-table-column prop="law_name" label="法名" min-width="200" show-overflow-tooltip />
          <el-table-column prop="article_no" label="条" width="90" />
          <el-table-column prop="status" label="状态" width="90" />
          <el-table-column prop="effective_date" label="施行" width="120" />
          <el-table-column label="正文" min-width="280" show-overflow-tooltip>
            <template #default="{ row }">{{ row.text }}</template>
          </el-table-column>
          <el-table-column label="来源" width="90">
            <template #default="{ row }">
              <a
                v-if="sourceHref(row as StatuteRow)"
                :href="sourceHref(row as StatuteRow) ?? ''"
                target="_blank"
                rel="noopener noreferrer"
              >
                原文
              </a>
              <span v-else class="page-caption">无</span>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          class="page-card"
          layout="prev, pager, next"
          :total="total"
          :page-size="PAGE_SIZE"
          :current-page="page"
          @current-change="changePage"
        />
      </template>
    </el-card>
  </section>
</template>
