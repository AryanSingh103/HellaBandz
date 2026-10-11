// Bounded, read-only smoke check of the running demo. No orders or research jobs.
// npm ci --prefix apps/web; node scripts/web/browser-check.cjs
// Uses installed Chrome, or QS_CHROMIUM. QS_WEB_URL/QS_API_URL override localhost.
const { createRequire } = require('node:module');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const { chromium } = createRequire(path.join(root, 'apps/web/package.json'))('playwright-core');
const web = process.env.QS_WEB_URL || 'http://localhost:3000';
const api = process.env.QS_API_URL || 'http://localhost:8000';
const out = path.join(root, 'artifacts/browser-smoke');
function accessKey() {
  if (process.env.QS_API_TOKEN) return process.env.QS_API_TOKEN;
  const env = path.join(root, '.env');
  const line = fs.existsSync(env) && fs.readFileSync(env, 'utf8').split(/\r?\n/).find(s => /^QS_API_TOKEN=/.test(s));
  return line ? line.slice(line.indexOf('=') + 1).trim().replace(/^(["'])(.*)\1$/, '$2') : '';
}
async function main() {
  const key = accessKey();
  const headers = key ? { Authorization: `Bearer ${key}` } : {};
  async function get(route) {
    const response = await fetch(api + route, { headers, signal: AbortSignal.timeout(15000) });
    assert(response.ok, `${route}: HTTP ${response.status}`);
    return response.json();
  }
  const [health, portfolios] = await Promise.all([get('/healthz'), get('/v1/portfolios')]);
  assert(health, 'API health response');
  const pid = process.env.QS_PORTFOLIO_ID || (portfolios.find(p => p.portfolio_id === 'main') || portfolios[0])?.portfolio_id || 'main';
  const before = await get(`/v1/verify?portfolio_id=${encodeURIComponent(pid)}`);
  const browser = await chromium.launch(process.env.QS_CHROMIUM ? { executablePath: process.env.QS_CHROMIUM, headless: true } : { channel: 'chrome', headless: true });
  const errors = [], checks = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
    const auth = await context.request.get(api + '/v1/auth/status');
    const status = await auth.json();
    if (status.required) {
      assert(key, 'Set QS_API_TOKEN or configure the ignored local .env');
      const session = await context.request.post(api + '/v1/auth/session', { headers });
      assert(session.ok(), `Operator sign-in: HTTP ${session.status()}`);
    }
    const page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message));
    fs.mkdirSync(out, { recursive: true });
    const headings = { browse: 'All strategies', marketplace: 'Research worth comparing.Strategies you can own.', portfolio: 'Portfolio', orders: 'Orders', research: 'Research', audit: 'Audit', analytics: 'Analytics', run: 'Run' };
    for (const [view, heading] of Object.entries(headings)) {
      await page.goto(`${web}/?${new URLSearchParams({ p: pid, view })}`, { waitUntil: 'domcontentloaded' });
      await (view === 'marketplace' ? page.locator('.market-hero h1') : page.getByRole('heading', { name: heading, exact: true })).waitFor({ timeout: 20000 });
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${view}: horizontal overflow`);
      checks.push(view);
    }
    for (const width of [390, 768, 1440]) {
      await page.setViewportSize({ width, height: 1050 });
      await page.goto(`${web}/?${new URLSearchParams({ p: pid, view: 'browse' })}`, { waitUntil: 'domcontentloaded' });
      await page.getByRole('heading', { name: headings.browse, exact: true }).waitFor();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Browse overflow at ${width}px`);
      await page.screenshot({ path: path.join(out, `browse-${width}.png`), fullPage: true });
    }
    const after = await get(`/v1/verify?portfolio_id=${encodeURIComponent(pid)}`);
    assert.notEqual(after.chain_ok, false, 'Ledger hash chain');
    assert.notEqual(after.match, false, 'Ledger replay digest');
    // A live worker may append legitimate events during the read-only check.
    if (before.events === after.events) assert.equal(after.stored_digest, before.stored_digest);
    assert.deepEqual(errors, [], 'Uncaught browser exceptions');
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify({ web, api, pid, checks, errors, verification: after, at: new Date().toISOString() }, null, 2));
    console.log(`PASS: ${checks.length} views, 3 viewport sizes, ledger verification. Results: artifacts/browser-smoke`);
  } finally {
    await browser.close();
  }
}
main().catch(e => { console.error(e.message); process.exitCode = 1; });
