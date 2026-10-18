<script setup lang="ts">
/**
 * 首页：两个核心模块 —— 任务（默认）与项目，用顶部胶囊标签切换。
 *
 * 路由驱动标签：`/` = 任务，`/projects` = 项目；`?task=<id>` 直接打开任务详情浮窗，
 * `?project=<id>` 把任务模块预先筛到某个项目（项目详情的「只看这个项目的任务」用它）。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, newIdempotencyKey } from '@/api/client'
import {
  getTask,
  publishTask,
  reorderTask,
  restoreTask,
  type TaskDetailDTO,
  type TaskListItemDTO,
} from '@/api/tasks'
import type { ReviewDecision, TaskStatus } from '@/api/types'
import type { ProjectDetailDTO, ProjectSummaryDTO } from '@/api/projects'
import TaskAssignForm from '@/components/task/TaskAssignForm.vue'
import TaskClaimForm from '@/components/task/TaskClaimForm.vue'
import TaskProgressForm from '@/components/task/TaskProgressForm.vue'
import TaskReasonForm from '@/components/task/TaskReasonForm.vue'
import TaskReclaimForm from '@/components/task/TaskReclaimForm.vue'
import TaskReviewForm from '@/components/task/TaskReviewForm.vue'
import TaskSubmitForm from '@/components/task/TaskSubmitForm.vue'
import type { TaskActionKind } from '@/components/task/task-format'
import HomeBoard from '@/components/home/HomeBoard.vue'
import HomeProjects from '@/components/home/HomeProjects.vue'
import ProjectDetailModal from '@/components/home/ProjectDetailModal.vue'
import ProjectFormModal from '@/components/home/ProjectFormModal.vue'
import TaskDetailModal from '@/components/home/TaskDetailModal.vue'
import TaskFormModal from '@/components/home/TaskFormModal.vue'
import { burstConfetti, showToast } from '@/components/home/feedback'
import {
  applyFilters,
  homeFilters,
  refreshHome,
  refreshHomeSilently,
  startHomePolling,
  stopHomePolling,
  taskList,
} from '@/stores/home'

const props = defineProps<{ tab: 'tasks' | 'projects' }>()

const route = useRoute()
const router = useRouter()

const selectedTask = ref<TaskListItemDTO | null>(null)
const selectedProject = ref<ProjectSummaryDTO | null>(null)
const projectFormMode = ref<'create' | 'edit'>('create')
const projectFormOpen = ref(false)
const projectFormTarget = ref<ProjectDetailDTO | ProjectSummaryDTO | null>(null)
const taskFormOpen = ref(false)
const taskFormMode = ref<'create' | 'edit'>('create')
const taskFormTarget = ref<TaskDetailDTO | null>(null)
const taskFormStatus = ref('TODO')
const taskFormProject = ref('')
const actionDialog = ref<{
  kind: TaskActionKind
  task: TaskListItemDTO
  detail: TaskDetailDTO | null
  preferredDecision?: ReviewDecision | null
} | null>(null)
const nowMs = ref(Date.now())
let clockTimer: ReturnType<typeof setInterval> | null = null

/**
 * 任务详情的操作入口：先关掉详情浮窗，再取一份最新详情打开对应动作表单。
 * （两层浮窗叠加会让 ESC/滚动锁定互相干扰，而旧版本号也会让动作直接撞 409。）
 */
async function openActionDialog(
  kind: TaskActionKind,
  task: TaskListItemDTO,
  preferredDecision?: ReviewDecision | null,
): Promise<void> {
  selectedTask.value = null
  let detail: TaskDetailDTO | null = null
  try {
    detail = await getTask(task.id)
  } catch {
    detail = null
  }
  actionDialog.value = { kind, task: detail ?? task, detail, preferredDecision: preferredDecision ?? null }
}

/**
 * 拖动换列：发布不需要额外输入，直接执行；其余动作（领取/提交/审核/回收/撤回/重开）
 * 需要表单输入，打开对应动作表单，并带上拖动方向表达的审核偏好（拖到已完成=通过）。
 */
async function onTaskTransition(payload: {
  task: TaskListItemDTO
  action: TaskActionKind
  preferredApprove: boolean
}): Promise<void> {
  const { task, action, preferredApprove } = payload
  if (action === 'publish') {
    try {
      await publishTask(task.id, task.version, newIdempotencyKey())
      showToast(`${task.display_number} 已发布`, '🌱')
      await refreshHomeSilently()
    } catch (error) {
      showToast(error instanceof ApiError ? error.displayMessage : '发布失败', '☁️')
      await refreshHomeSilently()
    }
    return
  }
  await openActionDialog(action, task, action === 'review' ? (preferredApprove ? 'APPROVE' : 'REJECT') : null)
}

