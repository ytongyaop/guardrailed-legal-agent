<script setup lang="ts">
/**
 * 根组件：只负责三件事——套不套侧栏外壳、收到 401 时跳登录、首次启动的免责提示。
 *
 * 外壳由路由 `meta.layout` 决定：登录与初始化是 `bare`，其余走 `AppShell`。
 * 免责提示放在这一层（不是 `AppShell`）：登录与初始化页也要提示。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import zhCn from 'element-plus/es/locale/lang/zh-cn'

import AppShell from '@/components/AppShell.vue'
import DisclaimerNotice from '@/components/DisclaimerNotice.vue'
import { loadRuntimeNotice } from '@/composables/runtimeNotice'
import { setSignedOutHandler, useSession } from '@/stores/session'

const route = useRoute()
const router = useRouter()
const session = useSession()

// 未登录时访问公开页（403/404 提示）也走裸布局：不给匿名会话渲染侧栏导航
const bare = computed(
  () => route.meta.layout === 'bare' || (session.user === null && route.meta.public === true),
)

setSignedOutHandler(() => {
  const current = router.currentRoute.value
  if (current.name === 'login') return
  void router.push({ name: 'login', query: { redirect: current.fullPath } })
})

onMounted(() => {
  void loadRuntimeNotice()
})
</script>

<template>
  <el-config-provider :locale="zhCn">
    <router-view v-if="bare" />
    <app-shell v-else>
      <router-view />
    </app-shell>
    <disclaimer-notice />
  </el-config-provider>
</template>
