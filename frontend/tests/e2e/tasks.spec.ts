/**
 * 任务模块端到端：默认全部项目、多种筛选、卡片详情浮窗、以及真实的任务流转
 * （准备中 → 发布 → 领取 → 提交 → 审核），全部通过界面或真实 API 完成。
 */
import { expect, test } from '@playwright/test'
import {
  collectRuntimeErrors,
  expectNoRuntimeErrors,
  claimViaAgentApi,
  getTask,
  seedProject,
  seedTask,
  submitViaAgentApi,
  taskCard,
  uniqueKey,
} from './helpers'

test.describe('任务模块 · 筛选', () => {
  test('按项目 / 难度 / 关键词筛选，并可一键清空', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'filter')
    const rare = `独特关键词${Date.now()}`
    const task = await seedTask(request, {
      projectId: project.id,
      title: `筛选用任务 ${rare}`,
      difficulty: 4,
      labels: ['筛选'],
    })
    await seedTask(request, { projectId: project.id, title: '同项目的另一张卡', difficulty: 1 })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    await expect(page.getByTestId('filter-project')).toHaveValue(project.id)
    await expect(page.locator('.board-ident strong')).toHaveText(project.title)
    await expect(page.locator('.board-ident small')).toContainText('已筛选')
    await expect(taskCard(page, task.display_number, project.id)).toBeVisible({ timeout: 20_000 })

    // 关键词筛选（URL 不发空 q；这里验证命中与清空）
    await page.getByTestId('task-search').fill(rare)
    await expect(page.locator('.task-card')).toHaveCount(1)
    await expect(taskCard(page, task.display_number, project.id)).toBeVisible()

    // 难度筛选叠加：L4 命中，改成 L1 后只剩另一张卡
    await page.getByTestId('task-search').fill('')
    await page.getByTestId('filter-difficulty').selectOption('4')
    await expect(page.locator('.task-card')).toHaveCount(1)
    await page.getByTestId('filter-difficulty').selectOption('1')
    await expect(page.locator('.task-card')).toHaveCount(1)
    await expect(page.locator('.task-card .priority')).toHaveText(['☁️ L1'])

    await page.getByTestId('clear-filters').click()
    await expect(page.getByTestId('filter-project')).toHaveValue('')
    await expect(page.locator('.board-ident strong')).toHaveText('全部任务')
    await expect(page.getByTestId('clear-filters')).toHaveCount(0)

    expectNoRuntimeErrors(errors)
  })

  test('按执行者头像快速筛选，再次点击恢复全部', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'people')
    const task = await seedTask(request, {
      projectId: project.id,
      title: `指定执行者的任务 ${Date.now()}`,
      claimMode: 'ASSIGNED',
      designatedAgentId: project.worker.id,
    })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    await expect(taskCard(page, task.display_number, project.id)).toBeVisible({ timeout: 20_000 })

    // 头像筛选的候选来自 Agent 名录（按页取完，可能晚于首屏）：先等它出现
    const pills = page.locator('.people-filter .p-filter:not(.all)')
    await expect(pills.first()).toBeVisible({ timeout: 20_000 })
    await pills.first().click()
    await expect(page.locator('.board-ident small')).toContainText('已筛选')
    await expect(page.getByTestId('clear-filters')).toBeVisible()

    await page.getByTestId('filter-agent-all').click()
    await expect(page.getByTestId('filter-agent-all')).toHaveAttribute('aria-pressed', 'true')

    expectNoRuntimeErrors(errors)
  })
})

