<script setup lang="ts">
// 未匹配路由。
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="view-not-found" data-testid="view-not-found">
    <div class="state-block">
      <p class="not-found__code" aria-hidden="true">404</p>
      <p class="state-title">页面不存在</p>
      <p class="state-description">该路径未在本地管理台中定义。</p>
      <RouterLink class="btn btn--primary" to="/">返回全局看板</RouterLink>
    </div>
  </div>
</template>

<style scoped>
.view-not-found {
  display: flex;
  justify-content: center;
  padding-top: var(--space-10);
}

.state-block {
  max-width: 420px;
  width: 100%;
  gap: var(--space-3);
}

.not-found__code {
  margin: 0;
  font-size: 2.5rem;
  font-weight: var(--weight-bold);
  color: var(--color-border-strong);
  line-height: 1;
}
</style>
