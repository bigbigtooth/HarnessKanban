/**
 * 系统设置 API：环境变量与 LLM 连接配置（后端 `/api/web/v1/settings`，T43）。
 *
 * 密文（api_key、密码类环境变量）只写不读：读取接口只回报是否已设置，不会回显内容。
 */
import { newIdempotencyKey, webApiClient } from './client'

export type EnvValueSource = 'override' | 'environment' | 'unset'

export interface SettingsEnvEntryDTO {
  key: string
  group: string
  description: string
  secret: boolean
  source: EnvValueSource
  /** 密文字段恒为 null，用 `has_value` 判断是否已设置。 */
  value: string | null
  has_value: boolean
}

export interface LlmSettingsDTO {
  base_url: string
  model: string
  temperature: number
  max_tokens: number
  system_prompt: string
  api_key_set: boolean
}

export interface SettingsDTO {
  llm: LlmSettingsDTO
  env: SettingsEnvEntryDTO[]
  env_groups: string[]
  updated_at: string | null
  settings_file: string
  notes: string
}

export interface LlmProbeDTO {
  ok: boolean
  message: string
  url: string
  status_code: number | null
  latency_ms: number | null
  model_count: number | null
  model_found: boolean | null
  models: string[]
}

export interface SettingsUpdatePayload {
  /** 值为 null 表示删除该覆盖项，回到 .env / 进程环境的值。 */
  env?: Record<string, string | null>
  llm?: Partial<{
    base_url: string
    api_key: string | null
    model: string
    temperature: number
    max_tokens: number
    system_prompt: string
  }>
}

export interface SettingsRequestOptions {
  signal?: AbortSignal
}

/** `GET /settings`：读设置（密文已脱敏）。 */
export function getSettings(options: SettingsRequestOptions = {}): Promise<SettingsDTO> {
  return webApiClient.get<SettingsDTO>('/settings', { signal: options.signal })
}

/** `PUT /settings`：部分合并写入。 */
export function updateSettings(
  payload: SettingsUpdatePayload,
  idempotencyKey: string = newIdempotencyKey(),
  options: SettingsRequestOptions = {},
): Promise<SettingsDTO> {
  return webApiClient.put<SettingsDTO>('/settings', payload, {
    idempotencyKey,
    signal: options.signal,
  })
}

/** `POST /settings/llm/test`：用已保存的配置探测 LLM 服务（不可达也返回结果，不报 5xx）。 */
export function testLlmConnection(
  idempotencyKey: string = newIdempotencyKey(),
  options: SettingsRequestOptions = {},
): Promise<LlmProbeDTO> {
  return webApiClient.post<LlmProbeDTO>('/settings/llm/test', {}, {
    idempotencyKey,
    signal: options.signal,
  })
}
