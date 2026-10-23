import { test, expect, type Page, type APIRequestContext } from '@playwright/test';

const headers = { Authorization: 'Bearer ' + 'a'.repeat(40) };
async function login(page: Page, role = 'operator') {
  await page.getByLabel('角色 Token').fill((role === 'operator' ? 'o' : 'a').repeat(40));
  await page.getByRole('button', { name: '连接', exact: true }).click();
  await expect(page.getByRole('button', { name: '切换身份' })).toBeVisible();
}
async function select(page: Page, id: number) {
  await page.getByRole('button', { name: new RegExp('^TUI' + id + '\\s') }).click();
}
async function waiting(page: Page, id: number) {
  await page.goto('/'); await login(page); await select(page, id);
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.getByRole('heading', { name: '等待人工审批', exact: true })).toBeVisible();
}
async function approver(page: Page) {
  await page.getByRole('button', { name: '切换身份' }).click(); await login(page, 'approver');
  await expect(page.getByRole('button', { name: '批准履约重试' })).toBeEnabled();
}
async function control(request: APIRequestContext, paused: boolean) {
  expect((await request.post('/__test/control', { headers, data: { paused } })).ok()).toBeTruthy();
}

test.afterEach(async ({ request }) => { await control(request, false); });

for (const body of ['null', '{"role":"approver"}']) {
  test('review: invalid identity response is rejected without rendering a broken session ' + body, async ({ page }) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/api/me', route => route.fulfill({ contentType: 'application/json', body }));
    await page.goto('/');
    await page.getByLabel('角色 Token').fill('o'.repeat(40));
    await page.getByRole('button', { name: '连接', exact: true }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByRole('button', { name: '切换身份' })).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}

test('review: invalid successful create response retains the original request for retry', async ({ page, request }) => {
  await page.goto('/'); await login(page); await select(page, 13);
  const keys: string[] = [];
  await page.route('**/api/tickets/TUI13/runs', async route => {
    keys.push(route.request().headers()['idempotency-key']);
    if (keys.length === 1) { await route.fetch(); await route.fulfill({ status: 202, contentType: 'application/json', body: '{' }); }
    else await route.continue();
  });
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  expect(new URL(page.url()).searchParams.get('request')).toBe(keys[0]);
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.getByRole('heading', { name: '等待人工审批', exact: true })).toBeVisible();
  expect(keys).toEqual([keys[0], keys[0]]);
  expect((await (await request.get('/__test/stats/TUI13', { headers })).json()).runs).toBe(1);
});

test('review: a delayed create response cannot navigate back into a departed task', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let arrived = false;
  await page.route('**/api/tickets/TUI14/runs', async route => {
    const response = await route.fetch(); arrived = true; await gate; await route.fulfill({ response });
  });
  await page.goto('/'); await login(page); await select(page, 14);
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect.poll(() => arrived).toBe(true);
  await page.getByRole('link', { name: 'TicketPilot', exact: true }).click();
  const response = page.waitForResponse('**/api/tickets/TUI14/runs'); release(); await response;
  await expect(page.getByRole('button', { name: '切换身份' })).toBeEnabled();
  await expect(page).toHaveURL('http://127.0.0.1:8021/');
  await expect(page.getByRole('heading', { name: '待处理工单' })).toBeVisible();
});

test('review: delayed approval response cannot place notices on a departed page', async ({ page, request }) => {
  await waiting(page, 15); await approver(page); await control(request, true);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let arrived = false;
  await page.route('**/api/approvals/*/decision', async route => {
    const response = await route.fetch(); arrived = true; await gate; await route.fulfill({ response });
  });
  await page.getByRole('button', { name: '批准履约重试' }).click();
  await expect.poll(() => arrived).toBe(true);
  await page.getByRole('link', { name: 'TicketPilot', exact: true }).click();
  const response = page.waitForResponse('**/api/approvals/*/decision'); release(); await response;
  await expect(page.getByRole('button', { name: '切换身份' })).toBeEnabled();
  await expect(page.getByRole('status')).toHaveCount(0);
});

