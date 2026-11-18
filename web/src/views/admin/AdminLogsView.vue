<script setup lang="ts">
/**
 * 操作日志（规格 006 §3 `/admin/logs`）：筛选、分页、CSV 导出。
 *
 * `total_all` 是最近 500 条窗口内的总数，不是全库总量；界面把它和窗口口径一起说清。
 */
import { computed, onMounted, ref } from 'vue'

import { exportLogs, fetchLogs } from '@/api/endpoints'
import type { LogPage, LogRow } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'

const ACTIONS: readonly string[] = ['全部', '核对', '问答']
const PAGE_SIZE = 50
const LOG_WINDOW = 500
const CSV_FILENAME = '操作日志.csv'

const { loading, error, run } = useAsyncTask()
const { loading: exporting, error: exportError, run: runExport } = useAsyncTask()

const action = ref('全部')
const keyword = ref('')
const page = ref(1)
const data = ref<LogPage | null>(null)
const flash = ref('')

const rows = computed<LogRow[]>(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const totalAll = computed(() => data.value?.total_all ?? 0)

async function load(): Promise<void> {
  const result = await run(() =>
    fetchLogs({ action: action.value, q: keyword.value, page: page.value, page_size: PAGE_SIZE }),
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

async function downloadCsv(): Promise<void> {
  flash.value = ''
  const blob = await runExport(() => exportLogs({ action: action.value, q: keyword.value }))
  if (!blob) return
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = CSV_FILENAME
  link.click()
  URL.revokeObjectURL(url)
  flash.value = '已导出当前筛选结果。'
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="操作日志" />

    <el-card class="page-card" shadow="never">
      <div class="page-actions">
        <el-select v-model="action" style="width: 130px" @change="search">
          <el-option v-for="item in ACTIONS" :key="item" :label="item" :value="item" />
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="关键词（用户 / 任务 / 详情），例如：admin 或 T-"
          clearable
          style="max-width: 360px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-button type="primary" :loading="loading" @click="search">筛选</el-button>
        <el-button :loading="exporting" @click="downloadCsv">导出 CSV</el-button>
      </div>
      <p class="page-caption">
        最近 {{ LOG_WINDOW }} 条窗口内共 {{ totalAll }} 条 · 筛出 {{ total }} 条
      </p>

      <error-notice v-if="error" :error="error" />
      <error-notice v-if="exportError" :error="exportError" />
      <el-alert v-if="flash" class="error-notice" type="success" :title="flash" :closable="false" />

      <el-skeleton v-if="loading && rows.length === 0" :rows="4" animated />
      <el-empty v-else-if="rows.length === 0" description="该筛选条件下没有日志。" />
      <template v-else>
        <el-table :data="rows" style="width: 100%">
          <el-table-column prop="created_at" label="时间" width="200" />
          <el-table-column prop="user_id" label="用户" width="140" />
          <el-table-column prop="action" label="动作" width="90" />
          <el-table-column prop="task_id" label="任务" width="150" />
          <el-table-column prop="detail" label="详情" min-width="240" show-overflow-tooltip />
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
