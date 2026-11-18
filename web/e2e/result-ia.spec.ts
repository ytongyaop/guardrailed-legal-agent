/**
 * 端到端用例（规格 006 批次 5：登录→核对→三层结果、过程开关、无模型门禁）。
 *
 * 三个用例都跑在**真实后端进程 + 真实 Edge 浏览器**上（见 `playwright.config.ts`）：
 * - `8021` 注入占位 Key 并打桩模型 → 核对链路完整跑通；
 * - `8022` 没有 Key → 用来验证 409 `NO_MODEL` 与页面门禁。
 *
 * 页面切换一般用侧栏点击（SPA 内跳转）：整页刷新会清掉内存会话，
 * 而「去设置打开过程开关再回来看同一次结果」正是要验证的行为。
 * 内存会话之外的恢复路径另有专项用例：`/check?task_id=…` 刷新后按 URL 重新拉详情。
 */

import { expect, test, type APIRequestContext, type Page } from '@playwright/test'

const MODEL_URL = 'http://127.0.0.1:8021'
const NO_MODEL_URL = 'http://127.0.0.1:8022'
const PASSWORD = 'e2e-pass-123'
const CONTRACT = [
  '第一条 本合同期限为一年，自 2026 年 1 月 1 日起至 2026 年 12 月 31 日止。',
  '第二条 试用期为六个月，试用期工资为转正工资的百分之七十。',
  '第三条 乙方同意放弃社会保险，由甲方每月另行支付补贴。',
  '第四条 乙方自愿放弃加班费。',
].join('\n')

/** 首次初始化管理员；重复运行时 409 视为已初始化（每次运行都换临时运行目录）。 */
async function ensureAdmin(request: APIRequestContext, base: string): Promise<void> {
  const response = await request.post(`${base}/api/setup/admin`, {
    data: {
      username: 'admin',
      password: PASSWORD,
      confirm_password: PASSWORD,
      enterprise_name: 'E2E 企业',
    },
  })
  expect([201, 409]).toContain(response.status())
}

/** 走真实登录页：填账号口令 → 登录 → 等到核对页路由。
 *
 * 注意：Element Plus 的 `el-input` 把透传属性绑定到原生 `<input>` 上，
 * 所以 `getByTestId('login-username')` 命中的就是输入框本身，不能再 `.locator('input')`。
 */
async function signIn(page: Page, base: string): Promise<void> {
  await page.goto(`${base}/login`)
  await page.getByTestId('login-username').fill('admin')
  await page.getByTestId('login-password').fill(PASSWORD)
  await page.getByTestId('login-submit').click()
  await page.waitForURL((url) => url.pathname === '/check')
}

/** 首次登录会弹「开始之前」引导（未确认前每次登录都弹）：先确认掉再操作页面。 */
async function dismissOnboarding(page: Page): Promise<void> {
  const confirm = page.getByRole('button', { name: '我知道了' })
  try {
    await confirm.waitFor({ state: 'visible', timeout: 3_000 })
  } catch {
    return
  }
  await confirm.click()
  await confirm.waitFor({ state: 'hidden' })
}

/** 登录 + 确认输入区已渲染（有模型的那一路）。 */
async function login(page: Page, base: string): Promise<void> {
  await signIn(page, base)
  await dismissOnboarding(page)
  await expect(page.getByTestId('check-form')).toBeVisible()
}

/** 发起一次核对；若服务端提出追问，先「跳过，直接看结论」（跳过会把首轮结果补存）。 */
async function runCheck(page: Page): Promise<void> {
  await page.getByTestId('contract-text').fill(CONTRACT)
  await page.getByTestId('start-check').click()

  const askPanel = page.getByTestId('ask-panel')
  const headline = page.getByTestId('verdict-headline')
  await expect(askPanel.or(headline).first()).toBeVisible({ timeout: 60_000 })
  if (await askPanel.isVisible()) {
    await page.getByRole('button', { name: '跳过，直接看结论' }).click()
  }
  await expect(headline).toBeVisible({ timeout: 60_000 })
}

