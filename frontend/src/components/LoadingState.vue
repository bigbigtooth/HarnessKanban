<script setup lang="ts">
/** 显式加载态：不使用空白或假数据占位。 */
withDefaults(defineProps<{ label?: string }>(), { label: '加载中…' })
</script>

<template>
  <div class="state-block state-loading" role="status" aria-live="polite" data-testid="state-loading">
    <span class="state-spinner" aria-hidden="true" />
    <p class="state-title">{{ label }}</p>
  </div>
</template>

<style scoped>
.state-loading {
  flex-direction: row;
  justify-content: center;
  gap: var(--space-2);
  padding: var(--space-4);
  color: var(--color-text-secondary);
}

.state-spinner {
  width: 1rem;
  height: 1rem;
  border: 2px solid var(--color-border-strong);
  border-top-color: var(--color-primary);
  border-radius: var(--radius-pill);
  animation: state-spin 0.7s linear infinite;
  flex: none;
}

@keyframes state-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
