<script setup lang="ts">
/**
 * 活动页（T33 / 产品 9.4、D17）。
 *
 * 数据来自 `GET /activity`（服务端最新在前、分页、按 scope 过滤）：
 * - 支持按项目、动作与归档口径筛选；分页保留当前筛选；
 * - 请求失败保留上一次数据并标记过期原因，不显示成“无活动”；
 * - 只有页面可见时轮询（10 秒，静默刷新）。
 *
 * 版式沿用共享骨架：页头 → 筛选工具栏 → 活动面板（列表 + 分页），加载/空/错误态都在面板内。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ApiError, LatestRequestGate } from '@/api/client'
import type { ActivityEventDTO } from '@/api/dashboard'
import { listActivity } from '@/api/dashboard'
import { fetchAllPages } from '@/api/paginate'
import { listProjectsPage, type ProjectSummaryDTO } from '@/api/projects'
import { createPoller, type VisiblePagePoller } from '@/stores/polling'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import LoadingState from '@/components/LoadingState.vue'
import ActivityFeed from '@/components/dashboard/ActivityFeed.vue'
import { describeDashboardError } from '@/components/dashboard/dashboard-format'

type Status = 'idle' | 'loading' | 'ready' | 'empty' | 'error'

const route = useRoute()
const router = useRouter()

const events = ref<ActivityEventDTO[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const status = ref<Status>('idle')
const error = ref<ApiError | null>(null)
const staleReason = ref('')

// 筛选支持 URL 深链（?project_id=&action=&include_archived=true&page=），便于定位一条真实活动链路。
const rawProject = route.query.project_id
const rawAction = route.query.action
const rawArchived = route.query.include_archived
const rawPage = Number(route.query.page ?? '')
const projectFilter = ref(typeof rawProject === 'string' ? rawProject : '')
const actionFilter = ref(typeof rawAction === 'string' ? rawAction : '')
const includeArchived = ref(rawArchived === 'true' || rawArchived === '1')
const projects = ref<ProjectSummaryDTO[]>([])

/**
 * 页头返回入口（导航增强，不改取数与筛选）：
 * 只有深链带 ``project_id`` 时才出现，点了回到首页任务模块并把该项目预先筛上。
 */
const returnProject = computed<{ id: string; title: string } | null>(() => {
  const deepLinked = typeof route.query.project_id === 'string' ? route.query.project_id : ''
  if (!deepLinked) return null
  return { id: deepLinked, title: projects.value.find((project) => project.id === deepLinked)?.title ?? '' }
})

const gate = new LatestRequestGate()
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const hasData = computed(() => events.value.length > 0)
const projectOptions = computed(() => {
  const options = projects.value.map((project) => ({ id: project.id, title: project.title }))
  if (projectFilter.value && !options.some((option) => option.id === projectFilter.value)) {
    options.unshift({ id: projectFilter.value, title: `该项目（${projectFilter.value}）` })
  }
  return options
})

/** 把当前筛选写回 URL（可分享/刷新后保留）。 */
function syncQuery(): void {
  void router.replace({
    query: {
      ...(projectFilter.value ? { project_id: projectFilter.value } : {}),
      ...(actionFilter.value.trim() ? { action: actionFilter.value.trim() } : {}),
      ...(includeArchived.value ? { include_archived: 'true' } : {}),
      ...(page.value > 1 ? { page: String(page.value) } : {}),
    },
  })
}

