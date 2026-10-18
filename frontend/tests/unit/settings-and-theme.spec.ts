/**
 * 设置 API 与主题切换单元测试。
 *
 * - 设置接口：路径、方法、幂等键、密文不回显（只带 api_key_set）。
 * - 主题：默认浅色、切换写 <html data-theme>、持久化到 localStorage、非法值忽略。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import { getSettings, testLlmConnection, updateSettings } from '@/api/settings'
import { createFetchStub, errEnvelope, okEnvelope } from './helpers/fetchStub'
import { DEFAULT_THEME, THEME_STORAGE_KEY, initTheme, setTheme, theme, toggleTheme } from '@/stores/theme'
import { resetProjectSkins, projectSkin, setProjectSkin } from '@/stores/projectSkins'
import { PROJECT_COVERS } from '@/stores/home'

const stub = createFetchStub()

beforeEach(() => {
  stub.reset()
  window.localStorage.clear()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

afterEach(() => {
  vi.unstubAllGlobals()
  window.localStorage.clear()
  resetProjectSkins()
})

describe('设置接口', () => {
  it('GET /settings 不带幂等键（读操作）', async () => {
    stub.reply({ json: okEnvelope({ llm: { api_key_set: false }, env: [], env_groups: [], updated_at: null, settings_file: '', notes: '' }) })
    const data = await getSettings()
    expect(stub.calls[0]!.method).toBe('GET')
    expect(stub.calls[0]!.url).toBe('/api/web/v1/settings')
    expect(stub.calls[0]!.idempotencyKey).toBeNull()
    expect(data.llm.api_key_set).toBe(false)
  })

  it('PUT /settings 带幂等键，且 body 只包含本次变更的字段', async () => {
    stub.reply({ json: okEnvelope({ llm: { api_key_set: true }, env: [], env_groups: [], updated_at: '2026-10-18T00:00:00Z', settings_file: '/tmp/x.json', notes: '' }) })
    const key = 'idem-settings-1'
    await updateSettings({ env: { LOG_LEVEL: 'DEBUG', CUSTOM_FLAG: null }, llm: { model: 'demo' } }, key)
    const call = stub.calls[0]!
    expect(call.method).toBe('PUT')
    expect(call.idempotencyKey).toBe(key)
    expect(JSON.parse(call.bodyText!)).toEqual({
      env: { LOG_LEVEL: 'DEBUG', CUSTOM_FLAG: null },
      llm: { model: 'demo' },
    })
  })

  it('测试连接：POST /settings/llm/test 原样回传不可达结果（不抛 5xx）', async () => {
    stub.reply({
      json: okEnvelope({ ok: false, message: '无法连接 http://127.0.0.1:9/v1/models（ConnectError）', url: 'http://127.0.0.1:9/v1/models', status_code: null, latency_ms: 12, model_count: null, model_found: null, models: [] }),
    })
    const probe = await testLlmConnection()
    const call = stub.calls[0]!
    expect(call.method).toBe('POST')
    expect(call.url).toBe('/api/web/v1/settings/llm/test')
    expect(call.idempotencyKey).toBeTruthy()
    expect(probe.ok).toBe(false)
    expect(probe.message).toContain('无法连接')
  })

  it('服务端拒绝时抛出带码的错误，不伪装成已保存', async () => {
    stub.reply({ status: 400, json: errEnvelope('INVALID_ARGUMENT', '环境变量名不合法', { details: { field: 'env', key: 'bad-key' } }) })
    await expect(updateSettings({ env: { 'bad-key': 'x' } }, 'idem-settings-2')).rejects.toMatchObject({
      code: 'INVALID_ARGUMENT',
    })
    await flushPromises()
  })
})

describe('主题', () => {
  it('默认浅色并写到 <html data-theme>', () => {
    const applied = initTheme()
    expect(applied).toBe(DEFAULT_THEME)
    expect(document.documentElement.dataset.theme).toBe('light')
  })

  it('切换主题会同步 html 属性与 localStorage', () => {
    setTheme('light')
    expect(toggleTheme()).toBe('dark')
    expect(theme.value).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
    expect(toggleTheme()).toBe('light')
  })

  it('localStorage 里的非法值被忽略，回落到默认主题', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'neon')
    expect(initTheme()).toBe(DEFAULT_THEME)
  })
})

describe('项目外观（本机偏好）', () => {
  it('未设置时由项目 id 派生，设置后保存并优先生效', () => {
    const derived = projectSkin('p-1')
    expect(derived.cover.cover).toBeLessThan(PROJECT_COVERS.length)

    setProjectSkin('p-1', { cover: 3, emoji: '🪐' })
    const stored = projectSkin('p-1')
    expect(stored.cover).toEqual({ cover: 3, emoji: '🪐' })
    expect(stored.palette).toEqual(PROJECT_COVERS[3])
    expect(window.localStorage.getItem('harvesstask.project-skins')).toContain('🪐')
  })

  it('损坏的本地数据不会让页面崩掉', () => {
    window.localStorage.setItem('harvesstask.project-skins', '{not json')
    expect(() => projectSkin('p-2')).not.toThrow()
    expect(projectSkin('p-2').cover.emoji).toBeTruthy()
  })
})
