/**
 * 项目模块端到端：网格与状态筛选、搜索、新建（草稿）、详情浮窗、编辑与删除。
 */
import { expect, test } from '@playwright/test'
import {
  collectRuntimeErrors,
  expectNoRuntimeErrors,
  projectCard,
  seedProject,
  seedTask,
  web,
} from './helpers'

test.describe('项目模块', () => {
  test('状态胶囊带真实数量，切换只显示对应状态的项目；已删除单独成档', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'chip')

    await page.goto('/projects')
    await expect(page.getByTestId('project-grid')).toBeVisible()
    await expect(page.getByTestId('project-summary')).toContainText('个项目')

    // 新建项目先落草稿：草稿档里能看到它
    const draft = await request.post('/api/web/v1/projects', {
      headers: { 'Idempotency-Key': `e2e-draft-${Date.now()}` },
      data: { title: `E2E 草稿项目 ${Date.now()}`, summary: '草稿档校验' },
    })
    expect(draft.ok()).toBeTruthy()
    const draftBody = (await draft.json()) as { data: { title: string } }

    await page.reload()
    await page.locator('[data-project-filter="DRAFT"]').click()
    await expect(projectCard(page, draftBody.data.title)).toBeVisible({ timeout: 20_000 })

    await page.locator('[data-project-filter="ACTIVE"]').click()
    await expect(projectCard(page, project.title)).toBeVisible({ timeout: 20_000 })
    await expect(projectCard(page, draftBody.data.title)).toHaveCount(0)

    // 搜索支持标题与简介
    await page.getByTestId('project-search').fill(project.title)
    await expect(page.locator('article.project-card')).toHaveCount(1)
    await page.getByTestId('project-search').fill('绝不可能命中的关键词')
    await expect(page.locator('.empty-projects')).toBeVisible()
    await page.getByRole('button', { name: '清空筛选' }).click()
    await expect(page.locator('article.project-card').first()).toBeVisible()

    expectNoRuntimeErrors(errors)
  })

  test('新建项目：写入服务端并出现在网格里（草稿）', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    const title = `E2E 新建项目 ${Date.now()}`
    await page.goto('/projects')

    await page.getByTestId('new-project').click()
    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await page.getByTestId('project-form-title').fill(title)
    await page.getByTestId('project-form-summary').fill('由内置浏览器创建')
    await page.getByTestId('project-form-background').fill('背景：验证新建项目表单可以真正写入服务端。')
    // 选第二套配色与第 5 个 emoji（本机外观偏好）
    await sheet.locator('.cover-swatch').nth(1).click()
    await sheet.locator('.emoji-option').nth(4).click()
    await expect(sheet.locator('.cover-preview strong')).toHaveText(title)
    await page.getByTestId('project-form-submit').click()
    await expect(page.locator('.modal-layer')).toHaveCount(0)

    const card = projectCard(page, title)
    await expect(card).toBeVisible({ timeout: 20_000 })
    await expect(card.locator('.status-pill')).toContainText('草稿')
    await expect(page.getByTestId('toast')).toContainText('已创建')

    expectNoRuntimeErrors(errors)
  })

  test('项目详情浮窗：背景 / 文档 / 成员 / 进度 / 生命周期操作都在，编辑可保存', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'detail')
    await seedTask(request, { projectId: project.id, title: `项目任务 ${Date.now()}` })

    await page.goto('/projects')
    const card = projectCard(page, project.title)
    await expect(card).toBeVisible({ timeout: 20_000 })
    await card.click()

    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await expect(sheet.locator('#project-modal-title')).toHaveText(project.title)
    await expect(sheet).toContainText('项目背景')
    await expect(sheet).toContainText('相关文档')
    await expect(sheet).toContainText('团队成员')
    await expect(sheet).toContainText('项目操作')
    await expect(sheet.locator('.side-card')).toHaveCount(4)
    // 播种出来的项目是「进行中」：可用动作是结项 / 归档 / 删除
    await expect(sheet.getByRole('button', { name: '结项' })).toBeVisible()
    await expect(sheet.getByRole('button', { name: '归档项目' })).toBeVisible()
    await expect(sheet.getByTestId('project-delete')).toBeVisible()

    // 编辑项目：改简介并保存
    await page.getByTestId('project-edit').click()
    const form = page.locator('.modal-layer .sheet').last()
    await expect(form.getByRole('heading', { name: '编辑项目' })).toBeVisible()
    await page.getByTestId('project-form-summary').fill('内置浏览器改过的简介')
    await page.getByTestId('project-form-submit').click()
    await expect(page.getByTestId('toast')).toContainText('已保存', { timeout: 20_000 })

    const detail = await request.get(`/api/web/v1/projects/${project.id}`)
    const body = (await detail.json()) as { data: { summary: string } }
    expect(body.data.summary).toBe('内置浏览器改过的简介')

    expectNoRuntimeErrors(errors)
  })

  test('「只看这个项目的任务」跳到任务模块并预先筛上该项目', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'jump')

    await page.goto('/projects')
    await projectCard(page, project.title).click()
    await page.getByTestId('project-view-tasks').click()

    // 跳到首页任务模块并把项目写进 URL（可分享/可回退）
    await expect(page).toHaveURL(new RegExp(`[?&]project=${project.id}`))
    await expect(page.getByTestId('task-board')).toBeVisible()
    await expect(page.getByTestId('filter-project')).toHaveValue(project.id)
    await expect(page.locator('.board-ident strong')).toHaveText(project.title)

    expectNoRuntimeErrors(errors)
  })

  test('删除项目：软删除后进入「已删除」档，恢复后回到在用状态', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'delete')

    await test.step('打开项目详情', async () => {
      await page.goto('/projects')
      // 项目数量很多：先用搜索把网格收窄到这一个项目，交互更稳、更快
      await page.getByTestId('project-search').fill(project.title)
      await expect(page.locator('article.project-card')).toHaveCount(1)
      await projectCard(page, project.title).click()
      await expect(page.locator('.modal-layer .sheet')).toBeVisible()
    })

    await test.step('填原因并确认删除', async () => {
      await page.getByTestId('project-delete').click()
      const dialog = page.getByTestId('base-dialog')
      await expect(dialog).toContainText('删除项目')
      await page.getByTestId('project-delete-reason').fill('端到端用例：验证软删除与恢复')
      await page.getByTestId('project-delete-confirm').click()
      await expect(dialog).toHaveCount(0, { timeout: 20_000 })
      await expect(page.getByTestId('toast')).toContainText('已删除')
    })

    await test.step('服务端已软删除', async () => {
      const deleted = await request.get(`/api/web/v1/projects/${project.id}`)
      expect(((await deleted.json()) as { data: { deleted_at: string | null } }).data.deleted_at).not.toBeNull()
    })

    await test.step('已删除档里能找到', async () => {
      await page.getByTestId('project-search').fill('')
      await page.locator('[data-project-filter="deleted"]').click()
      await page.getByTestId('project-search').fill(project.title)
      await expect(projectCard(page, project.title)).toBeVisible({ timeout: 20_000 })
    })

    expectNoRuntimeErrors(errors)
  })

  test('已删除项目可以在详情里恢复', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'restore')
    await web(request, 'POST', `/projects/${project.id}/delete`, {
      reason: '端到端用例：先删除再恢复',
      expected_version: project.version,
    })

    await page.goto('/projects')
    await page.locator('[data-project-filter="deleted"]').click()
    await page.getByTestId('project-search').fill(project.title)
    await expect(projectCard(page, project.title)).toBeVisible({ timeout: 20_000 })

    await projectCard(page, project.title).click()
    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await sheet.getByRole('button', { name: /恢复/ }).first().click()
    await expect(page.getByTestId('toast')).toContainText('现在', { timeout: 20_000 })

    const restored = await request.get(`/api/web/v1/projects/${project.id}`)
    expect(((await restored.json()) as { data: { deleted_at: string | null } }).data.deleted_at).toBeNull()

    expectNoRuntimeErrors(errors)
  })
})
