<script setup lang="ts">
/**
 * Agent 管理页（T29 / D16 / T30 / 产品 4、9）。
 *
 * 版式：页头（标题 + 创建入口）→ 筛选工具栏 → 操作提示 → 列表主体
 * （表格 + 命中说明 + 分页），选中 Agent 时右侧为详情面板（窄屏落到列表下方）。
 *
 * 硬性约束：
 * - 明文 Key 只在创建/重置成功后的交付框里出现一次（内存），关闭即清除；
 *   不写 localStorage/sessionStorage/URL，不从列表或详情接口读取，也不缓存；
 * - 服务端重放（key_delivery_unavailable=true）时只提示“已无法再次交付，请重新重置”，不展示任何 Key；
 * - 有效执行数与已占容量槽分别展示；“等待回收”标识到期未回收但占槽的执行；
 * - 禁用优先于在线状态显示；
 * - 每个写操作用自己的幂等键，失败重试沿用同一个键（useAgentAction），成功后换键；
 * - 软删除（T30）：删除必须填原因；已删除的行只允许「批量恢复」；翻页/改筛选即清空选择，
 *   绝不把不可见的行带进批量操作；批量结果逐项展示，一项失败不影响其他项。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ApiError, LatestRequestGate, type GatedRequest } from '@/api/client'
import { AgentRole } from '@/api/types'
import type {
  AgentBulkResultDTO,
  AgentDetailDTO,
  AgentExecutionBriefDTO,
  AgentSummaryDTO,
  CreateAgentInput,
} from '@/api/agents'
import {
  bulkDeleteAgents,
  bulkRestoreAgents,
  createAgent,
  deleteAgent,
  disableAgent,
  enableAgent,
  getAgent,
  listAgentsPage,
  rotateAgentKey,
  updateAgentRole,
} from '@/api/agents'
import { createPoller, type VisiblePagePoller } from '@/stores/polling'
import BaseDialog from '@/components/BaseDialog.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import AgentBulkResultDialog from '@/components/agent/AgentBulkResultDialog.vue'
import AgentCreateForm from '@/components/agent/AgentCreateForm.vue'
import AgentDeleteForm from '@/components/agent/AgentDeleteForm.vue'
import AgentDetailPanel from '@/components/agent/AgentDetailPanel.vue'
import AgentDisableForm from '@/components/agent/AgentDisableForm.vue'
import AgentFiltersBar from '@/components/agent/AgentFiltersBar.vue'
import AgentKeyDeliveryDialog from '@/components/agent/AgentKeyDeliveryDialog.vue'
import AgentRoleForm from '@/components/agent/AgentRoleForm.vue'
import {
  AGENT_DELETED_BADGE_LABEL,
  agentDeletedTooltip,
  agentDisplayState,
  agentFilterSummary,
  agentInitial,
  agentRoleLabel,
  agentStateLabel,
  bulkResultSummaryText,
  emptyAgentFilters,
  formatAgentTime,
  hasAgentFilters,
  isAgentDeleted,
  listCellSlotText,
  ONLINE_STATE_BADGE_CLASS,
  toAgentListQuery,
  type AgentListFilters,
} from '@/components/agent/agent-format'
import { useAgentAction } from '@/components/agent/useAgentAction'

type ListStatus = 'idle' | 'loading' | 'ready' | 'empty' | 'error'

interface Notice {
  kind: 'info' | 'error'
  message: string
}

/** 删除确认弹窗的目标（单条 1 个 / 批量选中项）。 */
interface DeleteTarget {
  id: string
  name: string
}

/** 一次性 Key 交付载荷：只存在于本页内存，关闭交付框即清空。 */
interface KeyDelivery {
  agentId: string
  agentName: string
  key: string | null
  unavailable: boolean
  mode: 'create' | 'rotate' | 'enable'
}

const filters = ref<AgentListFilters>(emptyAgentFilters())
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const items = ref<AgentSummaryDTO[]>([])
const listStatus = ref<ListStatus>('idle')
const listError = ref<ApiError | null>(null)

const selectedAgentId = ref<string | null>(null)
const detail = ref<AgentDetailDTO | null>(null)
const detailLoading = ref(false)
const detailError = ref<ApiError | null>(null)

/** 批量选择：只保存当前页可见的行 id，翻页/改筛选/刷新后自动收敛。 */
const selectedIds = ref<string[]>([])

const notice = ref<Notice | null>(null)
const showCreate = ref(false)
const showDisable = ref(false)
const showRole = ref(false)
const confirmRotate = ref(false)
const keyDelivery = ref<KeyDelivery | null>(null)

