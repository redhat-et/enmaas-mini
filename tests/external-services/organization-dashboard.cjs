// Inspect actual mock usage through PriceTag's authenticated dashboard APIs/UI.
// Explicitly promote the fixed local admin beforehand; this test changes no roles.
// Launch Node with NODE_EXTRA_CA_CERTS=<fixture>/ca.pem for APIRequestContext TLS.
// The Chromium SPKI pin below does not affect Node HTTPS requests.
const {chromium} = require('playwright');
const {readFileSync, writeFileSync} = require('node:fs');
const {X509Certificate, createHash} = require('node:crypto');
const assert = require('node:assert/strict');
const root = process.argv[2];
const credentials = JSON.parse(readFileSync(`${root}/credentials.json`));
const cert = new X509Certificate(readFileSync(`${root}/server.pem`));
const pin = createHash('sha256').update(cert.publicKey.export({type:'spki', format:'der'})).digest('base64');
let stage = 'launch';
(async () => {
  const browser = await chromium.launch({headless:true, args:[`--ignore-certificate-errors-spki-list=${pin}`]});
  const context = await browser.newContext();
  context.setDefaultTimeout(45000);
  try {
    stage = 'anonymous denial';
    const anonymous = await context.request.get('https://localhost:28443/api/v1/dashboard/overview?range=mtd', {maxRedirects:0});
    assert([302,401,403].includes(anonymous.status()));
    stage = 'SSO';
    const page = await context.newPage();
    await page.goto('https://localhost:28443/oauth/login');
    await page.locator('#username').fill('admin');
    await page.locator('#password').fill(credentials.admin);
    await page.locator('#kc-login').click();
    await page.waitForURL(url => url.origin === 'https://localhost:28443' && url.pathname !== '/oauth/callback');
    stage='admin boundary';
    const who=await context.request.get('https://localhost:28443/api/v1/whoami');
    assert.equal(who.status(),200);assert.equal((await who.json()).isSuperAdmin,true);
    const evidence={rounds:[],passed:false};
    for(let round=0;round<3;round++) {
      stage='concurrent organization APIs';
      const results=await Promise.all(['org/usage','org/charts','dashboard/overview','dashboard/models'].map(async route=>{
        const start=Date.now();
        const response=await context.request.get('https://localhost:28443/api/v1/'+route+'?range=mtd',{timeout:60000});
        assert.equal(response.status(),200,route+' must succeed');
        const data=await response.json();
        if(route==='org/usage')assert(Array.isArray(data)&&data.some(row=>row.requests>0));
        if(route==='org/charts')assert(data.overview.total_requests>0&&data.models.length>0&&data.timeline.length>0);
        if(route==='dashboard/overview')assert(data.total_requests>0);
        if(route==='dashboard/models')assert(data.some(row=>row.requests>0));
        return {route,status:response.status(),milliseconds:Date.now()-start};
      }));
      evidence.rounds.push(results);
    }
    stage='organization rendering';
    for(const [path,file] of [['/dashboard','organization-usage'],['/manager','organization-team']]) {
      await page.goto('https://localhost:28443'+path);
      await page.waitForFunction(()=>typeof Chart!=='undefined'&&Object.values(Chart.instances).filter(chart=>chart.data.datasets.some(d=>d.data.length>0)).length>=3,{},{timeout:60000});
      await page.waitForTimeout(1500);
      await page.screenshot({path:`${root}/${file}.png`});
    }
    evidence.passed=true;
    writeFileSync(`${root}/organization-dashboard-evidence.json`,JSON.stringify(evidence,null,2),{mode:0o600});
    console.log('PASS: three concurrent organization API rounds and populated rendered charts');
  } catch(error) {
    const page=context.pages().at(-1);
    if(page) {
      const url=new URL(page.url());
      console.error('Browser location: '+url.origin+url.pathname);
      await page.screenshot({path:`${root}/organization-dashboard-failure.png`});
    }
    throw error;
  } finally { await browser.close(); }
})().catch(error => {console.error(`FAIL: PriceTag organization dashboard at ${stage}: ${error.name}; inspect private fixture evidence`); process.exitCode=1;});
