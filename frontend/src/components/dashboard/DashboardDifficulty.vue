<script setup lang="ts">
/**
 * 难度分布（T33 / D17）：直接展示服务端 `difficulty_counts`（键为 "1".."5"）。
 * 条形宽度只用于同屏相对比较，数值文本才是事实；全为 0 时明确说明范围内没有任务。
 */
import { computed } from 'vue'
import { DIFFICULTIES, DIFFICULTY_LABEL } from '@/api/types'
import type { DashboardDTO } from '@/api/dashboard'

const props = defineProps<{ dashboard: DashboardDTO }>()

const rows = computed(() => {
  const counts = props.dashboard.difficulty_counts ?? {}
  const max = Math.max(1, ...DIFFICULTIES.map((level) => counts[String(level)] ?? 0))
  return DIFFICULTIES.map((level) => ({
    level,
    label: DIFFICULTY_LABEL[level],
    count: counts[String(level)] ?? 0,
    width: `${Math.round(((counts[String(level)] ?? 0) / max) * 100)}%`,
  }))
})

const total = computed(() => rows.value.reduce((sum, row) => sum + row.count, 0))
</script>

<template>
  <section class="panel" data-testid="dashboard-difficulty">
    <div class="panel-head">
      <h3 class="panel-title">难度分布</h3>
      <span class="section-hint">条形长度仅用于同屏相对比较，数值为准</span>
    </div>

    <div class="panel-body stack">
      <ul class="difficulty-list">
        <li
          v-for="row in rows"
          :key="row.level"
          class="difficulty-row"
          :data-difficulty="row.level"
          :data-testid="`dashboard-difficulty-row-${row.level}`"
        >
          <span class="badge badge--difficulty difficulty-row__label">{{ row.label }}</span>
          <span class="difficulty-row__bar" aria-hidden="true">
            <span class="difficulty-row__fill" :style="{ width: row.width }" />
          </span>
          <span class="difficulty-row__count tabular" :data-testid="`dashboard-difficulty-${row.level}`">
            {{ row.count }}
          </span>
        </li>
      </ul>

      <p v-if="total === 0" class="section-hint">当前统计范围内没有任务，五级难度计数均为 0。</p>
    </div>
  </section>
</template>

<style scoped>
.difficulty-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin: 0;
  padding: 0;
  list-style: none;
}

.difficulty-row {
  display: grid;
  grid-template-columns: 5.5rem minmax(0, 1fr) 2.5rem;
  align-items: center;
  gap: var(--space-3);
}

.difficulty-row__label {
  justify-self: start;
}

.difficulty-row__bar {
  display: block;
  height: 0.5rem;
  border-radius: var(--radius-pill);
  background: var(--color-surface-sunken);
  overflow: hidden;
}

.difficulty-row__fill {
  display: block;
  height: 100%;
  border-radius: var(--radius-pill);
  background: var(--difficulty-color, var(--color-primary));
}

.difficulty-row__count {
  text-align: right;
  font-weight: var(--weight-semibold);
}
</style>
