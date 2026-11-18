import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { defineConfig } from 'vite'

// Element Plus 按需引入：模板里写 <el-table> 时由插件把组件与其样式按需引入，
// 避免 `import 'element-plus'` 全量打包（规格 006「前端构建链与体积」）。
// `components.d.ts` 是插件生成的类型声明，随仓库提交，`vue-tsc` 靠它识别全局组件。
export default defineConfig({
  plugins: [
    vue(),
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css' })],
      dts: 'src/components.d.ts',
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,
    // 开发态把 /api 代理到 FastAPI（006 §1）。**changeOrigin 必须是 false**：
    // 后端 `_same_origin` 比较 Origin 与 Host，代理若改写 Host，浏览器带来的
    // Origin(http://localhost:5173) 就会与 Host 不一致而被 403 拒绝。
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