/** 删除确认弹窗（单条与批量共用）。 */
const showDelete = ref(false)
const deleteMode = ref<'single' | 'bulk'>('single')
const deleteTargets = ref<DeleteTarget[]>([])
const deleteExecutions = ref<AgentExecutionBriefDTO[] | null>(null)

/** 批量结果回执弹窗。 */
const showBulkResult = ref(false)
const bulkResult = ref<AgentBulkResultDTO | null>(null)
const bulkResultNames = ref<Record<string, string>>({})

const createAction = useAgentAction()
const rotateAction = useAgentAction()
const enableAction = useAgentAction()
const disableAction = useAgentAction()
const roleAction = useAgentAction()
const deleteAction = useAgentAction()
const bulkDeleteAction = useAgentAction()
const restoreAction = useAgentAction()
const bulkRestoreAction = useAgentAction()

/** 单条恢复的当前目标：换目标才换幂等键，同一目标的重试沿用原键。 */
const restoreTargetId = ref<string | null>(null)
/** 批量恢复的当前目标集合指纹：集合变化才换幂等键。 */
const bulkRestoreFingerprint = ref('')

const listGate = new LatestRequestGate()
const detailGate = new LatestRequestGate()
const deleteDetailGate = new LatestRequestGate()

const filterSummary = computed(() => agentFilterSummary(filters.value))
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const anyBusy = computed(
  () =>
    createAction.busy.value ||
    rotateAction.busy.value ||
    enableAction.busy.value ||
    disableAction.busy.value ||
    roleAction.busy.value ||
    deleteAction.busy.value ||
    bulkDeleteAction.busy.value ||
    restoreAction.busy.value ||
    bulkRestoreAction.busy.value,
)
const disableExecutions = computed<AgentExecutionBriefDTO[]>(() => detail.value?.current_executions ?? [])
const deleting = computed(() => deleteAction.busy.value || bulkDeleteAction.busy.value)
const deleteError = computed(() =>
  deleteMode.value === 'bulk' ? bulkDeleteAction.error.value : deleteAction.error.value,
)
/** 批量按钮的忙碌门：提交期间禁用，防止重复点击。 */
const bulkBusy = computed(
  () =>
    bulkDeleteAction.busy.value ||
    bulkRestoreAction.busy.value ||
    deleteAction.busy.value ||
    restoreAction.busy.value,
)

const emptyTitle = computed(() =>
  filters.value.deleted === 'true'
    ? '没有已删除的 Agent'
    : hasAgentFilters(filters.value)
      ? '没有符合筛选条件的 Agent'
      : '暂无 Agent',
)
const emptyDescription = computed(() =>
  filters.value.deleted === 'true'
    ? '删除的 Agent 会出现在这里，可随时恢复。'
    : hasAgentFilters(filters.value)
      ? `筛选条件：${filterSummary.value}`
      : '请先创建 Agent 并一次性交付 Key。',
)

// ------------------------------------------------------------------ 选择

const selectedItems = computed(() =>
  items.value.filter((agent) => selectedIds.value.includes(agent.id)),
)
const selectedCount = computed(() => selectedItems.value.length)
/** 选中项是否全部为已删除行（批量恢复的前提）。 */
const allSelectedDeleted = computed(
  () => selectedCount.value > 0 && selectedItems.value.every((agent) => isAgentDeleted(agent)),
)
/** 选中项是否全部为未删除行（批量删除的前提）。 */
const allSelectedActive = computed(
  () => selectedCount.value > 0 && selectedItems.value.every((agent) => !isAgentDeleted(agent)),
)
const allPageSelected = computed(
  () => items.value.length > 0 && selectedCount.value === items.value.length,
)
const partiallyPageSelected = computed(() => selectedCount.value > 0 && !allPageSelected.value)

function isRowSelected(agentId: string): boolean {
  return selectedIds.value.includes(agentId)
}

function toggleRowSelection(agentId: string): void {
  selectedIds.value = isRowSelected(agentId)
    ? selectedIds.value.filter((id) => id !== agentId)
    : [...selectedIds.value, agentId]
}

/** 表头全选：只覆盖当前页可见行。 */
function toggleSelectPage(): void {
  selectedIds.value = allPageSelected.value ? [] : items.value.map((agent) => agent.id)
}

function clearSelection(): void {
  selectedIds.value = []
}

// ------------------------------------------------------------------ 列表

