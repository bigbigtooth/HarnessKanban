/**
 * WebUI 访问密钥：客户端请求/登录态/登录页。
 *
 * 网络层用 fetch 替身；Cookie 由浏览器自动管理，这里验证「登录接口被调用、错误如实展示、
 * 403 会把应用切回未登录」这些应用层行为。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { FetchLike } from '@/api/client'
import { WEBUI_UNAUTHORIZED_EVENT } from '@/api/client'
import { getAuthStatus, loginWithKey, logoutWebUi } from '@/api/auth'
import { getSettings } from '@/api/settings'
import { createFetchStub, errEnvelope, okEnvelope } from './helpers/fetchStub'
import { authReady, authState, initAuth, login, logout, resetAuthState } from '@/stores/auth'
import LoginScreen from '@/components/auth/LoginScreen.vue'

const stub = createFetchStub()

beforeEach(() => {
  stub.reset()
  resetAuthState()
  vi.stubGlobal('fetch', stub.fetch as unknown as FetchLike)
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetAuthState()
  document.body.innerHTML = ''
})

describe('登录接口', () => {
  it('状态与登录走正确的路径和方法（登录是写操作，必须带幂等键）', async () => {
    stub.reply(
      { json: okEnvelope({ required: true, authenticated: false }) },
      { json: okEnvelope({ required: true, authenticated: true }) },
      { json: okEnvelope({ authenticated: false, required: true }) },
    )
    expect(await getAuthStatus()).toEqual({ required: true, authenticated: false })
    expect(stub.calls[0]!.url).toBe('/api/web/v1/auth/status')
    expect(stub.calls[0]!.method).toBe('GET')

    await loginWithKey('the-key')
    expect(stub.calls[1]!.url).toBe('/api/web/v1/auth/login')
    expect(stub.calls[1]!.method).toBe('POST')
    expect(stub.calls[1]!.idempotencyKey).toBeTruthy()
    expect(JSON.parse(stub.calls[1]!.bodyText!)).toEqual({ key: 'the-key' })

    await logoutWebUi()
    expect(stub.calls[2]!.url).toBe('/api/web/v1/auth/logout')
    expect(stub.calls[2]!.method).toBe('POST')
  })
})

describe('登录态', () => {
  it('未配置密钥时直接放行（本机回环开发）', async () => {
    stub.reply({ json: okEnvelope({ required: false, authenticated: true }) })
    await initAuth()
    expect(authState.checking).toBe(false)
    expect(authState.required).toBe(false)
    expect(authReady.value).toBe(true)
  })

  it('配置了密钥但未登录时不渲染业务界面', async () => {
    stub.reply({ json: okEnvelope({ required: true, authenticated: false }) })
    await initAuth()
    expect(authState.required).toBe(true)
    expect(authState.authenticated).toBe(false)
    expect(authReady.value).toBe(false)
  })

  it('登录成功后进入已认证状态；密钥错误时保留错误文案', async () => {
    stub.reply(
      { json: okEnvelope({ required: true, authenticated: false }) },
      { json: okEnvelope({ required: true, authenticated: true }) },
      { status: 403, json: errEnvelope('WEBUI_KEY_INVALID', 'WebUI 访问密钥不正确') },
    )
    await initAuth()
    expect(await login('good-key')).toBe(true)
    expect(authState.authenticated).toBe(true)
    expect(authReady.value).toBe(true)

    resetAuthState()
    stub.reset()
    stub.reply(
      { json: okEnvelope({ required: true, authenticated: false }) },
      { status: 403, json: errEnvelope('WEBUI_KEY_INVALID', 'WebUI 访问密钥不正确') },
    )
    await initAuth()
    expect(await login('bad-key')).toBe(false)
    expect(authState.authenticated).toBe(false)
    expect(authState.error).toContain('密钥不正确')
  })

  it('退出登录会调用服务端并回到未登录', async () => {
    stub.reply(
      { json: okEnvelope({ required: true, authenticated: false }) },
      { json: okEnvelope({ required: true, authenticated: true }) },
      { json: okEnvelope({ authenticated: false, required: true }) },
    )
    await initAuth()
    await login('good-key')
    await logout()
    expect(authState.authenticated).toBe(false)
    expect(stub.calls.some((call) => call.url === '/api/web/v1/auth/logout')).toBe(true)
  })
})

describe('密钥失效', () => {
  it('任意请求收到 403 WEBUI_KEY_REQUIRED 时会广播事件，供应用切回登录页', async () => {
    stub.reply({ status: 403, json: errEnvelope('WEBUI_KEY_REQUIRED', '缺少 WebUI 访问密钥') })
    const events: string[] = []
    const listener = (event: Event) => events.push((event as CustomEvent).detail?.code ?? '')
    window.addEventListener(WEBUI_UNAUTHORIZED_EVENT, listener)
    try {
      await expect(getSettings()).rejects.toMatchObject({ code: 'WEBUI_KEY_REQUIRED' })
      expect(events).toEqual(['WEBUI_KEY_REQUIRED'])
    } finally {
      window.removeEventListener(WEBUI_UNAUTHORIZED_EVENT, listener)
    }
  })

  it('登录后收到 403 会把状态切回未登录并提示重新登录', async () => {
    stub.reply(
      { json: okEnvelope({ required: true, authenticated: false }) },
      { json: okEnvelope({ required: true, authenticated: true }) },
      { status: 403, json: errEnvelope('WEBUI_KEY_INVALID', 'WebUI 访问密钥不正确') },
    )
    await initAuth()
    await login('good-key')
    expect(authState.authenticated).toBe(true)

    await expect(getSettings()).rejects.toMatchObject({ code: 'WEBUI_KEY_INVALID' })
    expect(authState.authenticated).toBe(false)
    expect(authState.error).toContain('失效')
    expect(authReady.value).toBe(false)
  })
})

describe('登录页', () => {
  function mountLogin() {
    return mount(LoginScreen, { attachTo: document.body })
  }

  it('输入密钥提交后调用登录接口，成功后清空输入', async () => {
    stub.reply({ json: okEnvelope({ required: true, authenticated: true }) })
    authState.checking = false
    authState.required = true
    const wrapper = mountLogin()
    const input = document.querySelector<HTMLInputElement>('[data-testid="auth-key"]')!
    input.value = 'the-key'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    document.querySelector<HTMLButtonElement>('[data-testid="auth-submit"]')!.click()
    await flushPromises()
    await flushPromises()
    const call = stub.calls.find((item) => item.url === '/api/web/v1/auth/login')
    expect(call).toBeTruthy()
    expect(JSON.parse(call!.bodyText!)).toEqual({ key: 'the-key' })
    expect((document.querySelector('[data-testid="auth-key"]') as HTMLInputElement).value).toBe('')
    wrapper.unmount()
  })

  it('密钥错误时展示服务端原因，不清空输入', async () => {
    stub.reply({ status: 403, json: errEnvelope('WEBUI_KEY_INVALID', 'WebUI 访问密钥不正确') })
    authState.checking = false
    authState.required = true
    const wrapper = mountLogin()
    const input = document.querySelector<HTMLInputElement>('[data-testid="auth-key"]')!
    input.value = 'bad-key'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    document.querySelector<HTMLButtonElement>('[data-testid="auth-submit"]')!.click()
    await flushPromises()
    await flushPromises()
    expect(document.querySelector('[data-testid="auth-error"]')?.textContent).toContain('密钥不正确')
    expect((document.querySelector('[data-testid="auth-key"]') as HTMLInputElement).value).toBe('bad-key')
    wrapper.unmount()
  })
})
