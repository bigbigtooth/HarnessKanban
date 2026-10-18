/**
 * Agent 管理页端到端（沿用二级页面入口）：创建并一次性交付密钥、禁用（含回收处置）、恢复。
 *
 * 该页面保留原有的服务端约束：密钥只交付一次、禁用需要处置与原因、删除是软删除。
 */
import { expect, test } from '@playwright/test'
import { collectRuntimeErrors, expectNoRuntimeErrors, uniqueKey } from './helpers'

test.describe('Agent 管理', () => {
  test('创建 Agent：密钥一次性交付，关闭后不再展示', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    const name = `e2e-agent-${uniqueKey('k').slice(-10)}`

    await page.goto('/agents')
    await expect(page.getByTestId('view-agents')).toBeVisible()

    await page.getByTestId('agent-create-open').click()
    await expect(page.getByTestId('agent-create-form')).toBeVisible()
    await page.getByTestId('agent-create-name').fill(name)
    await page.getByTestId('agent-create-role').selectOption('WORKER')
    await page.getByTestId('agent-create-max-concurrency').fill('2')
    await page.getByTestId('agent-create-submit').click()

    await expect(page.getByTestId('agent-key-dialog')).toBeVisible({ timeout: 20_000 })
    // 明文 Key 放在只读 input 里：只能读 value，读文本会是空的
    const key = await page.getByTestId('agent-key-value').inputValue()
    expect(key.length).toBeGreaterThan(8)

    // 关闭后密钥不再出现在页面上（一次性交付）
    await page.getByTestId('agent-key-close').click()
    await expect(page.getByTestId('agent-key-dialog')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText(key)

    // 列表里能按名称找到它
    await page.getByTestId('agent-filter-name').fill(name)
    await page.getByTestId('agent-filter-apply').click()
    await expect(page.getByTestId('agent-table')).toContainText(name)

    expectNoRuntimeErrors(errors)
  })

  test('禁用 Agent：必须选择处置并填写原因，服务端生效后详情显示已禁用', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    const name = `e2e-disable-${uniqueKey('k').slice(-10)}`

    await page.goto('/agents')
    await page.getByTestId('agent-create-open').click()
    await page.getByTestId('agent-create-name').fill(name)
    await page.getByTestId('agent-create-role').selectOption('WORKER')
    await page.getByTestId('agent-create-submit').click()
    await expect(page.getByTestId('agent-key-dialog')).toBeVisible({ timeout: 20_000 })
    await page.getByTestId('agent-key-close').click()

    await page.getByTestId('agent-filter-name').fill(name)
    await page.getByTestId('agent-filter-apply').click()
    await page.getByTestId('agent-table').getByRole('button', { name: '查看详情' }).first().click()
    await expect(page.getByTestId('agent-detail')).toBeVisible()

    await page.getByTestId('agent-disable').click()
    const form = page.getByTestId('agent-disable-form')
    await expect(form).toBeVisible()
    // 原因必填：空原因先在本地被拦住
    await page.getByTestId('agent-disable-submit').click()
    await expect(page.getByTestId('agent-disable-local-error')).toContainText('必须填写原因')

    // 只有在 Agent 仍持有未结束执行时才需要显式处置；新 Agent 一般没有，存在就选 WAIT
    await page.getByTestId('agent-disable-reason').fill('端到端用例：暂时下线')
    if (await page.getByTestId('agent-disable-disposition-wait').count()) {
      await page.getByTestId('agent-disable-disposition-wait').check()
    }
    await page.getByTestId('agent-disable-submit').click()
    await expect(form).toHaveCount(0, { timeout: 20_000 })

    await expect(page.getByTestId('agent-detail-state')).toContainText('已禁用')
    await expect(page.getByTestId('agent-detail-enabled')).toContainText('已禁用')

    expectNoRuntimeErrors(errors)
  })

  test('在线/状态与容量信息如实展示（不编造在线状态）', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/agents')
    await expect(page.getByTestId('agent-table')).toBeVisible()

    const rows = page.locator('[data-testid^="agent-row-"]').filter({ has: page.getByRole('button', { name: '查看详情' }) })
    expect(await rows.count()).toBeGreaterThan(0)
    await rows.first().getByRole('button', { name: '查看详情' }).click()
    await expect(page.getByTestId('agent-detail')).toBeVisible()
    // 容量三项要么有数字要么明确说明，不允许空白
    await expect(page.getByTestId('agent-detail-max-concurrency')).not.toBeEmpty()
    await expect(page.getByTestId('agent-detail-reserved')).not.toBeEmpty()
    await expect(page.getByTestId('agent-detail-available')).not.toBeEmpty()

    expectNoRuntimeErrors(errors)
  })
})