test('review: approval 503 after commit keeps only the original decision retryable', async ({ page, request }) => {
  await waiting(page, 16); await approver(page); await control(request, true);
  const runPath = '/api/runs/' + new URL(page.url()).searchParams.get('run');
  const snapshot = await (await request.get(runPath, { headers })).json();
  await page.route('**' + runPath, route => route.fulfill({ json: snapshot }));
  let attempts = 0;
  await page.route('**/api/approvals/*/decision', async route => {
    attempts++;
    if (attempts === 1) { await route.fetch(); await route.fulfill({ status: 503, json: { error: 'DATABASE_UNAVAILABLE' } }); }
    else await route.continue();
  });
  await page.getByRole('button', { name: '批准履约重试' }).click();
  await expect(page.getByRole('button', { name: '重试原审批决定' })).toBeVisible();
  await expect(page.getByRole('button', { name: '拒绝', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: '重试原审批决定' }).click();
  await page.unroute('**' + runPath);
  await control(request, false); await page.getByRole('button', { name: '刷新状态' }).click();
  await expect(page.getByRole('heading', { name: '已重新入队', exact: true })).toBeVisible();
  const result = await (await request.get('/__test/stats/TUI16', { headers })).json();
  expect(result).toMatchObject({ runs: 1, effects: 1, resume_jobs: 1 });
});

test('review: refreshing a selected ticket replaces its stale detail', async ({ page }) => {
  let changed = false;
  await page.route('**/api/tickets/TUI17', async route => {
    const response = await route.fetch();
    const body = await response.json();
    if (changed) body.body = '服务端已经更新工单内容';
    await route.fulfill({ json: body });
  });
  await page.goto('/'); await login(page); await select(page, 17);
  changed = true;
  await page.getByRole('button', { name: '刷新状态' }).click();
  await expect(page.locator('.ticket-body')).toHaveText('服务端已经更新工单内容');
});

test('review: ticket lookup failure does not hide the independent audit trail', async ({ page }) => {
  await waiting(page, 18);
  await expect(page.locator('.timeline li').first()).toBeVisible();
  let auditReads = 0;
  await page.route('**/api/runs/*/events?*', async route => { auditReads++; await route.continue(); });
  await page.route('**/api/tickets/TUI18', route => route.fulfill({ status: 503, json: { detail: 'TICKET_UNAVAILABLE' } }));
  await page.getByRole('button', { name: '刷新状态' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect.poll(() => auditReads).toBeGreaterThan(0);
  await expect(page.locator('.timeline li').first()).toBeVisible();
});

test('review: read polling cannot erase an unresolved approval submission error', async ({ page }) => {
  await waiting(page, 19); await approver(page);
  let reads = 0;
  await page.route('**/api/runs/*', async route => { reads++; await route.continue(); });
  await page.route('**/api/approvals/*/decision', route => route.abort('failed'));
  await page.getByRole('button', { name: '批准履约重试' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  const current = reads;
  await expect.poll(() => reads).toBeGreaterThan(current);
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('button', { name: '重试原审批决定' })).toBeVisible();
});

test('review: browser back restores a pending create request when run id is unchanged', async ({ page }) => {
  await page.route('**/api/tickets/TUI22/runs', route => route.abort('failed'));
  await page.goto('/'); await login(page); await select(page, 22);
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  const pending = page.url();
  await page.getByRole('link', { name: 'TicketPilot', exact: true }).click();
  await expect(page.getByRole('heading', { name: '待处理工单' })).toBeVisible();
  await page.goBack();
  expect(page.url()).toBe(pending);
  await expect(page.getByRole('button', { name: '开始诊断' })).toBeVisible();
});

test('review: redirected authentication never forwards the role token', async ({ page }) => {
  const outgoing: string[] = [];
  page.on('request', req => outgoing.push(req.url()));
  await page.route('**/api/me', route => route.fulfill({ status: 307, headers: { Location: 'http://127.0.0.1:9/collect' } }));
  await page.goto('/');
  await page.getByLabel('角色 Token').fill('o'.repeat(40));
  await page.getByRole('button', { name: '连接', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  expect(outgoing.some(url => url.includes(':9/'))).toBe(false);
  expect(outgoing.some(url => url.includes('o'.repeat(40)))).toBe(false);
});

test('review: a response for another run cannot populate the current approval page', async ({ page }) => {
  const current = '11111111-1111-4111-8111-111111111111';
  await page.route('**/api/runs/' + current, route => route.fulfill({ json: {
    run_id: '22222222-2222-4222-8222-222222222222', ticket_id: 'TUI21',
    status: 'RUNNING', evidence: [], result_json: null,
  } }));
  await page.goto('/?run=' + current); await login(page, 'approver');
  await expect(page.getByRole('alert')).toContainText('服务响应无效');
  await expect(page.getByRole('button', { name: '批准履约重试' })).toHaveCount(0);
});

test('review: browser drains 203 events and keeps the mobile page within its viewport', async ({ page }) => {
  const id = '33333333-3333-4333-8333-333333333333';
  const cursors: number[] = [];
  await page.route('**/api/runs/' + id, route => route.fulfill({ json: {
    run_id: id, ticket_id: 'TUI21', status: 'SUCCEEDED', disposition: 'NO_ACTION_REQUIRED', evidence: [],
    result_json: { summary: '合成无操作案例。', reason_code: 'ALREADY_QUEUED' },
  } }));
  await page.route('**/api/runs/' + id + '/events?*', route => {
    const cursor = Number(new URL(route.request().url()).searchParams.get('after_id'));
    cursors.push(cursor);
    return route.fulfill({ json: { events: Array.from({ length: Math.min(100, 203 - cursor) }, (_, index) => ({
      id: cursor + index + 1, event_type: 'TEST_EVENT', created_at: '2026-10-19T09:00:00Z', payload_json: {},
    })) } });
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?run=' + id); await login(page);
  await expect(page.getByRole('heading', { name: '调用轨迹 203 条' })).toBeVisible();
  expect(cursors).toEqual([0, 100, 200]);
  expect(await page.locator('.timeline li').count()).toBe(203);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('ticket summaries paginate and fetch the complete body only after selection', async ({ page }) => {
  await page.route('**/api/tickets', async route => {
    const response = await route.fetch({ url: route.request().url() + '?limit=5' });
    const result = await response.json();
    expect(result.tickets).toHaveLength(5);
    expect(result.tickets.every((t: object) => !('body' in t))).toBe(true);
    await route.fulfill({ response });
  });
  await page.goto('/'); await login(page);
  await expect(page.locator('.ticket-list .ticket')).toHaveCount(5);
  await page.getByRole('button', { name: '加载更多工单' }).click();
  await expect(page.locator('.ticket-list .ticket')).toHaveCount(24);
  await expect(page.getByRole('button', { name: '加载更多工单' })).toHaveCount(0);
  await select(page, 24);
  await expect(page.locator('.ticket-body')).toHaveText('合成正文'.repeat(500));
});

test('pending create URL restores a ticket outside the first page with the same key', async ({ page, request }) => {
  const key = 'ac000023-0000-4000-8000-000000000023';
  const created = await request.post('/api/tickets/TUI23/runs', {
    headers: { Authorization: 'Bearer ' + 'o'.repeat(40), 'Idempotency-Key': key },
    data: { auto_note: false },
  });
  expect(created.status()).toBe(202);
  const runId = (await created.json()).run_id;
  await page.route('**/api/tickets', async route => {
    const response = await route.fetch({ url: route.request().url() + '?limit=5' });
    expect((await response.json()).tickets.some((t: { id: string }) => t.id === 'TUI23')).toBe(false);
    await route.fulfill({ response });
  });
  await page.goto('/?ticket=TUI23&request=' + key); await login(page);
  await expect(page.locator('.ticket-body')).not.toBeEmpty();
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page).toHaveURL(new RegExp('run=' + runId));
  expect((await (await request.get('/__test/stats/TUI23', { headers })).json()).runs).toBe(1);
});

test('a delayed ticket detail cannot restore a departed selection', async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let arrived = false;
  await page.route('**/api/tickets/TUI24', async route => {
    const response = await route.fetch(); arrived = true; await gate; await route.fulfill({ response });
  });
  await page.goto('/'); await login(page); await select(page, 24);
  await expect.poll(() => arrived).toBe(true);
  await page.getByRole('link', { name: 'TicketPilot', exact: true }).click();
  release();
  await expect(page.getByRole('heading', { name: '待处理工单' })).toBeVisible();
  await expect(page.locator('.ticket-body')).toHaveCount(0);
});
