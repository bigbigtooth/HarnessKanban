<script setup lang="ts">
/**
 * 任务模块：默认展示全部项目的任务，支持项目 / 难度 / 领取模式 / 执行者 / 关键词 / 含归档
 * 六种筛选；点卡片打开任务详情浮窗。
 *
 * 布局沿用 demo 的 `.view-head` + `.board-bar` + `.board`（五列）。
 */
import { computed, onMounted, ref } from 'vue'

import { DIFFICULTY_LABEL, type Difficulty, type TaskStatus } from '@/api/types'
import type { TaskListItemDTO } from '@/api/tasks'
import { transitionAction, transitionRefusalMessage } from '@/components/task/task-format'
import AppIcon from './AppIcon.vue'
import { resolveDropAnchors } from './home-format'
import HomeTaskCard from './HomeTaskCard.vue'
import {
  BOARD_COLUMNS,
  activeFilterCount,
  activeAgents,
  agentAvatar,
  clearFilters,
  homeFilters,
  loadTasks,
  projectById,
  projectList,
  setFilter,
  setKeyword,
  taskList,
  taskStats,
  tasksByStatus,
} from '@/stores/home'
import type { AvatarView } from '@/stores/home'

const emit = defineEmits<{
  openTask: [task: TaskListItemDTO]
  createTask: [status: string]
  /** 卡片被拖到另一列：交给父组件按合法动作处理（发布/领取/提交/审核/重开…）。 */
  transition: [payload: { task: TaskListItemDTO; action: NonNullable<ReturnType<typeof transitionAction>>; preferredApprove: boolean }]
  /**
   * 列内调整顺序。锚点语义与 `POST /projects/{id}/task-order` 一致：
   * `beforeTaskId` 是目标位置左侧的卡片（移动后排在它后面），
   * `afterTaskId` 是目标位置右侧的卡片（移动后排在它前面）。
   */
  reorder: [payload: { task: TaskListItemDTO; status: TaskStatus; beforeTaskId: string | null; afterTaskId: string | null }]
  /** 非法转换：父组件负责提示并保持服务端顺序。 */
  refused: [message: string]
}>()

// ---------------------------------------------------------------------------
// 拖拽换列 / 列内排序
// ---------------------------------------------------------------------------
const draggedTask = ref<TaskListItemDTO | null>(null)
const dragOverColumn = ref<TaskStatus | null>(null)
/** 放置位置：在该列第几张卡片之前（= 卡片数量表示放到末尾）。 */
const dropIndex = ref(0)

function onCardDragStart(task: TaskListItemDTO): void {
  draggedTask.value = task
  dragOverColumn.value = task.status
  dropIndex.value = tasksByStatus.value[task.status].findIndex((item) => item.id === task.id)
}

function onCardDragEnd(): void {
  draggedTask.value = null
  dragOverColumn.value = null
}

/** 指针位置 → 插入下标（按每张卡片的中线判断）。 */
function computeDropIndex(list: HTMLElement, clientY: number): number {
  const cards = Array.from(list.querySelectorAll<HTMLElement>(':scope > .task-card'))
  for (let index = 0; index < cards.length; index += 1) {
    const rect = cards[index]!.getBoundingClientRect()
    if (clientY < rect.top + rect.height / 2) return index
  }
  return cards.length
}

function onColumnDragOver(status: TaskStatus, event: DragEvent): void {
  if (!draggedTask.value) return
  // 只有真正能产生业务动作的列才显示可放置状态（非法列保持“禁止”光标）
  const legal = draggedTask.value.status === status || transitionAction(draggedTask.value.status, status) !== null
  if (!legal) {
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'none'
    return
  }
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move'
  dragOverColumn.value = status
  const list = event.currentTarget as HTMLElement | null
  if (list) dropIndex.value = computeDropIndex(list, event.clientY)
}

function onColumnDragLeave(status: TaskStatus, event: DragEvent): void {
  // 指针移到子元素上也会触发 dragleave：只有真正离开整列才清除高亮
  const next = event.relatedTarget as Node | null
  const current = event.currentTarget as HTMLElement | null
  if (next && current?.contains(next)) return
  if (dragOverColumn.value === status) dragOverColumn.value = null
}