async function loadList(request?: GatedRequest, silent = false): Promise<void> {
  const context = request ?? listGate.begin()
  if (!silent) {
    listStatus.value = 'loading'
    listError.value = null
  }
  try {
    const result = await listAgentsPage(toAgentListQuery(filters.value, page.value, pageSize.value), {
      signal: context.signal,
    })
    if (!context.isCurrent()) return
    items.value = result.items
    total.value = result.total
    page.value = result.page
    pageSize.value = result.page_size
    listStatus.value = result.items.length > 0 ? 'ready' : 'empty'
    listError.value = null
    // 选择只认当前可见行：不可见的行绝不留在批量操作里。
    const visibleIds = new Set(result.items.map((agent) => agent.id))
    selectedIds.value = selectedIds.value.filter((id) => visibleIds.has(id))
  } catch (thrown) {
    if (!context.isCurrent()) return
    listError.value = thrown instanceof ApiError ? thrown : null
    listStatus.value = silent && items.value.length > 0 ? 'ready' : 'error'
  }
}

async function refreshList(silent = true): Promise<void> {
  await loadList(listGate.begin(), silent)
}

function applyFilters(next: AgentListFilters): void {
  filters.value = { ...next }
  page.value = 1
  clearSelection()
  void refreshList(false)
}

function resetFilters(): void {
  filters.value = emptyAgentFilters()
  page.value = 1
  clearSelection()
  void refreshList(false)
}

function gotoPage(next: number): void {
  if (next < 1 || next > totalPages.value) return
  page.value = next
  clearSelection()
  void refreshList(false)
}

// ------------------------------------------------------------------ 详情

async function loadDetail(agentId: string | null = selectedAgentId.value, silent = false): Promise<void> {
  if (!agentId) return
  const context = detailGate.begin()
  if (!silent) {
    detailLoading.value = true
    detailError.value = null
  }
  try {
    const result = await getAgent(agentId, { signal: context.signal })
    if (!context.isCurrent() || selectedAgentId.value !== agentId) return
    detail.value = result
    detailError.value = null
  } catch (thrown) {
    if (!context.isCurrent()) return
    detailError.value = thrown instanceof ApiError ? thrown : null
  } finally {
    if (context.isCurrent()) detailLoading.value = false
  }
}

function selectAgent(agentId: string): void {
  selectedAgentId.value = agentId
  detail.value = null
  void loadDetail(agentId)
}

function closeDetail(): void {
  detailGate.cancel()
  selectedAgentId.value = null
  detail.value = null
  detailError.value = null
  detailLoading.value = false
}

// ------------------------------------------------------------------ 动作

function showNotice(kind: Notice['kind'], message: string): void {
  notice.value = { kind, message }
}

/**
 * 交付一次性 Key。服务端没有返回 `agent_key`（例如幂等重放）时，
 * 只显示“已无法再次交付，请重新重置”，绝不展示旧 Key。
 */
function deliverKey(
  result: { id: string; name: string; agent_key?: string | null },
  mode: KeyDelivery['mode'],
): void {
  const key = result.agent_key ?? null
  keyDelivery.value = {
    agentId: result.id,
    agentName: result.name,
    key,
    unavailable: !key,
    mode,
  }
}

function closeKeyDelivery(): void {
  // 关闭即清除：本页不再持有明文 Key。
  keyDelivery.value = null
}

async function submitCreate(input: CreateAgentInput): Promise<void> {
  try {
    const result = await createAction.run((key) => createAgent(input, key))
    if (!result) return
    showCreate.value = false
    deliverKey(result, 'create')
    showNotice('info', `已创建 Agent「${result.name}」。请立即保存交付的 Key。`)
    await refreshList()
    selectAgent(result.id)
  } catch {
    // 失败原因由 createAction.error 呈现，表单内容保留。
  }
}

function openRotateConfirm(): void {
  confirmRotate.value = true
}

async function submitRotate(): Promise<void> {
  const agent = detail.value
  if (!agent) return
  try {
    const result = await rotateAction.run((key) => rotateAgentKey(agent.id, key))
    if (!result) return
    confirmRotate.value = false
    deliverKey(result, 'rotate')
    showNotice('info', 'Key 已重置：旧 Key 立即失效，请保存新交付的 Key。')
    await refreshList()
    await loadDetail(agent.id, true)
  } catch {
    // 失败：确认框保留，错误按原幂等键可重试。
  }
}

