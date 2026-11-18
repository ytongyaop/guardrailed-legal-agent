<script setup lang="ts">
/** 403 / 404 的统一提示：不崩、不说内部细节、给一条回去的路。 */
defineProps<{ heading: string; detail: string }>()
</script>

<template>
  <section class="page">
    <el-result icon="warning" :title="heading" :sub-title="detail">
      <template #extra>
        <el-button @click="$router.push({ name: 'check' })">回到合同核对</el-button>
      </template>
    </el-result>
  </section>
</template>
