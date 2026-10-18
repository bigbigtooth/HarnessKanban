<script setup lang="ts">
/** 空态：仅在服务端确认结果为空时使用；请求失败必须走 ErrorState。 */
withDefaults(defineProps<{ title?: string; description?: string }>(), {
  title: '暂无数据',
  description: '',
})
</script>

<template>
  <div class="state-block state-empty" role="status" data-testid="state-empty">
    <p class="state-title">{{ title }}</p>
    <p v-if="description" class="state-description">{{ description }}</p>
  </div>
</template>

<style scoped>
.state-empty {
  padding: var(--space-6) var(--space-4);
  border-style: dashed;
  background: var(--color-surface-muted);
}
</style>
