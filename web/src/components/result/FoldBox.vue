<script setup lang="ts">
/**
 * 折叠块（规格 005 第二层：法条原文 / 事实 / 类案默认折叠）。
 *
 * 用原生 `<details>` 而不是「渲染后 CSS 隐藏」：
 * - 默认不带 `open`，浏览器不渲染内容，测试与 E2E 能直接断言「没有展开」；
 * - 展开/收起不需要额外 JS 状态，也不会因为绑定丢失而意外全开；
 * - `toggle` 事件让调用方可以「打开时才加载重资源」（趋势图就靠它做异步 chunk）。
 * 注意：第三层的**过程信息**不在这里，它是条件渲染（`v-if`），不是折叠。
 */
defineProps<{ title: string; testid?: string }>()
const emit = defineEmits<{ toggle: [open: boolean] }>()

function onToggle(event: Event): void {
  const target = event.target
  if (target instanceof HTMLDetailsElement) emit('toggle', target.open)
}
</script>

<template>
  <details class="fold-box" :data-testid="testid ?? 'fold-box'" @toggle="onToggle">
    <summary class="fold-box-summary">{{ title }}</summary>
    <div class="fold-box-body">
      <slot />
    </div>
  </details>
</template>
