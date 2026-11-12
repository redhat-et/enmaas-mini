// Actual gateway role checks and RP-initiated logout; private fixture identities only.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const {execFileSync}=require('node:child_process');
const assert=require('node:assert/strict');
const root=process.argv[2];
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const cert=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
let stage='launch';const diagnostics={};
function role(action){execFileSync('python3',[`${__dirname}/gateway-sso.py`,action],{stdio:['ignore','pipe','pipe'],timeout:30000});}
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 let elevated=false;
 try {
  const context=await browser.newContext();const page=await context.newPage();
  page.on('console',message=>{if(message.text().includes('form-action'))diagnostics.formActionBlocked=true;});
  page.on('response',response=>{if(new URL(response.url()).pathname==='/logout' && response.request().method()==='POST')diagnostics.logoutStatus=response.status();});
  stage='issuer login';await page.goto('https://localhost:28443/oauth/login');
  await page.locator('#username').fill('admin');await page.locator('#password').fill(credentials.admin);
  await page.locator('#kc-login').click();
  await page.waitForURL(url=>url.origin==='https://localhost:28443' && url.pathname!='/oauth/callback');
  const adminStatus=()=>page.evaluate(async()=> (await fetch('/api/v1/admin/safety-net')).status);
  assert.equal(await adminStatus(),403);
  stage='promote existing session';elevated=true;role('promote');
  assert.equal(await adminStatus(),200,'current directory role grants admin access');
  stage='demote existing session';role('demote');elevated=false;
  assert.equal(await adminStatus(),403,'old session must not retain admin access');
  stage='establish shared observability session';await page.goto('https://localhost:19443/');
  await page.waitForURL(url=>url.origin==='https://localhost:19443');
  assert.equal(await page.evaluate(async()=> (await fetch('/oauth2/auth')).status),202);
  stage='gateway logout confirmation';await page.goto('https://localhost:28443/logout');
  assert.equal((await context.request.get('https://localhost:28443/api/v1/whoami',{maxRedirects:0})).status(),200,'GET logout must not mutate session');
  await page.locator('button[type=submit]').click();
  stage='issuer logout confirmation';await page.waitForURL(url=>url.origin==='https://localhost:18443');
  await page.locator('#kc-logout').click();
  await page.waitForURL(url=>url.origin==='https://localhost:28443' && url.pathname==='/login');
  stage='gateway signed out';
  const denied=await context.request.get('https://localhost:28443/api/v1/whoami',{maxRedirects:0});
  assert.equal(denied.status(),302);
  stage='issuer requires new authentication';await page.goto('https://localhost:28443/oauth/login');
  await page.locator('#username').waitFor();
  // OAuth2 Proxy owns a separate browser cookie; qualify its configured refresh window.
  stage='observability session expiry after issuer logout';
  const deadline=Date.now()+90000;let lost=false;
  while(Date.now()<deadline){
   const response=await context.request.get('https://localhost:19443/oauth2/auth',{maxRedirects:0});
   if(response.status()===401){lost=true;break;}
   assert.equal(response.status(),202);
   await page.waitForTimeout(5000);
  }
  assert.ok(lost,'observability session must fail after its one-minute refresh window');
  writeFileSync(`${root}/gateway-lifecycle-evidence.json`,JSON.stringify({passed:true,checks:[
   'promotion_current_role','demotion_existing_session_denied','logout_GET_nonmutating',
   'gateway_session_removed','issuer_reauthentication','observability_refresh_denial']}),{mode:0o600});
  console.log('PASS: current-role authorization, confirmed logout and bounded shared-session expiry');
 } finally {try{if(elevated)role('demote');}finally{await browser.close();}}
})().catch(()=>{console.error(`FAIL: gateway lifecycle at ${stage}; safe diagnostics=${JSON.stringify(diagnostics)}`);process.exitCode=1;});
