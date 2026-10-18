<script setup lang="ts">
/** 底部提示条（demo 的 #toast）：同一时刻显示一条，2.5s 自动消失。 */
import { toast } from './feedback'
</script>

<template>
  <div id="toast" :class="{ show: toast !== null }" role="status" aria-live="polite" data-testid="toast">
    <template v-if="toast">
      <span class="toast-emoji" aria-hidden="true">{{ toast.emoji }}</span>
      <span>{{ toast.message }}</span>
    </template>
  </div>
</template>
