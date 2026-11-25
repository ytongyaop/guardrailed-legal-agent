<script setup lang="ts">
/**
 * 首次启动免责提示（1.0.0 公开分发，软提示）。
 *
 * 挂在全站根组件 `App.vue` 而不是 `AppShell`：登录页、初始化页不套外壳，也要能提示。
 *
 * 口径：
 * 1. 只提示一次——任何关闭方式（右上角 ×、底部「我知道了」、点遮罩）都算已读，
 *    把版本号写进 localStorage 的 `mla.disclaimer.acknowledged`，之后不再弹；
 * 2. 换成新版本号会重新提示一次（免责边界变了要重新告知）；
 * 3. 不阻断任何操作——`close-on-click-modal` 保持默认 true，不关也能点背后的页面；
 * 4. localStorage 不可用（隐私模式）时退回内存标记：仍然提示，本次会话内关掉不再弹，
 *    刷新会再弹一次（记不住标记是可接受的代价，放弃告知才不可接受）。
 */
import { onMounted, ref } from 'vue'

const ACK_KEY = 'mla.disclaimer.acknowledged'
const ACK_VERSION = '1.0.0'

const TITLE = '使用前请知悉'
const PARAGRAPHS: readonly string[] = [
  '本工具通过自动化分析给出合同风险提示，不是法律意见，也不构成律师与客户的关系。',
  '分析结果可能遗漏或出错；涉及重大权益的事项，请咨询执业律师。',
  '你上传的合同正文会发送到你在设置页配置的模型服务商用于分析。',
]

const visible = ref(false)

/**
 * 存储不可用（隐私模式）时的会话内标记。
 *
 * 不写存储也要告知：本次页面会话内关掉就不再重复弹，刷新会再弹一次——这是可接受的代价，
 * 反过来「一次都不弹」等于放弃了告知义务。
 */
let acknowledgedInMemory = false

/** 可写的 localStorage；隐私模式下读写会抛异常，一律当成不可用。 */
function usableStorage(): Storage | null {
  try {
    const store = window.localStorage
    const probe = `${ACK_KEY}.probe`
    store.setItem(probe, '1')
    store.removeItem(probe)
    return store
  } catch {
    return null
  }
}

onMounted(() => {
  const store = usableStorage()
  // 存储不可用：退回内存标记，仍然提示（记不住就下次刷新再提一次）
  if (store === null) {
    visible.value = !acknowledgedInMemory
    return
  }
  try {
    visible.value = store.getItem(ACK_KEY) !== ACK_VERSION
  } catch {
    visible.value = !acknowledgedInMemory
  }
})

/** 任何关闭路径都走这里：先收弹窗，再尽量写标记（写失败不影响本次使用）。 */
function handleClose(): void {
  visible.value = false
  acknowledgedInMemory = true
  const store = usableStorage()
  if (store === null) return
  try {
    store.setItem(ACK_KEY, ACK_VERSION)
  } catch {
    // 写失败只影响下次是否再提示，不该让页面报错
  }
}
</script>

<template>
  <el-dialog :model-value="visible" :title="TITLE" width="560px" @update:model-value="handleClose">
    <p v-for="paragraph in PARAGRAPHS" :key="paragraph" class="disclaimer-paragraph">
      {{ paragraph }}
    </p>
    <template #footer>
      <el-button type="primary" @click="handleClose">我知道了</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.disclaimer-paragraph {
  margin: 0 0 12px;
  line-height: 1.7;
}

.disclaimer-paragraph:last-child {
  margin-bottom: 0;
}
</style>
