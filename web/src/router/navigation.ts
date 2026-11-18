/**
 * 侧栏导航（规格 006 §3：保留旧界面的 NAV 分组）。
 *
 * 分组只有「工作台 / 管理 / 管理端」，管理端整组仅管理员可见。
 * 这里只决定**显示什么**；能不能访问由后端 403/404 说了算。
 */

export interface NavItem {
  path: string
  label: string
}

export interface NavGroup {
  key: string
  label: string
  items: NavItem[]
}

export function navigationGroups(admin: boolean): NavGroup[] {
  const groups: NavGroup[] = [
    {
      key: 'workbench',
      label: '工作台',
      items: [{ path: '/check', label: '合同核对' }],
    },
    {
      key: 'manage',
      label: '管理',
      items: [
        { path: '/enterprise', label: '企业档案' },
        { path: '/contracts', label: '合同管理' },
        { path: '/history', label: '历史记录' },
        { path: '/settings', label: '设置' },
      ],
    },
  ]
  if (admin) {
    groups.push({
      key: 'admin',
      label: '管理端',
      items: [
        { path: '/admin/users', label: '用户与角色' },
        { path: '/admin/statutes', label: '法源库' },
        { path: '/admin/rules', label: '规则表' },
        { path: '/admin/logs', label: '操作日志' },
        { path: '/admin/evaluation', label: '评测' },
      ],
    })
  }
  return groups
}
