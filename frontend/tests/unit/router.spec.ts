/**
 * 路由表测试：首页两个核心模块 + 旧深链重定向 + 二级页面。
 */
import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'
import { NAV_ENTRIES, routes } from '@/router'

function createTestRouter() {
  return createRouter({ history: createMemoryHistory(), routes: [...routes] })
}

describe('导航入口', () => {
  it('首页只有任务与项目两个标签，二级页面单独列在「更多」里', () => {
    expect(NAV_ENTRIES.map((entry) => entry.label)).toEqual(['全局看板', 'Agent 管理', '活动日志'])
    expect(NAV_ENTRIES.map((entry) => entry.to)).toEqual(['/dashboard', '/agents', '/activity'])
  })

  it('每个二级入口都解析到已注册路由', () => {
    const router = createTestRouter()
    const expected: Record<string, string> = { '/dashboard': 'dashboard', '/agents': 'agents', '/activity': 'activity' }
    for (const entry of NAV_ENTRIES) {
      const resolved = router.resolve(entry.to)
      expect(resolved.name).toBe(expected[entry.to])
    }
  })
})

describe('首页路由', () => {
  it('/ 是任务模块（默认标签），/projects 是项目模块', () => {
    const router = createTestRouter()
    expect(router.resolve('/').name).toBe('home-tasks')
    expect(router.resolve('/').meta.tab).toBe('tasks')
    expect(router.resolve('/projects').name).toBe('home-projects')
    expect(router.resolve('/projects').meta.tab).toBe('projects')
  })

  it('旧的看板深链重定向到首页并带上查询参数', () => {
    const boardRoute = routes.find((route) => route.path === '/projects/:id/board')!
    const taskRoute = routes.find((route) => route.path === '/tasks/:id')!
    const boardRedirect = boardRoute.redirect as (to: { params: Record<string, string> }) => unknown
    const taskRedirect = taskRoute.redirect as (to: { params: Record<string, string> }) => unknown
    expect(boardRedirect({ params: { id: 'p-123' } })).toEqual({ path: '/', query: { project: 'p-123' } })
    expect(taskRedirect({ params: { id: 't-9' } })).toEqual({ path: '/', query: { task: 't-9' } })
  })

  it('未知路径走 not-found，不静默渲染首页', () => {
    const router = createTestRouter()
    expect(router.resolve('/definitely-not-a-route').name).toBe('not-found')
  })
})
