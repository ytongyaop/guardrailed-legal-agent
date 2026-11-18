/**
 * 路由表（规格 006 §3）。
 *
 * 守卫只负责体验：未登录去登录页，普通用户进管理端点去 403 提示页。
 * 真正的边界在 FastAPI 的 `permissions.py`，绕过守卫直接调接口一样会被拒绝。
 *
 * `/check` 与 `/history` 属于批次 5，这里先占位（侧栏分组与旧界面一致，点击不会 404）。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { bootstrapSession, isAdmin, useSession } from '@/stores/session'

const routes: RouteRecordRaw[] = [
  {
    path: '/setup',
    name: 'setup',
    component: () => import('@/views/SetupView.vue'),
    meta: { public: true, layout: 'bare', title: '首次使用' },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true, layout: 'bare', title: '登录' },
  },
  {
    path: '/',
    redirect: '/check',
  },
  {
    path: '/check',
    name: 'check',
    component: () => import('@/components/PendingView.vue'),
    props: {
      title: '合同核对',
      note: '合同核对、三层结果与问答在批次 5 交付。当前版本可先使用企业档案、合同管理与设置。',
    },
    meta: { layout: 'shell', title: '合同核对' },
  },
  {
    path: '/enterprise',
    name: 'enterprise',
    component: () => import('@/views/EnterpriseView.vue'),
    meta: { layout: 'shell', title: '企业档案' },
  },
  {
    path: '/contracts',
    name: 'contracts',
    component: () => import('@/views/ContractsView.vue'),
    meta: { layout: 'shell', title: '合同管理' },
  },
  {
    path: '/history',
    name: 'history',
    component: () => import('@/components/PendingView.vue'),
    props: {
      title: '历史记录',
      note: '历史列表、筛选、趋势与删除在批次 5 交付。',
    },
    meta: { layout: 'shell', title: '历史记录' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { layout: 'shell', title: '设置' },
  },
  {
    path: '/admin/users',
    name: 'admin-users',
    component: () => import('@/views/admin/AdminUsersView.vue'),
    meta: { layout: 'shell', admin: true, title: '用户与角色' },
  },
  {
    path: '/admin/statutes',
    name: 'admin-statutes',
    component: () => import('@/views/admin/AdminStatutesView.vue'),
    meta: { layout: 'shell', admin: true, title: '法源库' },
  },
  {
    path: '/admin/rules',
    name: 'admin-rules',
    component: () => import('@/views/admin/AdminRulesView.vue'),
    meta: { layout: 'shell', admin: true, title: '规则表与必问事实' },
  },
  {
    path: '/admin/logs',
    name: 'admin-logs',
    component: () => import('@/views/admin/AdminLogsView.vue'),
    meta: { layout: 'shell', admin: true, title: '操作日志' },
  },
  {
    path: '/admin/evaluation',
    name: 'admin-evaluation',
    component: () => import('@/views/admin/AdminEvaluationView.vue'),
    meta: { layout: 'shell', admin: true, title: '评测' },
  },
  {
    path: '/forbidden',
    name: 'forbidden',
    component: () => import('@/views/ForbiddenView.vue'),
    meta: { public: true, layout: 'shell', title: '无访问权限' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { public: true, layout: 'shell', title: '页面不存在' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  if (to.meta.public === true) return true
  await bootstrapSession()
  const session = useSession()
  if (session.user === null) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.meta.admin === true && !isAdmin()) {
    return { name: 'forbidden' }
  }
  return true
})

router.afterEach((to) => {
  const title = to.meta.title
  document.title = title ? `${title} · 小微企业用工风险辅助系统` : '小微企业用工风险辅助系统'
})
