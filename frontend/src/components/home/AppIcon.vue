<script setup lang="ts">
/** 20×20 描边图标；路径来自 demo 的 ICONS（见 icons.ts）。 */
import { ICONS, type IconName } from './icons'

defineProps<{ name: IconName }>()
</script>

<template>
  <!-- 路径是内置常量，不含用户输入 -->
  <svg
    viewBox="0 0 20 20"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    v-html="ICONS[name]"
  />
</template>
