/**
 * 图标集：路径直接取自 docs/html/demo-light.html 的 ICONS，另加主题/设置等新图标。
 * 全部按 20×20 视框、描边 1.7 绘制，颜色继承 currentColor。
 */
export const ICONS = {
  plus: '<path d="M10 4.2v11.6M4.2 10h11.6"/>',
  search: '<circle cx="8.8" cy="8.8" r="5.1"/><path d="m12.7 12.7 3.2 3.2"/>',
  calendar:
    '<rect x="3.2" y="4.8" width="13.6" height="12" rx="3"/><path d="M6.8 2.9v3.6M13.2 2.9v3.6M3.4 8.3h13.2"/>',
  check: '<path d="m4.2 10.2 3.6 3.6 8-8"/>',
  checklist:
    '<rect x="3.2" y="3.2" width="13.6" height="13.6" rx="3.2"/><path d="m6.6 7.2 1.4 1.4 2.6-2.6M11.8 8.2h2.1M6.6 12.1l1.4 1.4 2.6-2.6M11.8 13.1h2.1"/>',
  doc: '<path d="M5.2 2.8h6l3.6 3.6v9.1c0 .9-.7 1.7-1.7 1.7H5.2c-.9 0-1.7-.8-1.7-1.7V4.5c0-.9.8-1.7 1.7-1.7Z"/><path d="M11 2.9v4h3.6M6.6 10h5.2M6.6 13h3.6"/>',
  download: '<path d="M10 3.3v9M6.5 9.1l3.5 3.5 3.5-3.5M4.2 16.4h11.6"/>',
  remove: '<path d="M8.2 8.2 5.5 5.5M10 10l-4.5 4.5M11.8 8.2l2.7-2.7M14.5 14.5 10 10"/>',
  x: '<path d="m5 5 10 10M15 5 5 15"/>',
  edit: '<path d="M12.3 3.5 16.5 7.7 7.8 16.4l-4.6.8.8-4.6 8.3-9.1Z"/><path d="m11 5 4.2 4.2"/>',
  trash:
    '<path d="M4 5.8h12M8 5.8V4.2c0-.6.5-1.1 1.1-1.1h1.8c.6 0 1.1.5 1.1 1.1v1.6M6 5.8l.8 10c0 .8.7 1.4 1.5 1.4h3.4c.8 0 1.5-.6 1.5-1.4l.8-10"/><path d="M8.5 8.7v5.4M11.5 8.7v5.4"/>',
  arrow: '<path d="M6 14 14 6M8 6h6v6"/>',
  users:
    '<circle cx="7.2" cy="7.1" r="2.7"/><path d="M2.9 16c.2-2.5 2-4.1 4.3-4.1s4.1 1.6 4.3 4.1M12.2 4.8a2.7 2.7 0 0 1 0 4.8M13.7 12.3c2 .4 3.2 1.8 3.4 3.7"/>',
  upload:
    '<path d="M10 14.2V5.1M6.5 8.2 10 4.7l3.5 3.5M4.2 13.2v1.2c0 1 .8 1.8 1.8 1.8h8c1 0 1.8-.8 1.8-1.8v-1.2"/>',
  clock: '<circle cx="10" cy="10" r="7.1"/><path d="M10 5.9v4.4l2.9 1.7"/>',
  folder:
    '<path d="M3.2 6.6c0-1 .8-1.8 1.8-1.8h2.2l1.4 1.6h5.1c1 0 1.8.8 1.8 1.8v5.5c0 1-.8 1.8-1.8 1.8H5c-1 0-1.8-.8-1.8-1.8V6.6Z"/><path d="M3.4 9h13.2"/>',
  sparkle: '<path d="M10 2.7c.6 4.3 2.7 6.4 7 7-4.3.6-6.4 2.7-7 7-.6-4.3-2.7-6.4-7-7 4.3-.6 6.4-2.7 7-7Z"/>',
  eye: '<path d="M2.5 10s2.7-4.6 7.5-4.6 7.5 4.6 7.5 4.6-2.7 4.6-7.5 4.6S2.5 10 2.5 10Z"/><circle cx="10" cy="10" r="2.2"/>',
  closeSmall: '<path d="m5.8 5.8 8.4 8.4M14.2 5.8l-8.4 8.4"/>',
  arrowLeft: '<path d="M15 10H5M9.2 5.8 5 10l4.2 4.2"/>',
  // ---- 新增：主题 / 设置 / 二级页面入口 ----
  sun: '<circle cx="10" cy="10" r="3.5"/><path d="M10 2.6v1.9M10 15.5v1.9M2.6 10h1.9M15.5 10h1.9M4.8 4.8l1.34 1.34M13.86 13.86l1.34 1.34M15.2 4.8l-1.34 1.34M6.14 13.86 4.8 15.2"/>',
  moon: '<path d="M15.4 12.8A6.5 6.5 0 0 1 7.2 4.6a6.9 6.9 0 1 0 8.2 8.2Z"/>',
  sliders:
    '<path d="M4 6.4h12M4 13.6h12"/><circle cx="8.1" cy="6.4" r="2"/><circle cx="12.4" cy="13.6" r="2"/>',
  dots: '<circle cx="4.6" cy="10" r="1" fill="currentColor" stroke="none"/><circle cx="10" cy="10" r="1" fill="currentColor" stroke="none"/><circle cx="15.4" cy="10" r="1" fill="currentColor" stroke="none"/>',
  refresh: '<path d="M16.2 8.2A6.4 6.4 0 1 0 16 12.8"/><path d="M16.4 3.8v4.5h-4.5"/>',
  grid: '<rect x="3" y="3" width="6" height="6" rx="2"/><rect x="11" y="3" width="6" height="6" rx="2"/><rect x="3" y="11" width="6" height="6" rx="2"/><rect x="11" y="11" width="6" height="6" rx="2"/>',
  pulse: '<path d="M2.6 10.8h3.1l1.7-4.9 2.6 8.6 1.8-5.2 1.3 3.5h4.3"/>',
  robot:
    '<rect x="3.6" y="7" width="12.8" height="9" rx="3.2"/><circle cx="7.8" cy="11.4" r="1" fill="currentColor" stroke="none"/><circle cx="12.2" cy="11.4" r="1" fill="currentColor" stroke="none"/><path d="M10 4.3V7M8 16.2h4"/>',
  key: '<circle cx="7" cy="10" r="3.6"/><path d="M10.4 10H17M14.6 10v3.1M17 10v2.2"/>',
  bolt: '<path d="M11.2 2.6 4.8 11h4.2l-.6 6.4L15.2 9h-4.2l.2-6.4Z"/>',
  link: '<path d="M8.6 11.4 11.4 8.6"/><path d="M9.3 5.7l1-1a3.4 3.4 0 0 1 4.8 4.8l-1 1M10.7 14.3l-1 1a3.4 3.4 0 0 1-4.8-4.8l1-1"/>',
} as const

export type IconName = keyof typeof ICONS