function onColumnDrop(status: TaskStatus, event: DragEvent): void {
  event.preventDefault()
  const task = draggedTask.value
  if (!task) return
  const targetIndex = dropIndex.value
  const sameColumn = task.status === status
  draggedTask.value = null
  dragOverColumn.value = null

  if (!sameColumn) {
    const action = transitionAction(task.status, status)
    if (!action) {
      emit('refused', transitionRefusalMessage(task.status, status))
      return
    }
    emit('transition', { task, action, preferredApprove: status === 'DONE' })
    return
  }

  // 列内排序：拖回原位置视为无操作；锚点语义见 resolveDropAnchors。
  const anchors = resolveDropAnchors(
    tasksByStatus.value[status].map((item) => item.id),
    task.id,
    targetIndex,
  )
  if (!anchors.changed) return
  emit('reorder', {
    task,
    status,
    beforeTaskId: anchors.beforeTaskId,
    afterTaskId: anchors.afterTaskId,
  })
}

const todayLabel = computed(() => {
  const now = new Date()
  const week = ['日', '一', '二', '三', '四', '五', '六'][now.getDay()]
  return `${now.getMonth() + 1}月${now.getDate()}日 · 周${week}`
})

const scopeTitle = computed(() =>
  homeFilters.projectId ? (projectById.value.get(homeFilters.projectId)?.title ?? '全部任务') : '全部任务',
)

const difficultyOptions = computed(() =>
  ([1, 2, 3, 4, 5] as Difficulty[]).map((level) => ({ value: String(level), label: DIFFICULTY_LABEL[level] })),
)

/**
 * 快速筛选用的 Agent：优先取当前任务范围里真正有任务的执行者（按任务数排序），
 * 该范围还没有任何执行者时退回在线 Agent 名录；最多 6 个，避免头部过长。
 */
const quickAgents = computed(() => {
  const counts = new Map<string, number>()
  for (const task of taskList.items) {
    const agentId = task.assignee_agent_id ?? task.designated_agent_id
    if (agentId) counts.set(agentId, (counts.get(agentId) ?? 0) + 1)
  }
  const withTasks = [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([agentId]) => activeAgents.value.find((agent) => agent.id === agentId))
    .filter((agent): agent is NonNullable<typeof agent> => Boolean(agent))
  return (withTasks.length > 0 ? withTasks : activeAgents.value).slice(0, 6)
})

const summaryText = computed(() => {
  const stats = taskStats.value
  const parts = [
    `${taskList.total || stats.total} 张卡片`,
    `${stats.inFlight} 件在推进`,
  ]
  if (activeFilterCount.value > 0) parts.unshift(`已筛选 ${stats.total} 张`)
  return parts.join(' · ')
})

const showSkeleton = computed(() => taskList.status === 'loading' && taskList.items.length === 0)
const showError = computed(() => taskList.status === 'error' && taskList.items.length === 0)
const filterActive = computed(() => activeFilterCount.value > 0)

function ownerOf(task: TaskListItemDTO): { name: string; avatar: AvatarView } | null {
  const agentId = task.assignee_agent_id ?? task.designated_agent_id
  if (!agentId) return null
  const name = task.assignee_agent_id ? agentLabel(task.assignee_agent_id) : `指定 ${agentLabel(task.designated_agent_id ?? '')}`
  return { name, avatar: agentAvatar({ id: agentId, name }) }
}

function agentLabel(agentId: string): string {
  const agent = activeAgents.value.find((item) => item.id === agentId)
  return agent?.name ?? '未知 Agent'
}

function onProjectChange(event: Event): void {
  setFilter('projectId', (event.target as HTMLSelectElement).value)
}

function onDifficultyChange(event: Event): void {
  const value = (event.target as HTMLSelectElement).value
  setFilter('difficulty', value === '' ? '' : (Number(value) as Difficulty))
}

function onClaimModeChange(event: Event): void {
  setFilter('claimMode', (event.target as HTMLSelectElement).value as '' | 'OPEN' | 'ASSIGNED')
}

function toggleAssignee(agentId: string): void {
  setFilter('assigneeAgentId', homeFilters.assigneeAgentId === agentId ? '' : agentId)
}

function toggleArchived(): void {
  setFilter('includeArchived', !homeFilters.includeArchived)
}

