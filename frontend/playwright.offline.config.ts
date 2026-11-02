import { defineConfig } from '@playwright/test';

// Frontend-only regression checks with synthetic API responses; no PostgreSQL or model calls.
export default defineConfig({
  testDir: './tests/browser-offline',
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: 'http://127.0.0.1:8022',
    browserName: 'chromium',
    channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge',
    trace: 'off', screenshot: 'off', video: 'off',
  },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js preview --host 127.0.0.1 --port 8022 --strictPort',
    url: 'http://127.0.0.1:8022',
    reuseExistingServer: false,
  },
});
