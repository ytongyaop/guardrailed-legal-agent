<script setup lang="ts">
/** 页面标题只写标题：分页名与导航已经说明这一页做什么（与旧界面 `page_header` 一致）。 */
defineProps<{ title: string }>()
</script>

<template>
  <header class="page-header">
    <h1 class="page-title">{{ title }}</h1>
  </header>
</template>
