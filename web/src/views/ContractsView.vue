<script setup lang="ts">
/**
 * 合同管理（规格 006 §3 `/contracts`）：列表、搜索（文件名或正文）、看原文、删除。
 *
 * 搜索与分页都由服务端做（`total` 是筛选后的条数，`total_all` 是全部条数），
 * 前端不复制一份过滤逻辑，避免和仓储口径漂移。
 */
import { computed, onMounted, ref } from 'vue'

import {
  deleteContract,
  fetchContract,
  fetchContracts,
  type ContractQuery,
} from '@/api/endpoints'
import type { ContractDetail, ContractPage, ContractSummary } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { useSession } from '@/stores/session'

const PAGE_SIZE = 20

const session = useSession()
const { loading, error, run } = useAsyncTask()
const { loading: deleting, error: deleteError, run: runDelete } = useAsyncTask()

const keyword = ref('')
const page = ref(1)
const data = ref<ContractPage | null>(null)
const detail = ref<ContractDetail | null>(null)
const detailVisible = ref(false)
const pendingDelete = ref<ContractSummary | null>(null)
const deleteVisible = ref(false)

const items = computed<ContractSummary[]>(() => data.value?.items ?? [])
const total = computed(() => data.value?.total ?? 0)
const totalAll = computed(() => data.value?.total_all ?? 0)
const hasEnterprise = computed(() => (session.user?.enterprise_id ?? '') !== '')

async function load(): Promise<void> {
  const query: ContractQuery = { q: keyword.value, page: page.value, page_size: PAGE_SIZE }
  const result = await run(() => fetchContracts(query))
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

async function openDetail(row: ContractSummary): Promise<void> {
  const fetched = await run(() => fetchContract(row.contract_id))
  if (!fetched) return
  detail.value = fetched
  detailVisible.value = true
}

function askDelete(row: ContractSummary): void {
  pendingDelete.value = row
  deleteVisible.value = true
}

async function confirmDelete(): Promise<void> {
  const target = pendingDelete.value
  if (target === null) return
  const done = await runDelete(async () => {
    await deleteContract(target.contract_id)
    return true
  })
  if (!done) return
  deleteVisible.value = false
  pendingDelete.value = null
  detailVisible.value = false
  await load()
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="合同管理" />

    <el-alert
      v-if="!hasEnterprise"
      class="page-card"
      type="warning"
      :closable="false"
      show-icon
      title="当前账号未绑定企业，合同按企业隔离。"
    />

    <el-card class="page-card" shadow="never">
      <div class="page-actions">
        <el-input
          v-model="keyword"
          placeholder="搜索（文件名或正文），例如：试用期"
          clearable
          style="max-width: 360px"
          @keyup.enter="search"
          @clear="search"
        />
        <el-button type="primary" :loading="loading" @click="search">搜索</el-button>
      </div>
      <p class="page-caption">共 {{ totalAll }} 份 · 筛出 {{ total }} 份</p>

      <error-notice v-if="error" :error="error" />
      <error-notice v-if="deleteError" :error="deleteError" />

      <el-skeleton v-if="loading && items.length === 0" :rows="4" animated />
      <el-empty v-else-if="items.length === 0" description="还没有保存过合同；在合同核对里粘贴或上传后会自动存档。" />
      <template v-else>
        <el-table :data="items" style="width: 100%">
          <el-table-column prop="filename" label="文件名" min-width="260" show-overflow-tooltip />
          <el-table-column prop="char_count" label="字数" width="90" />
          <el-table-column prop="uploaded_at" label="上传时间" width="200" />
          <el-table-column label="操作" width="160">
            <template #default="{ row }">
              <el-button text type="primary" @click="openDetail(row as ContractSummary)">查看</el-button>
              <el-button text type="danger" @click="askDelete(row as ContractSummary)">删除</el-button>
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

    <el-dialog v-model="detailVisible" :title="detail?.filename ?? '合同原文'" width="760px">
      <pre class="mono-block contract-text">{{ detail?.text ?? '' }}</pre>
      <template #footer>
        <el-button @click="detailVisible = false">收起原文</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="deleteVisible" title="确认删除" width="420px">
      <p>确认删除“{{ pendingDelete?.filename ?? '该合同' }}”？删除后不可在“从历史合同选择”里找到。</p>
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button type="danger" :loading="deleting" @click="confirmDelete">确认删除</el-button>
      </template>
    </el-dialog>
  </section>
</template>