onMounted(() => {
  if (taskList.status === 'idle') void loadTasks()
})
</script>

<template>
  <section class="view view-tasks" data-testid="view-home-tasks">
    <div class="page-wrap">
      <div class="view-head">
        <div class="view-head-copy">
          <div class="eyebrow">
            <span class="live-dot" aria-hidden="true" />
            <span>{{ todayLabel }}</span>
            <span class="eyebrow-sep" aria-hidden="true" />
            <span class="eyebrow-long">全部项目 · 宜：把小事做好</span>
          </div>
          <h1>任务看板<span class="highlight">一览</span><span class="title-sticker" aria-hidden="true">🌷</span></h1>
          <p>默认展示所有项目的任务，筛选、搜索，点开卡片看详情。</p>
        </div>
        <div class="view-head-side">
          <div class="progress-card" aria-label="任务完成进度">
            <div class="donut">
              <svg viewBox="0 0 44 44" aria-hidden="true">
                <defs>
                  <linearGradient id="donutGradient" x1="4" y1="4" x2="40" y2="40" gradientUnits="userSpaceOnUse">
                    <stop stop-color="#8fe0c5" />
                    <stop offset="1" stop-color="#a9d8ff" />
                  </linearGradient>
                </defs>
                <circle class="donut-track" cx="22" cy="22" r="18" />
                <circle
                  class="donut-arc"
                  cx="22"
                  cy="22"
                  r="18"
                  :style="{
                    strokeDasharray: String(2 * Math.PI * 18),
                    strokeDashoffset: String(2 * Math.PI * 18 * (1 - taskStats.percent / 100)),
                  }"
                />
              </svg>
              <span class="donut-pct">{{ taskStats.percent }}%</span>
            </div>
            <div class="progress-copy">
              <strong>任务完成度</strong>
              <span>
                {{ taskStats.total === 0 ? '还没有任务哦' : `已完成 ${taskStats.done} / ${taskStats.total} 件` }}
              </span>
            </div>
          </div>
          <button class="btn btn-primary btn-lg" type="button" data-testid="new-task" @click="emit('createTask', 'TODO')">
            <AppIcon name="plus" />
            新建任务
          </button>
        </div>
      </div>

      <div class="board-bar">
        <div class="board-ident">
          <span class="board-badge" aria-hidden="true">🫧</span>
          <div>
            <strong>{{ scopeTitle }}</strong>
            <small data-testid="task-summary">{{ summaryText }}</small>
          </div>
        </div>
        <div class="board-tools">
          <select
            class="select select-compact"
            aria-label="按项目筛选"
            data-testid="filter-project"
            :value="homeFilters.projectId"
            @change="onProjectChange"
          >
            <option value="">全部项目</option>
            <option
              v-for="project in projectList.items.filter((item) => !item.deleted_at)"
              :key="project.id"
              :value="project.id"
            >
              {{ project.title }}
            </option>
          </select>
          <select
            class="select select-compact"
            aria-label="按难度筛选"
            data-testid="filter-difficulty"
            :value="homeFilters.difficulty"
            @change="onDifficultyChange"
          >
            <option value="">全部难度</option>
            <option v-for="option in difficultyOptions" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
          <select
            class="select select-compact"
            aria-label="按领取模式筛选"
            data-testid="filter-claim-mode"
            :value="homeFilters.claimMode"
            @change="onClaimModeChange"
          >
            <option value="">全部模式</option>
            <option value="OPEN">开放领取</option>
            <option value="ASSIGNED">指定执行者</option>
          </select>
          <div class="people-filter" role="group" aria-label="按执行者筛选任务">
            <button
              type="button"
              class="p-filter all"
              :aria-pressed="homeFilters.assigneeAgentId === ''"
              data-testid="filter-agent-all"
              @click="toggleAssignee('')"
            >
              全部
            </button>
            <button
              v-for="agent in quickAgents"
              :key="agent.id"
              type="button"
              class="p-filter"
              :aria-pressed="homeFilters.assigneeAgentId === agent.id"
              :title="agent.name"
              :aria-label="`只看 ${agent.name}`"
              @click="toggleAssignee(agent.id)"
            >
              <span aria-hidden="true">{{ agentAvatar(agent).emoji }}</span>
            </button>
          </div>
          <label class="search">
            <AppIcon name="search" />
            <span class="sr-only">搜索任务</span>
            <input
              type="search"
              placeholder="搜索任务标题 / 需求"
              autocomplete="off"
              data-testid="task-search"
              :value="homeFilters.keyword"
              @input="setKeyword(($event.target as HTMLInputElement).value)"
            />
          </label>
          <button
            type="button"
            class="chip"
            :aria-pressed="homeFilters.includeArchived"
            data-testid="filter-archived"
            @click="toggleArchived"
          >
            含归档
          </button>
          <button
            v-if="filterActive"
            type="button"
            class="btn btn-soft btn-sm"
            data-testid="clear-filters"
            @click="clearFilters()"
          >
            清空筛选<span class="filter-count">{{ activeFilterCount }}</span>
          </button>
        </div>
      </div>

      <div v-if="showSkeleton" class="board-state" data-testid="tasks-loading">
        <span class="state-face" aria-hidden="true">🫧</span>
        <strong>正在把卡片排好…</strong>
        <span>稍等一下，马上就好。</span>
      </div>
      <div v-else-if="showError" class="board-state is-error" data-testid="tasks-error">
        <span class="state-face" aria-hidden="true">☁️</span>
        <strong>任务没拉下来</strong>
        <span>{{ taskList.error?.displayMessage ?? '请稍后重试' }}</span>
        <button class="btn btn-soft" type="button" @click="loadTasks()">重新加载</button>
      </div>
      <div v-else class="board" aria-label="任务看板" data-testid="task-board">
        <section
          v-for="column in BOARD_COLUMNS"
          :key="column.status"
          class="column"
          :class="{ 'drag-over': dragOverColumn === column.status && draggedTask?.status !== column.status }"
          :data-column="column.status"
          :style="{ '--accent': `var(${column.accentVar})`, '--accent-line': `var(${column.lineVar})` }"
        >
          <div class="col-head">
            <div class="col-title">
              <span class="col-emoji" aria-hidden="true">{{ column.emoji }}</span>
              <div class="col-name">
                <h2>{{ column.name }}</h2>
                <span :data-testid="`column-count-${column.status}`">{{ tasksByStatus[column.status].length }}</span>
              </div>
            </div>
            <button
              class="col-add"
              type="button"
              :aria-label="`在「${column.name}」新建任务`"
              :title="`在${column.name}新建任务`"
              @click="emit('createTask', column.status)"
            >
              <AppIcon name="plus" />
            </button>
          </div>
          <div
            class="task-list"
            :class="{ 'is-drop-target': draggedTask !== null }"
            :data-col="column.status"
            role="list"
            @dragover="onColumnDragOver(column.status, $event)"
            @dragenter.prevent="onColumnDragOver(column.status, $event)"
            @dragleave="onColumnDragLeave(column.status, $event)"
            @drop="onColumnDrop(column.status, $event)"
          >
            <template v-for="(task, index) in tasksByStatus[column.status]" :key="task.id">
              <div
                v-if="
                  dragOverColumn === column.status &&
                  draggedTask !== null &&
                  draggedTask.id !== task.id &&
                  dropIndex === index
                "
                class="drop-placeholder"
                aria-hidden="true"
              />
              <HomeTaskCard
                :task="task"
                :index="index"
                :owner="ownerOf(task)"
                @open="emit('openTask', $event)"
                @dragstart="onCardDragStart"
                @dragend="onCardDragEnd"
              />
            </template>
            <div
              v-if="
                dragOverColumn === column.status &&
                draggedTask !== null &&
                dropIndex >= tasksByStatus[column.status].length
              "
              class="drop-placeholder"
              aria-hidden="true"
            />
            <div v-if="tasksByStatus[column.status].length === 0" class="empty-col">
              <span class="empty-face" aria-hidden="true">{{ filterActive ? '☁️' : column.emoji }}</span>
              <span>{{ filterActive ? '没有符合条件的卡片' : `这一列还空着 · ${column.hint}` }}</span>
            </div>
          </div>
          <button class="quick-add" type="button" @click="emit('createTask', column.status)">
            <AppIcon name="plus" />
            加一张卡片
          </button>
        </section>
      </div>
    </div>
  </section>
</template>
