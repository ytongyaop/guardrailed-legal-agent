<script setup lang="ts">
/** 404：未知路由不留白屏，也不回显路径细节。 */
import ResultPanel from '@/components/ResultPanel.vue'
</script>

<template>
  <result-panel heading="页面不存在" detail="链接可能已失效，请从左侧导航重新进入需要的页面。" />
</template>
