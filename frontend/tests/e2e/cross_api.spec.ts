/**
 * 联合验收（T37）：同一条任务上，浏览器与远程 AgentAPI 交替动作，双方状态互相可见。
 *
 * 剧本：
 *   播种          真实接口创建项目 + 主管 + 执行 Agent，并启用项目
 *   浏览器        任务出现在待办列
 *   agent(API)    领取
 *   浏览器        卡片出现在「进行中」
 *   agent(API)    提交
 *   浏览器        卡片出现在「待验收」，浮窗展示提交内容，主管审核通过
 *   agent(API)    读到的最新状态是 DONE
 *
 * 领取走 AgentAPI：该 Agent 只属于本项目，领取范围被成员关系限定在本项目这一条任务上。
 */
import { expect, test, type APIRequestContext } from '@playwright/test'
import {
  AGENT_API,
  addMember,
  claimViaAgentApi,
  collectRuntimeErrors,
  expectNoRuntimeErrors,
  getTask,
  seedAgent,
  seedTask,
  submitViaAgentApi,
  taskCard,
  uniqueKey,
  web,
  type ProjectRef,
} from './helpers'

async function agentReadTask(request: APIRequestContext, token: string, taskId: string) {
  const response = await request.fetch(`${AGENT_API}/tasks/${taskId}`, {
    headers: { Accept: 'application/json', AGENT_TOKEN: token },
  })
  if (!response.ok()) throw new Error(`AgentAPI 读任务失败：HTTP ${response.status()}`)
  const parsed = (await response.json()) as { data: { status: string; version: number } }
  return parsed.data
}

test.describe('浏览器与远程 Agent 交替操作同一条任务', () => {
  test('Agent 领取与提交后浏览器完成审核，Agent 侧读到的状态同步为 DONE', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const suffix = uniqueKey('cross')

    const project = await web<ProjectRef>(request, 'POST', '/projects', {
      title: `E2E 联合验收 ${suffix}`,
      summary: '浏览器与 Agent 交替操作',
      background: '背景：两侧状态必须互相可见。',
    })
    const supervisor = await seedAgent(request, 'SUPERVISOR', `主管-${suffix}`)
    const worker = await seedAgent(request, 'WORKER', `执行-${suffix}`)
    await addMember(request, project.id, supervisor.id)
    await addMember(request, project.id, worker.id)
    const active = await web<ProjectRef>(request, 'POST', `/projects/${project.id}/activate`, {
      expected_version: project.version,
    })
    expect(active.status).toBe('ACTIVE')

    const task = await seedTask(request, {
      projectId: project.id,
      title: `联合验收任务 ${suffix}`,
      description: '要求：两侧状态互见。',
      acceptance: '验收：浏览器完成审核后状态为已完成。',
      difficulty: 2,
      publish: true,
    })

    await page.goto('/')
    await page.getByTestId('filter-project').selectOption(project.id)
    const card = taskCard(page, task.display_number, project.id)
    await expect(card).toBeVisible({ timeout: 20_000 })
    await expect(card).toHaveAttribute('data-status', 'TODO')

    // Agent 领取（AgentAPI）
    const claim = await claimViaAgentApi<{ task_id: string; execution_id: string }>(request, worker.agent_key!)
    expect(claim.task_id).toBe(task.id)
    await expect(taskCard(page, task.display_number, project.id)).toHaveAttribute('data-status', 'IN_PROGRESS', { timeout: 20_000 })

    // Agent 提交（AgentAPI，含真实产物上传）
    const claimed = await getTask(request, task.id)
    await submitViaAgentApi(request, worker.agent_key!, {
      taskId: task.id,
      executionId: claim.execution_id,
      expectedVersion: Number(claimed.version),
      summary: 'Agent 侧提交的交付',
    })
    await expect(taskCard(page, task.display_number, project.id)).toHaveAttribute('data-status', 'IN_REVIEW', { timeout: 20_000 })

    // 浏览器审核通过
    await taskCard(page, task.display_number, project.id).click()
    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await expect(sheet).toContainText('Agent 侧提交的交付')
    await page.getByTestId('task-action-review').click()
    const reviewDialog = page.getByTestId('base-dialog')
    await expect(reviewDialog).toBeVisible()
    // Agent 目录按页加载：等表单自动选中可用主管后再提交
    await expect(page.getByTestId('review-reviewer')).not.toHaveValue('', { timeout: 20_000 })
    await page.getByTestId('review-decision-approve').click()
    await page.getByTestId('review-comment').fill('浏览器侧审核通过')
    await page.getByTestId('review-submit').click()
    await expect(reviewDialog).toHaveCount(0, { timeout: 20_000 })

    // Agent 读到的最新状态
    const final = await agentReadTask(request, worker.agent_key!, task.id)
    expect(final.status).toBe('DONE')
    await expect(taskCard(page, task.display_number, project.id)).toHaveAttribute('data-status', 'DONE', { timeout: 20_000 })

    expectNoRuntimeErrors(errors)
  })
})
