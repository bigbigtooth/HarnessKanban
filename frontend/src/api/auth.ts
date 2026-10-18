/**
 * WebUI 访问密钥相关接口。
 *
 * 浏览器登录成功后拿到的是一枚 HttpOnly 签名 Cookie（不把明文密钥放进 localStorage），
 * 之后所有同源 `/api` 请求会自动携带；密钥轮换会让旧 Cookie 立即失效。
 */
import { newIdempotencyKey, webApiClient } from './client'

export interface AuthStatusDTO {
  /** 服务端是否配置了访问密钥（配置了就必须登录）。 */
  required: boolean
  /** 当前请求是否已通过（无密钥配置时恒为 true）。 */
  authenticated: boolean
}

export interface AuthLoginDTO {
  required: boolean
  authenticated: boolean
}

/** `GET /auth/status`：前端启动时判断要不要显示登录页。 */
export async function getAuthStatus(): Promise<AuthStatusDTO> {
  return webApiClient.get<AuthStatusDTO>('/auth/status')
}

/** `POST /auth/login`：用密钥换取签名 Cookie。 */
export async function loginWithKey(key: string): Promise<AuthLoginDTO> {
  return webApiClient.post<AuthLoginDTO>(
    '/auth/login',
    { key },
    { idempotencyKey: newIdempotencyKey() },
  )
}

/** `POST /auth/logout`：清除登录 Cookie。 */
export async function logoutWebUi(): Promise<AuthLoginDTO> {
  return webApiClient.post<AuthLoginDTO>('/auth/logout', {}, { idempotencyKey: newIdempotencyKey() })
}
