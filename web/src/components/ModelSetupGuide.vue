<script setup lang="ts">
/**
 * 配置引导卡片（无模型时用）：讲清「两个 Key 的分工 / 去哪拿 / 大概花多少」，
 * 再给一个跳设置页的入口。只讲用户要做的事，不解释内部机制。
 *
 * 不写具体单价：价格会变，写死就会过期。非管理员看不到模型设置，按钮换成「联系管理员」，
 * 免得点进设置页扑空。
 */
import { computed } from 'vue'
import { useRouter } from 'vue-router'

import { isAdmin } from '@/stores/session'

const router = useRouter()
const admin = computed(() => isAdmin())
</script>

<template>
  <el-card class="page-card" shadow="never" data-testid="model-setup-guide">
    <h3 class="section-title">配置模型后才能核对</h3>
    <ul class="setup-roles">
      <li>
        <strong>大模型 Key（必需）</strong>：DeepSeek 等，负责判定合同风险。
      </li>
      <li>
        <strong>向量 Key（可选）</strong>：阿里云百炼，负责语义检索；不配则退化为关键词检索，仍可核对。
      </li>
    </ul>
    <p class="form-hint">
      DeepSeek 大模型 Key：
      <a href="https://platform.deepseek.com/api_keys" target="_blank" rel="noopener">platform.deepseek.com</a>
    </p>
    <p class="form-hint">
      阿里云百炼向量 Key：
      <a href="https://bailian.console.aliyun.com/" target="_blank" rel="noopener">bailian.console.aliyun.com</a>
    </p>
    <p class="form-hint">
      按调用量计费，一份合同的核对成本在几分钱量级；新账号通常有赠送额度，具体以服务商页面为准。
    </p>
    <div v-if="admin" class="page-actions">
      <el-button type="primary" @click="router.push({ name: 'settings' })">去设置页</el-button>
    </div>
    <p v-else class="form-hint">当前账号看不到模型设置，请联系管理员配置。</p>
  </el-card>
</template>

<style scoped>
.setup-roles {
  margin: 0 0 12px;
  padding-left: 20px;
  line-height: 1.9;
}
</style>
