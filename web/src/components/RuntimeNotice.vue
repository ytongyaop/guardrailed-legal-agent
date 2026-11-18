<script setup lang="ts">
/** 运行目录 / 迁移提示：每会话只显示一次，可手动关闭。 */
import { computed } from 'vue'

import { dismissRuntimeNotice, useRuntimeNotice } from '@/composables/runtimeNotice'

const notice = useRuntimeNotice()
const hasLines = computed(() => notice.lines.length > 0)
</script>

<template>
  <div v-if="hasLines" class="runtime-notice">
    <el-alert
      v-for="line in notice.lines"
      :key="line.text"
      :type="line.level"
      :title="line.text"
      :closable="false"
      show-icon
    />
    <el-button text size="small" @click="dismissRuntimeNotice">关闭提示</el-button>
  </div>
</template>