async function submitEnable(): Promise<void> {
  const agent = detail.value
  if (!agent) return
  try {
    const result = await enableAction.run((key) => enableAgent(agent.id, false, key))
    if (!result) return
    if (result.agent_key) deliverKey(result, 'enable')
    showNotice('info', `已启用 Agent「${result.name}」。`)
    await refreshList()
    await loadDetail(agent.id, true)
  } catch {
    // 失败原因由 enableAction.error 呈现。
  }
}

async function submitDisable(payload: { disposition: 'WAIT' | 'NOW'; reason: string }): Promise<void> {
  const agent = detail.value
  if (!agent) return
  try {
    const result = await disableAction.run((key) =>
      disableAgent(agent.id, payload.disposition, payload.reason, key),
    )
    if (!result) return
    showDisable.value = false
    const reclaimed = result.reclaimed_execution_ids.length
    showNotice(
      'info',
      `已禁用 Agent「${result.name}」，处置 ${result.disposition}${
        reclaimed > 0 ? `，立即回收 ${reclaimed} 个执行（任务退回待办）` : '，未回收执行'
      }。`,
    )
    await refreshList()
    await loadDetail(agent.id, true)
  } catch {
    // 失败：表单保留（含原因与处置选择），错误按原键可重试。
  }
}

async function submitRole(role: AgentRole): Promise<void> {
  const agent = detail.value
  if (!agent) return
  try {
    const result = await roleAction.run((key) => updateAgentRole(agent.id, role, agent.version, key))
    if (!result) return
    showRole.value = false
    showNotice('info', `角色已变更为${agentRoleLabel(result.role)}。`)
    await refreshList()
    await loadDetail(agent.id, true)
  } catch {
    // 失败：表单保留，错误按原键可重试。
  }
}

// ------------------------------------------------------------------ 删除

const deleteReservedCount = computed(() =>
  deleteTargets.value.reduce((sum, target) => {
    const row = items.value.find((agent) => agent.id === target.id)
    return sum + (row?.reserved_execution_count ?? 0)
  }, 0),
)

/**
 * 单条删除：先读取目标的当前执行，弹窗据此决定是否要求 WAIT/NOW 处置；
 * 读取失败不阻塞删除，弹窗会要求明确处置，最终仍由服务端校验。
 */
async function openDeleteForRow(agent: AgentSummaryDTO): Promise<void> {
  deleteMode.value = 'single'
  deleteTargets.value = [{ id: agent.id, name: agent.name }]
  deleteExecutions.value = null
  deleteAction.reset()
  showDelete.value = true
  const context = deleteDetailGate.begin()
  try {
    const result = await getAgent(agent.id, { signal: context.signal })
    if (!context.isCurrent()) return
    deleteExecutions.value = result.current_executions ?? []
  } catch {
    if (context.isCurrent()) deleteExecutions.value = null
  }
}

/** 批量删除：目标就是当前选中行（选择只包含当前页可见行）。 */
function openBulkDelete(): void {
  const targets = selectedItems.value
  if (targets.length === 0) return
  deleteDetailGate.cancel()
  deleteMode.value = 'bulk'
  deleteTargets.value = targets.map((agent) => ({ id: agent.id, name: agent.name }))
  deleteExecutions.value = null
  bulkDeleteAction.reset()
  showDelete.value = true
}

function closeDelete(): void {
  deleteDetailGate.cancel()
  showDelete.value = false
  deleteTargets.value = []
  deleteExecutions.value = null
  deleteAction.reset()
  bulkDeleteAction.reset()
}

function closeBulkResult(): void {
  showBulkResult.value = false
  bulkResult.value = null
  bulkResultNames.value = {}
}

function openBulkResult(result: AgentBulkResultDTO, names: Record<string, string>): void {
  bulkResult.value = result
  bulkResultNames.value = names
  showBulkResult.value = true
  showNotice(
    result.failed.length > 0 ? 'error' : 'info',
    `${bulkResultSummaryText(result)}。`,
  )
}