test.describe('任务模块 · 拖拽', () => {
  /** 在浏览器里派发一次原生 HTML5 拖拽（dragstart → dragenter/dragover → drop → dragend）。 */
  async function dragCardToColumn(page: import('@playwright/test').Page, taskId: string, column: string, position: 'top' | 'bottom' = 'top') {
    return page.evaluate(({ taskId: id, column: target, position: where }) => {
      const src = document.querySelector(`article.task-card[data-task-id="${id}"]`)
      const list = document.querySelector(`.column[data-column="${target}"] .task-list`)
      if (!src || !list) throw new Error(`拖拽目标缺失：${id} / ${target}`)
      const dt = new DataTransfer()
      const rect = list.getBoundingClientRect()
      // 列表是可滚动容器：rect.bottom 可能仍落在最后一张卡片中点之上。
      // 放到末尾时用「最后一张卡片的底边」作为参照，才能稳定算出“插到最后”。
      const cards = Array.from(list.querySelectorAll<HTMLElement>('.task-card'))
      const lastCard = cards[cards.length - 1]
      const clientY =
        where === 'top'
          ? (cards[0]?.getBoundingClientRect().top ?? rect.top) + 4
          : (lastCard ? lastCard.getBoundingClientRect().bottom : rect.bottom) - 4
      const at = {
        bubbles: true,
        cancelable: true,
        dataTransfer: dt,
        clientX: rect.left + 30,
        clientY,
      }
      src.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: dt }))
      list.dispatchEvent(new DragEvent('dragenter', at))
      list.dispatchEvent(new DragEvent('dragover', at))
      list.dispatchEvent(new DragEvent('drop', at))
      src.dispatchEvent(new DragEvent('dragend', { bubbles: true, cancelable: true, dataTransfer: dt }))
    }, { taskId, column, position })
  }

  test('拖到下一列 = 发布；非法列被拒绝；列内拖动会保存顺序', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'drag')
    const first = await seedTask(request, { projectId: project.id, title: `拖拽甲 ${Date.now()}` })
    // 第二张卡用于列内排序：它已发布，保证待办列有两张卡
    await seedTask(request, {
      projectId: project.id,
      title: `拖拽乙 ${Date.now()}`,
      publish: true,
    })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    await expect(taskCard(page, first.display_number, project.id)).toBeVisible({ timeout: 20_000 })

    // 1) 准备中 → 待办：publish 不需要额外输入，直接生效
    await dragCardToColumn(page, first.id, 'TODO')
    await expect(taskCard(page, first.display_number, project.id)).toHaveAttribute('data-status', 'TODO', { timeout: 20_000 })
    await expect(page.getByTestId('toast')).toContainText('已发布')

    // 2) 待办 → 已完成是非法转换：拒绝并保持原状态
    await dragCardToColumn(page, first.id, 'DONE')
    await expect(page.getByTestId('toast')).toContainText('非法转换', { timeout: 10_000 })
    await expect(taskCard(page, first.display_number, project.id)).toHaveAttribute('data-status', 'TODO')

    // 3) 列内排序：把当前第一张拖到末尾，顺序应反转（服务端 sort_order 生效）
    const todoCards = page.locator('.column[data-column="TODO"] .task-card')
    await expect(todoCards).toHaveCount(2)
    const orderBefore = await todoCards.evaluateAll((els) => els.map((el) => el.getAttribute('data-display-number')))
    const topCardId = await todoCards.first().getAttribute('data-task-id')
    expect(topCardId).toBeTruthy()

    await dragCardToColumn(page, topCardId!, 'TODO', 'bottom')
    await expect
      .poll(
        async () => todoCards.evaluateAll((els) => els.map((el) => el.getAttribute('data-display-number'))),
        { timeout: 15_000 },
      )
      .toEqual([orderBefore[1], orderBefore[0]])

    // 4) 待办 → 进行中需要选代领 Agent：拖过去直接打开领取表单
    await dragCardToColumn(page, first.id, 'IN_PROGRESS')
    await expect(page.getByTestId('base-dialog')).toBeVisible()
    await expect(page.getByTestId('claim-title')).toBeVisible()
    await page.getByTestId('claim-cancel').click()

    expectNoRuntimeErrors(errors)
  })
})

