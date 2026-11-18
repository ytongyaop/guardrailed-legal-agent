import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig } from 'vitest/config'

// 测试态同样启用按需组件解析：组件测试里用到的 <el-*> 与生产构建走同一条解析路径，
// 不会出现「构建能过、测试里组件是未知标签」的两套行为。
export default defineConfig({
  plugins: [
    vue(),
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.spec.ts'],
    setupFiles: ['tests/setup.ts'],
    restoreMocks: true,
    // 首次 import 路由组件要加载 Element Plus（含表格/下拉/表单），jsdom 下比生产慢一个数量级；
    // 批次 5 的 `/check` 引入了结果页与问答组件，首次导航解析路由组件要几十秒，故放宽到 45s。
    testTimeout: 45000,
    // Element Plus 的组件样式走 `theme-chalk/*.css`：必须让 Vite 处理 node_modules 里的
    // Element Plus，否则 Node 的 ESM loader 会直接读 .css 并报 ERR_UNKNOWN_FILE_EXTENSION。
    server: {
      deps: {
        inline: ['element-plus'],
      },
    },
  },
})
