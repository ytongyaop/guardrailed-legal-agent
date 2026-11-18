/**
 * Playwright 配置（规格 006 批次 5「E2E」）。
 *
 * 两个后端进程各自用独立临时运行目录：
 * - `8021` 注入占位 Key + 打桩模型 → 可以真的跑通核对与三层渲染；
 * - `8022` 不给 Key → `model_configured=false`，用于验证「未配置模型」门禁。
 *
 * 浏览器用本机 **Edge**（`channel: 'msedge'`）：本机下载 Chromium 受网络限制
 * （`npx playwright install chromium` 无法完成），Edge 与 Chromium 内核同源，
 * 因此 E2E 仍跑在真实浏览器里，不是用 jsdom 冒充。
 */

import { fileURLToPath } from 'node:url'
import path from 'node:path'

import { defineConfig } from '@playwright/test'

const WEB_DIR = fileURLToPath(new URL('.', import.meta.url))
const ROOT = path.dirname(WEB_DIR)
const PYTHON = process.env['MLA_PYTHON'] ?? path.join(ROOT, '.venv', 'Scripts', 'python.exe')
const MODEL_URL = 'http://127.0.0.1:8021'
const NO_MODEL_URL = 'http://127.0.0.1:8022'
// 默认用本机 Edge；换机器/换浏览器时用环境变量覆盖（Chromium 需先 `npx playwright install`）
const CHANNEL = process.env['MLA_PLAYWRIGHT_CHANNEL'] ?? 'msedge'

function serverArgs(port: number, home: string, extra = ''): string {
  return `"${PYTHON}" scripts/e2e_server.py --port ${port} --home ${home}${extra}`
}

export default defineConfig({
  testDir: './e2e',
  // 两个后端都是单 worker 单进程（内存会话/任务登记），用例必须串行
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [['list']],
  use: {
    channel: CHANNEL,
    headless: true,
    baseURL: MODEL_URL,
    trace: 'off',
    screenshot: 'only-on-failure',
  },
  webServer: [
    {
      command: serverArgs(8021, '_tmp_e2e/model'),
      url: `${MODEL_URL}/api/setup`,
      cwd: ROOT,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
    },
    {
      command: serverArgs(8022, '_tmp_e2e/no-model', ' --no-model'),
      url: `${NO_MODEL_URL}/api/setup`,
      cwd: ROOT,
      reuseExistingServer: false,
      timeout: 120_000,
      stdout: 'ignore',
      stderr: 'pipe',
    },
  ],
})
