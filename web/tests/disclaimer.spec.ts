/**
 * 首次启动免责提示（1.0.0 公开分发，软提示）。
 *
 * 口径：首次打开显示一次，任何关闭方式都算已读；之后不再弹。
 * 存储键 `mla.disclaimer.acknowledged` 存版本号——换版本会重新提示。
 *
 * 断言语义（弹没弹、存储里写了什么），不做整段 DOM 快照。
 */

import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import DisclaimerNotice from '@/components/DisclaimerNotice.vue'

const ACK_KEY = 'mla.disclaimer.acknowledged'
const ACK_VERSION = '1.0.0'

function mountNotice() {
  return mount(DisclaimerNotice, { global: { stubs: { teleport: true } } })
}

type Notice = ReturnType<typeof mountNotice>

function dialogOf(wrapper: Notice) {
  const dialog = wrapper.findAllComponents({ name: 'ElDialog' })[0]
  if (!dialog) throw new Error('没有渲染出 ElDialog')
  return dialog
}

/**
 * 弹窗是否在展示。
 *
 * Element Plus 关闭后只是隐藏、并不销毁正文，所以「弹不弹」看 `modelValue` 这个开关。
 */
function showsDialog(wrapper: Notice): boolean {
  return dialogOf(wrapper).props('modelValue') === true
}

beforeEach(() => {
  localStorage.clear()
})

describe('首次启动免责提示', () => {
  it('localStorage 为空时弹窗出现，三段文案都在', async () => {
    const wrapper = mountNotice()
    await flushPromises()

    expect(showsDialog(wrapper)).toBe(true)
    const text = wrapper.text()
    expect(text).toContain('使用前请知悉')
    expect(text).toContain('不是法律意见，也不构成律师与客户的关系')
    expect(text).toContain('涉及重大权益的事项，请咨询执业律师')
    expect(text).toContain('你上传的合同正文会发送到你在设置页配置的模型服务商用于分析')
    const confirm = wrapper.findAll('button').find((node) => node.text().includes('我知道了'))
    expect(confirm, '弹窗应有「我知道了」').toBeDefined()
  })

  it('点「我知道了」写入确认版本号，立即关闭且重挂不再弹', async () => {
    const wrapper = mountNotice()
    await flushPromises()
    expect(showsDialog(wrapper)).toBe(true)

    const confirm = wrapper.findAll('button').find((node) => node.text().includes('我知道了'))
    await confirm?.trigger('click')
    await flushPromises()

    expect(localStorage.getItem(ACK_KEY)).toBe(ACK_VERSION)
    expect(showsDialog(wrapper)).toBe(false)

    wrapper.unmount()
    const remounted = mountNotice()
    await flushPromises()
    expect(showsDialog(remounted)).toBe(false)
  })

  it('关掉弹窗（右上角 ×）同样算已读', async () => {
    const wrapper = mountNotice()
    await flushPromises()
    expect(showsDialog(wrapper)).toBe(true)

    // 点遮罩 / 点 × 都是 Element Plus 往组件发 update:modelValue=false
    dialogOf(wrapper).vm.$emit('update:modelValue', false)
    await flushPromises()

    expect(localStorage.getItem(ACK_KEY)).toBe(ACK_VERSION)
    expect(showsDialog(wrapper)).toBe(false)
  })

  it('存储里已有当前版本号：直接不弹', async () => {
    localStorage.setItem(ACK_KEY, ACK_VERSION)

    const wrapper = mountNotice()
    await flushPromises()

    expect(showsDialog(wrapper)).toBe(false)
  })

  it('存储不可用（隐私模式）：仍然提示，关闭后同一实例不再弹，重挂会再弹', async () => {
    const denied = () => {
      throw new Error('storage denied')
    }
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(denied)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(denied)

    const wrapper = mountNotice()
    await flushPromises()

    // 隐私模式不等于免告知：首次挂载照样弹
    expect(showsDialog(wrapper)).toBe(true)

    dialogOf(wrapper).vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(showsDialog(wrapper)).toBe(false)

    wrapper.unmount()
    // 内存标记随实例消失：同一页面会话内换实例（等价于刷新）会再提示一次
    const remounted = mountNotice()
    await flushPromises()
    expect(showsDialog(remounted)).toBe(true)
  })
})
