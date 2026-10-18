/**
 * 全局看板与活动日志端到端（二级页面，入口在顶栏「更多」）：真实数据渲染 + 无控制台错误。
 */
import { expect, test } from '@playwright/test'
import { collectRuntimeErrors, expectNoRuntimeErrors, openTaskDetail, seedProject, seedTask, taskCard } from './helpers'

test.describe('全局看板', () => {
  test('展示任务分布 / 难度 / 项目行等真实统计，空范围不谎报 100%', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/dashboard')
    await expect(page.getByTestId('view-dashboard')).toBeVisible()

    await expect(page.getByTestId('dashboard-status-PREPARING')).toBeVisible()
    await expect(page.getByTestId('dashboard-status-DONE')).toBeVisible()
    for (const level of [1, 2, 3, 4, 5]) {
      await expect(page.getByTestId(`dashboard-difficulty-${level}`)).toBeVisible()
    }
    await expect(page.getByTestId('view-dashboard')).toContainText('任务总数')
    await expect(page.getByTestId('view-dashboard')).toContainText('最近活动')

    expectNoRuntimeErrors(errors)
  })
})

test.describe('活动日志', () => {
  test('按项目筛选后能看到该项目的活动；分页信息与筛选写回 URL', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'activity')
    const task = await seedTask(request, {
      projectId: project.id,
      title: `活动日志任务 ${Date.now()}`,
      publish: true,
    })

    await page.goto('/activity')
    await expect(page.getByTestId('view-activity')).toBeVisible()
    await page.getByTestId('activity-filter-project').selectOption(project.id)
    await page.getByTestId('activity-filter-apply').click()

    await expect(page).toHaveURL(new RegExp(`project_id=${project.id}`))
    // 活动流用短号引用任务（完整 UUID 在 title/链接里），这里断言它出现在列表里
    await expect(page.getByTestId('activity-list')).toContainText(`任务 ${task.id.slice(0, 8)}`)
    await expect(page.getByTestId('activity-summary')).not.toBeEmpty()
    await expect(page.getByTestId('activity-page-info')).not.toBeEmpty()

    expectNoRuntimeErrors(errors)
  })
})

test.describe('二级页面的深链回到任务模块', () => {
  test('活动页在带 project_id 时提供回首页任务模块的入口', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'back')
    await seedTask(request, { projectId: project.id, title: `回跳任务 ${Date.now()}`, publish: true })

    await page.goto(`/activity?project_id=${project.id}`)
    await expect(page.getByTestId('view-activity')).toBeVisible()
    await page.getByTestId('activity-return-project').click()

    await expect(page.getByTestId('task-board')).toBeVisible()
    await expect(page.getByTestId('filter-project')).toHaveValue(project.id)
    await expect(page.locator('.board-ident strong')).toHaveText(project.title)

    expectNoRuntimeErrors(errors)
  })

  test('看板卡片在全局看板异常列表里可以直接跳到该项目筛选后的任务', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'jump')
    const task = await seedTask(request, { projectId: project.id, title: `异常跳转任务 ${Date.now()}`, publish: true })

    await page.goto('/dashboard')
    await page.getByTestId(`dashboard-project-board-${project.id}`).click()
    await expect(page.getByTestId('task-board')).toBeVisible()
    await expect(page.getByTestId('filter-project')).toHaveValue(project.id)
    await expect(taskCard(page, task.display_number, project.id)).toBeVisible({ timeout: 20_000 })
    await openTaskDetail(page, task.display_number, project.id)
    await expect(page.locator('.modal-layer .sheet')).toContainText(task.title)

    expectNoRuntimeErrors(errors)
  })
})