async function load(silent = false): Promise<void> {
  const context = gate.begin()
  if (!silent) {
    status.value = 'loading'
    error.value = null
  }
  try {
    const result = await listActivity(
      {
        projectId: projectFilter.value || null,
        action: actionFilter.value.trim() || null,
        includeArchived: includeArchived.value,
        page: page.value,
        pageSize: pageSize.value,
      },
      { signal: context.signal },
    )
    if (!context.isCurrent()) return
    events.value = result.items
    total.value = result.total
    page.value = result.page
    pageSize.value = result.page_size
    status.value = result.items.length > 0 ? 'ready' : 'empty'
    error.value = null
    staleReason.value = ''
  } catch (thrown) {
    if (!context.isCurrent()) return
    const apiError = thrown instanceof ApiError ? thrown : null
    error.value = apiError
    if (hasData.value) {
      staleReason.value = describeDashboardError(apiError) || '请求失败'
      status.value = 'ready'
    } else {
      status.value = 'error'
    }
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

function applyFilters(): void {
  page.value = 1
  syncQuery()
  void load(false)
}

function resetFilters(): void {
  projectFilter.value = ''
  actionFilter.value = ''
  includeArchived.value = false
  page.value = 1
  syncQuery()
  void load(false)
}

function gotoPage(next: number): void {
  if (next < 1 || next > totalPages.value) return
  page.value = next
  syncQuery()
  void load(false)
}

const poller: VisiblePagePoller = createPoller({ intervalMs: 10_000, immediateOnStart: false })

onMounted(() => {
  if (Number.isFinite(rawPage) && rawPage >= 1) page.value = Math.floor(rawPage)
  void load(false)
  void loadProjectOptions()
  poller.start(async () => {
    await load(true)
  })
})

onBeforeUnmount(() => {
  poller.stop()
  gate.cancel()
})
</script>

<template>
  <div class="page" data-testid="view-activity">
    <header class="page-head">
      <div class="page-head__text">
        <RouterLink
          v-if="returnProject"
          class="page-head__return"
          :to="{ path: '/', query: { project: returnProject.id } }"
          data-testid="activity-return-project"
        >
          ← 回首页看这个项目的任务{{ returnProject.title ? `：${returnProject.title}` : '' }}
        </RouterLink>
        <h2 class="page-title">活动</h2>
        <p class="page-subtitle">服务端追加写入的历史记录，最新在前；筛选与页码写回 URL，可直接分享定位。</p>
      </div>
      <div class="page-actions">
        <button
          type="button"
          class="btn btn--primary"
          data-testid="activity-refresh"
          :disabled="status === 'loading'"
          @click="load(false)"
        >
          刷新
        </button>
      </div>
    </header>

    <form class="toolbar" data-testid="activity-filters" @submit.prevent="applyFilters">
      <label class="toolbar__field">
        <span class="toolbar__label">项目</span>
        <select v-model="projectFilter" class="select" data-testid="activity-filter-project">
          <option value="">全部可访问项目</option>
          <option v-for="project in projectOptions" :key="project.id" :value="project.id">{{ project.title }}</option>
        </select>
      </label>
      <label class="toolbar__field">
        <span class="toolbar__label">动作（精确匹配，如 project.activate）</span>
        <input
          v-model="actionFilter"
          class="input"
          type="text"
          data-testid="activity-filter-action"
          autocomplete="off"
        />
      </label>
      <label class="toolbar__field">
        <span class="toolbar__label">每页条数</span>
        <select v-model.number="pageSize" class="select" data-testid="activity-page-size">
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
        </select>
      </label>
      <label class="checkbox-line">
        <input v-model="includeArchived" type="checkbox" data-testid="activity-filter-include-archived" />
        包含归档项目
      </label>
      <span class="toolbar__spacer" />
      <div class="btn-group">
        <button type="submit" class="btn btn--primary" data-testid="activity-filter-apply">筛选</button>
        <button type="button" class="btn btn--ghost" data-testid="activity-filter-reset" @click="resetFilters">
          清空筛选
        </button>
      </div>
    </form>

    <section class="panel">
      <div class="panel-head">
        <h3 class="panel-title">活动记录</h3>
        <p class="toolbar__summary" data-testid="activity-summary">
          共 {{ total }} 条活动（服务端最新在前）· 第 {{ page }} / {{ totalPages }} 页
        </p>
      </div>

      <div class="panel-body panel-body--tight stack">
        <p v-if="staleReason" class="notice notice--warning" role="alert" data-testid="activity-stale">
          数据已过期：最近一次请求失败（{{ staleReason }}）。下方仍显示上一次成功获取的数据，不是最新状态。
        </p>

        <LoadingState v-if="status === 'loading'" label="加载活动记录…" />
        <ErrorState v-else-if="status === 'error'" :error="error" @retry="load(false)" />
        <EmptyState
          v-else-if="status === 'empty'"
          title="没有符合条件的活动"
          description="活动为追加写入的历史记录：尝试放宽筛选条件。"
        />
        <ActivityFeed v-else-if="status === 'ready'" :events="events" />
      </div>

      <div v-if="status === 'ready'" class="panel-foot">
        <nav class="pager" aria-label="活动分页">
          <button
            type="button"
            class="btn btn--sm"
            data-testid="activity-page-prev"
            :disabled="page <= 1"
            @click="gotoPage(page - 1)"
          >
            上一页
          </button>
          <span class="pager__info tabular" data-testid="activity-page-info">第 {{ page }} / {{ totalPages }} 页</span>
          <button
            type="button"
            class="btn btn--sm"
            data-testid="activity-page-next"
            :disabled="page >= totalPages"
            @click="gotoPage(page + 1)"
          >
            下一页
          </button>
        </nav>
      </div>
    </section>
  </div>
</template>

<style scoped>
/* 返回项目看板：与 App 顶栏同一胶囊样式（边框 + 主色文字 + pill），页头里靠左独立一行。 */
.page-head__return {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.2rem 0.6rem;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-pill);
  color: var(--color-primary);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  text-decoration: none;
  white-space: nowrap;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
}

.page-head__return:hover {
  background: var(--color-primary-soft);
  text-decoration: none;
}

.page-subtitle {
  max-width: 60ch;
}

.pager {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.pager__info {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}
</style>
