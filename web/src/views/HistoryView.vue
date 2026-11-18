<script setup lang="ts">
/**
 * 历史记录（规格 006 §3 `/history`，对应旧界面 `_page_history`）。
 *
 * 口径全部由服务端给：筛选（`all/risk/degraded`）在「最近 200 次核对」窗口内做，
 * `total_all` 是窗口内条数而不是全库总量，所以页面上要把 `window_size` 一起说清楚（006 未决问题 11）。
 * 分页只切当前窗口的页，不改变窗口口径；软删后记录从列表消失，操作日志仍保留。
 */
import { computed, defineAsyncComponent, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import {
  deleteHistoryEntry,
  fetchHistory,
  fetchTrend,
  type HistoryFilter,
} from '@/api/endpoints'
import type { ApiError } from '@/api/errors'
import type { HistoryPage, HistorySummary, TrendView } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import FoldBox from '@/components/result/FoldBox.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { useSession } from '@/stores/session'

// ECharts 只在打开趋势时需要：异步组件让它的 chunk 不进入首屏包（006 §1「按需引入」）
const TrendChart = defineAsyncComponent(() => import('@/components/TrendChart.vue'))

const PAGE_SIZE = 20

const session = useSession()
const router = useRouter()
const { loading, error, run } = useAsyncTask()
const { loading: deleting, error: deleteError, run: runDelete } = useAsyncTask()

const filter = ref<HistoryFilter>('all')
const page = ref(1)
const data = ref<HistoryPage | null>(null)
const trend = ref<TrendView | null>(null)
const trendError = ref<ApiError | null>(null)
const pendingDelete = ref<HistorySummary | null>(null)
const deleteVisible = ref(false)
/** 趋势图只在用户展开「统计与趋势」时才加载 ECharts chunk（006 §1「按需引入」）。 */
const trendOpen = ref(false)

const rows = computed<HistorySummary[]>(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const totalAll = computed(() => data.value?.total_all ?? 0)
const windowSize = computed(() => data.value?.window_size ?? 200)
const deletedCount = computed(() => data.value?.deleted_count ?? 0)
const hasEnterprise = computed(() => (session.user?.enterprise_id ?? '') !== '')

async function load(): Promise<void> {
  const result = await run(() => fetchHistory({ filter: filter.value, page: page.value, page_size: PAGE_SIZE }))
  if (result) data.value = result
}

async function loadTrend(): Promise<void> {
  trendError.value = null
  try {
    trend.value = await fetchTrend()
  } catch (failure) {
    trendError.value = failure as ApiError
  }
}

function changeFilter(next: string | number | boolean | undefined): void {
  filter.value = next === 'risk' || next === 'degraded' ? next : 'all'
  page.value = 1
  void load()
}

function changePage(next: number): void {
  page.value = next
  void load()
}

function openDetail(row: HistorySummary): void {
  void router.push({ name: 'history-detail', params: { taskId: row.task_id } })
}

function askDelete(row: HistorySummary): void {
  pendingDelete.value = row
  deleteVisible.value = true
}

async function confirmDelete(): Promise<void> {
  const target = pendingDelete.value
  if (target === null) return
  const done = await runDelete(async () => {
    await deleteHistoryEntry(target.task_id)
    return true
  })
  if (!done) return
  deleteVisible.value = false
  pendingDelete.value = null
  if (rows.value.length === 1 && page.value > 1) page.value -= 1
  await load()
  await loadTrend()
}

function statusOf(row: HistorySummary): string {
  return row.refused ? '越界拒答' : row.verdict.status === 'risk' ? '有风险' : row.verdict.status === 'partial' ? '覆盖不完整' : '未见风险'
}

onMounted(async () => {
  if (!hasEnterprise.value) return
  await load()
  await loadTrend()
})
</script>

<template>
  <section class="page">
    <page-header title="核对历史" />

    <el-card v-if="!hasEnterprise" class="page-card" shadow="never">
      <el-alert
        type="warning"
        :closable="false"
        show-icon
        title="当前账号未绑定企业。核对记录按企业隔离，先完善企业档案才能查看。"
      />
      <el-button type="primary" @click="router.push({ name: 'enterprise' })">去完善企业档案</el-button>
    </el-card>

    <template v-else>
      <el-card class="page-card" shadow="never">
        <div class="page-actions">
          <el-radio-group :model-value="filter" @change="changeFilter">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="risk">有风险</el-radio-button>
            <el-radio-button value="degraded">已降级</el-radio-button>
          </el-radio-group>
        </div>
        <p class="page-caption" data-testid="history-caption">
          共 {{ total }} 次有效核对 · 已排除 {{ deletedCount }} 条已删除记录 · 本页仅显示核对记录 ·
          筛选与统计窗口：最近 {{ windowSize }} 次核对（窗口内共 {{ totalAll }} 条）
        </p>

        <error-notice v-if="error" :error="error" @retry="load" />
        <error-notice v-if="deleteError" :error="deleteError" />

        <el-skeleton v-if="loading && rows.length === 0" :rows="5" animated />
        <el-empty v-else-if="rows.length === 0" description="还没有核对记录。" />
        <template v-else>
          <el-table :data="rows" style="width: 100%" data-testid="history-table">
            <el-table-column label="合同" min-width="240">
              <template #default="{ row }">
                <p class="history-name">{{ (row as HistorySummary).contract_name }}</p>
                <p class="page-caption">
                  {{ (row as HistorySummary).created_at ?? '时间未记录' }} ·
                  {{ (row as HistorySummary).verdict.risk_count }} 条冲突 ·
                  {{ (row as HistorySummary).verdict.review_count }} 条待确认 ·
                  {{ (row as HistorySummary).suggestion_count }} 条建议 ·
                  {{ (row as HistorySummary).latency_text }}
                </p>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="260">
              <template #default="{ row }">
                <div class="history-badges">
                  <el-tag size="small" effect="plain">{{ statusOf(row as HistorySummary) }}</el-tag>
                  <el-tag
                    :type="(row as HistorySummary).verify_ok ? 'success' : 'danger'"
                    size="small"
                    effect="plain"
                  >
                    {{ (row as HistorySummary).verify_ok ? '引用可溯源' : '引用不可溯源' }}
                  </el-tag>
                  <el-tag v-if="(row as HistorySummary).degraded_visible" type="warning" size="small" effect="plain">
                    已降级
                  </el-tag>
                  <el-tag v-if="(row as HistorySummary).ask_rounds" type="primary" size="small" effect="plain">
                    补问 {{ (row as HistorySummary).ask_rounds }} 轮
                  </el-tag>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160">
              <template #default="{ row }">
                <el-button text type="primary" @click="openDetail(row as HistorySummary)">查看</el-button>
                <el-button text type="danger" @click="askDelete(row as HistorySummary)">删除</el-button>
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

      <el-card class="page-card" shadow="never">
        <fold-box title="统计与趋势" testid="trend-block" @toggle="trendOpen = $event">
          <error-notice v-if="trendError" :error="trendError" @retry="loadTrend" />
          <template v-if="trend">
            <div class="process-metrics">
              <div class="process-metric">
                <span class="process-metric-value">{{ trend.recent }}</span>
                <span class="process-metric-label">最近一次（条）</span>
              </div>
              <div class="process-metric">
                <span class="process-metric-value">{{ trend.average.toFixed(1) }}</span>
                <span class="process-metric-label">平均（条）</span>
              </div>
              <div class="process-metric">
                <span class="process-metric-value">{{ trend.max }}</span>
                <span class="process-metric-label">最多（条）</span>
              </div>
            </div>
            <trend-chart v-if="trendOpen" :points="trend.points" />
            <p class="page-caption" data-testid="trend-note">{{ trend.note }}</p>
          </template>
        </fold-box>
      </el-card>
    </template>

    <el-dialog v-model="deleteVisible" title="确认删除" width="460px">
      <p>
        确认删除“{{ pendingDelete?.contract_name ?? '该核对记录' }} ·
        {{ pendingDelete?.created_at ?? '时间未记录' }}”这条记录？
        删除后历史页不再显示，操作日志仍保留。
      </p>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDelete">确认删除</el-button>
      </template>
    </el-dialog>
  </section>
</template>
