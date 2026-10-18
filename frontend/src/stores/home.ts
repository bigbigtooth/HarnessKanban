/**
 * 首页数据层（任务 + 项目两个核心模块共用）。
 *
 * 任务模块默认展示**全部项目**的任务（服务端 `status=ALL` 一次取全五态），
 * 项目模块需要项目清单、每个项目的任务完成度（来自 `/dashboard`）以及 Agent 名录
 * （筛选、头像堆、成员选择都用它）。
 *
 * 约定：读取失败时保留上一次的数据，只把错误挂到 `error` 上（与旧页面一致的“数据可能过期”语义），
 * 不把失败当成空列表。
 */
import { computed, reactive } from 'vue'

import { listAgentsPage, type AgentSummaryDTO } from '@/api/agents'
import { ApiError, LatestRequestGate } from '@/api/client'
import { getDashboard, type DashboardDTO } from '@/api/dashboard'
import { fetchAllPages } from '@/api/paginate'
import { listProjectsPage, type ProjectSummaryDTO } from '@/api/projects'
import { ALL_TASK_STATUSES, listTasks, type TaskListItemDTO } from '@/api/tasks'
import { TASK_STATUS_COLUMNS, type ClaimMode, type Difficulty, type TaskStatus } from '@/api/types'
import { createPoller, type VisiblePagePoller } from '@/stores/polling'
import { theme } from '@/stores/theme'

export type LoadStatus = 'idle' | 'loading' | 'ready' | 'error'

/** 看板五列：与任务状态一一对应（demo 的四列 + 准备中）。 */
export interface BoardColumn {
  status: TaskStatus
  name: string
  emoji: string
  accentVar: string
  lineVar: string
  hint: string
}

export const BOARD_COLUMNS: readonly BoardColumn[] = [
  {
    status: 'PREPARING',
    name: '准备中',
    emoji: '📋',
    accentVar: '--col-preparing-soft',
    lineVar: '--col-preparing-line',
    hint: '还没发布',
  },
  {
    status: 'TODO',
    name: '待办',
    emoji: '🌱',
    accentVar: '--col-todo-soft',
    lineVar: '--col-todo-line',
    hint: '新鲜的灵感',
  },
  {
    status: 'IN_PROGRESS',
    name: '进行中',
    emoji: '🫧',
    accentVar: '--col-doing-soft',
    lineVar: '--col-doing-line',
    hint: '正在冒泡',
  },
  {
    status: 'IN_REVIEW',
    name: '待验收',
    emoji: '🔍',
    accentVar: '--col-review-soft',
    lineVar: '--col-review-line',
    hint: '帮忙看一眼',
  },
  {
    status: 'DONE',
    name: '已完成',
    emoji: '🎀',
    accentVar: '--col-done-soft',
    lineVar: '--col-done-line',
    hint: '今天也很棒',
  },
] as const

export const BOARD_COLUMN_BY_STATUS: Record<TaskStatus, BoardColumn> = BOARD_COLUMNS.reduce(
  (acc, column) => {
    acc[column.status] = column
    return acc
  },
  {} as Record<TaskStatus, BoardColumn>,
)

/** 一页取满：服务端 page_size 上限 100。 */
export const TASK_PAGE_SIZE = 100

/**
 * 一次请求最多翻多少页。看板/网格要展示“全部”内容，而服务端单页上限是 100，
 * 所以这里把所有页都取回来（1000 条上限，超出属于异常规模，宁可少取也不打满服务）。
 */
// 分页取数统一走 api/paginate.ts（活动页/看板页与首页共用同一套规则）。

export interface HomeFilters {
  /** 空串 = 全部项目（默认）。 */
  projectId: string
  difficulty: '' | Difficulty
  claimMode: '' | ClaimMode
  /** 实际执行者（快速筛选：只看某个 Agent 手上的活）。 */
  assigneeAgentId: string
  /** 指定执行者（指派模式的任务）。 */
  designatedAgentId: string
  includeArchived: boolean
  keyword: string
}

export function emptyFilters(): HomeFilters {
  return {
    projectId: '',
    difficulty: '',
    claimMode: '',
    assigneeAgentId: '',
    designatedAgentId: '',
    includeArchived: false,
    keyword: '',
  }
}

export const homeFilters = reactive<HomeFilters>(emptyFilters())

export const taskList = reactive({
  items: [] as TaskListItemDTO[],
  total: 0,
  status: 'idle' as LoadStatus,
  error: null as ApiError | null,
  updatedAt: '',
})

