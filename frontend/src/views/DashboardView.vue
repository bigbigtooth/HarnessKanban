<script setup lang="ts">
/**
 * 全局看板（T33 / D17 / 产品 3、9、18）。
 *
 * 硬性约束：
 * - 所有统计来自**同一份** `GET /dashboard` 响应：`as_of`、`scope`、五态/难度/项目计数、
 *   完成率、执行与容量、Agent 状态、异常。前端不拼接各页面缓存，也不自行汇总；
 * - 请求失败时**保留上一次数据**并标记“已过期”及原因（绝不显示伪造的 0 或空系统）；
 * - 显示 scope 标识与 as_of，明确“员工范围只统计已发布任务”；
 * - “工作中（有效执行）”与“已占容量槽”并列，等待回收单独可见；
 * - 异常/项目行提供跳转到对应看板的入口；
 * - 仅页面可见时轮询（10 秒），隐藏时暂停。
 *
 * 版式（UI-DESIGN §4.2）：页头 → 统计范围工具栏 → 口径条 → 概览计数 → 任务分布（五态 + 五难度）
 * → 执行与容量 → Agent 状态 → 待处理异常 → 项目状态 → 最近活动。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, LatestRequestGate } from '@/api/client'
import type { DashboardDTO } from '@/api/dashboard'
import { getDashboard } from '@/api/dashboard'
import { fetchAllPages } from '@/api/paginate'
import { listProjectsPage, type ProjectSummaryDTO } from '@/api/projects'
import { createPoller, type VisiblePagePoller } from '@/stores/polling'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import DashboardDifficulty from '@/components/dashboard/DashboardDifficulty.vue'
import DashboardExceptions from '@/components/dashboard/DashboardExceptions.vue'
import DashboardTaskCounts from '@/components/dashboard/DashboardTaskCounts.vue'
import DashboardWorkload from '@/components/dashboard/DashboardWorkload.vue'
import { describeDashboardError, scopeSummary } from '@/components/dashboard/dashboard-format'

const route = useRoute()
const router = useRouter()

const dashboard = ref<DashboardDTO | null>(null)
const loading = ref(false)
const error = ref<ApiError | null>(null)
const staleReason = ref('')
const lastSuccessAt = ref<string | null>(null)

// scope 支持 URL 深链（?project_id=&include_archived=true），便于把统计口径发给同事或复现问题。
const rawProject = route.query.project_id
const rawArchived = route.query.include_archived
const includeArchived = ref(rawArchived === 'true' || rawArchived === '1')
const projectScope = ref(typeof rawProject === 'string' ? rawProject : '')
const projects = ref<ProjectSummaryDTO[]>([])

const gate = new LatestRequestGate()

const scopeText = computed(() => scopeSummary(dashboard.value?.scope ?? null))
const asOf = computed(() => dashboard.value?.as_of ?? '—')
/** 已加载项目中若不含当前 scope 项目（例如深链到列表之外的项目），仍要在下拉中占位显示。 */
const scopeOptions = computed(() => {
  const options = projects.value.map((project) => ({ id: project.id, title: project.title }))
  if (projectScope.value && !options.some((option) => option.id === projectScope.value)) {
    options.unshift({ id: projectScope.value, title: `该项目（${projectScope.value}）` })
  }
  return options
})

/** 概览计数（产品 §9.1）：只重排同一份 payload 里已有的数字，不新增统计口径。 */
const overview = computed(() => {
  const data = dashboard.value
  if (!data) return []
  return [
    {
      label: '任务总数',
      value: String(data.task_total),
      hint: `已归档 ${data.archived_task_total}（默认口径外）`,
    },
    {
      label: '可见项目',
      value: String(data.project_total),
      hint: `进行中 ${data.project_counts.ACTIVE ?? 0}`,
    },
    {
      label: 'Agent 总数',
      value: String(data.agent_counts.total),
      hint: `已启用 ${data.agent_counts.enabled}`,
    },
    {
      label: '在线 Agent',
      value: String(data.agent_counts.online),
      hint: `离线 ${data.agent_counts.offline} · 未连接 ${data.agent_counts.never_connected}`,
    },
    {
      label: '工作中 Agent',
      value: String(data.agent_counts.working),
      hint: '持有未到期的有效执行',
    },
    {
      label: '待处理异常',
      value: String(data.exception_counts.total),
      hint: `需回收 ${data.exception_counts.OVERDUE_RECLAIM_REQUIRED} · 指定者不可用 ${data.exception_counts.UNAVAILABLE_DESIGNATED_AGENT}`,
    },
  ]
})

async function loadDashboard(silent = false): Promise<void> {
  const context = gate.begin()
  if (!silent) loading.value = true
  try {
    const result = await getDashboard(
      { projectId: projectScope.value || null, includeArchived: includeArchived.value },
      { signal: context.signal },
    )
    if (!context.isCurrent()) return
    dashboard.value = result
    error.value = null
    staleReason.value = ''
    lastSuccessAt.value = new Date().toISOString()
  } catch (thrown) {
    if (!context.isCurrent()) return
    const apiError = thrown instanceof ApiError ? thrown : null
    error.value = apiError
    // 失败保留旧数据并标记过期（含原因），不把失败当成 0。
    if (dashboard.value) {
      staleReason.value = describeDashboardError(apiError) || '请求失败'
    }
  } finally {
    if (context.isCurrent()) loading.value = false
  }
}

