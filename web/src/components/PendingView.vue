<script setup lang="ts">
/** 批次 5 才交付的页面在这里占位：侧栏分组保持与旧界面一致，点击不会 404。 */
import PageHeader from './PageHeader.vue'

defineProps<{ title: string; note: string }>()
</script>

<template>
  <section class="page">
    <page-header :title="title" />
    <el-alert type="info" :title="note" :closable="false" show-icon />
  </section>
</template>