export const projectList = reactive({
  items: [] as ProjectSummaryDTO[],
  status: 'idle' as LoadStatus,
  error: null as ApiError | null,
})

export const agentList = reactive({
  items: [] as AgentSummaryDTO[],
  status: 'idle' as LoadStatus,
  error: null as ApiError | null,
})

/**
 * 顶栏计数：二级页面（全局看板 / Agent / 活动）也要显示真实的任务与项目数量。
 * 与 `taskList/projectList` 分开存放，避免首页卸载时被重置成 0；
 * 查询统一用 pageSize=1，只读服务端的 total，成本极低。
 */
export const shellCounts = reactive({
  tasks: 0,
  projects: 0,
  onlineAgents: 0,
  loadedAt: 0,
})

export const dashboardState = reactive({
  data: null as DashboardDTO | null,
  status: 'idle' as LoadStatus,
  error: null as ApiError | null,
})

export const projectTaskIndex = reactive({
  /** 项目 → 该项目任务上的执行者（不受任务模块筛选影响，用于项目卡片头像堆）。 */
  workers: new Map<string, string[]>(),
  status: 'idle' as LoadStatus,
})

const tasksGate = new LatestRequestGate()
const projectsGate = new LatestRequestGate()

let keywordTimer: ReturnType<typeof setTimeout> | null = null
let poller: VisiblePagePoller | null = null

function nowLabel(): string {
  return new Date().toLocaleTimeString('zh-CN', { hour12: false })
}

function describe(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  return new ApiError({
    code: 'NETWORK_ERROR',
    message: error instanceof Error ? error.message : '请求失败',
    httpStatus: 0,
    kind: 'network',
    retryable: true,
  })
}

// ---------------------------------------------------------------------------
// 读取
// ---------------------------------------------------------------------------

/** 任务列表：默认全部项目、全部状态。 */
export async function loadTasks(options: { silent?: boolean } = {}): Promise<void> {
  if (!options.silent) taskList.status = 'loading'
  const gate = tasksGate.begin()
  try {
    // 看板一次展示全部匹配任务（分页取完），否则列计数与完成度都会只反映前 100 条。
    const page = await fetchAllPages((pageNumber) =>
      listTasks(
        {
          projectId: homeFilters.projectId || null,
          status: ALL_TASK_STATUSES,
          difficulty: homeFilters.difficulty === '' ? null : homeFilters.difficulty,
          claimMode: homeFilters.claimMode === '' ? null : homeFilters.claimMode,
          assigneeAgentId: homeFilters.assigneeAgentId || null,
          designatedAgentId: homeFilters.designatedAgentId || null,
          keyword: homeFilters.keyword,
          includeArchived: homeFilters.includeArchived,
          page: pageNumber,
          pageSize: TASK_PAGE_SIZE,
        },
        { signal: gate.signal },
      ),
    )
    if (!gate.isCurrent()) return
    taskList.items = page.items
    taskList.total = page.total
    shellCounts.tasks = page.total
    taskList.status = 'ready'
    taskList.error = null
    taskList.updatedAt = nowLabel()
  } catch (error) {
    if (!gate.isCurrent()) return
    taskList.error = describe(error)
    // 已有数据时保留“可展示”状态，只把错误挂出来，避免一次抖动把看板清成错误页。
    taskList.status = taskList.items.length > 0 ? 'ready' : 'error'
  }
}

/** 项目卡片上的执行者头像：单独取一次“不带筛选”的任务列表，避免被任务模块的筛选带偏。 */
async function loadProjectTaskIndex(): Promise<void> {
  try {
    const page = await fetchAllPages((pageNumber) =>
      listTasks({ status: ALL_TASK_STATUSES, page: pageNumber, pageSize: TASK_PAGE_SIZE }),
    )
    const workers = new Map<string, string[]>()
    for (const task of page.items) {
      if (task.is_archived) continue
      const agentId = task.assignee_agent_id ?? task.designated_agent_id
      if (!agentId) continue
      const list = workers.get(task.project_id) ?? []
      if (!list.includes(agentId)) list.push(agentId)
      workers.set(task.project_id, list)
    }
    projectTaskIndex.workers = workers
    projectTaskIndex.status = 'ready'
  } catch {
    // 纯装饰性数据：失败时保留旧值，不打扰用户。
    if (projectTaskIndex.status !== 'ready') projectTaskIndex.status = 'error'
  }
}

