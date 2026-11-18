import 'vue-router'

declare module 'vue-router' {
  interface RouteMeta {
    /** 公开页：不需要登录即可访问（初始化、登录、403/404 提示页）。 */
    public?: boolean
    /** 管理端页：只有管理员进得去（前端只做体验，后端 403 才是边界）。 */
    admin?: boolean
    /** `bare` 表示不套侧栏外壳（登录、初始化）。 */
    layout?: 'bare' | 'shell'
    /** 页面标题，仅用于 document.title。 */
    title?: string
  }
}

export {}
