import { defineConfig, devices } from '@playwright/test'

/**
 * E2E：真实浏览器 + 真实服务（Vite dev server 127.0.0.1:7100，/api 代理到 Django 7101，
 * AgentAPI 127.0.0.1:7102）。用例内的数据全部经真实接口现场播种，无业务 mock。
 *
 * 首页一次要渲染 100 张卡片并轮询多个接口，所以单测超时放宽到 60s、并发降到 4，
 * 避免 8 个 worker 一起把本机 dev server 压慢后出现假失败。
 */
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  workers: 4,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:7100',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'pnpm dev',
    url: 'http://127.0.0.1:7100',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