/** 项目清单：在用项目 + 回收站（已删除）各取一页，供状态筛选与计数使用。 */
export async function loadProjects(options: { silent?: boolean } = {}): Promise<void> {
  if (!options.silent) projectList.status = 'loading'
  const gate = projectsGate.begin()
  try {
    const [active, deleted] = await Promise.all([
      fetchAllPages((pageNumber) =>
        listProjectsPage({ includeArchived: true, page: pageNumber, pageSize: TASK_PAGE_SIZE }, { signal: gate.signal }),
      ),
      fetchAllPages((pageNumber) =>
        listProjectsPage(
          { includeArchived: true, deleted: true, page: pageNumber, pageSize: TASK_PAGE_SIZE },
          { signal: gate.signal },
        ),
      ),
    ])
    if (!gate.isCurrent()) return
    projectList.items = [...active.items, ...deleted.items]
    shellCounts.projects = active.total
    projectList.status = 'ready'
    projectList.error = null
  } catch (error) {
    if (!gate.isCurrent()) return
    projectList.error = describe(error)
    projectList.status = projectList.items.length > 0 ? 'ready' : 'error'
  }
}

/** Agent 名录：筛选下拉、负责人头像、项目成员选择共用。 */
export async function loadAgents(options: { silent?: boolean } = {}): Promise<void> {
  if (agentList.status === 'idle') agentList.status = 'loading'
  try {
    const page = await fetchAllPages((pageNumber) =>
      listAgentsPage({ page: pageNumber, pageSize: TASK_PAGE_SIZE }),
    )
    agentList.items = page.items
    shellCounts.onlineAgents = page.items.filter(
      (agent) => agent.online_state === 'ONLINE_IDLE' || agent.online_state === 'ONLINE_WORKING',
    ).length
    agentList.status = 'ready'
    agentList.error = null
  } catch (error) {
    agentList.error = describe(error)
    if (agentList.status !== 'ready') agentList.status = 'error'
    if (!options.silent) throw error
  }
}

/** 全局看板数据：项目卡片的任务完成度、小分队在线人数都取自这里。 */
export async function loadDashboard(options: { silent?: boolean } = {}): Promise<void> {
  if (dashboardState.status === 'idle') dashboardState.status = 'loading'
  try {
    const data = await getDashboard({ includeArchived: true })
    dashboardState.data = data
    dashboardState.status = 'ready'
    dashboardState.error = null
  } catch (error) {
    dashboardState.error = describe(error)
    if (dashboardState.status !== 'ready') dashboardState.status = 'error'
    if (!options.silent) throw error
  }
}

/** 顶栏计数：只取三张列表的 total（pageSize=1），任何页面都可以随时刷新。 */
export async function refreshShellCounts(): Promise<void> {
  const [tasks, projects, agents] = await Promise.all([
    listTasks({ status: ALL_TASK_STATUSES, page: 1, pageSize: 1 }).catch(() => null),
    listProjectsPage({ includeArchived: true, page: 1, pageSize: 1 }).catch(() => null),
    listAgentsPage({ online: true, page: 1, pageSize: 1 }).catch(() => null),
  ])
  if (tasks) shellCounts.tasks = tasks.total
  if (projects) shellCounts.projects = projects.total
  if (agents) shellCounts.onlineAgents = agents.total
  shellCounts.loadedAt = Date.now()
}

/** 首屏 / 手动刷新：任务与项目是主数据，失败会冒泡给页面；Agent 与看板是装饰性数据。 */
export async function refreshHome(): Promise<void> {
  const results = await Promise.allSettled([
    loadTasks(),
    loadProjects(),
    loadProjectTaskIndex(),
    loadAgents({ silent: true }),
    loadDashboard({ silent: true }),
  ])
  const failed = results.find((result) => result.status === 'rejected')
  if (failed && failed.status === 'rejected') throw failed.reason
}

/** 静默刷新（轮询用）：只更新数据，不改 loading 态。 */
export async function refreshHomeSilently(): Promise<void> {
  await Promise.allSettled([
    loadTasks({ silent: true }),
    loadProjects({ silent: true }),
    loadProjectTaskIndex(),
    loadAgents({ silent: true }),
    loadDashboard({ silent: true }),
  ])
}

export function startHomePolling(intervalMs?: number): VisiblePagePoller {
  if (poller) return poller
  poller = createPoller({ intervalMs, immediateOnStart: false })
  poller.start(() => refreshHomeSilently())
  return poller
}

export function stopHomePolling(): void {
  poller?.stop()
  poller = null
}

