<script setup lang="ts">
/**
 * Agent 列表筛选栏（T29 / D16）：名称、角色、状态、在线四个条件。
 * 筛选由页面保存（父组件持有），组件本身只维护输入草稿。
 *
 * 「状态」下拉把启用/禁用与「已删除」合成一个入口（全部 / 已启用 / 已禁用 / 已删除）：
 * 选「已删除」时列表只查软删除的 Agent，启用筛选被清空（服务端 `deleted=true` 只返回已删除的）。
 *
 * 版式：共享 `.toolbar` 工具栏，右侧收拢「筛选 / 清空筛选」按钮。
 */
import { computed, reactive, watch } from 'vue'
import { AGENT_ROLES, AgentRole } from '@/api/types'
import {
  agentStatusFilterValue,
  emptyAgentFilters,
  hasAgentFilters,
  withAgentStatusFilter,
  type AgentListFilters,
  type AgentStatusFilterValue,
} from './agent-format'

const props = withDefaults(
  defineProps<{
    filters: AgentListFilters
    loading?: boolean
  }>(),
  { loading: false },
)

const emit = defineEmits<{ apply: [filters: AgentListFilters]; reset: [] }>()

const draft = reactive<AgentListFilters>({ ...props.filters })

watch(
  () => props.filters,
  (next) => {
    Object.assign(draft, next)
  },
  { deep: true },
)

/**
 * 「状态」下拉（含「已删除」）的读写代理：
 * 写回时改写 `enabled` / `deleted` 两个字段，保证两者不会同时生效。
 */
const status = computed<AgentStatusFilterValue>({
  get: () => agentStatusFilterValue(draft),
  set: (value) => {
    const next = withAgentStatusFilter(draft, value)
    draft.enabled = next.enabled
    draft.deleted = next.deleted
  },
})

function apply(): void {
  emit('apply', { ...draft })
}

function reset(): void {
  Object.assign(draft, emptyAgentFilters())
  emit('reset')
}

const canReset = () => hasAgentFilters(draft)
</script>

<template>
  <form class="toolbar agent-filters" data-testid="agent-filters" @submit.prevent="apply">
    <label class="toolbar__field agent-filters__name">
      <span class="toolbar__label">名称</span>
      <input
        v-model="draft.name"
        class="input"
        type="text"
        data-testid="agent-filter-name"
        placeholder="模糊匹配名称"
      />
    </label>

    <label class="toolbar__field agent-filters__select">
      <span class="toolbar__label">角色</span>
      <select v-model="draft.role" class="select" data-testid="agent-filter-role">
        <option value="">全部</option>
        <option v-for="role in AGENT_ROLES" :key="role" :value="role">
          {{ role === AgentRole.SUPERVISOR ? '主管' : '员工' }}
        </option>
      </select>
    </label>

    <label class="toolbar__field agent-filters__select">
      <span class="toolbar__label">状态</span>
      <select v-model="status" class="select" data-testid="agent-filter-enabled">
        <option value="">全部</option>
        <option value="true">已启用</option>
        <option value="false">已禁用</option>
        <option value="deleted">已删除</option>
      </select>
    </label>

    <label class="toolbar__field agent-filters__select">
      <span class="toolbar__label">在线</span>
      <select v-model="draft.online" class="select" data-testid="agent-filter-online">
        <option value="">全部</option>
        <option value="true">在线</option>
        <option value="false">不在线（含禁用与未连接）</option>
      </select>
    </label>

    <span class="toolbar__spacer" />

    <div class="btn-group">
      <button type="submit" class="btn btn--primary" data-testid="agent-filter-apply" :disabled="loading">
        {{ loading ? '查询中…' : '筛选' }}
      </button>
      <button
        type="button"
        class="btn btn--ghost"
        data-testid="agent-filter-reset"
        :disabled="!canReset()"
        @click="reset"
      >
        清空筛选
      </button>
    </div>
  </form>
</template>

<style scoped>
/* 工具栏字段宽度：名称更宽，三个下拉等宽，超窄屏自动换行 */
.agent-filters__name {
  flex: 0 1 18rem;
}

.agent-filters__select {
  flex: 0 1 10rem;
}
</style>
