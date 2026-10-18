/**
 * 首页冒烟：任务与项目两个核心模块、主题切换、二级页面入口，且全程无 console 错误。
 *
 * 真实服务：所有数据由真实 WebAPI 现场播种，页面走真实前端 dev server。
 */
import { expect, test } from '@playwright/test'
import { collectRuntimeErrors, expectNoRuntimeErrors, seedProject, seedTask, taskCard } from './helpers'

test.describe('首页两个核心模块', () => {
  test('默认进入任务模块，展示全部项目的任务看板', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/')

    await expect(page.getByTestId('view-home-tasks')).toBeVisible()
    await expect(page.getByTestId('tab-tasks')).toHaveAttribute('aria-selected', 'true')
    // 五列看板：准备中 / 待办 / 进行中 / 待验收 / 已完成
    const board = page.getByTestId('task-board')
    await expect(board.locator('.column')).toHaveCount(5)
    await expect(board.locator('.column .col-name h2')).toHaveText(['准备中', '待办', '进行中', '待验收', '已完成'])
    // 默认不限定项目：作用域显示「全部任务」
    await expect(page.locator('.board-ident strong')).toHaveText('全部任务')
    await expect(page.locator('.board-ident small')).toContainText('张卡片')

    expectNoRuntimeErrors(errors)
  })

  test('顶部标签在任务与项目之间切换，且都写进 URL', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/')

    await page.getByTestId('tab-projects').click()
    await expect(page).toHaveURL(/\/projects$/)
    await expect(page.getByTestId('view-home-projects')).toBeVisible()
    await expect(page.getByTestId('project-grid')).toBeVisible()

    await page.getByTestId('tab-tasks').click()
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByTestId('task-board')).toBeVisible()

    expectNoRuntimeErrors(errors)
  })

  test('旧深链重定向到首页', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/board')
    await expect(page).toHaveURL(/\/$/)
    await expect(page.getByTestId('task-board')).toBeVisible()
    expectNoRuntimeErrors(errors)
  })

  test('点击任务卡片弹出任务详情浮窗（要求 / 验收标准 / 执行信息 / 附件 / 时间线）', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'detail')
    const task = await seedTask(request, {
      projectId: project.id,
      title: `详情浮窗任务 ${Date.now()}`,
      description: '需求：点开卡片能看到要求原文。',
      acceptance: '验收：浮窗里能看到验收标准原文。',
      labels: ['前端'],
    })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    const card = taskCard(page, task.display_number, project.id)
    await expect(card).toBeVisible({ timeout: 20_000 })
    await card.click()

    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await expect(sheet.locator('#task-detail-title')).toHaveText(task.title)
    await expect(sheet.locator('.modal-overline')).toContainText(task.display_number)
    await expect(sheet.locator('.task-detail-badges')).toContainText('准备中')
    await expect(sheet).toContainText('需求：点开卡片能看到要求原文。')
    await expect(sheet).toContainText('验收：浮窗里能看到验收标准原文。')
    await expect(sheet.locator('.side-label')).toHaveText([
      'PROJECT · 所属项目',
      'ASSIGNEE · 执行者',
      'DEADLINE · 计划截止',
      'HISTORY · 执行与提交',
    ])
    await expect(sheet).toContainText('活动时间线')

    expectNoRuntimeErrors(errors)
  })

  test('主题切换：浅色 ↔ 深色，重新加载后保持', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

    await page.getByTestId('theme-toggle').click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await page.reload()
    await expect(page.getByTestId('task-board')).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')

    await page.getByTestId('theme-toggle').click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    await page.reload()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')

    expectNoRuntimeErrors(errors)
  })

  test('「更多」菜单进入全局看板 / Agent 管理 / 活动日志，各页渲染且无控制台错误', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/')

    const targets: Array<{ testId: string; url: RegExp; view: string }> = [
      { testId: 'more-dashboard', url: /\/dashboard$/, view: 'view-dashboard' },
      { testId: 'more-agents', url: /\/agents$/, view: 'view-agents' },
      { testId: 'more-activity', url: /\/activity$/, view: 'view-activity' },
    ]
    for (const target of targets) {
      await page.getByTestId('more-menu').click()
      await page.getByTestId(target.testId).click()
      await expect(page).toHaveURL(target.url)
      await expect(page.getByTestId(target.view)).toBeVisible()
    }

    // 顶栏在二级页面同样存在，点标签回首页
    await page.getByTestId('tab-tasks').click()
    await expect(page.getByTestId('task-board')).toBeVisible()

    expectNoRuntimeErrors(errors)
  })

  test('顶栏计数在二级页面也是真实数字，且离开首页后不被清零', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    // 保证库里至少有一个项目/任务，计数不为 0 才有意义
    const project = await seedProject(request, 'count')
    await seedTask(request, { projectId: project.id, title: `计数用任务 ${Date.now()}` })

    // 冷启动直接进二级页面
    await page.goto('/agents')
    await expect(page.getByTestId('task-tab-count')).not.toHaveText('0', { timeout: 20_000 })
    await expect(page.getByTestId('project-tab-count')).not.toHaveText('0', { timeout: 20_000 })

    // 首页 → 二级页面（首页卸载不应把计数清成 0）
    await page.getByTestId('tab-tasks').click()
    await expect(page.getByTestId('task-board')).toBeVisible()
    await expect(page.getByTestId('task-tab-count')).not.toHaveText('0')
    await page.getByTestId('more-menu').click()
    await page.getByTestId('more-agents').click()
    await expect(page).toHaveURL(/\/agents$/)
    await expect(page.getByTestId('task-tab-count')).not.toHaveText('0')
    await expect(page.getByTestId('project-tab-count')).not.toHaveText('0')

    expectNoRuntimeErrors(errors)
  })

  test('未知路径给出 404 页而不是静默跳首页', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/definitely-not-a-route')
    await expect(page.getByTestId('view-not-found')).toBeVisible()
    expectNoRuntimeErrors(errors)
  })
})