/** 测试与页面重挂载使用：清掉定时器与数据。 */
export function resetHomeState(): void {
  stopHomePolling()
  if (keywordTimer !== null) {
    clearTimeout(keywordTimer)
    keywordTimer = null
  }
  Object.assign(homeFilters, emptyFilters())
  taskList.items = []
  taskList.total = 0
  taskList.status = 'idle'
  taskList.error = null
  projectList.items = []
  projectList.status = 'idle'
  projectList.error = null
  agentList.items = []
  agentList.status = 'idle'
  agentList.error = null
  dashboardState.data = null
  dashboardState.status = 'idle'
  dashboardState.error = null
  projectTaskIndex.workers = new Map()
  projectTaskIndex.status = 'idle'
  shellCounts.tasks = 0
  shellCounts.projects = 0
  shellCounts.onlineAgents = 0
  shellCounts.loadedAt = 0
}

// ---------------------------------------------------------------------------
// 筛选
// ---------------------------------------------------------------------------

/** 关键词输入防抖（350ms），其余筛选立即生效。 */
export function setKeyword(value: string): void {
  homeFilters.keyword = value
  if (keywordTimer !== null) clearTimeout(keywordTimer)
  keywordTimer = setTimeout(() => {
    keywordTimer = null
    void loadTasks()
  }, 350)
}

export function setFilter<K extends keyof HomeFilters>(key: K, value: HomeFilters[K]): void {
  homeFilters[key] = value
  void loadTasks()
}

export function clearFilters(): void {
  Object.assign(homeFilters, emptyFilters())
  void loadTasks()
}

export function applyFilters(patch: Partial<HomeFilters>): void {
  Object.assign(homeFilters, patch)
  void loadTasks()
}

export const activeFilterCount = computed(() => {
  let count = 0
  if (homeFilters.projectId) count += 1
  if (homeFilters.difficulty !== '') count += 1
  if (homeFilters.claimMode !== '') count += 1
  if (homeFilters.assigneeAgentId) count += 1
  if (homeFilters.designatedAgentId) count += 1
  if (homeFilters.includeArchived) count += 1
  if (homeFilters.keyword.trim()) count += 1
  return count
})

// ---------------------------------------------------------------------------
// 派生数据
// ---------------------------------------------------------------------------

export const tasksByStatus = computed<Record<TaskStatus, TaskListItemDTO[]>>(() => {
  const buckets = {} as Record<TaskStatus, TaskListItemDTO[]>
  for (const status of TASK_STATUS_COLUMNS) buckets[status] = []
  for (const task of taskList.items) {
    if (buckets[task.status]) buckets[task.status].push(task)
  }
  return buckets
})

export const taskStats = computed(() => {
  const total = taskList.items.length
  const done = tasksByStatus.value.DONE.length
  const inFlight = tasksByStatus.value.IN_PROGRESS.length + tasksByStatus.value.IN_REVIEW.length
  return {
    total,
    done,
    inFlight,
    percent: total ? Math.round((done / total) * 100) : 0,
  }
})

export const projectById = computed(() => {
  const map = new Map<string, ProjectSummaryDTO>()
  for (const project of projectList.items) map.set(project.id, project)
  return map
})

export const agentById = computed(() => {
  const map = new Map<string, AgentSummaryDTO>()
  for (const agent of agentList.items) map.set(agent.id, agent)
  return map
})

/** 每个项目的任务统计（完成度 / 总数），来自 /dashboard。 */
export const projectStatsById = computed(() => {
  const map = new Map<string, DashboardDTO['projects'][number]>()
  for (const row of dashboardState.data?.projects ?? []) map.set(row.project_id, row)
  return map
})

export const onlineAgentCount = computed(
  () =>
    agentList.items.filter(
      (agent) => agent.online_state === 'ONLINE_IDLE' || agent.online_state === 'ONLINE_WORKING',
    ).length,
)

export function agentName(agentId: string | null | undefined): string {
  if (!agentId) return ''
  return agentById.value.get(agentId)?.name ?? '未知 Agent'
}

export const activeAgents = computed(() =>
  agentList.items.filter((agent) => !agent.deleted_at && agent.enabled),
)

// ---------------------------------------------------------------------------
// 头像 / 封面：把后端数据映射成 demo 的视觉元素（确定性哈希，刷新不变）
// ---------------------------------------------------------------------------

