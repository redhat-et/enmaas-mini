// Read-only local PriceTag recording. Login and credentials stay outside video.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync,mkdirSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
const root=process.argv[2];
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const certificate=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(certificate.publicKey.export({type:'spki',format:'der'})).digest('base64');
const origin='https://localhost:28443';
let stage='start';
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 try {
  const sessions={};
  for(const name of ['admin','alice']) {
   stage=`authenticate ${name}`;
   const login=await browser.newContext();const page=await login.newPage();
   await page.goto(origin+'/oauth/login');await page.locator('#username').fill(name);
   await page.locator('#password').fill(credentials[name]);await page.locator('#kc-login').click();
   await page.waitForURL(u=>u.origin===origin&&u.pathname!='/oauth/callback');
   sessions[name]=await login.storageState();await login.close();
  }
  const directory=`${root}/metering-video`;mkdirSync(directory,{recursive:true,mode:0o700});
  // One recorded page; switch authenticated sessions in memory, never via login UI.
  const context=await browser.newContext({storageState:sessions.admin,viewport:{width:1440,height:1000},recordVideo:{dir:directory,size:{width:1440,height:1000}}});
  const page=await context.newPage();const evidence={pages:[],passed:false};
  for(const [persona,path,endpoint] of [
   ['admin','/dashboard','dashboard/overview'],
   ['admin','/manager','org/charts'],
   ['alice','/manager','org/charts'],
  ]) {
   stage=`${persona} ${path}`;
   await context.clearCookies();await context.addCookies(sessions[persona].cookies);
   const response=await context.request.get(origin+'/api/v1/'+endpoint+'?range=mtd');
   assert.equal(response.status(),200);const data=await response.json();
   const requests=(data.overview||data).total_requests;
   assert(requests>0,`${stage}: recorded usage required`);
   await page.goto(origin+path);
   await page.waitForFunction(()=>typeof Chart!=='undefined'&&Object.values(Chart.instances).filter(c=>c.data.datasets.some(d=>d.data.length>0)).length>=3,{},{timeout:60000});
   await page.waitForTimeout(3000);
   await page.mouse.move(1050,650);
   for(let step=0;step<100;step++) {
    const before=await page.evaluate(()=>window.scrollY);
    await page.mouse.wheel(0,180);await page.waitForTimeout(500);
    const after=await page.evaluate(()=>window.scrollY);
    if(after===before)break;
   }
   await page.waitForTimeout(2500);
   evidence.pages.push({persona,path,requests,populated:true});
  }
  const video=page.video();await context.close();
  evidence.video=await video.path();evidence.passed=true;
  writeFileSync(`${root}/metering-walkthrough-evidence.json`,JSON.stringify(evidence,null,2),{mode:0o600});
  console.log('PASS: populated administrator and manager views; credential-free video saved');
 } finally {await browser.close();}
})().catch(error=>{console.error(`FAIL: ${stage}: ${error.name}; no credentials logged`);process.exitCode=1;});
