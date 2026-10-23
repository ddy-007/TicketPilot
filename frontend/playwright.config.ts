import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  timeout: 45000,
  expect: { timeout: 15000 },
  reporter: 'list',
  globalTeardown: './tests/browser/teardown.ts',
  use: { baseURL: 'http://127.0.0.1:8021', browserName: 'chromium', channel: 'msedge',
    trace: 'off', screenshot: 'only-on-failure', video: 'off' },
  webServer: {
    command: 'uv run --directory ../backend --locked python -m tests.ui_server',
    url: 'http://127.0.0.1:8021/health/ready',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 15000 },
    timeout: 90000,
  },
});