async function submitDelete(payload: { reason: string; reclaim: 'WAIT' | 'NOW' | null }): Promise<void> {
  const targets = deleteTargets.value
  if (targets.length === 0) return

  if (deleteMode.value === 'bulk') {
    const names = Object.fromEntries(targets.map((target) => [target.id, target.name]))
    try {
      const result = await bulkDeleteAction.run((key) =>
        bulkDeleteAgents(
          { agentIds: targets.map((target) => target.id), reason: payload.reason, reclaim: payload.reclaim },
          key,
        ),
      )
      if (!result) return
      closeDelete()
      openBulkResult(result, names)
      clearSelection()
      await refreshList()
    } catch {
      // 整体失败（权限/参数）：确认框保留，错误按原键可重试。
    }
    return
  }

  const target = targets[0]!
  try {
    const result = await deleteAction.run((key) =>
      deleteAgent(target.id, { reason: payload.reason, reclaim: payload.reclaim }, key),
    )
    if (!result) return
    const reclaimed = result.reclaimed_execution_ids.length
    closeDelete()
    showNotice(
      'info',
      `已删除 Agent「${result.name}」${
        reclaimed > 0 ? `，立即回收 ${reclaimed} 个执行（任务退回待办）` : ''
      }；可在「已删除」中恢复。`,
    )
    clearSelection()
    await refreshList()
    if (selectedAgentId.value === target.id) await loadDetail(target.id, true)
  } catch {
    // 失败：弹窗保留（含原因与处置），服务端原因原样展示，可按原键重试。
  }
}

// ------------------------------------------------------------------ 恢复

/** 单条恢复 = 对已删除的 Agent 调 enable（服务端清空 deleted_at 并重新启用）。 */
async function submitRestore(agent: { id: string; name: string }): Promise<void> {
  if (restoreTargetId.value !== agent.id) {
    // 换目标 = 换一次逻辑动作，必须换幂等键（同一键不能用于不同的资源）。
    restoreAction.reset()
    restoreTargetId.value = agent.id
  }
  try {
    const result = await restoreAction.run((key) => enableAgent(agent.id, false, key))
    if (!result) return
    if (result.agent_key) deliverKey(result, 'enable')
    showNotice('info', `已恢复 Agent「${result.name}」。`)
    selectedIds.value = selectedIds.value.filter((id) => id !== agent.id)
    await refreshList()
    if (selectedAgentId.value === agent.id) await loadDetail(agent.id, true)
  } catch {
    const error = restoreAction.error.value
    showNotice('error', error ? error.displayMessage : `恢复 Agent「${agent.name}」失败。`)
  }
}

function restoreFromDetail(): void {
  const agent = detail.value
  if (agent) void submitRestore(agent)
}

/** 批量恢复：逐项结果（一项失败不影响其他项）由回执弹窗列出。 */
async function submitBulkRestore(): Promise<void> {
  const targets = selectedItems.value
  if (targets.length === 0) return
  const ids = targets.map((agent) => agent.id)
  const fingerprint = ids.join(',')
  if (bulkRestoreFingerprint.value !== fingerprint) {
    bulkRestoreAction.reset()
    bulkRestoreFingerprint.value = fingerprint
  }
  const names = Object.fromEntries(targets.map((agent) => [agent.id, agent.name]))
  try {
    const result = await bulkRestoreAction.run((key) => bulkRestoreAgents(ids, key))
    if (!result) return
    openBulkResult(result, names)
    clearSelection()
    await refreshList()
    if (selectedAgentId.value && ids.includes(selectedAgentId.value)) {
      await loadDetail(selectedAgentId.value, true)
    }
  } catch {
    const error = bulkRestoreAction.error.value
    showNotice('error', error ? error.displayMessage : '批量恢复失败。')
  }
}

function closeCreate(): void {
  showCreate.value = false
  createAction.reset()
}

function closeDisable(): void {
  showDisable.value = false
  disableAction.reset()
}

function openDisableForm(): void {
  showDisable.value = true
}

function closeRole(): void {
  showRole.value = false
  roleAction.reset()
}

function openRoleForm(): void {
  showRole.value = true
}

function closeRotate(): void {
  confirmRotate.value = false
  rotateAction.reset()
}

// ------------------------------------------------------------------ 生命周期

const poller: VisiblePagePoller = createPoller({ intervalMs: 10_000, immediateOnStart: false })

onMounted(() => {
  void refreshList(false)
  poller.start(async () => {
    await refreshList(true)
    if (selectedAgentId.value) await loadDetail(selectedAgentId.value, true)
  })
})

onBeforeUnmount(() => {
  poller.stop()
  listGate.cancel()
  detailGate.cancel()
  deleteDetailGate.cancel()
  keyDelivery.value = null
})
</script>

