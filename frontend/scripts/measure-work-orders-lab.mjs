// Local-only lab: node scripts/measure-work-orders-lab.mjs BUILD_DIR... > results.json
// All API data is synthetic and intercepted. No backend or production writes.
import { chromium } from '@playwright/test';
import { createServer } from 'node:http';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { basename, extname, join, resolve } from 'node:path';
import { gzipSync } from 'node:zlib';

const samples = Number(process.env.LAB_SAMPLES || 5);
const rowCount = Number(process.env.LAB_ROWS || 1000);
const user = { id: 999, company_id: 999, email: 'planner@synthetic.invalid', employee_id: 'LAB-999', first_name: 'Synthetic', last_name: 'Planner', role: 'manager', is_active: true, is_superuser: false };
const orders = Array.from({ length: rowCount }, (_, index) => ({
  id: index + 1, work_order_number: `WO-${String(index + 1).padStart(4, '0')}`,
  part_id: index + 1, part_number: `PN-${index + 1}`, part_name: `Synthetic bracket ${index + 1}`,
  part_type: 'manufactured', work_order_type: 'production', status: 'released', priority: index % 5 + 1,
  quantity_ordered: 100, quantity_complete: index % 10, customer_name: `Synthetic customer ${index % 20}`,
  due_date: '2099-09-30', version: 1, created_at: '2026-09-07T12:00:00Z',
}));
const mime = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2', '.json': 'application/json' };

async function serve(dir) {
  const files = new Map();
  const walk = folder => {
    for (const name of readdirSync(folder)) {
      const file = join(folder, name);
      if (statSync(file).isDirectory()) walk(file);
      else {
        const data = readFileSync(file);
        files.set(file.slice(dir.length).replaceAll('\\', '/'), { data, gzip: gzipSync(data), type: mime[extname(file)] || 'application/octet-stream' });
      }
    }
  };
  walk(dir);
  const server = createServer((request, response) => {
    const path = new URL(request.url, 'http://local.invalid').pathname;
    const file = files.get(path) || files.get('/index.html');
    response.writeHead(200, { 'Content-Type': file.type, 'Content-Encoding': 'gzip', 'Cache-Control': 'no-store' });
    response.end(file.gzip);
  });
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  return { server, origin: `http://127.0.0.1:${server.address().port}` };
}

const browser = await chromium.launch({ headless: true });
const builds = await Promise.all(process.argv.slice(2).map(async path => ({ dir: resolve(path), label: basename(path), ...await serve(resolve(path)), runs: [] })));

async function setup(build) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, timezoneId: 'America/Chicago' });
  await context.addInitScript(({ user }) => {
    sessionStorage.setItem('token', 'synthetic-lab-token');
    sessionStorage.setItem('user', JSON.stringify(user));
    localStorage.setItem(`werco-completed-tours:${user.company_id}:${user.id}`, JSON.stringify(['getting-started']));
  }, { user });
  const page = await context.newPage();
  await page.routeWebSocket('**/*', socket => socket.close());
  const errors = [];
  const apiReads = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === build.origin) return route.continue();
    if (!url.pathname.startsWith('/api/v1/')) return route.abort();
    if (!['GET', 'OPTIONS'].includes(request.method())) return route.fulfill({ status: 400, json: { detail: 'This lab allows synthetic reads only' } });
    apiReads.push(url.pathname);
    await new Promise(done => setTimeout(done, 30));
    let data = [];
    if (url.pathname.endsWith('/users/me')) data = user;
    else if (url.pathname.includes('/companies/me')) data = { id: 999, name: 'Synthetic Lab', slug: 'synthetic-lab', is_active: true };
    else if (/\/work-orders\/?$/.test(url.pathname)) {
      const search = url.searchParams.get('search') || '';
      const skip = Number(url.searchParams.get('skip') || 0);
      const limit = Number(url.searchParams.get('limit') || 500);
      data = orders.filter(order => order.work_order_number.includes(search)).slice(skip, skip + limit);
    } else if (url.pathname.includes('unread-count') || url.pathname.includes('approval-summary')) data = { count: 0 };
    await route.fulfill({ status: 200, json: data });
  });
  const cdp = await context.newCDPSession(page);
  await cdp.send('Emulation.setCPUThrottlingRate', { rate: 4 });
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: 20, downloadThroughput: 625000, uploadThroughput: 625000 });
  return { page, context, errors, apiReads };
}

