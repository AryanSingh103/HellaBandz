// Browser-level launch/export check. Creates one fresh 100-step paper portfolio.
const {createRequire} = require('node:module');
const path = require('node:path');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const {chromium} = createRequire(path.join(root, 'apps/web/package.json'))('playwright-core');
const api = 'http://127.0.0.1:8000';
const web = 'http://127.0.0.1:3000';
const out = __dirname;
async function get(route) {
  const response = await fetch(api + route, {signal: AbortSignal.timeout(20000)});
  assert(response.ok, `${route} HTTP ${response.status}`);
  return response.json();
}
async function main() {
  const browser = await chromium.launch({channel:'chrome', headless:true});
  const errors = [];
  try {
    const context = await browser.newContext({acceptDownloads:true, viewport:{width:1440,height:1050}});
    const page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(web+'/?view=simulation&p=main', {waitUntil:'domcontentloaded'});
    await page.getByRole('heading',{name:'Simulation',exact:true}).waitFor();
    console.log('Simulation page loaded');
    const selector = page.getByLabel('Simulation market data');
    await selector.locator('option[value="alpaca-sip-1hour-2023-01-03-2026-09-30"]').waitFor({state:'attached'});
    assert.equal(await selector.inputValue(), 'alpaca-sip-1hour-2023-01-03-2026-09-30');
    await page.waitForTimeout(2000);
    if (!(await page.getByLabel('Simulation length').isVisible())) {
      await page.locator('summary').filter({hasText:'New simulation'}).click();
    }
    console.log('Launcher opened');
    await page.getByLabel('Simulation length').selectOption('100');
    const [request, response] = await Promise.all([
      page.waitForRequest(r => r.url().endsWith('/v1/jobs') && r.method()==='POST'),
      page.waitForResponse(r => r.url().endsWith('/v1/jobs') && r.request().method()==='POST'),
      page.getByRole('button',{name:'Start simulation',exact:true}).click(),
    ]);
    const payload = request.postDataJSON();
    assert.equal(payload.kind,'coordinated');
    assert.equal(payload.crash_at,null);
    assert.equal(payload.steps,100);
    assert.equal(payload.dataset_id,'alpaca-sip-1hour-2023-01-03-2026-09-30');
    assert(response.ok(),`Launch HTTP ${response.status()}`);
    const submitted = await response.json();
    fs.writeFileSync(path.join(out,'browser-launch.json'),JSON.stringify({payload,submitted},null,2));
    let job;
    const deadline=Date.now()+15*60*1000;
    let previous='';
    while(Date.now()<deadline) {
      const jobs=await get('/v1/jobs');
      job=jobs.find(j=>j.portfolio_id===payload.portfolio_id);
      assert(job,'Fresh browser job exists');
      const stage=`${job.status}:${job.stage}:${Math.floor(job.progress/20)*20}`;
      if(stage!==previous){console.log(stage);previous=stage;}
      if(!['queued','running'].includes(job.status))break;
      await new Promise(resolve=>setTimeout(resolve,1500));
    }
    assert.equal(job.status,'completed',JSON.stringify(job));
    await page.goto(web+'/?'+new URLSearchParams({view:'simulation',p:payload.portfolio_id}),{waitUntil:'domcontentloaded'});
    await page.getByRole('button',{name:'Download full history',exact:true}).waitFor({timeout:30000});
    const downloadPromise=page.waitForEvent('download',{timeout:90000});
    await page.getByRole('button',{name:'Download full history',exact:true}).click();
    const download=await downloadPromise;
    const target=path.join(out,'browser-export.json');
    await download.saveAs(target);
    const exported=JSON.parse(fs.readFileSync(target,'utf8'));
    assert.equal(exported.simulation.config.kind,'coordinated');
    assert.equal(exported.simulation.config.portfolio_id,payload.portfolio_id);
    assert.equal(exported.steps.length,100);
    const verification=await get('/v1/verify?'+new URLSearchParams({portfolio_id:payload.portfolio_id}));
    assert.equal(verification.match,true);
    assert.equal(verification.chain_ok,true);
    assert(exported.agents.length>=4,'Three challenger roles plus passive core are recorded');
    assert(exported.agents.some(a=>a.funded_in_this_simulation),'Funded sleeve is shown');
    assert(exported.buy_and_hold_comparison,'Matched baseline present');
    await page.screenshot({path:path.join(out,'simulation-desktop.png'),fullPage:false});
    await page.setViewportSize({width:390,height:1050});
    await page.screenshot({path:path.join(out,'simulation-mobile.png'),fullPage:false});
    assert.deepEqual(errors,[],'No uncaught browser exceptions');
    const result={status:'PASS',payload,job,verification,export_keys:Object.keys(exported),
      agent_count:exported.agents.length,metrics:exported.metrics,errors};
    fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify(result,null,2));
    console.log(JSON.stringify({status:'PASS',portfolio_id:payload.portfolio_id,metrics:exported.metrics,agent_count:exported.agents.length}));
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;});
