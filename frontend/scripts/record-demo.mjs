// Deterministic, disposable browser recording. Never logs in to the user's demo database.
import { chromium, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdir, readFile, writeFile, access } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const output = path.resolve(process.argv[2] ?? path.join(root, 'artifacts/m5-demo-2026-10-23.mp4'));
if (!process.env.M0_TEST_ADMIN_URL) throw new Error('Set M0_TEST_ADMIN_URL for a disposable recording.');
try { await access(output); throw new Error('Output exists; choose a new path.'); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const base = 'http://127.0.0.1:8023';
try { await fetch(base + '/health/live'); throw new Error('Recording port is already in use.'); }
catch (error) { if (!(error instanceof TypeError)) throw error; }
const headers = { Authorization: 'Bearer ' + 'a'.repeat(40) };
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const directory = path.join(path.dirname(output), 'm5-recording-' + Date.now());
await mkdir(directory, { recursive: true });
const server = spawn('uv', ['run', '--locked', 'python', '-m', 'tests.ui_server'], {
  cwd: path.join(root, 'backend'),
  env: { ...process.env, M4_TEST_PORT: '8023' }, windowsHide: true, stdio: 'ignore',
});
let browser, context, page, video;
const measurements = {};
async function request(url, data) {
  const result = await fetch(base + url, {
    method: data ? 'POST' : 'GET', headers: { ...headers, 'Content-Type': 'application/json' },
    ...(data ? { body: JSON.stringify(data) } : {}),
  });
  if (!result.ok) throw new Error('Recording fixture request failed: HTTP ' + result.status);
  return result.json();
}
async function caption(title, text) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.evaluate(({ title, text }) => {
    document.querySelector('[data-m5-caption]')?.remove();
    const panel = document.createElement('section');
    panel.setAttribute('data-m5-caption', '');
    Object.assign(panel.style, {
      position: 'fixed', zIndex: '9999', left: '0', right: '0', bottom: '0',
      padding: '14px 30px', background: '#10283ff5', color: '#fff',
      fontFamily: 'Microsoft YaHei, sans-serif', boxShadow: '0 -4px 20px #0002',
      pointerEvents: 'none',
    });
    const heading = document.createElement('strong'); heading.textContent = title;
    heading.style.cssText = 'display:block;font-size:22px;letter-spacing:.5px;margin-bottom:5px';
    const detail = document.createElement('div'); detail.textContent = text; detail.style.fontSize = '16px';
    const label = document.createElement('div');
    label.textContent = '确定性演示 · 固定模型替身 · 真实 PostgreSQL / HTTP / 审批 / Worker · 合成数据';
    label.style.cssText = 'font-size:12px;color:#a9d4ea;margin-top:5px';
    panel.append(heading, detail, label); document.body.append(panel);
  }, { title, text });
}
async function login(role = 'operator') {
  await page.getByLabel('角色 Token').fill((role === 'operator' ? 'o' : 'a').repeat(40));
  await page.getByRole('button', { name: '连接', exact: true }).click();
  await expect(page.getByRole('button', { name: '切换身份' })).toBeVisible();
}
async function start(ticket) {
  await page.goto(base); await login();
  await page.getByRole('button', { name: new RegExp('^' + ticket + '\\s') }).click();
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.getByRole('heading', { name: '等待人工审批', exact: true })).toBeVisible();
}
async function approver() {
  await page.getByRole('button', { name: '切换身份' }).click(); await login('approver');
  await expect(page.getByRole('button', { name: '批准履约重试' })).toBeEnabled();
}
try {
  let ready = false;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (server.exitCode !== null) throw new Error('Recording fixture exited early.');
    try { ready = (await fetch(base + '/health/ready')).ok; } catch {}
    if (ready) break;
    await wait(500);
  }
  if (!ready) throw new Error('Recording fixture did not become ready.');
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  context = await browser.newContext({ viewport: { width: 1440, height: 900 },
    recordVideo: { dir: directory, size: { width: 1440, height: 900 } } });
  page = await context.newPage(); video = page.video();
  const began = Date.now();
  const until = async second => { const remaining = began + second * 1000 - Date.now(); if (remaining > 0) await wait(remaining); };
  await page.goto(base);
  await caption('TicketPilot | 可审批、可恢复的履约重试', '单场景：已支付但履约发布超时。模型查证并提案，人批准后由服务端受控执行。');
  await until(15);
  await login(); await page.getByRole('button', { name: /^TUI1\s/ }).click();
  await caption('00:15 | 业务边界', '三种只读工具、一个受审批的重试操作；不退款、不改地址、不自动关闭工单。');
  await until(30);
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.getByRole('heading', { name: '等待人工审批', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '批准履约重试' })).toBeDisabled();
  await expect(page.locator('.timeline li').first()).toBeVisible();
  await caption('00:30 | 查证与提案', '证据与提案已经保存。操作员不能批准；模型没有写工具。此处诊断由固定模型替身产生。');
  await until(60);
  const original = page.url();
  await page.reload(); await login('approver');
  if (page.url() !== original) throw new Error('Refresh changed the run.');
  await caption('01:00 | 刷新恢复同一 Run', '页面刷新后重新登录审批员。审批引用原提案哈希；刷新不创建新任务。');
  await until(75);
  await page.getByRole('button', { name: '批准履约重试' }).click();
  await expect(page.getByRole('heading', { name: '已重新入队', exact: true })).toBeVisible();
  measurements.approved = await request('/__test/stats/TUI1');
  expect(measurements.approved).toMatchObject({ runs: 1, effects: 1, resume_jobs: 1, worker_failed: false });
  await caption('01:15 | 批准并验证业务效果', '实际数据库：Run=1、恢复任务=1、重新入队事件=1。QUEUED 表示重新入队，不表示已发货。');
  await page.screenshot({ path: path.join(directory, 'approved.png') });
  await until(100);
  await start('TUI2'); await approver();
  await page.getByLabel('审批理由').fill('合成演示：拒绝高影响操作');
  await page.getByRole('button', { name: '拒绝', exact: true }).click();
  await expect(page.getByRole('heading', { name: '审批已拒绝', exact: true })).toBeVisible();
  measurements.rejected = await request('/__test/stats/TUI2');
  expect(measurements.rejected.effects).toBe(0);
  await caption('01:40 | 拒绝分支', '审批拒绝后结束，数据库业务效果为 0；人工拒绝属于预期结果。');
  await until(120);
  await start('TUI3'); await approver();
  await request('/__test/control', { version_ticket: 'TUI3' });
  await page.getByRole('button', { name: '批准履约重试' }).click();
  await expect(page.getByRole('heading', { name: '提案已失效，需重新诊断' })).toBeVisible();
  measurements.stale = await request('/__test/stats/TUI3');
  expect(measurements.stale.effects).toBe(0);
  await caption('02:00 | 审批后的版本变化', '测试控制器模拟订单版本从 v3 变为 v4。旧提案被服务端拦截，业务效果为 0。');
  await page.screenshot({ path: path.join(directory, 'stale.png') });
  await until(150);
  const fixed = JSON.parse(await readFile(path.join(root, 'artifacts/m5-fixed-2026-10-23.json'), 'utf8'));
  const summary = fixed.summary['fixed/test'];
  await page.setContent('<body><h1>M5 | 评测结果与限制</h1><div id="results"></div></body>');
  // The app CSP persists across setContent; DOM style properties also work with it.
  await page.evaluate(() => {
    Object.assign(document.body.style, { background: '#eef3f7', fontFamily: 'Microsoft YaHei,sans-serif', padding: '65px', color: '#16324a' });
    document.querySelector('h1').style.fontSize = '42px';
    Object.assign(document.querySelector('#results').style, { fontSize: '25px', lineHeight: '2.2', whiteSpace: 'pre-line' });
  });
  await page.locator('#results').evaluate((node, text) => node.textContent = text,
    `冻结案例：4 dev + 12 test\n固定流程 test：${summary.end_to_end.numerator}/${summary.end_to_end.denominator} 符合预期\n真实 Agent：既有 dev 4 次 API 失败；本次最小探测 HTTP 402\nAgent test 尚未运行，模型费用未知；不能声称模型成功率或收益\n崩溃恢复与幂等性：查看随附完整测试报告与 M5-评测报告.md`);
  await caption('02:30 | 实测数字与已知限制', '固定流程通过不能代替真实模型评测。单场景、单 Worker、合成业务和本地演示身份。');
  await page.screenshot({ path: path.join(directory, 'results.png') });
  await until(180);
  await context.close(); context = null;
  const input = await video.path();
  const ffmpeg = spawn('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-n', '-i', input,
    '-t', '180', '-vf', 'fps=25', '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', output], { windowsHide: true, stdio: 'ignore' });
  const [code] = await once(ffmpeg, 'exit');
  if (code !== 0) throw new Error('Video encoding failed; original WebM preserved.');
  await writeFile(output.replace(/\.mp4$/i, '-evidence.json'), JSON.stringify({
    fixed_model_substitute: true, synthetic_data: true, measurements, directory,
  }, null, 2));
  console.log('Saved ' + output);
} finally {
  if (context) await context.close();
  if (browser) await browser.close();
  try { await request('/__test/shutdown', {}); } catch {}
  // Wait for Python finally to drop only its own random test database.
  const ended = Promise.race([once(server, 'exit'), wait(25000)]);
  if (server.exitCode === null) await ended;
  if (server.exitCode === null) throw new Error('Fixture cleanup did not finish; inspect the owned process.');
}
