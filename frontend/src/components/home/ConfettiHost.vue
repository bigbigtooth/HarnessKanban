<script setup lang="ts">
/** 礼花层（demo 的 #confetti-host）：从屏幕中下方爆开 26 片，1.2s 后自动移除。 */
import { confettiPieces } from './feedback'
</script>

<template>
  <div id="confetti-host" aria-hidden="true">
    <span
      v-for="piece in confettiPieces"
      :key="piece.id"
      class="confetti"
      :style="{
        '--x0': piece.x0,
        '--y0': piece.y0,
        '--dx': piece.dx,
        '--dy': piece.dy,
        '--rot': piece.rot,
        '--c': piece.color,
        '--w': piece.width,
        '--h': piece.height,
        '--r': piece.radius,
        '--delay': piece.delay,
      }"
    />
  </div>
</template>