test.describe('任务模块 · 创建与流转', () => {
  test('新建任务：直接发布前必须先填任务要求与验收标准', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'create')

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    await page.getByTestId('new-task').click()
    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await expect(sheet.getByRole('heading', { name: '种一张新卡片' })).toBeVisible()

    // 只填标题 → 拒绝发布并给出可执行的原因
    await page.getByTestId('task-form-title').fill('缺验收标准的任务')
    await page.getByTestId('task-form-submit').click()
    await expect(page.getByTestId('task-form-error')).toContainText('任务要求与验收标准')

    // 补齐后创建并直接发布到待办
    const title = `端到端新建任务 ${Date.now()}`
    await page.getByTestId('task-form-title').fill(title)
    await page.getByTestId('task-form-description').fill('要求：由内置浏览器创建。')
    await page.getByTestId('task-form-acceptance').fill('验收：出现在待办列。')
    await page.getByTestId('task-form-labels').fill('验收, 前端')
    await page.getByTestId('task-form-submit').click()
    await expect(page.locator('.modal-layer')).toHaveCount(0)

    const todoColumn = page.locator('.column[data-column="TODO"]')
    await expect(todoColumn.locator('.task-card', { hasText: title })).toBeVisible({ timeout: 20_000 })

    expectNoRuntimeErrors(errors)
  })

  test('完整流转：发布 → Agent 领取 → 提交 → 浏览器审核通过', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'lifecycle')
    const task = await seedTask(request, {
      projectId: project.id,
      title: `流转任务 ${Date.now()}`,
      publish: true,
    })
    expect(task.status).toBe('TODO')

    // 真实 Agent 领取并提交，构造待验收样本
    const claim = await claimViaAgentApi<{ task_id: string; execution_id: string; version: number }>(
      request,
      project.worker.agent_key!,
    )
    expect(claim.task_id).toBe(task.id)
    const claimed = await getTask(request, task.id)
    await submitViaAgentApi(request, project.worker.agent_key!, {
      taskId: task.id,
      executionId: claim.execution_id,
      expectedVersion: Number(claimed.version),
    })
    const submitted = await getTask(request, task.id)
    expect(submitted.status).toBe('IN_REVIEW')

    // 浏览器侧打开待验收卡片 → 审核通过
    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    const card = taskCard(page, task.display_number, project.id)
    await expect(card).toBeVisible({ timeout: 20_000 })
    expect(await card.getAttribute('data-status')).toBe('IN_REVIEW')
    await card.click()

    await page.getByTestId('task-action-review').click()
    const reviewDialog = page.getByTestId('base-dialog')
    await expect(reviewDialog).toBeVisible()
    await expect(page.getByTestId('review-submission-summary')).toContainText('Agent 侧提交的交付')
    // Agent 目录按页加载：等表单自动选中可用主管后再提交
    await expect(page.getByTestId('review-reviewer')).not.toHaveValue('', { timeout: 20_000 })
    await page.getByTestId('review-decision-approve').click()
    await page.getByTestId('review-comment').fill('内置浏览器审核通过')
    await page.getByTestId('review-submit').click()
    await expect(reviewDialog).toHaveCount(0, { timeout: 20_000 })

    const final = await getTask(request, task.id)
    expect(final.status).toBe('DONE')
    await expect(taskCard(page, task.display_number, project.id)).toHaveAttribute('data-status', 'DONE', { timeout: 20_000 })

    expectNoRuntimeErrors(errors)
  })

  test('发布与归档都能在详情浮窗里完成，并即时反映到看板', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'publish')
    const task = await seedTask(request, {
      projectId: project.id,
      title: `待发布任务 ${Date.now()}`,
    })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    const card = taskCard(page, task.display_number, project.id)
    await expect(card).toBeVisible({ timeout: 20_000 })
    await card.click()
    await page.getByTestId('task-action-publish').click()
    // 快捷动作完成后浮窗会自行关闭（不叠在按钮上等）
    await expect(page.locator('.modal-layer')).toHaveCount(0, { timeout: 20_000 })
    await expect(taskCard(page, task.display_number, project.id)).toHaveAttribute('data-status', 'TODO', { timeout: 20_000 })

    // 归档：需要填写原因
    await taskCard(page, task.display_number, project.id).click()
    await page.getByTestId('task-action-archive').click()
    const archiveDialog = page.getByTestId('base-dialog')
    await expect(archiveDialog).toBeVisible()
    await page.getByTestId('reason-input').fill('端到端用例归档')
    await page.getByTestId('reason-submit').click()
    await expect(archiveDialog).toHaveCount(0, { timeout: 20_000 })

    const archived = await getTask(request, task.id)
    expect(archived.is_archived).toBe(true)

    expectNoRuntimeErrors(errors)
  })

  test('修改领取安排：切换为指定 Agent 后任务落到该项目成员身上', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'assign')
    const task = await seedTask(request, {
      projectId: project.id,
      title: `改指派任务 ${Date.now()}`,
      publish: true,
    })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    const card = taskCard(page, task.display_number, project.id)
    await expect(card).toBeVisible({ timeout: 20_000 })
    await card.click()
    await page.getByTestId('task-action-assign').click()

    const assignDialog = page.getByTestId('base-dialog')
    await expect(assignDialog).toBeVisible()
    await page.getByTestId('assign-claim-mode').selectOption('ASSIGNED')
    await page.getByTestId('assign-designated').selectOption(project.worker.id)
    await page.getByTestId('assign-submit').click()
    await expect(assignDialog).toHaveCount(0, { timeout: 20_000 })

    const assigned = await getTask(request, task.id)
    expect(assigned.claim_mode).toBe('ASSIGNED')
    expect(assigned.designated_agent_id).toBe(project.worker.id)

    expectNoRuntimeErrors(errors)
  })

  test('清空筛选后回到全部项目视图（不残留任何筛选条件）', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const project = await seedProject(request, 'reset')
    await seedTask(request, { projectId: project.id, title: `重置用任务 ${uniqueKey('t')}` })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    await page.getByTestId('filter-claim-mode').selectOption('OPEN')
    await expect(page.locator('.board-ident small')).toContainText('已筛选')

    await page.getByTestId('clear-filters').click()
    await expect(page.getByTestId('filter-project')).toHaveValue('')
    await expect(page.getByTestId('filter-claim-mode')).toHaveValue('')
    await expect(page.getByTestId('filter-archived')).toHaveAttribute('aria-pressed', 'false')

    expectNoRuntimeErrors(errors)
  })
})
