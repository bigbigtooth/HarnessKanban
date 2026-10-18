/**
 * 端到端测试公共助手（真实服务，无业务 mock）。
 *
 * 运行前提：
 * - Django WebAPI：127.0.0.1:7101（前端 dev server 把 /api 代理到它）
 * - AgentAPI：127.0.0.1:7102（用于让真实 Agent 领取与提交）
 * - 前端 dev server：127.0.0.1:7100（由 playwright.config.ts 自动拉起）
 *
 * 每个用例都用真实接口现场播种自己的数据（唯一后缀），互不依赖。
 */
import { expect, type APIRequestContext, type ConsoleMessage, type Locator, type Page } from '@playwright/test'

export const WEB_API = '/api/web/v1'
export const AGENT_API = 'http://127.0.0.1:7102/api/agent/v1'

interface Envelope<T> {
  data: T
  request_id: string
}

interface ErrorEnvelope {
  error: { code: string; message: string; details: Record<string, unknown> }
  request_id: string
}

export function uniqueKey(prefix: string): string {
  return `e2e-${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`
}

/** 浏览器 Console 错误收集：任一 error 级别日志或 pageerror 都会让用例失败。 */
export function collectRuntimeErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('console', (message: ConsoleMessage) => {
    if (message.type() === 'error') errors.push(`console.error: ${message.text()}`)
  })
  page.on('pageerror', (error: Error) => {
    errors.push(`pageerror: ${error.message}`)
  })
  return errors
}

export function expectNoRuntimeErrors(errors: string[]): void {
  expect(errors, `页面出现运行时错误：\n${errors.join('\n')}`).toEqual([])
}

/** 调用真实 WebAPI；失败时抛出带服务端原话的错误，绝不静默。 */
export async function web<T>(
  request: APIRequestContext,
  method: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (method !== 'GET') headers['Idempotency-Key'] = uniqueKey('seed')
  const response = await request.fetch(`${WEB_API}${path}`, {
    method,
    headers,
    data: body === undefined ? undefined : body,
  })
  const text = await response.text()
  let parsed: unknown = null
  try {
    parsed = JSON.parse(text)
  } catch {
    parsed = text
  }
  if (!response.ok()) {
    const envelope = parsed as ErrorEnvelope
    throw new Error(`${method} ${path} -> HTTP ${response.status()} ${JSON.stringify(envelope?.error ?? parsed)}`)
  }
  return (parsed as Envelope<T>).data
}

export interface ProjectRef {
  id: string
  title: string
  status: string
  version: number
  content_version: number
}

export interface AgentRef {
  id: string
  name: string
  role: 'SUPERVISOR' | 'WORKER'
  agent_key?: string
}

export interface TaskRow {
  id: string
  display_number: string
  title: string
  status: string
  version: number
  claim_mode: string
  project_id: string
}

/** 播种一个在用项目（真实 POST /projects + activate）。 */
export async function seedProject(request: APIRequestContext, prefix = 'p', role = 'WORKER'): Promise<ProjectRef & { worker: AgentRef }> {
  const suffix = uniqueKey(prefix)
  const project = await web<ProjectRef>(request, 'POST', '/projects', {
    title: `E2E 项目 ${suffix}`,
    summary: '端到端测试现场播种',
    background: '背景：由 e2e 用例通过真实接口写入。',
  })
  const supervisor = await seedAgent(request, 'SUPERVISOR', `主管-${suffix}`)
  await addMember(request, project.id, supervisor.id)
  const worker = role === 'WORKER' ? await seedAgent(request, 'WORKER', `执行-${suffix}`) : supervisor
  if (role === 'WORKER') await addMember(request, project.id, worker.id)
  const activated = await web<ProjectRef>(request, 'POST', `/projects/${project.id}/activate`, {
    expected_version: project.version,
  })
  return { ...activated, title: activated.title ?? project.title, worker }
}

export async function seedAgent(
  request: APIRequestContext,
  role: 'SUPERVISOR' | 'WORKER',
  name: string,
): Promise<AgentRef> {
  const created = await web<AgentRef>(request, 'POST', '/agents', {
    name,
    role,
    max_concurrency: 3,
  })
  if (!created.agent_key) throw new Error('创建 Agent 未返回一次性密钥，无法继续真实 Agent 流程')
  return created
}

export async function addMember(request: APIRequestContext, projectId: string, agentId: string): Promise<void> {
  await web(request, 'PUT', `/projects/${projectId}/members/${agentId}`, {})
}

