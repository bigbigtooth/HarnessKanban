/**
 * WebUI 登录态。
 *
 * 服务端配置了 `WEBUI_ACCESS_KEY` 时，`GET /auth/status` 返回 `required=true`；
 * 未通过时应用只渲染登录页，不加载任何业务数据。登录成功后服务端下发 HttpOnly
 * 签名 Cookie，前端不保存明文密钥；任意请求收到 403（密钥缺失/失效）会自动退回登录页。
 */
import { computed, reactive } from 'vue'

import { getAuthStatus, loginWithKey, logoutWebUi } from '@/api/auth'
import { WEBUI_UNAUTHORIZED_EVENT } from '@/api/client'

export interface AuthState {
  /** 正在向服务端确认登录要求。 */
  checking: boolean
  /** 服务端是否启用访问密钥。 */
  required: boolean
  /** 当前是否已通过。 */
  authenticated: boolean
  /** 登录/连接错误文案。 */
  error: string
}

export const authState = reactive<AuthState>({
  checking: true,
  required: false,
  authenticated: false,
  error: '',
})

/** 应用可以渲染业务界面（无需登录，或已登录）。 */
export const authReady = computed(() => !authState.checking && (!authState.required || authState.authenticated))

let listenerRegistered = false

function registerUnauthorizedListener(): void {
  if (listenerRegistered || typeof window === 'undefined') return
  listenerRegistered = true
  window.addEventListener(WEBUI_UNAUTHORIZED_EVENT, () => {
    // 密钥轮换或被服务端拒绝：丢掉登录态，回到登录页。
    authState.authenticated = false
    authState.required = true
    authState.error = '访问密钥已失效，请重新登录。'
  })
}

/** 应用启动时调用。 */
export async function initAuth(): Promise<void> {
  registerUnauthorizedListener()
  authState.checking = true
  authState.error = ''
  try {
    const status = await getAuthStatus()
    authState.required = status.required
    authState.authenticated = status.authenticated
  } catch (error) {
    // 服务不可达时按“需要登录”处理，登录页会展示错误并允许重试。
    authState.required = true
    authState.authenticated = false
    authState.error = error instanceof Error ? error.message : '无法连接服务端'
  } finally {
    authState.checking = false
  }
}

/** 提交访问密钥；成功返回 true。 */
export async function login(key: string): Promise<boolean> {
  const trimmed = key.trim()
  authState.error = ''
  if (!trimmed) {
    authState.error = '请输入访问密钥'
    return false
  }
  try {
    const result = await loginWithKey(trimmed)
    authState.required = result.required
    authState.authenticated = result.authenticated
    return result.authenticated
  } catch (error) {
    authState.error = error instanceof Error ? error.message : '登录失败'
    return false
  }
}

/** 退出登录：清除服务端 Cookie，回到登录页。 */
export async function logout(): Promise<void> {
  try {
    await logoutWebUi()
  } catch {
    // 即使服务端清理失败，也要回到登录页；下一次请求会再次触发校验。
  }
  authState.authenticated = false
  authState.error = ''
}

/** 测试用：重置登录态。 */
export function resetAuthState(): void {
  authState.checking = true
  authState.required = false
  authState.authenticated = false
  authState.error = ''
}
