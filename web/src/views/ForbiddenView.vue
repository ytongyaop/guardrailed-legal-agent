<script setup lang="ts">
/** 403：普通用户直接访问管理端时的统一提示；真正的拒绝仍来自后端。 */
import ResultPanel from '@/components/ResultPanel.vue'
</script>

<template>
  <result-panel heading="没有访问权限" detail="该页面仅管理员可用。如需权限，请联系管理员调整角色。" />
</template>