async function loadProjectOptions(): Promise<void> {
  try {
    const result = await fetchAllPages((page) => listProjectsPage({ includeArchived: true, page, pageSize: 100 }))
    projects.value = result.items
  } catch {
    projects.value = []
  }
}

function applyScope(): void {
  void router.replace({
    query: {
      ...(projectScope.value ? { project_id: projectScope.value } : {}),
      ...(includeArchived.value ? { include_archived: 'true' } : {}),
    },
  })
  void loadDashboard(false)
}

const poller: VisiblePagePoller = createPoller({ intervalMs: 10_000, immediateOnStart: false })

onMounted(() => {
  void loadDashboard(false)
  void loadProjectOptions()
  poller.start(async () => {
    await loadDashboard(true)
  })
})

onBeforeUnmount(() => {
  poller.stop()
  gate.cancel()
})
</script>

<template>
  <div class="page" data-testid="view-dashboard">
    <header class="page-head">
      <div class="page-head__text">
        <h2 class="page-title">全局看板</h2>
        <p class="page-subtitle">
          所有计数来自同一份服务端统计快照（GET /dashboard），不拼接各页面缓存；范围与统计时刻见下方口径条。
        </p>
      </div>
      <div class="page-actions">
        <button
          type="button"
          class="btn btn--primary"
          data-testid="dashboard-refresh"
          :disabled="loading"
          @click="loadDashboard(false)"
        >
          {{ loading ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </header>

    <form class="toolbar" data-testid="dashboard-scope-bar" @submit.prevent="applyScope">
      <label class="toolbar__field">
        <span class="toolbar__label">统计范围</span>
        <select v-model="projectScope" class="select" data-testid="dashboard-project-scope">
          <option value="">全部可访问项目</option>
          <option v-for="project in scopeOptions" :key="project.id" :value="project.id">
            {{ project.title }}
          </option>
        </select>
      </label>
      <label class="checkbox-line">
        <input v-model="includeArchived" type="checkbox" data-testid="dashboard-include-archived" />
        包含归档（项目与任务）
      </label>
      <span class="toolbar__spacer" />
      <button type="submit" class="btn" data-testid="dashboard-scope-apply">应用范围</button>
    </form>

    <section class="panel" aria-label="统计口径与统计时刻">
      <dl class="meta-list panel-body panel-body--tight">
        <dt>统计口径</dt>
        <dd data-testid="dashboard-scope">{{ scopeText }}</dd>
        <dt>服务端快照</dt>
        <dd data-testid="dashboard-as-of">
          统计时刻（服务端 as_of，UTC）：{{ asOf }}<span v-if="lastSuccessAt" class="muted">
            · 本页最近一次成功刷新：{{ lastSuccessAt }}</span>
        </dd>
      </dl>
    </section>

    <p
      v-if="staleReason && dashboard"
      class="notice notice--warning"
      role="alert"
      data-testid="dashboard-stale"
    >
      数据已过期：最近一次请求失败（{{ staleReason }}）。下方仍显示上一次成功获取的数据（as_of {{ asOf }}），不是 0，也不是最新状态；请刷新或排查本机服务。
    </p>

    <LoadingState v-if="loading && !dashboard" label="加载全局统计…" />
    <ErrorState
      v-else-if="error && !dashboard"
      :error="error"
      retry-label="重新加载"
      @retry="loadDashboard(false)"
    />

    <template v-if="dashboard">
      <section class="section" data-testid="dashboard-overview">
        <div class="section-head">
          <h3 class="section-title">概览计数</h3>
          <p class="section-hint">同一统计范围与统计时刻下的汇总；明细见下方各分区。</p>
        </div>
        <div class="stat-grid">
          <div v-for="item in overview" :key="item.label" class="stat">
            <span class="stat__label">{{ item.label }}</span>
            <span class="stat__value">{{ item.value }}</span>
            <span class="stat__hint">{{ item.hint }}</span>
          </div>
        </div>
      </section>

      <div class="grid-2">
        <DashboardTaskCounts :dashboard="dashboard" />
        <DashboardDifficulty :dashboard="dashboard" />
      </div>

      <DashboardWorkload :dashboard="dashboard" />
      <DashboardExceptions :dashboard="dashboard" />

      <section class="panel" data-testid="dashboard-recent-activity">
        <div class="panel-head">
          <h3 class="panel-title">最近活动</h3>
          <RouterLink class="btn btn--sm" to="/activity">查看活动页</RouterLink>
        </div>
        <div class="panel-body">
          <p class="section-hint">
            发布、领取、提交、审核、回收等动作由服务端追加写入并按时间倒序排列；完整链路可在活动页按项目与动作筛选查看。
          </p>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page-subtitle {
  max-width: 60ch;
}
</style>
