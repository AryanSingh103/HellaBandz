const {createRequire}=require('node:module');
const path=require('node:path');
const fs=require('node:fs');
const assert=require('node:assert/strict');
const {chromium}=createRequire(path.resolve('apps/web/package.json'))('playwright-core');
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:1050}}), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto('http://127.0.0.1:3000/?view=simulation&p=sim-mv3lx4bs');
    await page.getByRole('button',{name:'Download full history',exact:true}).waitFor();
    await page.getByText('Step 100 of 100',{exact:true}).waitFor();
    await page.getByText('$9,751.62',{exact:true}).first().waitFor({timeout:60000});
    await page.getByText(/No active strategy passed the current promotion gates/).waitFor({timeout:60000});
    assert.equal(await page.getByRole('button',{name:'Sign out',exact:true}).count(),0);
    await page.screenshot({path:path.join(__dirname,'merged-desktop.png')});
    await page.setViewportSize({width:390,height:1050});
    await page.screenshot({path:path.join(__dirname,'merged-mobile.png')});
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(__dirname,'merged-browser-results.json'),JSON.stringify({status:'PASS',errors,portfolio:'sim-mv3lx4bs',checks:['Completed simulation visible','Final account value loaded correctly','Promotion context loaded','History download available','Auth-disabled sign-out hidden','No browser exceptions']},null,2));
    console.log('Merged browser smoke PASS');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