<template>
  <div class="page" data-testid="view-agents">
    <header class="page-head">
      <div class="page-head__text">
        <h2 class="page-title">Agent 管理</h2>
        <p class="page-subtitle">列表展示服务端判定的在线状态与容量槽；明文 Key 只在创建或重置成功后一次性交付。</p>
      </div>
      <div class="page-actions">
        <button
          type="button"
          class="btn btn--primary"
          data-testid="agent-create-open"
          @click="showCreate = true"
        >
          创建 Agent
        </button>
      </div>
    </header>

    <AgentFiltersBar
      :filters="filters"
      :loading="listStatus === 'loading'"
      @apply="applyFilters"
      @reset="resetFilters"
    />

    <p class="page-subtitle" data-testid="agent-filter-summary">
      当前筛选：{{ filterSummary }} · 共 {{ total }} 个 Agent
    </p>

    <p
      v-if="notice"
      class="notice"
      :class="notice.kind === 'error' ? 'notice--error' : 'notice--info'"
      role="status"
      data-testid="agent-notice"
    >
      {{ notice.message }}
    </p>

    <div class="agents-body" :class="{ 'agents-body--split': Boolean(selectedAgentId) }">
      <div class="agents-main section">
        <LoadingState v-if="listStatus === 'loading'" label="加载 Agent 列表…" />
        <ErrorState v-else-if="listStatus === 'error'" :error="listError" @retry="refreshList(false)" />
        <EmptyState
          v-else-if="listStatus === 'empty'"
          :title="emptyTitle"
          :description="emptyDescription"
        />

        <template v-else-if="listStatus === 'ready'">
          <!-- 行选择时出现：取消选择 / 批量删除（仅未删除行）/ 批量恢复（仅已删除行） -->
          <div v-if="selectedCount > 0" class="toolbar agent-bulk" data-testid="agent-bulk-bar">
            <p class="toolbar__summary" data-testid="agent-bulk-count">
              已选 {{ selectedCount }} 项
            </p>
            <span class="toolbar__spacer" />
            <div class="btn-group">
              <button
                type="button"
                class="btn btn--danger btn--sm"
                data-testid="agent-bulk-delete"
                :disabled="!allSelectedActive || bulkBusy"
                :title="allSelectedActive ? '删除选中的 Agent（必须填写原因）' : '已删除的行只能批量恢复'"
                @click="openBulkDelete"
              >
                {{ bulkDeleteAction.busy.value ? '删除中…' : '批量删除' }}
              </button>
              <button
                type="button"
                class="btn btn--sm"
                data-testid="agent-bulk-restore"
                :disabled="!allSelectedDeleted || bulkBusy"
                :title="allSelectedDeleted ? '恢复选中的已删除 Agent' : '批量恢复只适用于已删除的行'"
                @click="submitBulkRestore"
              >
                {{ bulkRestoreAction.busy.value ? '恢复中…' : '批量恢复' }}
              </button>
              <button
                type="button"
                class="btn btn--ghost btn--sm"
                data-testid="agent-bulk-clear"
                :disabled="bulkBusy"
                @click="clearSelection"
              >
                取消选择
              </button>
            </div>
          </div>

          <div class="table-wrap">
            <table class="table agents-table" data-testid="agent-table">
              <thead>
                <tr>
                  <th scope="col" class="agents-table__check">
                    <input
                      type="checkbox"
                      aria-label="全选当前页"
                      data-testid="agent-select-all"
                      :checked="allPageSelected"
                      :indeterminate="partiallyPageSelected"
                      @change="toggleSelectPage"
                    />
                  </th>
                  <th scope="col">名称</th>
                  <th scope="col">角色</th>
                  <th scope="col">显示状态</th>
                  <th scope="col">启用</th>
                  <th scope="col">容量槽（已占/上限）</th>
                  <th scope="col">可用容量</th>
                  <th scope="col">授权项目数</th>
                  <th scope="col">最后心跳</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="agent in items"
                  :key="agent.id"
                  :data-testid="`agent-row-${agent.id}`"
                  :class="{
                    'is-selected': agent.id === selectedAgentId,
                    'is-checked': isRowSelected(agent.id),
                  }"
                >
                  <td class="agents-table__check">
                    <input
                      type="checkbox"
                      :aria-label="`选择 ${agent.name}`"
                      :data-testid="`agent-row-checkbox-${agent.id}`"
                      :checked="isRowSelected(agent.id)"
                      @change="toggleRowSelection(agent.id)"
                    />
                  </td>
                  <td>
                    <span class="row agents-table__identity">
                      <span class="avatar avatar--muted" aria-hidden="true">{{ agentInitial(agent.name) }}</span>
                      <span class="truncate strong" :data-testid="`agent-row-name-${agent.id}`">{{ agent.name }}</span>
                      <span
                        v-if="isAgentDeleted(agent)"
                        class="badge badge--danger"
                        :title="agentDeletedTooltip(agent)"
                        :data-testid="`agent-row-deleted-${agent.id}`"
                      >
                        {{ AGENT_DELETED_BADGE_LABEL }}
                      </span>
                    </span>
                  </td>
                  <td>
                    <span
                      class="badge"
                      :class="agent.role === 'SUPERVISOR' ? 'badge--primary' : 'badge--neutral'"
                      :data-testid="`agent-row-role-${agent.id}`"
                    >
                      {{ agentRoleLabel(agent.role) }}
                    </span>
                  </td>
                  <td>
                    <span
                      class="badge"
                      :class="ONLINE_STATE_BADGE_CLASS[agentDisplayState(agent)]"
                      :data-state="agentDisplayState(agent)"
                      :data-testid="`agent-row-state-${agent.id}`"
                    >
                      {{ agentStateLabel(agent) }}
                    </span>
                  </td>
                  <td>
                    <span
                      class="badge"
                      :class="agent.enabled ? 'badge--success' : 'badge--neutral'"
                      :data-testid="`agent-row-enabled-${agent.id}`"
                    >
                      {{ agent.enabled ? '已启用' : '已禁用' }}
                    </span>
                  </td>
                  <td>
                    <span class="tabular" :data-testid="`agent-row-reserved-${agent.id}`">
                      {{ listCellSlotText(agent) }}
                    </span>
                  </td>
                  <td>
                    <span class="tabular" :data-testid="`agent-row-available-${agent.id}`">
                      {{ agent.available_capacity }}
                    </span>
                  </td>
                  <td>
                    <span class="tabular" :data-testid="`agent-row-projects-${agent.id}`">
                      {{ (agent.project_ids ?? []).length }}
                    </span>
                  </td>
                  <td>
                    <span
                      class="tabular text-xs nowrap"
                      :title="agent.last_heartbeat_at ?? ''"
                      :data-testid="`agent-row-heartbeat-${agent.id}`"
                    >
                      {{ formatAgentTime(agent.last_heartbeat_at) || '从未连接' }}
                    </span>
                  </td>
                  <td>
                    <span class="btn-group agents-table__actions">
                      <button
                        type="button"
                        class="btn btn--sm"
                        :data-testid="`agent-row-detail-${agent.id}`"
                        @click="selectAgent(agent.id)"
                      >
                        查看详情
                      </button>
                      <button
                        v-if="isAgentDeleted(agent)"
                        type="button"
                        class="btn btn--sm"
                        :data-testid="`agent-restore-${agent.id}`"
                        :disabled="bulkBusy"
                        @click="submitRestore(agent)"
                      >
                        恢复
                      </button>
                      <button
                        v-else
                        type="button"
                        class="btn btn--sm btn--danger"
                        :data-testid="`agent-delete-${agent.id}`"
                        :disabled="deleting"
                        @click="openDeleteForRow(agent)"
                      >
                        删除
                      </button>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="agents-main__foot">
            <p class="section-hint" data-testid="agent-list-note">
              列表展示服务端返回的占槽与可用容量；“有效执行（工作中）”“等待回收”在详情中单独展示。
            </p>
            <nav class="pager" aria-label="分页">
              <button
                type="button"
                class="btn btn--sm"
                data-testid="agent-page-prev"
                :disabled="page <= 1"
                @click="gotoPage(page - 1)"
              >
                上一页
              </button>
              <span class="pager__info tabular" data-testid="agent-page-info">
                第 {{ page }} / {{ totalPages }} 页（共 {{ total }} 条，每页 {{ pageSize }}）
              </span>
              <button
                type="button"
                class="btn btn--sm"
                data-testid="agent-page-next"
                :disabled="page >= totalPages"
                @click="gotoPage(page + 1)"
              >
                下一页
              </button>
            </nav>
          </div>
        </template>
      </div>

      <div v-if="selectedAgentId" class="agents-body__side">
        <AgentDetailPanel
          :agent="detail"
          :loading="detailLoading"
          :error="detailError"
          :busy="anyBusy"
          @refresh="loadDetail(selectedAgentId, false)"
          @rotate-key="openRotateConfirm"
          @enable="submitEnable"
          @disable="openDisableForm"
          @restore="restoreFromDetail"
          @change-role="openRoleForm"
          @close="closeDetail"
        />
      </div>
    </div>

    <AgentCreateForm
      :open="showCreate"
      :busy="createAction.busy.value"
      :error="createAction.error.value"
      @close="closeCreate"
      @submit="submitCreate"
    />

    <AgentDisableForm
      :open="showDisable"
      :agent-name="detail?.name ?? ''"
      :executions="disableExecutions"
      :reserved-count="detail?.capacity.reserved_execution_count ?? 0"
      :busy="disableAction.busy.value"
      :error="disableAction.error.value"
      @close="closeDisable"
      @submit="submitDisable"
    />

    <AgentDeleteForm
      :open="showDelete"
      :mode="deleteMode"
      :targets="deleteTargets"
      :executions="deleteExecutions"
      :reserved-count="deleteReservedCount"
      :busy="deleting"
      :error="deleteError"
      @close="closeDelete"
      @submit="submitDelete"
    />

    <AgentBulkResultDialog
      :open="showBulkResult"
      :result="bulkResult"
      :names="bulkResultNames"
      @close="closeBulkResult"
    />

    <AgentRoleForm
      :open="showRole"
      :agent="detail"
      :busy="roleAction.busy.value"
      :error="roleAction.error.value"
      @close="closeRole"
      @submit="submitRole"
    />

    <BaseDialog
      :open="confirmRotate"
      title="重置 Agent Key"
      description="重置后旧 Key 立即失效，新 Key 只在下方交付框显示一次。"
      @close="closeRotate"
    >
      <div class="stack">
        <p data-testid="agent-rotate-confirm-text">
          确认重置「{{ detail?.name }}」的 Key？已领取的执行及其固定截止时刻不会改变。
        </p>
        <p
          v-if="rotateAction.error.value"
          class="notice notice--error"
          role="alert"
          data-testid="agent-rotate-error"
        >
          {{ rotateAction.error.value.message }}（{{ rotateAction.error.value.code }}）
        </p>
      </div>
      <template #footer>
        <button type="button" class="btn" data-testid="agent-rotate-cancel" @click="closeRotate">取消</button>
        <button
          type="button"
          class="btn btn--primary"
          data-testid="agent-rotate-confirm"
          :disabled="rotateAction.busy.value"
          @click="submitRotate"
        >
          {{ rotateAction.busy.value ? '重置中…' : '确认重置' }}
        </button>
      </template>
    </BaseDialog>

    <AgentKeyDeliveryDialog
      :open="keyDelivery !== null"
      :agent-name="keyDelivery?.agentName ?? ''"
      :agent-key="keyDelivery?.key ?? null"
      :key-delivery-unavailable="keyDelivery?.unavailable ?? false"
      :mode="keyDelivery?.mode ?? 'create'"
      @close="closeKeyDelivery"
    />
  </div>
