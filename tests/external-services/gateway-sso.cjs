// Browser-level checks against the actual rendered Praxis -> PriceTag boundary.
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const root=process.argv[2];
const mode=process.argv[3] || 'unknown';
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const cert=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
let stage='launch';
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 try {
  for(const name of ['admin','alice','bob']) {
   stage=`${name}: login`;
   const context=await browser.newContext();
   try {
    const page=await context.newPage();
    await page.goto('https://localhost:28443/oauth/login');
    stage=`${name}: issuer form at ${new URL(page.url()).pathname}`;
    await page.locator('#username').fill(name);
    await page.locator('#password').fill(credentials[name]);
    const callback=page.waitForResponse(r=>new URL(r.url()).origin==='https://localhost:28443' && new URL(r.url()).pathname==='/oauth/callback');
    await page.locator('#kc-login').click();
    const response=await callback;
    stage=`${name}: callback authorization`;
    if(mode==='unknown' || name==='bob') {
     assert.equal(response.status(),403);
     assert.ok((await context.cookies('https://localhost:28443')).every(c=>!c.name.includes('session')));
    } else {
     assert.equal(response.status(),302);
     await page.waitForURL(url=>url.origin==='https://localhost:28443' && url.pathname!='/oauth/callback');
     const result=await page.evaluate(async()=>{
      const r=await fetch('/api/v1/whoami');return {status:r.status,body:await r.json()};
     });
     assert.equal(result.status,200);
     assert.equal(result.body.user,`${name}@example.test`);
     // Provisioning must not grant admin access from the issuer's observability group.
     const adminStatus=await page.evaluate(async()=> (await fetch('/api/v1/admin/safety-net')).status);
     assert.equal(adminStatus,403);
     if(mode==='lifecycle' && name==='alice') {
      stage='alice: deactivate existing session';
      execFileSync('python3',[`${__dirname}/gateway-sso.py`,'deactivate'],{stdio:['ignore','pipe','pipe'],timeout:30000});
      const denied=await page.evaluate(async()=> (await fetch('/api/v1/whoami')).status);
      assert.equal(denied,403,'existing session loses access after account deactivation');
      const next=page.waitForResponse(r=>new URL(r.url()).origin==='https://localhost:28443' && new URL(r.url()).pathname==='/oauth/callback');
      await page.goto('https://localhost:28443/oauth/login');
      assert.equal((await next).status(),403,'issuer session cannot restore inactive account');
     }
    }
    const hidden=await page.evaluate(async()=> (await fetch('/api/v1/users')).status);
    assert.equal(hidden,404);
    if(mode==='lifecycle') {
     stage=`${name}: shared issuer session at observability`;
     await page.goto('https://localhost:19443/');
     await page.waitForURL(url=>url.origin==='https://localhost:19443');
     assert.equal(await page.locator('#username').count(),0,'shared issuer session requires no second password');
     const access=await page.evaluate(async()=> (await fetch('/oauth2/auth')).status);
     assert.equal(access,name==='admin'?202:401,'observability uses its explicit group gate');
    }
    console.log(`PASS: ${name} gateway identity boundary (${mode})`);
   } finally {await context.close();}
  }
 } finally {await browser.close();}
})().catch(()=>{console.error(`FAIL: gateway SSO at ${stage}; no credential diagnostics printed`);process.exitCode=1;});