test('登录 → 核对 → 三层结果：条号常驻、原文折叠、第三层不渲染', async ({ page, request }) => {
  await ensureAdmin(request, MODEL_URL)
  await login(page, MODEL_URL)
  await runCheck(page)

  // 第一层：结论句、结论明细、条目与法名条号常驻
  await expect(page.getByTestId('task-meta')).toContainText('已存入企业档案')
  await expect(page.getByTestId('verdict-headline')).not.toBeEmpty()
  await expect(page.getByTestId('finding-card').first()).toBeVisible()
  await expect(page.getByTestId('statute-label').first()).toContainText('《')

  // 第二层：法条原文默认折叠（关闭的 <details> 内文字对用户不可见）
  const statuteFold = page.getByTestId('statute-fold').first()
  await expect(statuteFold).toBeVisible()
  await expect(statuteFold.locator('.fold-text')).toBeHidden()
  await statuteFold.locator('summary').click()
  await expect(statuteFold.locator('.fold-text')).toBeVisible()

  // 处理建议不折叠；免责声明只出现一次且在最后
  await expect(page.getByTestId('suggestions')).toBeVisible()
  await expect(page.getByTestId('disclaimer')).toHaveCount(1)

  // 第三层默认不存在：不是「渲染后隐藏」
  await expect(page.getByTestId('process-layer')).toHaveCount(0)
  await expect(page.getByText('查询路径与判定来源')).toHaveCount(0)
})

test('过程开关：打开后只增加第三层，第一层不变', async ({ page, request }) => {
  await ensureAdmin(request, MODEL_URL)
  await login(page, MODEL_URL)
  await runCheck(page)

  const headlineBefore = await page.getByTestId('verdict-headline').innerText()
  const detailsBefore = await page.getByTestId('verdict-details').innerText()
  const cardsBefore = await page.getByTestId('finding-card').count()
  await expect(page.getByTestId('process-layer')).toHaveCount(0)

  // 侧栏进设置页 → 打开「显示查询路径与判定来源」
  await page.getByRole('menuitem', { name: '设置' }).click()
  const switchItem = page.locator('.el-form-item').filter({ hasText: '显示查询路径与判定来源' })
  await switchItem.locator('.el-switch').click()
  await expect(page.getByText('界面设置已保存。')).toBeVisible()

  // 回到核对页：SPA 内跳转，结果不重跑，第三层按新开关出现
  await page.getByRole('menuitem', { name: '合同核对' }).click()
  await expect(page.getByTestId('process-layer')).toBeVisible()
  await expect(page.getByTestId('process-layer')).toContainText('查询路径与判定来源')
  await expect(page.getByTestId('process-layer')).toContainText('引用条文')
  await expect(page.getByTestId('process-layer')).toContainText('耗时')
  await expect(page.getByTestId('finding-source').first()).toBeVisible()

  // 第一层逐项不变
  await expect(page.getByTestId('verdict-headline')).toHaveText(headlineBefore)
  await expect(page.getByTestId('verdict-details')).toHaveText(detailsBefore)
  await expect(page.getByTestId('finding-card')).toHaveCount(cardsBefore)

  // 复位开关，避免影响重复运行时的观感
  await page.getByRole('menuitem', { name: '设置' }).click()
  await page.locator('.el-form-item').filter({ hasText: '显示查询路径与判定来源' }).locator('.el-switch').click()
  await expect(page.getByText('界面设置已保存。')).toBeVisible()
})

test('历史列表与只读详情：行可见、结果同一套三层、没有问答面板', async ({ page, request }) => {
  await ensureAdmin(request, MODEL_URL)
  await login(page, MODEL_URL)
  await runCheck(page)

  await page.getByRole('menuitem', { name: '历史记录' }).click()
  await expect(page.getByTestId('history-caption')).toContainText('最近 200 次核对')
  const table = page.getByTestId('history-table')
  await expect(table).toContainText('粘贴合同')

  await table.getByRole('button', { name: '查看' }).first().click()
  await page.waitForURL(/\/history\/T-/)
  await expect(page.getByTestId('verdict-headline')).toBeVisible()
  await expect(page.getByTestId('statute-label').first()).toBeVisible()
  await expect(page.getByTestId('disclaimer')).toHaveCount(1)
  // 只读：历史详情没有追问、可选补充与问答
  await expect(page.getByTestId('qa-panel')).toHaveCount(0)
  await expect(page.getByTestId('ask-panel')).toHaveCount(0)
  await expect(page.getByTestId('optional-facts')).toHaveCount(0)
})

