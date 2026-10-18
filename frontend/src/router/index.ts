/**
 * 路由表。
 *
 * 首页只有两个核心模块（REQ「首页保留只有任务和项目两个核心模块」）：
 * - `/`          任务（默认标签）
 * - `/projects`  项目
 * 二者由同一个 `HomeView` 渲染，顶部胶囊标签在两者间切换。
 *
 * 全局看板 / Agent 管理 / 活动保留为二级页面，入口在顶栏「更多」菜单里；
 * 旧的深链（`/board`、`/projects/:id/board`、`/tasks/:id`）重定向到首页并带上查询参数。
 */
import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

export interface NavEntry {
  /** 导航显示文案。 */
  label: string
  /** 目标路由。 */
  to: string
  /** 该入口所属分组（顶栏「更多」菜单按此分组）。 */
  group: string
}

/** 二级页面入口（首页的两个核心模块由标签切换，不在此列）。 */
export const NAV_ENTRIES: readonly NavEntry[] = [
  { label: '全局看板', to: '/dashboard', group: '概览' },
  { label: 'Agent 管理', to: '/agents', group: '管理' },
  { label: '活动日志', to: '/activity', group: '审计' },
] as const

export const routes: readonly RouteRecordRaw[] = [
  {
    path: '/',
    name: 'home-tasks',
    component: () => import('@/views/HomeView.vue'),
    props: { tab: 'tasks' },
    meta: { navLabel: '任务', tab: 'tasks' },
  },
  {
    path: '/projects',
    name: 'home-projects',
    component: () => import('@/views/HomeView.vue'),
    props: { tab: 'projects' },
    meta: { navLabel: '项目', tab: 'projects' },
  },
  // 旧深链：项目看板与任务详情都收敛到首页（带上筛选 / 打开详情的查询参数）。
  {
    path: '/board',
    redirect: () => ({ path: '/' }),
  },
  {
    path: '/projects/:id/board',
    redirect: (to) => ({ path: '/', query: { project: String(to.params.id) } }),
  },
  {
    path: '/tasks/:id',
    redirect: (to) => ({ path: '/', query: { task: String(to.params.id) } }),
  },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
    meta: { navLabel: '全局看板' },
  },
  {
    path: '/agents',
    name: 'agents',
    component: () => import('@/views/AgentsView.vue'),
    meta: { navLabel: 'Agent 管理' },
  },
  {
    path: '/activity',
    name: 'activity',
    component: () => import('@/views/ActivityView.vue'),
    meta: { navLabel: '活动日志' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes: [...routes],
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { top: 0 }
  },
})

export default router
