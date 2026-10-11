const {createRequire}=require('node:module');
const path=require('node:path'), fs=require('node:fs'), assert=require('node:assert/strict');
const {chromium}=createRequire(path.resolve('apps/web/package.json'))('playwright-core');
(async()=>{
 const {payload}=JSON.parse(fs.readFileSync(path.join(__dirname,'browser-launch.json')));
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1050}}), errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:3000/?'+new URLSearchParams({view:'simulation',p:payload.portfolio_id}));
  await page.getByText('Experimental agents',{exact:true}).first().waitFor({timeout:60000});
  await page.getByText('Buy & hold everything',{exact:true}).first().waitFor();
  await page.getByText('Step 100 of 100',{exact:true}).waitFor();
  await page.locator('.sim-agent').nth(3).waitFor({timeout:60000});
  assert.equal(await page.locator('.sim-agent').count(),4);
  const cards=await page.locator('.sim-agents').innerText();
  assert(!cards.includes('No trades yet'));
  assert.equal((cards.match(/attributed fills\./g)||[]).length,4);
  assert.equal(await page.locator('header').getByRole('button',{name:'Run research',exact:true}).count(),0);
  await page.locator('.sim-agents').screenshot({path:path.join(__dirname,'experimental-agents.png')});
  await page.getByRole('heading',{name:'Account value over market time',exact:true}).locator('..').screenshot({path:path.join(__dirname,'experimental-comparison.png')});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(path.join(__dirname,'final-ui-results.json'),JSON.stringify({status:'PASS',portfolio_id:payload.portfolio_id,checks:['Four agent cards use attributed fills','Experimental agents curve visible','Buy-and-hold curve visible','Header research button absent','No browser exceptions'],cards,errors},null,2));
  console.log('Final cards and comparison graph PASS');
 } finally {await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1});