test('未配置模型：页面无输入区，直接调接口得到 409 NO_MODEL', async ({ page, request }) => {
  await ensureAdmin(request, NO_MODEL_URL)
  await signIn(page, NO_MODEL_URL)

  // 门禁提示必须可见，且没有输入区 / 追问 / 结果占位
  await expect(page.getByTestId('no-model-gate')).toBeVisible()
  await expect(page.getByText('未配置模型，合同核对不可用')).toBeVisible()
  await expect(page.getByText('去设置页配置 API Key')).toBeVisible()
  await expect(page.getByTestId('check-form')).toHaveCount(0)
  await expect(page.getByTestId('ask-panel')).toHaveCount(0)
  await expect(page.getByTestId('result-placeholder')).toHaveCount(0)

  // API 侧同一道门禁：不入队、不生成残缺任务
  const me = await page.request.get(`${NO_MODEL_URL}/api/auth/me`)
  expect(me.status()).toBe(200)
  const csrf = (await me.json()) as { csrf_token: string }
  const response = await page.request.post(`${NO_MODEL_URL}/api/checks`, {
    data: { text: CONTRACT, archive: true },
    headers: { 'X-CSRF-Token': csrf.csrf_token },
  })
  expect(response.status()).toBe(409)
  const body = (await response.json()) as { error: { code: string; action: { type: string } | null } }
  expect(body.error.code).toBe('NO_MODEL')
  expect(body.error.action?.type).toBe('open_settings')
})

test('整页刷新：按 URL 里的任务号恢复同一次结果，条目数与过程层都与刷新前一致', async ({ page, request }) => {
  await ensureAdmin(request, MODEL_URL)
  await login(page, MODEL_URL)
  await runCheck(page)

  // 核对完成后 URL 记着本次任务号（`router.replace`，不新增历史记录）
  await expect(page).toHaveURL(/[?&]task_id=T-/)

  const headlineBefore = await page.getByTestId('verdict-headline').innerText()
  const cardsBefore = await page.getByTestId('finding-card').count()
  const processBefore = await page.getByTestId('process-layer').count()
  expect(cardsBefore).toBeGreaterThan(0)

  // 刷新不能重新发起核对（否则等于再花一次模型额度）
  const checkPosts: string[] = []
  page.on('request', (req) => {
    if (req.method() === 'POST' && req.url().includes('/api/checks')) checkPosts.push(req.url())
  })

  await page.reload()

  await expect(page.getByTestId('verdict-headline')).toHaveText(headlineBefore)
  await expect(page.getByTestId('finding-card')).toHaveCount(cardsBefore)
  await expect(page.getByTestId('process-layer')).toHaveCount(processBefore)
  expect(checkPosts).toHaveLength(0)

  // 第二层仍默认折叠；追问已处理过，刷新后不再挡住结果；问答入口还在
  await expect(page.getByTestId('statute-fold').first().locator('.fold-text')).toBeHidden()
  await expect(page.getByTestId('ask-panel')).toHaveCount(0)
  await expect(page.getByTestId('qa-panel')).toBeVisible()

  // 过程开关由服务端决定：打开后刷新，第三层照样出现，条目数不变
  await page.getByRole('menuitem', { name: '设置' }).click()
  await page.locator('.el-form-item').filter({ hasText: '显示查询路径与判定来源' }).locator('.el-switch').click()
  await expect(page.getByText('界面设置已保存。')).toBeVisible()
  await page.getByRole('menuitem', { name: '合同核对' }).click()
  await expect(page.getByTestId('process-layer')).toBeVisible()

  await page.reload()
  await expect(page.getByTestId('process-layer')).toBeVisible()
  await expect(page.getByTestId('verdict-headline')).toHaveText(headlineBefore)
  await expect(page.getByTestId('finding-card')).toHaveCount(cardsBefore)

  // 复位开关，避免影响重复运行时的观感
  await page.getByRole('menuitem', { name: '设置' }).click()
  await page.locator('.el-form-item').filter({ hasText: '显示查询路径与判定来源' }).locator('.el-switch').click()
  await expect(page.getByText('界面设置已保存。')).toBeVisible()
})

test('URL 里的任务号读不到：清掉 query 并提示「该结果不可访问或已删除」', async ({ page, request }) => {
  await ensureAdmin(request, MODEL_URL)
  await login(page, MODEL_URL)

  await page.goto(`${MODEL_URL}/check?task_id=T-NOT-EXIST`)

  await expect(page.getByTestId('restore-notice')).toContainText('该结果不可访问或已删除')
  await expect(page).toHaveURL(/\/check$/)
  await expect(page.getByTestId('result-placeholder')).toBeVisible()
  await expect(page.getByTestId('check-form')).toBeVisible()
})
