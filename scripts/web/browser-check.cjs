const { chromium } = require('C:/Users/Krish/Projects/agent-marketplace/frontend/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const out = path.join(root, 'artifacts/ui-fix');
const base = process.env.QS_WEB_URL || 'http://localhost:3000';
const mode = process.argv.includes('--live') ? 'live' : 'contract';
const manifest = JSON.parse(fs.readFileSync(path.join(root,'data/manifests/T20261010203916.json')));
const equityDataset = {...JSON.parse(fs.readFileSync(path.join(root,'data/fixtures/synthetic_1h/manifest.json'))),execution_usable:true,synthetic:true,quote_currency:'USD',lots_per_base_unit:1,quality:{ok:true}};
const cryptoDataset = {...equityDataset,dataset_id:'browser-contract-crypto',content_sha256:'contract-crypto-hash',source:'Browser contract fixture',symbols:['BTCUSDT'],quote_currency:'USDT',lots_per_base_unit:1000,synthetic:true};
const state = {opened:true,initial_cash:1e10,cash:9e9,equity:1e10,market_value:1e9,reserved_cash:0,realized_pnl:0,fees_paid:1000,slippage_cost:10000,allocations:{momentum:4e9},positions:{'momentum|AAPL':{qty:10,basis:1e9,reserved:0}},orders:{},last_bar:{AAPL:[1767205800,1e8]},kill_switch:false,kill_reason:''};
const portfolio = {seq:402,digest:'contract-digest',head_hash:'contract-hash',state,equity:1e10,updated_at:new Date().toISOString()};
const order = {order_id:'older-order',agent_id:'momentum',symbol:'AAPL',side:'BUY',requested_qty:10,approved_qty:10,filled_qty:5,order_type:'MKT',status:'working',outcome:'APPROVE',rules:[],cursor_seq:20};
const event = {seq:20,type:'OrderFilled',order_id:'older-order',agent_id:'momentum',symbol:'AAPL',side:'BUY',qty:5,px:1e8,fee:1000,hash:'contract-event-hash'};
const checks=[], errors=[];
let jobs=[],submitted=[],askMode='success',historyFailure=false;
let launchedBrowser;
async function mocked(page) {
  await page.route('http://localhost:8000/**',async route=>{
    const req=route.request(),u=new URL(req.url()),p=u.searchParams.get('portfolio_id')||'main',body=req.method()==='POST'?req.postDataJSON():null;
    const send=(v,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(v)});
    if(historyFailure&&u.pathname==='/v1/history/events')return route.abort('failed');
    if(u.pathname==='/v1/portfolios')return send(['main','ui-history-audit','crypto-contract'].map(portfolio_id=>({portfolio_id,ownership:'supervisor'})));
    if(u.pathname==='/v1/datasets')return send([equityDataset,cryptoDataset]);
    if(u.pathname==='/v1/tournaments')return send([{run_id:manifest.run_id,manifest,manifest_hash:'contract-hash',created_at:manifest.created_at}]);
    if(u.pathname==='/v1/context')return send(p==='main'?{portfolio_id:p,association:'unassociated',manifest:null}:{portfolio_id:p,run_id:manifest.run_id,dataset_id:p==='crypto-contract'?cryptoDataset.dataset_id:equityDataset.dataset_id,manifest:p==='crypto-contract'?{...manifest,dataset:{...manifest.dataset,dataset_id:cryptoDataset.dataset_id,content_sha256:cryptoDataset.content_sha256}}:manifest});
    if(u.pathname==='/v1/portfolio')return p==='main'?send({detail:'portfolio not opened'},404):send(p==='crypto-contract'?{...portfolio,state:{...state,last_bar:{BTCUSDT:[1767205800,42000000]},positions:{'momentum|BTCUSDT':{qty:10,basis:420000000,reserved:0}}}}:portfolio);
    if(u.pathname==='/v1/runtime')return send({worker_alive:true,worker_last_heartbeat:new Date().toISOString(),pending_commands:0,ready:true,ownership:'supervisor',manager_alive:true,intentional_down:false,status:'running',recovery:null});
    if(u.pathname==='/v1/verify')return send(p==='main'?{status:'not_opened',events:0,match:null,chain_ok:null}:{events:402,match:true,chain_ok:true,stored_digest:'same',replayed_digest:'same'});
    if(u.pathname==='/v1/capabilities')return send({initial_capital:10000,research_provider:'labeled fixture/fallback',speech_provider:'browser fallback',execution:'historical simulated, long-only, cash-funded',live_observation:false,kalshi_discovery:false});
    if(u.pathname==='/v1/jobs'&&body){const j={...body,job_id:'Jcontract',run_id:body.run_id||'Tcontract',portfolio_id:body.portfolio_id||null,status:'queued',stage:'queued',progress:0,total:100,created_at:new Date().toISOString()};jobs=[j];return send(j,202);}
    if(u.pathname==='/v1/jobs')return send(jobs);
    if(u.pathname.endsWith('/stop')){jobs=jobs.map(j=>({...j,status:'stopped',stop_requested:true}));return send(jobs[0]);}
    if(u.pathname==='/v1/orders'&&body){submitted.push(body);return send({command_seq:7,status:'queued'},202);}
    if(u.pathname.startsWith('/v1/commands/'))return send({status:'done',response:{outcome:'APPROVE',approved_qty:10}});
    if(u.pathname.startsWith('/v1/orders/'))return send(body?{status:'processed'}:{order,commands:[{seq:7,kind:'submit_order',status:'done'}],events:[{seq:20,type:event.type,event}]});
    if(u.pathname.startsWith('/v1/history/')){const collection=u.pathname.split('/').pop(),older=u.searchParams.has('cursor');return send({items:collection==='orders'?[{...order,order_id:older?'oldest-order':'older-order'}]:p==='main'?[]:[{seq:20,type:event.type,event,cursor_seq:20}],snapshot_seq:402,next_cursor:older?null:20});}
    if(u.pathname.startsWith('/v1/replay/'))return send({event,before:{...state,equity:1e10},after:{...state,cash:8.5e9,fees_paid:2000,equity:1e10},progress:20,total:20,through_seq:20});
    if(u.pathname==='/v1/analytics/history')return send({points:p==='main'?[]:[{seq:1,ts:1767205800,equity:1e10,exposure:1e9,agents:{momentum:{pnl:0,exposure:1e9}}},{seq:2,ts:1767209400,equity:1.01e10,exposure:1.1e9,agents:{momentum:{pnl:1e8,exposure:1.1e9}}}],definitions:{agent_pnl:'Realized plus marked holdings minus basis minus fees',exposure:'Held quantity times applied close',throughput:'Processed commands per 10-second bucket'},benchmark:{status:'unavailable',reason:'No matching baseline artifact'}});
    if(u.pathname.startsWith('/v1/analytics/')||u.pathname==='/v1/agent-messages')return send([]);
    if(u.pathname==='/v1/ask'){if(askMode==='network')return route.abort('failed');if(askMode==='malformed')return send({wrong:'invalid'});return send({answer:`Grounded contract answer for ${body.portfolio_id}`,mode:'deterministic'});}
    if(u.pathname==='/v1/speak')return send({detail:'not configured'},501);
    if(u.pathname.startsWith('/v1/admin/'))return send({status:'processed'});
    return send({detail:'Unhandled contract fixture: '+u.pathname},404);
  });
}
async function run() {
  fs.mkdirSync(out,{recursive:true});
  const browser=await chromium.launch({executablePath:'C:/Users/Krish/AppData/Local/ms-playwright/chromium-1234/chrome-win64/chrome.exe',headless:true});
  launchedBrowser=browser;
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
  page.on('pageerror',e=>errors.push(e.message));
  if(mode==='contract')await mocked(page);
  async function check(name,fn){await fn();checks.push(name);console.log('PASS '+name);}
  const go=async(view='browse',p='ui-history-audit',extra='')=>{await page.goto(`${base}/?p=${p}&view=${view}${extra}`,{waitUntil:'domcontentloaded'});await page.getByRole('heading',{name:view==='browse'?'Browse Strategies':view[0].toUpperCase()+view.slice(1),exact:true}).waitFor();};
  await check('responsive populated Browse and real assets',async()=>{
    for(const width of [390,768,1440,1920]){await page.setViewportSize({width,height:1050});await go();await page.locator('.strategy-card').first().waitFor();await page.screenshot({path:path.join(out,`${mode}-browse-${width}.png`),fullPage:true});assert.equal(await page.locator('.strategy-card').count(),3);assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));assert.equal(await page.locator('.tickers img').evaluateAll(imgs=>imgs.filter(i=>!i.complete||!i.naturalWidth).length),0);}
    await page.setViewportSize({width:1440,height:1050});
  });
  await check('search, category, sort, clear and empty results',async()=>{
    await page.getByRole('link',{name:'Reversion',exact:true}).click();assert.equal(await page.locator('.strategy-card').count(),1);await page.getByRole('link',{name:'Crypto',exact:true}).click();await page.getByText('No strategies match these filters.',{exact:false}).waitFor();await page.getByRole('button',{name:'Clear filters'}).click();await page.getByLabel('Search strategies').fill('nonexistent-contract');await page.getByText('No strategies match these filters.',{exact:false}).waitFor();await page.getByLabel('Search strategies').fill('');await page.getByLabel('Sort strategies').selectOption('risk');assert.equal(await page.locator('.strategy-card').count(),3);
  });
  await check('strategy detail and browser back navigation',async()=>{
    await page.locator('.strategy-card').first().click();await page.getByRole('dialog').waitFor();await page.getByRole('button',{name:'Close details',exact:true}).click();await page.getByRole('link',{name:/^research$/i}).click();await page.getByRole('heading',{name:'Research',exact:true}).waitFor();await page.goBack();await page.getByRole('heading',{name:'Browse Strategies',exact:true}).waitFor();
  });
  await check('all operational navigation',async()=>{for(const view of ['portfolio','orders','research','audit','analytics','run']){await go(view);await page.screenshot({path:path.join(out,`${mode}-${view}.png`),fullPage:true});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}});
  await check('unopened verification is neutral',async()=>{await go('audit',mode==='live'?'never-opened-check':'main');await page.getByText('Not opened: no ledger to verify.',{exact:true}).waitFor();assert.equal(await page.getByText('Integrity mismatch.',{exact:false}).count(),0);await page.screenshot({path:path.join(out,`${mode}-unopened.png`),fullPage:true});});
  if(mode==='contract'){
    await check('market and limit order tickets, retry identity',async()=>{await go('orders');await page.getByLabel('Order quantity').fill('10');await page.getByRole('button',{name:'Submit order',exact:true}).click();await page.getByRole('button',{name:'Retry same request'}).click();assert.equal(submitted.length,2);assert.deepEqual(submitted[0],submitted[1]);await page.getByRole('button',{name:'Sell',exact:true}).click();await page.getByRole('button',{name:'Limit',exact:true}).click();await page.getByLabel('Limit price').fill('100');await page.getByRole('button',{name:'Submit order',exact:true}).click();assert.equal(submitted.at(-1).side,'SELL');assert.equal(submitted.at(-1).order_type,'LMT');});
    await check('history paging, lifecycle, cancel, selected-event replay',async()=>{await page.getByRole('button',{name:'Older',exact:true}).click();await page.getByRole('button',{name:'oldest-order',exact:true}).click();await page.getByRole('dialog',{name:'Order lifecycle'}).waitFor();await page.getByRole('button',{name:'Cancel working order'}).click();await page.getByRole('button',{name:'Replay through event'}).click();await page.getByText('Replayed 20 / 20 events. Live ledger unchanged.').waitFor();});
    await check('Ask input, network, malformed and retry recovery',async()=>{await go();await page.getByRole('button',{name:'Ask',exact:true}).click();const q=page.getByLabel('Ask question');await q.fill('x'.repeat(501));await page.getByRole('button',{name:'Ask',exact:true}).last().click();await page.getByText('Enter a nonempty question of no more than 500 characters.').waitFor();await q.fill('What is our current exposure?');askMode='network';await page.getByRole('button',{name:'Ask',exact:true}).last().click();await page.getByRole('button',{name:'Retry',exact:true}).waitFor();askMode='malformed';await page.getByRole('button',{name:'Retry',exact:true}).click();await page.getByText('The answer provider returned an invalid response.',{exact:false}).waitFor();askMode='success';await page.getByRole('button',{name:'Retry',exact:true}).click();await page.getByText('Grounded contract answer for ui-history-audit',{exact:true}).waitFor();});
    await check('microphone permission failure and usable typed controls',async()=>{await page.evaluate(()=>{window.SpeechRecognition=class{start(){this.onerror({error:'not-allowed'});}abort(){}};});await page.getByRole('button',{name:'Ask using microphone'}).click();await page.getByText('Microphone permission denied.',{exact:false}).waitFor();assert.equal(await page.getByRole('button',{name:'Ask',exact:true}).last().isEnabled(),true);});
    await check('crypto units and explicit unrelated research cannot relabel account',async()=>{await go('portfolio','crypto-contract',`&run=${manifest.run_id}`);await page.getByText('10,000.00 USDT',{exact:true}).waitFor();await page.getByRole('link',{name:/^orders$/i}).click();await page.getByText('Last close: 42,000.00 USDT',{exact:true}).waitFor();await page.getByLabel('Order quantity').fill('0.01');await page.getByRole('button',{name:'Submit order',exact:true}).click();assert.equal(submitted.at(-1).qty,10);await page.screenshot({path:path.join(out,'contract-crypto.png'),fullPage:true});});
    await check('job launch, persisted refresh and orderly stop',async()=>{await go('run');await page.getByRole('button',{name:'Launch research',exact:true}).click();await page.getByText('Job accepted:',{exact:false}).waitFor();await page.reload();await page.getByText('Jcontract',{exact:true}).waitFor();await page.getByRole('button',{name:'Stop',exact:true}).click();await page.getByText('stopped',{exact:true}).waitFor();});
    await check('failed history seed on portfolio switch never shows old stream events',async()=>{await page.routeWebSocket(/\/v1\/stream/,ws=>{const crypto=new URL(ws.url()).searchParams.get('portfolio_id')==='crypto-contract';ws.send(JSON.stringify({type:'events',events:[crypto?{seq:5,type:'BarApplied'}:{seq:21,type:'OrderClosed'}]}));});const rows=page.locator('section',{has:page.getByRole('heading',{name:'Recent ledger stream'})}).locator('tbody tr');await go('audit');await rows.filter({hasText:'OrderClosed'}).waitFor();assert.equal(await rows.filter({hasText:'OrderFilled'}).count(),1);historyFailure=true;await page.getByLabel('Portfolio',{exact:true}).selectOption('crypto-contract');await rows.filter({hasText:'BarApplied'}).waitFor();assert.equal(await rows.count(),1);assert.equal(await rows.filter({hasText:/OrderFilled|OrderClosed/}).count(),0);await page.getByText('Stream connected',{exact:true}).waitFor();historyFailure=false;await page.unrouteAll({behavior:'ignoreErrors'});await mocked(page);});
    await check('API failure marks worker unknown and retained data stale',async()=>{await go('portfolio');await page.getByText('API connected',{exact:true}).waitFor();await page.route('http://localhost:8000/v1/runtime*',r=>r.abort('failed'));await page.getByText('API unavailable',{exact:true}).waitFor();await page.getByText('Worker unknown',{exact:true}).waitFor();});
  }
  assert.deepEqual(errors,[],'No uncaught browser errors');checks.push('No uncaught browser errors');
  fs.writeFileSync(path.join(out,`${mode}-browser-results.json`),JSON.stringify({mode,base,checks,errors,at:new Date().toISOString()},null,2));
  await browser.close();
}
run().catch(async e=>{console.error(e);await launchedBrowser?.close();process.exitCode=1;});