/** 播种一个任务（默认落在「准备中」，publish=true 时立刻发布并返回最新版本）。 */
export async function seedTask(
  request: APIRequestContext,
  input: {
    projectId: string
    title: string
    description?: string
    acceptance?: string
    difficulty?: number
    claimMode?: 'OPEN' | 'ASSIGNED'
    designatedAgentId?: string
    labels?: string[]
    publish?: boolean
  },
): Promise<TaskRow> {
  const created = await web<TaskRow>(request, 'POST', '/tasks', {
    project_id: input.projectId,
    title: input.title,
    description: input.description ?? '端到端用例播种的任务要求。',
    acceptance_criteria: input.acceptance ?? '端到端用例播种的验收标准。',
    difficulty: input.difficulty ?? 1,
    claim_mode: input.claimMode ?? 'OPEN',
    ...(input.claimMode === 'ASSIGNED' && input.designatedAgentId
      ? { designated_agent_id: input.designatedAgentId }
      : {}),
    ...(input.labels ? { labels: input.labels } : {}),
  })
  if (!input.publish) return created
  const published = await web<TaskRow>(request, 'POST', `/tasks/${created.id}/publish`, {
    expected_version: created.version,
  })
  return published
}

export async function getTask(request: APIRequestContext, taskId: string): Promise<TaskRow & Record<string, unknown>> {
  return web<TaskRow & Record<string, unknown>>(request, 'GET', `/tasks/${taskId}`)
}

/** 让真实 Agent 通过 AgentAPI 领取任务。 */
export async function claimViaAgentApi<T = Record<string, unknown>>(
  request: APIRequestContext,
  token: string,
): Promise<T> {
  const response = await request.post(`${AGENT_API}/get_task/?l1=1&l2=1&l3=1&l4=1&l5=1`, {
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': uniqueKey('agent'),
      AGENT_TOKEN: token,
    },
    data: {},
  })
  const text = await response.text()
  const parsed = JSON.parse(text) as Envelope<T> | ErrorEnvelope
  if (!response.ok() || 'error' in parsed) {
    throw new Error(`AgentAPI 领取失败：HTTP ${response.status()} ${text.slice(0, 300)}`)
  }
  return parsed.data
}

/** 让真实 Agent 上传一个产物并正式提交。 */
export async function submitViaAgentApi(
  request: APIRequestContext,
  token: string,
  input: { taskId: string; executionId: string; expectedVersion: number; summary?: string },
): Promise<void> {
  const fileName = `e2e-${Date.now()}.md`
  const upload = await request.post(
    `${AGENT_API}/tasks/${input.taskId}/attachments?purpose=EXECUTION_ARTIFACT&execution_id=${input.executionId}&filename=${fileName}`,
    {
      headers: {
        'Content-Type': 'text/markdown',
        'Idempotency-Key': uniqueKey('agent'),
        AGENT_TOKEN: token,
      },
      data: `# 端到端产物\n\n由 e2e 用例通过真实 AgentAPI 上传。\n`,
    },
  )
  if (!upload.ok()) throw new Error(`产物上传失败：HTTP ${upload.status()} ${(await upload.text()).slice(0, 200)}`)
  const uploaded = JSON.parse(await upload.text()) as Envelope<{ file_id: string; oss_url?: string | null }>
  const ossUrl = uploaded.data.oss_url ?? null

  const response = await request.post(`${AGENT_API}/tasks/${input.taskId}/submit`, {
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      'Idempotency-Key': uniqueKey('agent'),
      AGENT_TOKEN: token,
    },
    data: {
      execution_id: input.executionId,
      expected_version: input.expectedVersion,
      summary: input.summary ?? 'Agent 侧提交的交付',
      content: ossUrl ? `产物下载：${ossUrl}` : '本次无文件产物',
      self_check: [{ criterion: '端到端用例播种的验收标准。', passed: true, evidence: fileName }],
      artifacts: [{ file_id: uploaded.data.file_id }],
      links: [],
    },
  })
  if (!response.ok()) throw new Error(`提交失败：HTTP ${response.status()} ${(await response.text()).slice(0, 300)}`)
}

// ---------------------------------------------------------------------------
// 页面定位助手（只依赖页面上真实存在的 data-* 事实）
// ---------------------------------------------------------------------------

/**
 * 看板卡片：显示编号只在项目内唯一（每个项目都从 TASK-0001 开始），
 * 因此断言必须同时锁定项目，避免跨项目重名命中多张卡片。
 */
export function taskCard(page: Page, displayNumber: string, projectId: string): Locator {
  return page.locator(
    `article.task-card[data-project-id="${projectId}"][data-display-number="${displayNumber}"]`,
  )
}

export function projectCard(page: Page, title: string): Locator {
  return page.locator('article.project-card').filter({ has: page.getByRole('heading', { name: title }) })
}

/** 打开任务详情浮窗（点击卡片）并等待详情加载完成。 */
export async function openTaskDetail(page: Page, displayNumber: string, projectId: string): Promise<void> {
  const card = taskCard(page, displayNumber, projectId)
  await expect(card).toBeVisible({ timeout: 20_000 })
  await card.click()
  await expect(page.locator('.modal-layer .sheet')).toBeVisible()
  await expect(page.getByRole('heading', { name: /TASK-/ }).first()).toBeVisible()
}

/** 关闭浮窗（点右上角 ×）；浮窗已经自己关掉时直接返回。 */
export async function closeModal(page: Page): Promise<void> {
  if ((await page.locator('.modal-layer').count()) === 0) return
  await page.locator('.modal-layer .sheet-close').first().click()
  await expect(page.locator('.modal-layer')).toHaveCount(0)
}
