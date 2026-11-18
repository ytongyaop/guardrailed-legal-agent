<script setup lang="ts">
/** 骨架外壳：侧栏 + 主区；登录页与初始化页不套这一层（`meta.layout = bare`）。 */
import OnboardingDialog from './OnboardingDialog.vue'
import RuntimeNotice from './RuntimeNotice.vue'
import NavSidebar from './NavSidebar.vue'
</script>

<template>
  <div class="shell">
    <nav-sidebar />
    <div class="shell-main">
      <runtime-notice />
      <main class="shell-content">
        <slot />
      </main>
    </div>
    <onboarding-dialog />
  </div>
</template>
