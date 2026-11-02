import { test, expect, type Page } from '@playwright/test';
import type { Run, Ticket } from '../../src/api';

const runId = '11111111-1111-4111-8111-111111111111';
const ticket: Ticket = {
  id: 'TMOCK', subject: '合成回归工单', body: '仅用于前端测试', status: 'OPEN', bound_order_id: 'OMOCK',
};

function pendingRun(expiresAt: string): Run {
  return {
    run_id: runId, ticket_id: ticket.id, status: 'WAITING_APPROVAL', disposition: null, evidence: [],
    approval: {
      approval_id: '22222222-2222-4222-8222-222222222222', decision: 'PENDING',
      plan_hash: 'a'.repeat(64), reason: null, expires_at: expiresAt,
      plan: {
        tool: 'retry_fulfillment', args: { order_id: 'OMOCK', expected_version: 3, expected_error_code: 'QUEUE_PUBLISH_TIMEOUT' },
        evidence_ids: ['ev_test'], reason: '合成回归提案', expected_effect: '重新入队', expires_at: expiresAt,
      },
    },
  };
}

async function connect(page: Page, run?: Run) {
  const role = run ? 'approver' : 'operator';
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    let status = 200;
    if (path === '/api/me') body = { user: role, role, tenant: 'demo' };
    else if (path === '/api/tickets') body = { tickets: [ticket], next_after_id: null };
    else if (path === '/api/tickets/TMOCK') body = ticket;
    else if (path === '/api/tickets/TMOCK/runs') { status = 503; body = { error: 'BUSINESS_UNAVAILABLE' }; }
    else if (path === '/api/runs/' + runId) body = run;
    else if (path === '/api/runs/' + runId + '/events') body = { events: [] };
    else if (path.endsWith('/decision')) { status = 503; body = { error: 'DATABASE_UNAVAILABLE' }; }
    else { status = 404; body = { error: 'NOT_FOUND' }; }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  });
  await page.goto(run ? '/?run=' + runId : '/');
  await page.getByLabel('角色 Token').fill('synthetic-offline-test');
  await page.getByRole('button', { name: '连接', exact: true }).click();
  await expect(page.getByRole('button', { name: '切换身份' })).toBeVisible();
}

test('explicit navigation clears departed submission errors while refresh retains them', async ({ page }) => {
  await connect(page);
  await page.getByRole('button', { name: /^TMOCK/ }).click();
  await page.getByRole('button', { name: '开始诊断', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('业务服务暂不可用');
  await page.getByRole('button', { name: '刷新状态' }).click();
  await expect(page.locator('.ticket-body')).toHaveText(ticket.body);
  await expect(page.getByRole('alert')).toContainText('业务服务暂不可用');
  await page.getByRole('button', { name: '工单列表', exact: true }).click();
  await expect(page.getByRole('heading', { name: '待处理工单' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

test('approval expires locally even while the next server poll is pending', async ({ page }) => {
  const now = new Date('2030-01-01T00:00:00Z');
  await page.clock.install({ time: now });
  await connect(page, pendingRun(new Date(now.getTime() + 60000).toISOString()));
  const approve = page.getByRole('button', { name: '批准履约重试', exact: true });
  await expect(approve).toBeEnabled();
  // Keep the next read pending, so only the local deadline can update the card.
  let finish!: () => void;
  const gate = new Promise<void>(resolve => { finish = resolve; });
  await page.route('**/api/runs/' + runId, async route => { await gate; await route.abort(); });
  try {
    await page.clock.fastForward(61000);
    await expect(approve).toBeDisabled();
    await expect(page.locator('.badge').filter({ hasText: '审批已过期' })).toBeVisible();
  } finally { finish(); }
});

test('clock changes are rechecked on render and when the page regains focus', async ({ page }) => {
  const now = new Date('2030-01-01T00:00:00Z');
  await page.clock.install({ time: now });
  await connect(page, pendingRun(new Date(now.getTime() + 60000).toISOString()));
  const approve = page.getByRole('button', { name: '批准履约重试', exact: true });
  await expect(approve).toBeEnabled();
  await page.clock.setSystemTime(new Date(now.getTime() + 120000));
  await page.getByLabel('审批理由（可选，请勿填写凭证或个人信息）').fill('时钟已变化');
  await expect(approve).toBeDisabled();
  await page.clock.setSystemTime(now);
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(approve).toBeEnabled();
  await page.clock.setSystemTime(new Date(now.getTime() + 120000));
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(approve).toBeDisabled();
});

test('read refresh retains the original uncertain approval decision', async ({ page }) => {
  await connect(page, pendingRun(new Date(Date.now() + 60000).toISOString()));
  await page.getByRole('button', { name: '批准履约重试', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', { name: '重试原审批决定' })).toBeVisible();
  await page.getByRole('button', { name: '刷新状态' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', { name: '重试原审批决定' })).toBeVisible();
  await expect(page.getByRole('button', { name: '拒绝', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '工单列表', exact: true }).click();
  await expect(page.getByRole('heading', { name: '待处理工单' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});

for (const colorScheme of ['light', 'dark'] as const) {
  test(`primary button text remains readable in ${colorScheme} mode and on hover`, async ({ page }) => {
    await page.emulateMedia({ colorScheme });
    await page.goto('/');
    const button = page.getByRole('button', { name: '连接', exact: true });
    for (const hover of [false, true]) {
      if (hover) await button.hover();
      const contrast = await button.evaluate(element => {
        const css = getComputedStyle(element);
        function luminance(rgb: string) {
          const values = rgb.match(/[\d.]+/g)!.slice(0, 3).map(Number).map(n => {
            const value = n / 255;
            return value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
          });
          return values[0] * .2126 + values[1] * .7152 + values[2] * .0722;
        }
        const a = luminance(css.color), b = luminance(css.backgroundColor);
        return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
      });
      expect(contrast).toBeGreaterThanOrEqual(4.5);
    }
    await expect(button).toBeEnabled();
  });
}