</template>

<style scoped>
/* 列表与详情：宽屏并排（详情为右栏粘性面板），窄屏退化为单列 */
.agents-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: var(--space-4);
  align-items: start;
  min-width: 0;
}

.agents-body--split {
  grid-template-columns: minmax(0, 1fr) minmax(340px, 420px);
}

.agents-body__side {
  position: sticky;
  top: calc(var(--topbar-height) + var(--space-4));
  min-width: 0;
}

@media (max-width: 1180px) {
  .agents-body--split {
    grid-template-columns: minmax(0, 1fr);
  }

  .agents-body__side {
    position: static;
  }
}

.agents-main__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.agents-main__foot .section-hint {
  flex: 1 1 20rem;
  max-width: 70ch;
}

.pager {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-left: auto;
}

.pager__info {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}

/* 表格：名称列不换行截断，末尾操作列右对齐 */
.agents-table__identity {
  min-width: 10rem;
}

.agents-table td:last-child,
.agents-table th:last-child {
  text-align: right;
}

.agents-table tbody tr.is-selected {
  background: var(--color-primary-soft);
  box-shadow: inset 3px 0 0 var(--color-primary);
}

/* 批量选择的行：与详情选中态可同时出现（勾选标记在首列） */
.agents-table tbody tr.is-checked {
  background: var(--color-surface-sunken);
}

.agents-table tbody tr.is-checked.is-selected {
  background: var(--color-primary-soft);
}

.agents-table__check {
  width: 2.5rem;
  text-align: center;
}

.agents-table__check input[type='checkbox'] {
  width: 1rem;
  height: 1rem;
  accent-color: var(--color-primary);
}

/* 操作列：按钮组按行内右对齐（保持既有的右对齐视觉） */
.agents-table__actions {
  display: inline-flex;
}

.agent-bulk {
  align-items: center;
}

.agent-bulk .toolbar__summary {
  color: var(--color-text-secondary);
}

.nowrap {
  white-space: nowrap;
}
</style>
