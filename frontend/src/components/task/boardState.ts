/**
 * 看板页面状态（T31）。
 *
 * 只在内存中保存筛选条件与 Agent 目录缓存（不写 localStorage，见 T28/D15）：
 * - 筛选按项目保存，因此在详情弹窗开关、以及在项目页/看板之间来回导航后仍然保留；
 * - Agent 目录（名称映射、可选执行者/主管）全局缓存，减少重复请求。
 */

import { reactive } from 'vue'
import type { ApiError } from '@/api/client'
import { listAgents, type AgentOptionDTO } from '@/api/tasks'
import type { ClaimMode, Difficulty, TaskStatus, UUID } from '@/api/types'

export interface TaskFilters {
  difficulty: Difficulty | null
  status: TaskStatus | null
  claimMode: ClaimMode | null
  designatedAgentId: string
  assigneeAgentId: string
  keyword: string
  includeArchived: boolean
}

export function createEmptyFilters(): TaskFilters {
  return {
    difficulty: null,
    status: null,
    claimMode: null,
    designatedAgentId: '',
    assigneeAgentId: '',
    keyword: '',
    includeArchived: false,
  }
}

/** 项目键：有项目时用项目 ID，跨项目列表用 `__all__`。 */
/**
 * 按作用域保存筛选条件（内存，不落 localStorage）。
 * 用 reactive 记录而不是 Map，保证取回的嵌套对象是同一个响应式引用（页面与缓存共享）。
 */
const filtersByScope = reactive<Record<string, TaskFilters>>({})

export function filtersForScope(scopeKey: string): TaskFilters {
  if (!filtersByScope[scopeKey]) {
    filtersByScope[scopeKey] = createEmptyFilters()
  }
  return filtersByScope[scopeKey] as TaskFilters
}

export function setFiltersForScope(scopeKey: string, patch: Partial<TaskFilters>): void {
  const current = filtersForScope(scopeKey)
  Object.assign(current, patch)
}

export function resetFiltersForScope(scopeKey: string): void {
  Object.assign(filtersForScope(scopeKey), createEmptyFilters())
}

export function activeFilterCount(filters: TaskFilters): number {
  let count = 0
  if (filters.difficulty != null) count += 1
  if (filters.status) count += 1
  if (filters.claimMode) count += 1
  if (filters.designatedAgentId) count += 1
  if (filters.assigneeAgentId) count += 1
  if (filters.keyword.trim()) count += 1
  if (filters.includeArchived) count += 1
  return count
}

/** 同一组筛选条件的稳定签名：用于判断是否需要重新取数。 */
export function filterSignature(filters: TaskFilters): string {
  return JSON.stringify({
    difficulty: filters.difficulty,
    status: filters.status,
    claimMode: filters.claimMode,
    designatedAgentId: filters.designatedAgentId,
    assigneeAgentId: filters.assigneeAgentId,
    keyword: filters.keyword.trim(),
    includeArchived: filters.includeArchived,
  })
}

// ---------------------------------------------------------------------------
// Agent 目录（只读缓存）
// ---------------------------------------------------------------------------

export interface AgentDirectory {
  items: AgentOptionDTO[]
  status: 'idle' | 'loading' | 'ready' | 'error'
  error: ApiError | null
  loadedAt: number | null
}

export const agentDirectory = reactive<AgentDirectory>({
  items: [],
  status: 'idle',
  error: null,
  loadedAt: null,
})

let inflight: Promise<void> | null = null

/** 目录分页上限：单页 100，最多 10 页（1000 个 Agent），避免无限翻页。 */
const AGENT_PAGE_SIZE = 100
const AGENT_MAX_PAGES = 10

/**
 * 拉取 Agent 目录（同一时刻只发一个请求）。
 * 服务端 page_size 上限为 100，这里按页取完，保证名称映射与候选列表不会漏掉较新的 Agent。
 */
export function ensureAgentDirectory(force = false): Promise<void> {
  if (!force && agentDirectory.status === 'ready') return Promise.resolve()
  if (inflight) return inflight
  agentDirectory.status = 'loading'
  inflight = (async () => {
    const collected: AgentOptionDTO[] = []
    let total = Number.POSITIVE_INFINITY
    for (let page = 1; page <= AGENT_MAX_PAGES && collected.length < total; page += 1) {
      const response = await listAgents({ page, pageSize: AGENT_PAGE_SIZE })
      collected.push(...response.items)
      total = response.total
      if (response.items.length === 0) break
    }
    agentDirectory.items = collected
    agentDirectory.status = 'ready'
    agentDirectory.error = null
    agentDirectory.loadedAt = Date.now()
  })()
    .catch((error: unknown) => {
      agentDirectory.status = 'error'
      agentDirectory.error = error as ApiError
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}

/** 仅用于测试/重新挂载时清空缓存。 */
export function resetAgentDirectory(): void {
  agentDirectory.items = []
  agentDirectory.status = 'idle'
  agentDirectory.error = null
  agentDirectory.loadedAt = null
  inflight = null
}

export function agentById(agentId: string | null | undefined): AgentOptionDTO | null {
  if (!agentId) return null
  return agentDirectory.items.find((item) => item.id === agentId) ?? null
}

/** Agent 展示名：目录缺失时用短 ID 明确标注“未知”，不编造名字。 */
export function agentName(agentId: string | null | undefined): string {
  if (!agentId) return ''
  const agent = agentById(agentId)
  if (agent) return agent.name
  return `未知 Agent（${agentId.slice(0, 8)}…）`
}

export function agentLabel(agentId: string | null | undefined): string {
  const name = agentName(agentId)
  if (!name) return '—'
  const agent = agentById(agentId)
  if (!agent) return name
  const role = agent.role === 'SUPERVISOR' ? '主管' : '员工'
  const state = agent.enabled ? '启用' : '已禁用'
  return `${name}（${role}/${state}）`
}

/** 可指定/可代领的 Agent：启用且（若已知项目成员）属于该项目。 */
export function assignableAgents(projectId: UUID | null): AgentOptionDTO[] {
  return agentDirectory.items
    .filter((agent) => agent.enabled)
    .filter((agent) => {
      if (!projectId) return true
      if (!agent.project_ids) return true
      return agent.project_ids.includes(projectId)
    })
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** 有效主管候选（审核人）：启用、角色为主管；提交者由调用方排除。 */
export function supervisorCandidates(projectId: UUID | null, excludeAgentId?: UUID | null): AgentOptionDTO[] {
  return assignableAgents(projectId)
    .filter((agent) => agent.role === 'SUPERVISOR')
    .filter((agent) => !excludeAgentId || agent.id !== excludeAgentId)
}