const AVATAR_EMOJI = ['🐰', '🍑', '🍙', '🐟', '🐣', '🦊', '🐨', '🐼', '🦉', '🐙', '🌷', '🍀']
const AVATAR_COLORS = ['#c6f1df', '#ffd4df', '#ffe9b3', '#cfe2ff', '#e2d8ff', '#ffdcc9', '#c9f0ff', '#f4e0ff']

export const AVATAR_COUNT = AVATAR_EMOJI.length

function hashString(value: string): number {
  let hash = 0
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) >>> 0
  }
  return hash
}

export interface AvatarView {
  emoji: string
  color: string
  label: string
  title: string
}

export function agentAvatar(agent: { id?: string; name: string } | null | undefined): AvatarView {
  const seed = agent?.id ?? agent?.name ?? '—'
  const index = hashString(seed) % AVATAR_EMOJI.length
  return {
    emoji: AVATAR_EMOJI[index],
    color: AVATAR_COLORS[index % AVATAR_COLORS.length],
    label: (agent?.name ?? '—').slice(0, 1).toUpperCase(),
    title: agent?.name ?? '未指派',
  }
}

/** 项目封面色板：浅色取 demo-light 的六套薄荷/糖果渐变，深色取 demo-dark 的霓虹色。 */
export interface CoverPalette {
  c1: string
  c2: string
  barA: string
  barB: string
  dot: string
}

export const PROJECT_COVERS: readonly CoverPalette[] = [
  { c1: '#ffb7c7', c2: '#ff8fb1', barA: '#ff9db8', barB: '#ffc3d2', dot: '#ff8fb1' },
  { c1: '#a9e7d1', c2: '#8fe0c5', barA: '#8fe0c5', barB: '#bdf0dd', dot: '#5fbfa2' },
  { c1: '#d8c9ff', c2: '#b9a4ff', barA: '#b9a4ff', barB: '#d8c9ff', dot: '#9c86e8' },
  { c1: '#b8ddff', c2: '#8cc8ff', barA: '#8cc8ff', barB: '#c8e6ff', dot: '#6da9dd' },
  { c1: '#ffc8ad', c2: '#ffa479', barA: '#ffa479', barB: '#ffd8c4', dot: '#e08a5f' },
  { c1: '#cfe8a9', c2: '#a9d97f', barA: '#a9d97f', barB: '#dcefc2', dot: '#7fae55' },
] as const

export const PROJECT_COVERS_DARK: readonly CoverPalette[] = [
  { c1: '#7028d8', c2: '#ff4f9a', barA: '#d8ff66', barB: '#79e5ff', dot: '#c59fff' },
  { c1: '#064e5b', c2: '#1dbfa5', barA: '#79e5ff', barB: '#d8ff66', dot: '#79e5ff' },
  { c1: '#cb2579', c2: '#6339e8', barA: '#ff8ad8', barB: '#b18cff', dot: '#ff8ad8' },
  { c1: '#123b91', c2: '#0fa6c4', barA: '#79e5ff', barB: '#d8ff66', dot: '#79e5ff' },
  { c1: '#8e2d52', c2: '#e9642f', barA: '#ffb86b', barB: '#ff8ad8', dot: '#ffb86b' },
  { c1: '#2c1d6b', c2: '#8b5cf6', barA: '#b18cff', barB: '#79e5ff', dot: '#b18cff' },
] as const

export const PROJECT_EMOJIS = ['🍓', '🧸', '🎙️', '🍰', '💌', '🌈', '🛸', '🌷', '🐣', '🧁', '📚', '🪐'] as const

export interface CoverView extends CoverPalette {
  index: number
  emoji: string
}

/** 当前主题下的一组封面色（浅色 / 深色各六套）。 */
export function coverChoicesFor(name: 'light' | 'dark'): readonly CoverPalette[] {
  return name === 'dark' ? PROJECT_COVERS_DARK : PROJECT_COVERS
}

/** 跟随当前主题的色板（组件里直接调用即可，主题切换会自动重算）。 */
export function currentCoverChoices(): readonly CoverPalette[] {
  return coverChoicesFor(theme.value)
}

export function paletteFor(index: number): CoverPalette {
  const choices = currentCoverChoices()
  return choices[index] ?? choices[0]
}

export function projectCoverView(projectId: string): CoverView {
  const index = hashString(projectId) % PROJECT_COVERS.length
  const emoji = PROJECT_EMOJIS[hashString(`emoji:${projectId}`) % PROJECT_EMOJIS.length]
  return { index, emoji, ...paletteFor(index) }
}

export const emojiChoices = PROJECT_EMOJIS
