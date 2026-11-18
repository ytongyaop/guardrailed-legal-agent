<script setup lang="ts">
/**
 * 用户与角色（规格 006 §3 `/admin/users`）：列表、改角色、删账号。
 *
 * 删账号必须回填目标用户名（后端 `DELETE` 也要求 `confirm_username`）；
 * 删除结果里的合同 / 任务 / 日志条数原样呈现，不加工。
 */
import { onMounted, reactive, ref } from 'vue'

import { deleteUser, fetchUsers, patchUserRole } from '@/api/endpoints'
import type { ApiError } from '@/api/errors'
import type { AdminUser } from '@/api/types'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { toApiError, useAsyncTask } from '@/composables/asyncTask'

const ROLES: readonly string[] = ['用户', '管理员']

const { loading, error, run } = useAsyncTask()
const { loading: deleting, error: deleteError, run: runDelete } = useAsyncTask()

const users = ref<AdminUser[]>([])
const drafts = reactive<Record<string, string>>({})
const savingId = ref('')
const flash = ref('')
const deleteVisible = ref(false)
const deleteTarget = ref<AdminUser | null>(null)
const confirmName = ref('')
const actionError = ref<ApiError | null>(null)

async function load(): Promise<void> {
  const rows = await run(() => fetchUsers())
  if (!rows) return
  users.value = rows
  for (const user of rows) drafts[user.user_id] = user.role
}

async function saveRole(user: AdminUser): Promise<void> {
  actionError.value = null
  flash.value = ''
  savingId.value = user.user_id
  try {
    const updated = await patchUserRole(user.user_id, drafts[user.user_id] ?? user.role)
    drafts[updated.user_id] = updated.role
    users.value = users.value.map((item) => (item.user_id === updated.user_id ? updated : item))
    flash.value = `已把 ${updated.username} 的角色改为 ${updated.role}。`
  } catch (failure) {
    actionError.value = toApiError(failure)
  } finally {
    savingId.value = ''
  }
}

function askDelete(user: AdminUser): void {
  actionError.value = null
  deleteTarget.value = user
  confirmName.value = ''
  deleteVisible.value = true
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (target === null) return
  const result = await runDelete(() => deleteUser(target.user_id, confirmName.value))
  if (!result) return
  deleteVisible.value = false
  deleteTarget.value = null
  flash.value = `已删除账号 ${result.username}：合同 ${result.contracts} 条、任务 ${result.tasks} 条、日志 ${result.logs} 条。`
  await load()
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="用户与角色" />

    <el-card class="page-card" shadow="never">
      <error-notice v-if="error" :error="error" />
      <error-notice v-if="actionError" :error="actionError" />
      <el-alert v-if="flash" class="error-notice" type="success" :title="flash" :closable="false" />

      <el-skeleton v-if="loading && users.length === 0" :rows="4" animated />
      <el-table v-else :data="users" style="width: 100%">
        <el-table-column prop="username" label="用户名" min-width="140" />
        <el-table-column prop="enterprise_id" label="企业" min-width="140" />
        <el-table-column prop="created_at" label="建档时间" width="200" />
        <el-table-column label="角色" width="240">
          <template #default="{ row }">
            <el-select v-model="drafts[row.user_id]" size="small" style="width: 110px">
              <el-option v-for="role in ROLES" :key="role" :label="role" :value="role" />
            </el-select>
            <el-button
              text
              type="primary"
              :loading="savingId === row.user_id"
              @click="saveRole(row as AdminUser)"
            >
              保存
            </el-button>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="110">
          <template #default="{ row }">
            <el-button text type="danger" @click="askDelete(row as AdminUser)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card class="page-card" shadow="never">
      <h2 class="page-title">删除账号</h2>
      <p class="page-caption">
        删除后该账号及其企业、合同、任务、日志一并移除，不可恢复；操作会写一条管理员日志。
      </p>
    </el-card>

    <el-dialog v-model="deleteVisible" title="确认删除账号" width="460px">
      <p>
        要删除的账号：{{ deleteTarget?.username ?? '' }}。删除不可恢复，请输入该用户名以确认。
      </p>
      <error-notice v-if="deleteError" :error="deleteError" />
      <el-input v-model="confirmName" :placeholder="deleteTarget?.username ?? ''" />
      <template #footer>
        <el-button @click="deleteVisible = false">取消</el-button>
        <el-button
          type="danger"
          :disabled="confirmName.trim() === '' || confirmName.trim() !== (deleteTarget?.username ?? '')"
          :loading="deleting"
          @click="confirmDelete"
        >
          确认删除账号
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>
