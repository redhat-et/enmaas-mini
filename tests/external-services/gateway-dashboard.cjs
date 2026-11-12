// Verify supported panels receive real gateway series behind the SSO proxy.
const {chromium}=require('playwright');
const {readFileSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
const root=process.argv[2];
const dashboard=process.argv[3]||'gateway';
assert(['gateway','host'].includes(dashboard),'supported dashboard');
const user=JSON.parse(readFileSync(`${root}/credentials.json`));
const cert=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 try {
  const context=await browser.newContext();const page=await context.newPage();
  await page.goto('https://localhost:19443/');
  await page.locator('#username').fill('admin');await page.locator('#password').fill(user.admin);
  await page.locator('#kc-login').click();
  await page.waitForURL(url=>url.origin==='https://localhost:19443');
  const expected=JSON.parse(readFileSync(`${__dirname}/perses/${dashboard}-dashboard.json`));
  const queries=Object.values(expected.spec.panels).map(p=>p.spec.queries[0].spec.plugin.spec.query);
  const samples=new Set();const pending=[];
  page.on('response',response=>{
   if(response.url().includes('query_range') && response.status()===200) pending.push((async()=>{
    const request=response.request();
    const encoded=request.url()+' '+(request.postData()||'');
    const decoded=decodeURIComponent(encoded.replaceAll('+',' '));
    const result=await response.json();
    const series=result.data?.result || [];
    if(series.length && series.every(row=>row.values?.length && row.values.every(value=>Number.isFinite(Number(value[1]))))) for(const query of queries) if(decoded.includes(query)) samples.add(query);
   })());
  });
  await page.goto(`https://localhost:19443/projects/praxis/dashboards/enmaas-${dashboard}`);
  for(const panel of Object.values(expected.spec.panels)) await page.getByText(panel.spec.display.name,{exact:true}).waitFor();
  await page.locator('canvas').first().waitFor();
  await page.waitForTimeout(3000);
  await Promise.all(pending);
  assert.equal(samples.size,queries.length,'every supported panel receives real series');
  await page.screenshot({path:`${root}/${dashboard}-dashboard.png`,fullPage:true});
  console.log(`PASS: ${queries.length} ${dashboard} panels render and receive real series through authenticated Perses`);
 } finally {await browser.close();}
})().catch(()=>{console.error('FAIL: gateway dashboard qualification; private screenshot/state may assist diagnosis');process.exitCode=1;});
