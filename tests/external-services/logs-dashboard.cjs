const {url: endpoint}=require('./local-endpoints.cjs');
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
  stage='login';await page.goto(`${endpoint('observability')}/`);
  await page.locator('#username').fill('admin');await page.locator('#password').fill(user.admin);
  await page.locator('#kc-login').click();
  await page.waitForURL(url=>url.origin===endpoint('observability'));
  stage='evidence-window';
  const evidence=JSON.parse(readFileSync(`${root}/log-runtime-evidence.json`));
  const start=evidence.started_unix_ms,end=evidence.ended_unix_ms;
  assert(Number.isSafeInteger(start)&&Number.isSafeInteger(end)&&start>0&&end>start,'run log-runtime.py to capture a bounded evidence window');
  stage='query';
  const query=encodeURIComponent('{service_name="'+evidence.service+'"}');
  const response=await context.request.get(`${endpoint('observability')}/proxy/globaldatasources/loki/loki/api/v1/query_range?query=`+query+'&limit=100&start='+BigInt(start)*1000000n+'&end='+BigInt(end)*1000000n);
  assert.equal(response.status(),200);
  const data=await response.json();
  const values=data.data.result.flatMap(row=>row.values);
  assert.equal(values.length,evidence.records,'all runtime events are queryable');
  assert(values.every(value=>value[1]==='gateway event'),'original messages removed');
  stage='table';
  await page.goto(`${endpoint('observability')}/projects/praxis/dashboards/enmaas-logs?start=`+start+'&end='+end+'&refresh=0s');
  await page.getByText('Sanitized service log events',{exact:true}).waitFor();
  await page.getByText('gateway event',{exact:true}).first().waitFor();
  await page.screenshot({path:`${root}/logs-dashboard.png`,fullPage:true});
  stage='denial-boundaries';
  for(const name of [null,'alice','bob']) {
   const denied=await browser.newContext();
   try {
    if(name) {
     const login=await denied.newPage();await login.goto(`${endpoint('observability')}/`);
     await login.locator('#username').fill(name);await login.locator('#password').fill(user[name]);
     await login.locator('#kc-login').click();
     await login.waitForURL(url=>url.origin===endpoint('observability'));
    }
    assert.equal((await denied.request.get(`${endpoint('observability')}/oauth2/auth`)).status(),401);
    for(const path of ['/proxy/globaldatasources/loki/loki/api/v1/labels','/projects/praxis/dashboards/enmaas-logs']) {
     const response=await denied.request.get(endpoint('observability')+path,{maxRedirects:0});
     assert([302,401,403].includes(response.status()),'unauthorized log access denied');
    }
   } finally {await denied.close();}
  }
  writeFileSync(`${root}/logs-dashboard-evidence.json`,JSON.stringify({logQuery:true,logTable:true,denied:['anonymous','alice','bob']}),{mode:0o600});
  console.log('PASS: anonymous, Alice and Bob denied log dashboard and query proxy');
 } catch(error) {await page.screenshot({path:`${root}/logs-dashboard-failure.png`,fullPage:true});throw error;} finally {await browser.close();}
})().catch(error=>{console.error('FAIL: log view at '+stage+': '+error.name+(error.code==='ERR_ASSERTION'?' '+String(error.message).split('\n')[0].slice(0,240):''));process.exitCode=1;});
