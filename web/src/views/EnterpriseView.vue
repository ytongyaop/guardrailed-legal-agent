<script setup lang="ts">
/**
 * 企业档案（规格 006 §3 `/enterprise`）。
 *
 * 三项必选（用工主体类型 / 工时制度 / 是否缴纳社保）在提交前先提示一遍，
 * 但以后端 400 `details.fields` 为准；删除要单独确认，删除后合同一并移除、历史保留。
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { deleteEnterprise, fetchEnterprise, saveEnterprise } from '@/api/endpoints'
import ErrorNotice from '@/components/ErrorNotice.vue'
import PageHeader from '@/components/PageHeader.vue'
import { useAsyncTask } from '@/composables/asyncTask'
import { reloadSession, useSession } from '@/stores/session'

const EMPLOYER_TYPES: readonly string[] = ['有限责任公司', '个体工商户', '个人独资企业']
const WORKING_HOURS: readonly string[] = ['标准工时', '综合计算工时', '不定时工时']
const SOCIAL_INSURANCE: readonly string[] = ['是', '否']

const session = useSession()
const { loading, error, run } = useAsyncTask()
const { loading: deleting, error: deleteError, run: runDelete } = useAsyncTask()

const form = reactive({
  name: '',
  employer_type: '',
  staff_size: 0,
  working_hours: '',
  social_insurance: '',
})
const bound = ref(false)
const hint = ref('')
const flash = ref('')
const confirmDelete = ref(false)

const hasEnterprise = computed(() => bound.value)

async function load(): Promise<void> {
  const enterprise = await run(() => fetchEnterprise())
  if (enterprise === undefined) return
  bound.value = enterprise !== null
  form.name = enterprise?.name ?? ''
  form.employer_type = enterprise?.employer_type ?? ''
  form.staff_size = enterprise?.staff_size ?? 0
  form.working_hours = enterprise?.working_hours ?? ''
  form.social_insurance = enterprise?.social_insurance ?? ''
}

function validate(): string {
  const missing: string[] = []
  if (!form.employer_type.trim()) missing.push('用工主体类型')
  if (!form.working_hours.trim()) missing.push('工时制度')
  if (!form.social_insurance.trim()) missing.push('是否缴纳社保')
  return missing.length > 0 ? `保存前请先选择：${missing.join('、')}` : ''
}

async function submit(): Promise<void> {
  flash.value = ''
  hint.value = validate()
  if (hint.value) return
  const saved = await run(() => saveEnterprise({ ...form }))
  if (!saved) return
  bound.value = true
  const wasBound = session.user !== null && session.user.enterprise_id !== ''
  if (!wasBound) {
    // 首次保存由后端绑定企业；重新自述让会话里的 enterprise_id 与之一致
    await reloadSession()
  }
  flash.value = '档案已保存。'
}

async function remove(): Promise<void> {
  flash.value = ''
  const done = await runDelete(async () => {
    await deleteEnterprise()
    return true
  })
  if (!done) return
  confirmDelete.value = false
  bound.value = false
  form.name = ''
  form.employer_type = ''
  form.staff_size = 0
  form.working_hours = ''
  form.social_insurance = ''
  flash.value = '企业档案已删除。'
}

onMounted(load)
</script>

<template>
  <section class="page">
    <page-header title="企业档案" />

    <el-card class="page-card" shadow="never">
      <el-skeleton v-if="loading && !hasEnterprise && !error" :rows="4" animated />
      <template v-else>
        <error-notice v-if="error" :error="error" />
        <el-alert v-if="flash" class="error-notice" type="success" :title="flash" :closable="false" />
        <el-alert v-if="hint" class="error-notice" type="error" :title="hint" :closable="false" />

        <el-form label-width="120px" class="form-narrow" @submit.prevent="submit">
          <el-form-item label="企业名称">
            <el-input v-model="form.name" placeholder="例如：某某科技有限公司" />
          </el-form-item>
          <el-form-item label="用工主体类型">
            <el-select v-model="form.employer_type" placeholder="请选择用工主体类型">
              <el-option v-for="kind in EMPLOYER_TYPES" :key="kind" :label="kind" :value="kind" />
            </el-select>
          </el-form-item>
          <el-form-item label="用工人数">
            <el-input-number v-model="form.staff_size" :min="0" :step="1" />
          </el-form-item>
          <el-form-item label="工时制度">
            <el-select v-model="form.working_hours" placeholder="请选择工时制度">
              <el-option v-for="hours in WORKING_HOURS" :key="hours" :label="hours" :value="hours" />
            </el-select>
          </el-form-item>
          <el-form-item label="是否缴纳社保">
            <el-select v-model="form.social_insurance" placeholder="请选择是否缴纳社保">
              <el-option
                v-for="option in SOCIAL_INSURANCE"
                :key="option"
                :label="option"
                :value="option"
              />
            </el-select>
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="loading" @click="submit">保存档案</el-button>
          </el-form-item>
        </el-form>
      </template>
    </el-card>

    <el-card v-if="hasEnterprise" class="page-card" shadow="never">
      <h2 class="page-title">删除企业档案</h2>
      <p class="page-caption">删除后企业信息与已保存合同一并移除，核对历史保留。</p>
      <error-notice v-if="deleteError" :error="deleteError" />
      <el-checkbox v-model="confirmDelete">我确认删除该企业档案</el-checkbox>
      <div class="page-actions">
        <el-button type="danger" :disabled="!confirmDelete" :loading="deleting" @click="remove">
          确认删除
        </el-button>
      </div>
    </el-card>
  </section>
</template>
