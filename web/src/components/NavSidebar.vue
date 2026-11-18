<script setup lang="ts">
/**
 * 侧栏（规格 006 §3 / §4）：品牌、分组导航、底部用户名与角色、退出登录。
 *
 * 管理端整组只在管理员会话里出现——这只是体验；普通用户直接访问 `/admin/*`
 * 仍会被后端 403 拦下。
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/errors'
import { navigationGroups } from '@/router/navigation'
import { isAdmin, signOut, useSession } from '@/stores/session'

import ErrorNotice from './ErrorNotice.vue'

const session = useSession()
const route = useRoute()
const router = useRouter()

const groups = computed(() => navigationGroups(isAdmin()))
const activePath = computed(() => route.path)
const signingOut = ref(false)
const error = ref<ApiError | null>(null)

async function handleSignOut(): Promise<void> {
  signingOut.value = true
  error.value = null
  try {
    await signOut()
    await router.push({ name: 'login' })
  } catch (failure) {
    error.value = failure instanceof ApiError ? failure : null
  } finally {
    signingOut.value = false
  }
}
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-brand">
      <img class="sidebar-logo" src="/logo.svg" alt="系统标识" />
      <span class="sidebar-brand-text">小微企业用工风险辅助系统</span>
    </div>

    <el-menu :default-active="activePath" router class="sidebar-menu">
      <el-menu-item-group v-for="group in groups" :key="group.key" :title="group.label">
        <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
          {{ item.label }}
        </el-menu-item>
      </el-menu-item-group>
    </el-menu>

    <div class="sidebar-footer">
      <div v-if="session.user" class="sidebar-user">
        {{ session.user.username }} · {{ session.user.role }}
      </div>
      <el-button text :loading="signingOut" @click="handleSignOut">退出登录</el-button>
      <error-notice v-if="error" :error="error" />
    </div>
  </aside>
</template>
