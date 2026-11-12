// Real Tempo search and trace detail through the existing observability SSO gate.
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
  for(let i=0;i<10;i++) assert.equal((await context.request.get('https://localhost:28443/login')).status(),200);
  stage='login';await page.goto('https://localhost:19443/');
  await page.locator('#username').fill('admin');await page.locator('#password').fill(user.admin);
  await page.locator('#kc-login').click();
  await page.waitForURL(url=>url.origin==='https://localhost:19443');
  stage='search';const search=await context.request.get('https://localhost:19443/proxy/globaldatasources/tempo/api/search?q='+encodeURIComponent('{resource.service.name =~ "enmaas-gateway.*"}')+'&limit=20');
  assert.equal(search.status(),200);const results=await search.json();
  assert(results.traces?.length,'real gateway traces available');
  stage='table';await page.goto('https://localhost:19443/projects/praxis/dashboards/enmaas-traces');
  await page.getByText('Recent gateway traces',{exact:true}).waitFor();
  const row=page.getByRole('row').nth(1);
  await row.waitFor();
  stage='detail';const detail=page.waitForResponse(r=>/\/api\/(?:v2\/)?traces\//.test(r.url()) && r.status()===200).catch(()=>null);
  const link=row.getByRole('link');
  await link.scrollIntoViewIfNeeded();
  const box=await link.boundingBox();assert(box,'visible trace link');
  // This pinned UI reports the link as disabled to locator.click. Use a real
  // pointer click, which still exercises browser hit testing and navigation.
  await page.mouse.click(box.x+box.width/2,box.y+box.height/2);
  stage='detail-response';
  const response=await detail;assert(response,'trace detail response');const data=await response.json();
  const trace=data.trace||data;
  const resources=trace.batches||trace.resourceSpans;
  assert(resources?.some(r=>r.scopeSpans?.some(s=>s.spans?.length)),'trace detail contains spans');
  await page.getByText('Request spans',{exact:true}).waitFor();
  await page.getByText('Service & Operation',{exact:true}).waitFor();
  await page.waitForTimeout(1000);
  assert(!JSON.stringify(data).includes('SENTINEL_GATEWAY_'),'sensitive sentinel absent');
  await page.screenshot({path:`${root}/trace-dashboard.png`,fullPage:true});
  console.log('PASS: authenticated trace search, table selection and span detail');
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
    for(const path of ['/proxy/globaldatasources/tempo/api/search','/projects/praxis/dashboards/enmaas-traces']) {
     const response=await denied.request.get('https://localhost:19443'+path,{maxRedirects:0});
     assert([302,401,403].includes(response.status()),'unauthorized trace access denied');
    }
   } finally {await denied.close();}
  }
  writeFileSync(`${root}/trace-dashboard-evidence.json`,JSON.stringify({traceSearch:true,spanNavigation:true,denied:['anonymous','alice','bob']}),{mode:0o600});
  console.log('PASS: anonymous, Alice and Bob denied trace dashboard and query proxy');
 } catch(error) {await page.screenshot({path:`${root}/trace-dashboard-failure.png`,fullPage:true});throw error;} finally {await browser.close();}
})().catch(error=>{console.error('FAIL: trace navigation at '+stage+': '+error.name);process.exitCode=1;});
