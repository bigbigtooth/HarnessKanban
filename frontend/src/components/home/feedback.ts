/**
 * 轻量 toast 与礼花：全局单例，任何组件都能调用 `showToast` / `burstConfetti`。
 *
 * 外观沿用 demo：底部居中的深色药丸提示（2.5s 自动消失）、中心爆开的 26 片礼花。
 */
import { ref } from 'vue'

export interface ToastState {
  message: string
  emoji: string
}

export const TOAST_DURATION_MS = 2500

/** 当前提示；同一时刻只显示一条（与 demo 的 #toast 行为一致）。 */
export const toast = ref<ToastState | null>(null)
export const confettiPieces = ref<ConfettiPiece[]>([])

export interface ConfettiPiece {
  id: number
  x0: string
  y0: string
  dx: string
  dy: string
  rot: string
  color: string
  width: string
  height: string
  radius: string
  delay: string
}

const CONFETTI_COLORS = ['#8fe0c5', '#ffc2d4', '#ffe49c', '#aed8ff', '#d8c9ff', '#ffc6a9']
const CONFETTI_COUNT = 26
const CONFETTI_LIFETIME_MS = 1200

let toastTimer: ReturnType<typeof setTimeout> | null = null
let confettiSeed = 0

export function showToast(message: string, emoji = '🌷'): void {
  toast.value = { message, emoji }
  if (toastTimer !== null) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toast.value = null
    toastTimer = null
  }, TOAST_DURATION_MS)
}

export function hideToast(): void {
  toast.value = null
  if (toastTimer !== null) {
    clearTimeout(toastTimer)
    toastTimer = null
  }
}

export function burstConfetti(): void {
  if (typeof window === 'undefined') return
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  const originX = window.innerWidth / 2
  const originY = window.innerHeight * 0.56
  const pieces: ConfettiPiece[] = []
  for (let index = 0; index < CONFETTI_COUNT; index += 1) {
    const angle = Math.random() * Math.PI * 2
    const distance = 70 + Math.random() * 260
    confettiSeed += 1
    pieces.push({
      id: confettiSeed,
      x0: `${originX}px`,
      y0: `${originY}px`,
      dx: `${Math.cos(angle) * distance}px`,
      dy: `${Math.sin(angle) * distance + 80}px`,
      rot: `${Math.random() * 720 - 360}deg`,
      color: CONFETTI_COLORS[index % CONFETTI_COLORS.length],
      width: `${5 + Math.random() * 7}px`,
      height: `${5 + Math.random() * 7}px`,
      radius: Math.random() > 0.5 ? '50%' : '3px',
      delay: `${Math.random() * 70}ms`,
    })
  }
  confettiPieces.value = [...confettiPieces.value, ...pieces]
  const ids = new Set(pieces.map((piece) => piece.id))
  setTimeout(() => {
    confettiPieces.value = confettiPieces.value.filter((piece) => !ids.has(piece.id))
  }, CONFETTI_LIFETIME_MS)
}