/** 列内排序：服务端保存顺序，失败时以服务端顺序为准回退。 */
async function onTaskReorder(payload: {
  task: TaskListItemDTO
  status: TaskStatus
  beforeTaskId: string | null
  afterTaskId: string | null
}): Promise<void> {
  try {
    await reorderTask(
      payload.task.project_id,
      {
        taskId: payload.task.id,
        expectedVersion: payload.task.version,
        status: payload.status,
        ...(payload.beforeTaskId ? { beforeTaskId: payload.beforeTaskId } : {}),
        ...(payload.afterTaskId ? { afterTaskId: payload.afterTaskId } : {}),
      },
      newIdempotencyKey(),
    )
    await refreshHomeSilently()
  } catch (error) {
    showToast(error instanceof ApiError ? error.displayMessage : '排序失败', '☁️')
    await refreshHomeSilently()
  }
}

async function openEditTask(task: TaskListItemDTO): Promise<void> {
  try {
    taskFormTarget.value = await getTask(task.id)
  } catch {
    taskFormTarget.value = null
  }
  taskFormMode.value = 'edit'
  taskFormOpen.value = true
}

/** 详情浮窗里的「直接操作」：发布 / 归档这类无需额外输入的动作。 */
async function onQuickAction(kind: TaskActionKind): Promise<void> {
  const task = selectedTask.value
  if (!task) return
  try {
    if (kind === 'publish') {
      await publishTask(task.id, task.version, newIdempotencyKey())
      showToast(`${task.display_number} 已发布`, '🌱')
    } else if (kind === 'restore') {
      await restoreTask(task.id, task.version, newIdempotencyKey())
      showToast(`${task.display_number} 已恢复`, '📦')
    } else {
      await openActionDialog(kind, task)
      return
    }
    selectedTask.value = null
    await refreshHomeSilently()
  } catch (error) {
    const conflict = error instanceof ApiError && error.isConflict
    showToast(error instanceof ApiError ? error.displayMessage : '操作失败', '☁️')
    if (conflict) await reloadSelectedTask(task.id)
  }
}

function openTaskForm(status: string): void {
  taskFormMode.value = 'create'
  taskFormTarget.value = null
  taskFormStatus.value = status
  // 正在按项目筛选时，新任务默认落在这个项目上（而不是项目清单里的第一个）。
  taskFormProject.value = homeFilters.projectId
  taskFormOpen.value = true
}

/** 动作失败后的兜底：把详情浮窗换成服务端最新版本，避免下一次点击继续撞 409。 */
async function reloadSelectedTask(taskId: string): Promise<void> {
  const fresh = taskList.items.find((item) => item.id === taskId)
  if (fresh) {
    selectedTask.value = { ...fresh }
    return
  }
  try {
    selectedTask.value = { ...(await getTask(taskId)) }
  } catch {
    selectedTask.value = null
  }
}

async function onTaskSaved(taskId: string | null): Promise<void> {
  taskFormOpen.value = false
  taskFormTarget.value = null
  if (taskId) burstConfetti()
  await refreshHome()
  // 编辑后详情浮窗可能还开着，它的版本号已经过期：立刻换成最新，否则接着点「发布」必冲突。
  if (taskId && selectedTask.value?.id === taskId) await reloadSelectedTask(taskId)
}

function onProjectSaved(): void {
  projectFormOpen.value = false
  projectFormTarget.value = null
  void refreshHome()
}

function openProjectForm(): void {
  projectFormMode.value = 'create'
  projectFormTarget.value = null
  projectFormOpen.value = true
}

function openProjectEdit(project: ProjectDetailDTO): void {
  selectedProject.value = null
  projectFormMode.value = 'edit'
  projectFormTarget.value = project
  projectFormOpen.value = true
}

function viewProjectTasks(projectId: string): void {
  selectedProject.value = null
  applyFilters({ projectId })
  void router.replace({ path: '/', query: { project: projectId } })
}

const showTasks = computed(() => props.tab === 'tasks')

onMounted(async () => {
  try {
    await refreshHome()
  } catch (error) {
    if (error instanceof ApiError) showToast(`首屏加载失败：${error.displayMessage}`, '☁️')
  }
  startHomePolling()
  clockTimer = setInterval(() => {
    nowMs.value = Date.now()
  }, 30_000)
})

onBeforeUnmount(() => {
  if (clockTimer !== null) clearInterval(clockTimer)
  // 只停轮询，保留已加载的数据：返回首页时可立即呈现，顶栏计数也不会被清零。
  stopHomePolling()
})

/** 深链：?task= 打开任务详情，?project= 预筛任务模块。 */
watch(
  () => route.query,
  async (query) => {
    const projectId = typeof query.project === 'string' ? query.project : ''
    if (projectId) applyFilters({ projectId })
    const taskId = typeof query.task === 'string' ? query.task : ''
    if (!taskId) return
    const local = taskList.items.find((item) => item.id === taskId)
    if (local) {
      selectedTask.value = local
      return
    }
    try {
      const detail = await getTask(taskId)
      selectedTask.value = { ...detail }
    } catch {
      showToast('找不到这个任务', '☁️')
    }
  },
  { immediate: true },
)

