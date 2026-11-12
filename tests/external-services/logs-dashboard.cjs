// Real sanitized container logs through the existing observability SSO gate.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
const root=process.argv[2];
let stage='start';
const user=JSON.parse(readFileSync(`${root}/credentials.json`));
const cert=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 const context=await browser.newContext();const page=await context.newPage();
 context.setDefaultTimeout(15000);
 try {
  stage='login';await page.goto('https://localhost:19443/');
  await page.locator('#username').fill('admin');await page.locator('#password').fill(user.admin);
  await page.locator('#kc-login').click();
  await page.waitForURL(url=>url.origin==='https://localhost:19443');
  const evidence=JSON.parse(readFileSync(`${root}/log-runtime-evidence.json`));
  stage='query';
  const query=encodeURIComponent('{service_name="'+evidence.service+'"}');
  const response=await context.request.get('https://localhost:19443/proxy/globaldatasources/loki/loki/api/v1/query_range?query='+query+'&limit=100');
  assert.equal(response.status(),200);
  const data=await response.json();
  const values=data.data.result.flatMap(row=>row.values);
  assert.equal(values.length,evidence.records,'all runtime events are queryable');
  assert(values.every(value=>value[1]==='gateway event'),'original messages removed');
  stage='table';
  await page.goto('https://localhost:19443/projects/praxis/dashboards/enmaas-logs');
  await page.getByText('Sanitized service log events',{exact:true}).waitFor();
  await page.getByText('gateway event',{exact:true}).first().waitFor();
  await page.screenshot({path:`${root}/logs-dashboard.png`,fullPage:true});
  stage='denial-boundaries';
  for(const name of [null,'alice','bob']) {
   const denied=await browser.newContext();
   try {
    if(name) {
     const login=await denied.newPage();await login.goto('https://localhost:19443/');
     await login.locator('#username').fill(name);await login.locator('#password').fill(user[name]);
     await login.locator('#kc-login').click();
     await login.waitForURL(url=>url.origin==='https://localhost:19443');
    }
    assert.equal((await denied.request.get('https://localhost:19443/oauth2/auth')).status(),401);
    for(const path of ['/proxy/globaldatasources/loki/loki/api/v1/labels','/projects/praxis/dashboards/enmaas-logs']) {
     const response=await denied.request.get('https://localhost:19443'+path,{maxRedirects:0});
     assert([302,401,403].includes(response.status()),'unauthorized log access denied');
    }
   } finally {await denied.close();}
  }
  writeFileSync(`${root}/logs-dashboard-evidence.json`,JSON.stringify({logQuery:true,logTable:true,denied:['anonymous','alice','bob']}),{mode:0o600});
  console.log('PASS: anonymous, Alice and Bob denied log dashboard and query proxy');
 } catch(error) {await page.screenshot({path:`${root}/logs-dashboard-failure.png`,fullPage:true});throw error;} finally {await browser.close();}
})().catch(error=>{console.error('FAIL: log view at '+stage+': '+error.name);process.exitCode=1;});