try {
  // Interleave builds to reduce systematic warm-machine/order bias.
  for (let sample = 0; sample < samples; sample += 1) {
    for (const build of builds) {
      const { page, context, errors, apiReads } = await setup(build);
      await page.goto(`${build.origin}/work-orders`, { waitUntil: 'domcontentloaded' });
      try {
        await page.locator('table tbody tr').filter({ hasText: 'WO-0001' }).first().waitFor({ state: 'visible', timeout: 15000 });
      } catch (error) {
        process.stderr.write(JSON.stringify({ errors, apiReads, text: (await page.locator('body').innerText()).slice(0, 6000) }));
        throw error;
      }
      const readyMs = await page.evaluate(() => new Promise(done => requestAnimationFrame(() => done(performance.now()))));
      const nodeCount = await page.locator('*').count();
      const initialJs = await page.evaluate(() => performance.getEntriesByType('resource').filter(row => row.name.includes('/assets/') && row.name.endsWith('.js')).map(row => new URL(row.name).pathname));
      const searchTerm = `WO-${String(rowCount).padStart(4, '0')}`;
      const filterStart = await page.evaluate(() => performance.now());
      await page.getByPlaceholder('Search by WO#, unit #, part, or customer...').fill(searchTerm);
      await page.waitForResponse(response => new URL(response.url()).searchParams.get('search') === searchTerm);
      await page.waitForFunction(() => document.querySelectorAll('table tbody tr').length === 1);
      const filterMs = await page.evaluate(start => performance.now() - start, filterStart);
      const openStart = await page.evaluate(() => performance.now());
      await page.getByRole('button', { name: /^Import Nest Package$/i }).click();
      await page.getByText(/creates a new released laser cutting work order/i).waitFor();
      const openMs = await page.evaluate(start => performance.now() - start, openStart);
      const openedJs = await page.evaluate(() => performance.getEntriesByType('resource').filter(row => row.name.includes('/assets/') && row.name.endsWith('.js')).map(row => new URL(row.name).pathname));
      build.runs.push({ ready_ms: Math.round(readyMs), filter_ms: Math.round(filterMs), import_open_ms: Math.round(openMs), dom_nodes: nodeCount, initial_js_files: initialJs.length, deferred_js_files: openedJs.filter(file => !initialJs.includes(file)), page_errors: errors });
      process.stderr.write(`${build.label} sample ${sample + 1}: ready ${Math.round(readyMs)}ms, filter ${Math.round(filterMs)}ms, import ${Math.round(openMs)}ms\n`);
      await context.close();
    }
  }
  let chunkFailureProbe;
  if (process.env.LAB_PROBE_CHUNK_FAILURE === '1') {
    const build = builds[builds.length - 1];
    const { page, context } = await setup(build);
    let aborted = false;
    await page.route('**/assets/LaserNestImportWizard-*.js', route => {
      if (!aborted) { aborted = true; return route.abort('failed'); }
      return route.continue();
    });
    await page.goto(`${build.origin}/work-orders`, { waitUntil: 'domcontentloaded' });
    await page.locator('table tbody tr').filter({ hasText: 'WO-0001' }).first().waitFor({ state: 'visible' });
    await page.getByRole('button', { name: /^Import Nest Package$/i }).click();
    await page.getByText('The import tools could not load. Your work-order list is still available.').waitFor();
    await page.getByRole('button', { name: /^Retry loading$/i }).click();
    let recovered = false;
    try { await page.getByText(/creates a new released laser cutting work order/i).waitFor({ timeout: 3000 }); recovered = true; } catch { /* Report actual browser module-cache behavior. */ }
    let recoveredAfterReload = false;
    if (!recovered) {
      await Promise.all([
        page.waitForEvent('domcontentloaded'),
        page.getByRole('button', { name: /^Reload page$/i }).click(),
      ]);
      await page.locator('table tbody tr').filter({ hasText: 'WO-0001' }).first().waitFor({ state: 'visible' });
      await page.getByRole('button', { name: /^Import Nest Package$/i }).click();
      await page.getByText(/creates a new released laser cutting work order/i).waitFor();
      recoveredAfterReload = true;
    }
    chunkFailureProbe = { build: build.label, aborted, recovered_after_retry: recovered, recovered_after_reload: recoveredAfterReload };
    await context.close();
  }
  const median = values => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  process.stdout.write(`${JSON.stringify({
    chunk_failure_probe: chunkFailureProbe,
    environment: { browser: browser.version(), rows: rowCount, samples, viewport: '1440x1000', cpu_throttle: 4, network_mbps: 5, network_latency_ms: 20, synthetic_api_delay_ms: 30, note: 'Controlled local lab; not field performance or a production backend benchmark.' },
    builds: builds.map(({ label, runs }) => ({ label, medians: Object.fromEntries(['ready_ms', 'filter_ms', 'import_open_ms', 'dom_nodes'].map(key => [key, median(runs.map(run => run[key]))])), runs })),
  }, null, 2)}\n`);
} finally {
  await browser.close();
  for (const build of builds) await new Promise(done => build.server.close(done));
}
