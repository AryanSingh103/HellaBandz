// Live browser acceptance: no intercepted fixtures. Drives the deployed web app against the real API,
// manager and workers using fresh isolated portfolios. Never touches main or ui-history-audit ledgers.
const { chromium } = require(process.env.QS_PLAYWRIGHT || 'C:/Users/Krish/Projects/agent-marketplace/frontend/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const out = path.resolve(__dirname, '../../artifacts/ui-fix');
const web = process.env.QS_WEB_URL || 'http://localhost:3000';
const api = process.env.QS_API_URL || 'http://localhost:8000';
const tag = Date.now().toString(36);
const pid = `accept-${tag}`, stopPid = `accept-stop-${tag}`;
const results = { web, api, pid, stopPid, checks: [], errors: [], facts: {} };
const get = async (p, q = {}) => { const r = await fetch(`${api}${p}?${new URLSearchParams(q)}`); return { status: r.status, body: await r.json().catch(() => null) }; };
const post = async (p, q, body) => { const r = await fetch(`${api}${p}?${new URLSearchParams(q)}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); return { status: r.status, body: await r.json().catch(() => null) }; };
const until = async (fn, timeout, what) => { const end = Date.now() + timeout; for (;;) { const v = await fn(); if (v) return v; if (Date.now() > end) throw new Error(`timed out: ${what}`); await new Promise(r => setTimeout(r, 1000)); } };
const job = async id => (await get('/v1/jobs')).body.find(j => j.job_id === id);
let browser;
async function run() {
  fs.mkdirSync(out, { recursive: true });
  browser = await chromium.launch({ executablePath: process.env.QS_CHROMIUM || 'C:/Users/Krish/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
  page.on('pageerror', e => results.errors.push(e.message));
  const check = async (name, fn) => { await fn(); results.checks.push(name); console.log('PASS ' + name); };
  const go = async (view, p) => { await page.goto(`${web}/?p=${p}&view=${view}`, { waitUntil: 'domcontentloaded' }); await page.getByRole('heading', { name: view[0].toUpperCase() + view.slice(1), exact: true }).waitFor(); };
  const accepted = async () => { const text = await page.getByText(/Job accepted: J/).textContent(); return text.match(/Job accepted: (\S+)/)[1]; };
  const preserved = (await get('/v1/portfolio', { portfolio_id: 'ui-history-audit' })).body.digest;
  let runId, researchJob, sessionJob;

  // QS_RUN_ID reuses an existing frozen run (skips the provider-backed research launch) when iterating.
  if (process.env.QS_RUN_ID) { runId = process.env.QS_RUN_ID; results.facts.research = { runId, reused: true, manifest_hash: (await get('/v1/tournaments', { limit: 50 })).body.find(t => t.run_id === runId).manifest_hash }; await go('run', pid); }
  else await check('browser dataset selection launches real research to a frozen manifest', async () => {
    await go('run', pid);
    await page.getByLabel('Registered dataset').selectOption('synthetic-1h-seed42');
    await page.getByRole('button', { name: 'Launch research', exact: true }).click();
    researchJob = await accepted();
    const done = await until(async () => { const j = await job(researchJob); return ['completed', 'failed', 'stopped', 'interrupted'].includes(j.status) && j; }, 900000, 'research job');
    assert.equal(done.status, 'completed', JSON.stringify(done));
    runId = done.run_id;
    const manifest = (await get(`/v1/tournaments/${runId}/manifest`)).body;
    assert.equal(manifest.proposals.length, 3);
    results.facts.research = { job: researchJob, runId, sources: manifest.proposals.map(p => p.source), manifest_hash: done.result.manifest_hash };
  });
  if (!process.env.QS_RUN_ID) await check('research job and its result persist across refresh', async () => {
    await page.reload();
    const card = page.locator('article.job', { hasText: researchJob });
    await card.waitFor(); await card.getByText('completed', { exact: true }).waitFor();
  });
  await check('fresh isolated session from the frozen run completes and verifies', async () => {
    await page.getByLabel('Registered dataset').selectOption('synthetic-1h-seed42');
    await page.getByLabel('Session research run').selectOption(runId);
    await page.getByLabel('Session portfolio ID').fill(pid);
    await page.getByLabel('Session steps').fill('40');
    await page.getByLabel('Session speed').fill('20');
    await page.getByRole('button', { name: 'Launch isolated session' }).click();
    sessionJob = await accepted();
    const done = await until(async () => { const j = await job(sessionJob); return ['completed', 'failed', 'stopped', 'interrupted'].includes(j.status) && j; }, 300000, 'session job');
    assert.equal(done.status, 'completed', JSON.stringify(done));
    const verify = (await get('/v1/verify', { portfolio_id: pid })).body;
    assert.equal(verify.match, true); assert.equal(verify.chain_ok, true);
    results.facts.session = { job: sessionJob, result: done.result, events: verify.events };
    await page.reload();
    await page.locator('article.job', { hasText: sessionJob }).getByRole('button', { name: /Open portfolio/ }).click();
    await page.getByRole('heading', { name: 'Portfolio', exact: true }).waitFor();
    await page.screenshot({ path: path.join(out, 'live-portfolio.png'), fullPage: true });
  });
  await check('research association is scoped to the new portfolio', async () => {
    assert.equal((await get('/v1/context', { portfolio_id: pid })).body.run_id, runId);
    await go('research', pid);
    await page.getByText(`Portfolio ${pid}: ${runId}.`, { exact: true }).waitFor();
    await page.screenshot({ path: path.join(out, 'live-research.png'), fullPage: true });
  });
  let orderId;
  await check('real market order, same-request retry has one effect', async () => {
    await go('orders', pid);
    await page.getByLabel('Order quantity').fill('1');
    await page.getByRole('button', { name: 'Submit order', exact: true }).click();
    await page.getByText(/^Decision: (APPROVE|REDUCE|REJECT)/).waitFor({ timeout: 20000 });
    orderId = (await page.locator('.request-result .mono').textContent()).replace('Request ', '').trim();
    await page.getByRole('button', { name: 'Retry same request' }).click();
    await page.getByText(/^Decision: /).waitFor({ timeout: 20000 });
    const detail = await until(async () => { const d = (await get(`/v1/orders/${orderId}`, { portfolio_id: pid })).body; return d.order && d; }, 20000, 'order detail');
    const decisions = detail.events.filter(e => ['OrderAccepted', 'OrderRejected'].includes(e.type));
    assert.equal(decisions.length, 1, 'retry must not create a second decision');
    results.facts.marketOrder = { orderId, outcome: detail.order.outcome, status: detail.order.status, commands: detail.commands.length };
  });
  await check('limit order rests, lifecycle opens, cancel works, selected-event replay leaves digest unchanged', async () => {
    const before = (await get('/v1/portfolio', { portfolio_id: pid })).body;
    await page.getByRole('button', { name: 'Limit', exact: true }).click();
    await page.getByLabel('Limit price').fill('1');
    await page.getByRole('button', { name: 'Submit order', exact: true }).click();
    await page.getByText('Decision: REJECT', { exact: true }).waitFor({ timeout: 20000 });
    const farId = (await page.locator('.request-result .mono').textContent()).replace('Request ', '').trim();
    results.facts.farLimit = (await get(`/v1/orders/${farId}`, { portfolio_id: pid })).body.order.rules;
    assert.deepEqual(results.facts.farLimit, ['R6_LIMIT_OUTSIDE_BAND']);
    const close = Object.values(before.state.last_bar)[0][1] / 1e6;
    await page.getByLabel('Limit price').fill((Math.floor(close * 97) / 100).toFixed(2));
    await page.getByRole('button', { name: 'Submit order', exact: true }).click();
    await page.getByText(/^Decision: (APPROVE|REDUCE)/).waitFor({ timeout: 20000 });
    await page.getByRole('button', { name: 'Open lifecycle' }).click();
    const dialog = page.getByRole('dialog', { name: 'Order lifecycle' });
    await dialog.waitFor();
    await dialog.getByRole('button', { name: 'Replay through event' }).first().click();
    await page.getByText(/Replayed \d+ \/ \d+ events\. Live ledger unchanged\./).waitFor({ timeout: 30000 });
    const mid = (await get('/v1/portfolio', { portfolio_id: pid })).body;
    assert.equal(mid.seq > before.seq, true);
    await page.keyboard.press('Escape');
    const limitId = (await page.locator('.request-result .mono').textContent()).replace('Request ', '').trim();
    assert.equal((await get('/v1/portfolio', { portfolio_id: pid })).body.digest, mid.digest, 'replay must not write');
    await page.goto(`${web}/?p=${pid}&view=orders&order=${encodeURIComponent(limitId)}`);
    await page.getByRole('dialog', { name: 'Order lifecycle' }).getByRole('button', { name: 'Cancel working order' }).click();
    await until(async () => (await get(`/v1/orders/${limitId}`, { portfolio_id: pid })).body.order.status === 'cancelled', 20000, 'cancellation');
    results.facts.limitOrder = { limitId, status: 'cancelled' };
  });
  await check('older history pages and full verification in the browser', async () => {
    await go('audit', pid);
    await page.getByText(/^Verified: \d+ events, hash chain intact, projection matches replay\.$/).waitFor({ timeout: 30000 });
    const older = page.getByRole('button', { name: 'Older', exact: true });
    await until(async () => await older.isEnabled(), 15000, 'older page available');
    await older.click(); await page.getByText('Page 2', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(out, 'live-audit.png'), fullPage: true });
  });
  await check('managed worker crash: selected portfolio down and queueing while another stays healthy', async () => {
    const digest = (await get('/v1/portfolio', { portfolio_id: pid })).body.digest;
    assert.equal((await post('/v1/runtime', { portfolio_id: pid }, { action: 'crash' })).status, 200);
    await until(async () => (await get('/v1/runtime', { portfolio_id: pid })).body.status === 'intentional_down', 20000, 'intentional down');
    await go('orders', pid);
    await page.getByText('Worker intentionally down', { exact: true }).waitFor({ timeout: 15000 });
    assert.equal((await get('/v1/runtime', { portfolio_id: 'ui-history-audit' })).body.ready, true);
    await page.getByLabel('Order quantity').fill('1');
    await page.getByRole('button', { name: 'Submit order', exact: true }).click();
    await page.getByText('Queued: awaiting the selected execution worker.', { exact: true }).waitFor({ timeout: 20000 });
    await page.getByRole('button', { name: 'Retry same request' }).click();
    await until(async () => (await get('/v1/runtime', { portfolio_id: pid })).body.pending_commands === 1, 15000, 'exactly one queued command');
    assert.equal((await get('/v1/portfolio', { portfolio_id: pid })).body.digest, digest);
    await page.screenshot({ path: path.join(out, 'live-worker-down.png'), fullPage: true });
    assert.equal((await post('/v1/runtime', { portfolio_id: pid }, { action: 'restart' })).status, 200);
    await until(async () => { const r = (await get('/v1/runtime', { portfolio_id: pid })).body; return r.ready && r.pending_commands === 0; }, 45000, 'restart drains queue');
    const verify = (await get('/v1/verify', { portfolio_id: pid })).body;
    assert.equal(verify.match, true);
    await page.getByText('Worker healthy', { exact: true }).waitFor({ timeout: 15000 });
    results.facts.recovery = { events: verify.events, digest: verify.stored_digest };
  });
  await check('running session stops from the browser and keeps its ledger', async () => {
    await go('run', stopPid);
    await page.getByLabel('Registered dataset').selectOption('synthetic-1h-seed42');
    await page.getByLabel('Session research run').selectOption(runId);
    await page.getByLabel('Session portfolio ID').fill(stopPid);
    await page.getByLabel('Session steps').fill('2000');
    await page.getByLabel('Session speed').fill('2');
    await page.getByRole('button', { name: 'Launch isolated session' }).click();
    const id = await accepted();
    await until(async () => (await job(id)).progress >= 2, 120000, 'session progress');
    await page.locator('article.job', { hasText: id }).getByRole('button', { name: 'Stop', exact: true }).click();
    const stopped = await until(async () => { const j = await job(id); return j.status !== 'running' && j.status !== 'queued' && j; }, 60000, 'session stop');
    assert.equal(stopped.status, 'stopped', JSON.stringify(stopped));
    await page.locator('article.job', { hasText: id }).getByText('stopped', { exact: true }).waitFor();
    assert.equal((await get('/v1/verify', { portfolio_id: stopPid })).body.match, true);
    results.facts.stopped = { job: id, progress: stopped.progress };
  });
  await check('Ask: validation, real grounded answer, and no carry-over to another portfolio', async () => {
    await go('portfolio', pid);
    await page.getByRole('button', { name: 'Ask', exact: true }).click();
    const q = page.getByLabel('Ask question');
    await q.fill('x'.repeat(501)); await page.getByRole('button', { name: 'Ask', exact: true }).last().click();
    await page.getByText('Enter a nonempty question of no more than 500 characters.').waitFor();
    await q.fill('What is our current equity and exposure?'); await page.getByRole('button', { name: 'Ask', exact: true }).last().click();
    const answer = page.locator('.answer');
    await answer.waitFor({ timeout: 60000 });
    results.facts.ask = (await answer.textContent()).slice(0, 600);
    assert.match(results.facts.ask, new RegExp(pid));
    await page.screenshot({ path: path.join(out, 'live-ask.png') });
  });
  await check('invalid inputs are rejected with 4xx, not 500', async () => {
    for (const cash of ['abc', '-1', '0', 'NaN', 'Infinity', '1e400']) { const r = await post('/v1/admin/portfolio', { portfolio_id: `accept-bad-${tag}`, wait: false }, { initial_cash_usd: cash }); assert.ok(r.status >= 400 && r.status < 500, `initial_cash_usd ${cash} -> ${r.status}`); }
    for (const capital of ['abc', '-1', 'NaN']) { const r = await post('/v1/admin/allocations', { portfolio_id: `accept-bad-${tag}`, wait: false }, { agent_id: 'a', capital_usd: capital }); assert.ok(r.status >= 400 && r.status < 500, `capital_usd ${capital} -> ${r.status}`); }
    assert.equal((await get('/v1/runtime', { portfolio_id: `accept-bad-${tag}` })).body.pending_commands, 0, 'invalid requests must not queue commands');
    const dup = await fetch(`${api}/v1/portfolio?portfolio_id=ui-history-audit&portfolio_id=main`);
    assert.ok(dup.status >= 400 && dup.status < 500, `duplicate portfolio_id -> ${dup.status}`);
    const bad = await get('/v1/portfolio', { portfolio_id: 'a&b c' });
    assert.ok(bad.status >= 400 && bad.status < 500, `invalid identifier -> ${bad.status}`);
    const t = (await get(`/v1/tournaments/${runId}/manifest`)).body;
    const tampered = await post('/v1/tournaments', {}, { run_id: runId, manifest: { ...t, audit_probe: true }, manifest_hash: results.facts.research.manifest_hash });
    assert.ok(tampered.status >= 400 && tampered.status < 500, `tampered manifest -> ${tampered.status}`);
    assert.equal((await get(`/v1/tournaments/${runId}/manifest`)).body.audit_probe, undefined);
  });
  await check('preserved history demo untouched', async () => {
    const now = (await get('/v1/portfolio', { portfolio_id: 'ui-history-audit' })).body;
    assert.equal(now.digest, preserved); assert.equal(now.seq, 2214);
  });
  assert.deepEqual(results.errors, [], 'No uncaught browser errors');
}
run().catch(e => { console.error(e); results.failure = String(e && e.stack || e); process.exitCode = 1; }).finally(async () => {
  fs.writeFileSync(path.join(out, 'live-acceptance-results.json'), JSON.stringify({ ...results, at: new Date().toISOString() }, null, 2));
  await browser?.close();
});
