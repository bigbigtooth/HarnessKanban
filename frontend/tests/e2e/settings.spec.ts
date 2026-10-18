/**
 * 设置浮窗端到端：环境变量与 LLM 设置（半透明设置按钮 → 浮窗 → 保存 → 服务端持久化）。
 *
 * 用例自己清理写入的覆盖项，避免污染本机服务配置。
 */
import { expect, test, type APIRequestContext } from '@playwright/test'
import { collectRuntimeErrors, expectNoRuntimeErrors, uniqueKey, web } from './helpers'

interface EnvEntry {
  key: string
  group: string
  source: string
  value: string | null
  has_value: boolean
  secret: boolean
}

interface SettingsView {
  llm: { base_url: string; model: string; api_key_set: boolean; temperature: number; max_tokens: number }
  env: EnvEntry[]
  updated_at: string | null
  settings_file: string
}

/** 读回服务端设置（用于断言“保存真的落库”，而不是只看界面提示）。 */
async function readSettings(request: APIRequestContext) {
  return web<SettingsView>(request, 'GET', '/settings')
}

/** 清掉该覆盖项，恢复本机原状。 */
async function clearOverride(request: APIRequestContext, key: string) {
  await web(request, 'PUT', '/settings', { env: { [key]: null } })
}

// 设置是全局单例（同一份 runtime_settings.json）：这些用例必须串行，
// 否则并发写会互相覆盖，出现“刚保存就被另一个用例改掉”的假失败。
test.describe.configure({ mode: 'serial' })

test.describe('设置浮窗', () => {
  test('右上角半透明按钮打开设置；LLM 与 环境变量 两页都在', async ({ page }) => {
    const errors = collectRuntimeErrors(page)
    await page.goto('/')

    const button = page.getByTestId('open-settings')
    await expect(button).toBeVisible()
    await button.click()

    const sheet = page.locator('.modal-layer .sheet')
    await expect(sheet).toBeVisible()
    await expect(sheet.locator('#settings-title')).toHaveText('设置')
    await expect(page.getByTestId('settings-tab-llm')).toHaveAttribute('aria-selected', 'true')
    await expect(page.getByTestId('llm-base-url')).toBeVisible()
    await expect(page.getByTestId('llm-model')).toBeVisible()
    await expect(page.getByTestId('llm-api-key')).toBeVisible()

    await page.getByTestId('settings-tab-env').click()
    await expect(sheet).toContainText('环境变量')
    await expect(sheet).toContainText('基础')
    await expect(page.getByTestId('env-input-LOG_LEVEL')).toBeVisible()
    await expect(page.getByTestId('env-new-key')).toBeVisible()

    expectNoRuntimeErrors(errors)
  })

  test('LLM 设置保存后落库：重新打开浮窗仍是新值，密钥只回报“已设置”', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const before = await readSettings(request)
    const baseUrl = `http://127.0.0.1:9/${uniqueKey('v1')}`

    await page.goto('/')
    await page.getByTestId('open-settings').click()
    await page.getByTestId('llm-base-url').fill(baseUrl)
    await page.getByTestId('llm-model').fill('e2e-model')
    await page.getByTestId('llm-api-key').fill('sk-e2e-secret')
    await page.getByTestId('settings-save').click()
    await expect(page.getByTestId('toast')).toContainText('设置已保存', { timeout: 20_000 })

    const saved = await readSettings(request)
    expect(saved.llm.base_url).toBe(baseUrl.replace(/\/$/, ''))
    expect(saved.llm.model).toBe('e2e-model')
    expect(saved.llm.api_key_set).toBe(true)
    // 密钥不回显：接口里没有它的原文
    expect(JSON.stringify(saved)).not.toContain('sk-e2e-secret')

    // 重开浮窗：密钥位置只提示“已设置 · 留空表示不修改”
    await page.reload()
    await page.getByTestId('open-settings').click()
    await expect(page.getByTestId('llm-base-url')).toHaveValue(saved.llm.base_url)
    await expect(page.getByTestId('llm-api-key')).toHaveAttribute('placeholder', /留空保持/)

    // 还原：删掉测试写入的 api_key 与 base_url，回到测试前状态
    await web(request, 'PUT', '/settings', {
      llm: { api_key: null, base_url: before.llm.base_url, model: before.llm.model },
    })

    expectNoRuntimeErrors(errors)
  })

  test('环境变量：新增自定义变量落库，勾选清除后从服务端移除', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const key = `E2E_FLAG_${Date.now()}`

    await page.goto('/')
    await page.getByTestId('open-settings').click()
    await page.getByTestId('settings-tab-env').click()
    await page.getByTestId('env-new-key').fill(key)
    await page.getByTestId('env-new-value').fill('on')
    await page.getByTestId('settings-save').click()
    await expect(page.getByTestId('toast')).toContainText('设置已保存', { timeout: 20_000 })

    const saved = await readSettings(request)
    const entry = saved.env.find((item) => item.key === key)
    expect(entry?.value).toBe('on')
    expect(entry?.source).toBe('override')

    // 刷新后仍能看到「本机覆盖」
    await page.reload()
    await page.getByTestId('open-settings').click()
    await page.getByTestId('settings-tab-env').click()
    await expect(page.locator(`[data-testid="env-input-${key}"]`)).toHaveValue('on')

    // 勾选清除 → 保存 → 服务端不再有这项覆盖
    await page.locator(`[data-testid="env-clear-${key}"]`).check()
    await expect(page.locator(`[data-testid="env-input-${key}"]`)).toBeDisabled()
    await page.getByTestId('settings-save').click()
    await expect(page.getByTestId('toast')).toContainText('设置已保存', { timeout: 20_000 })

    // 保存是异步落库：轮询服务端直到该覆盖项确实消失（不被上一条 toast 骗过）
    await expect
      .poll(async () => (await readSettings(request)).env.some((item) => item.key === key), { timeout: 15_000 })
      .toBe(false)
    await clearOverride(request, key)

    expectNoRuntimeErrors(errors)
  })

  test('测试连接：不可达地址如实报错，不谎报成功', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    const before = await readSettings(request)
    await web(request, 'PUT', '/settings', { llm: { base_url: 'http://127.0.0.1:9/v1' } })

    await page.goto('/')
    await page.getByTestId('open-settings').click()
    await page.getByTestId('llm-test').click()
    await expect(page.getByTestId('llm-probe-result')).toContainText('无法连接', { timeout: 20_000 })

    await web(request, 'PUT', '/settings', { llm: { base_url: before.llm.base_url } })
    expectNoRuntimeErrors(errors)
  })

  test('密文字段留空保存不会清掉已保存的密钥', async ({ page, request }) => {
    const errors = collectRuntimeErrors(page)
    await web(request, 'PUT', '/settings', { llm: { api_key: 'sk-keep-me' } })

    await page.goto('/')
    await page.getByTestId('open-settings').click()
    await page.getByTestId('llm-model').fill('keep-key-model')
    await page.getByTestId('settings-save').click()
    await expect(page.getByTestId('toast')).toContainText('设置已保存', { timeout: 20_000 })

    const saved = await readSettings(request)
    expect(saved.llm.api_key_set).toBe(true)
    expect(saved.llm.model).toBe('keep-key-model')

    await web(request, 'PUT', '/settings', { llm: { api_key: null, model: '' } })
    expectNoRuntimeErrors(errors)
  })
})