/** 动作表单完成后：关闭表单、刷新看板，并按需撒花。 */
async function onActionDone(message: string, conflict = false): Promise<void> {
  actionDialog.value = null
  if (!conflict) showToast(message, '🎀')
  else showToast(message, '☁️')
  await refreshHomeSilently()
  if (!conflict) burstConfetti()
}
</script>

<template>
  <main>
    <HomeBoard
      v-if="showTasks"
      @open-task="selectedTask = $event"
      @create-task="openTaskForm"
      @transition="onTaskTransition"
      @reorder="onTaskReorder"
      @refused="showToast($event, '☁️')"
    />
    <HomeProjects
      v-else
      @open-project="selectedProject = $event"
      @create-project="openProjectForm"
    />

    <!-- 任务详情浮窗 -->
    <TaskDetailModal
      :task="selectedTask"
      :now-ms="nowMs"
      @close="selectedTask = null"
      @action="onQuickAction"
      @edit="selectedTask && openEditTask(selectedTask)"
    />

    <!-- 任务新建 / 编辑 -->
    <TaskFormModal
      :open="taskFormOpen"
      :mode="taskFormMode"
      :task="taskFormTarget"
      :project-id="taskFormProject"
      :initial-status="taskFormStatus"
      @close="taskFormOpen = false"
      @saved="onTaskSaved"
    />

    <!-- 项目详情 / 新建 / 编辑 -->
    <ProjectDetailModal
      :project="selectedProject"
      @close="selectedProject = null"
      @edit="openProjectEdit"
      @view-tasks="viewProjectTasks"
    />
    <ProjectFormModal
      :open="projectFormOpen"
      :mode="projectFormMode"
      :project="projectFormTarget"
      @close="projectFormOpen = false"
      @saved="onProjectSaved"
    />

    <!-- 任务动作表单（沿用既有幂等表单，动作逻辑与旧看板一致） -->
    <TaskClaimForm
      v-if="actionDialog?.kind === 'claim'"
      :key="`claim:${actionDialog.task.id}`"
      :task="actionDialog.task"
      :project-id="actionDialog.task.project_id"
      :detail="actionDialog.detail"
      @close="actionDialog = null"
      @claimed="() => onActionDone('已领取执行，开始干活吧')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
    <TaskSubmitForm
      v-else-if="actionDialog?.kind === 'submit'"
      :key="`submit:${actionDialog.task.id}`"
      :task="actionDialog.task"
      :detail="actionDialog.detail"
      :now-ms="nowMs"
      @close="actionDialog = null"
      @submitted="() => onActionDone('已正式提交，任务进入待验收')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
    <TaskReviewForm
      v-else-if="actionDialog?.kind === 'review'"
      :key="`review:${actionDialog.task.id}`"
      :task="actionDialog.task"
      :detail="actionDialog.detail"
      :preferred-decision="actionDialog.preferredDecision ?? null"
      @close="actionDialog = null"
      @reviewed="() => onActionDone('审核已记录')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
    <TaskReclaimForm
      v-else-if="actionDialog?.kind === 'reclaim'"
      :key="`reclaim:${actionDialog.task.id}`"
      :task="actionDialog.task"
      :detail="actionDialog.detail"
      :now-ms="nowMs"
      @close="actionDialog = null"
      @ended="() => onActionDone('已回收执行，任务回到待办')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
    <TaskProgressForm
      v-else-if="actionDialog?.kind === 'progress'"
      :key="`progress:${actionDialog.task.id}`"
      :task="actionDialog.task"
      :detail="actionDialog.detail"
      :now-ms="nowMs"
      @close="actionDialog = null"
      @updated="() => onActionDone('进度已记录')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
    <TaskReasonForm
      v-else-if="actionDialog && ['withdraw', 'reopen', 'archive'].includes(actionDialog.kind)"
      :key="`${actionDialog.kind}:${actionDialog.task.id}`"
      :mode="(actionDialog.kind as 'withdraw' | 'reopen' | 'archive')"
      :task="actionDialog.task"
      @close="actionDialog = null"
      @saved="() => onActionDone('操作已生效')"
      @reopened="() => onActionDone('已重新打开任务')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
    <TaskAssignForm
      v-else-if="actionDialog?.kind === 'assign'"
      :key="`assign:${actionDialog.task.id}`"
      :task="actionDialog.task"
      :project-id="actionDialog.task.project_id"
      @close="actionDialog = null"
      @saved="() => onActionDone('领取安排已更新')"
      @conflict="() => onActionDone('版本冲突：已为你刷新最新状态', true)"
    />
  </main>
</template>
