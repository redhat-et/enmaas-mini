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
  context.setDefaultTimeout(15000);
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
    stage = 'usage';
    const overview = await context.request.get('https://localhost:28443/api/v1/dashboard/overview?range=mtd');
    assert.equal(overview.status(), 200);
    const data = await overview.json();
    assert(data.total_requests > 0 && data.total_tokens > 0, 'actual recorded mock usage required');
    const models = await context.request.get('https://localhost:28443/api/v1/dashboard/models?range=mtd');
    assert.equal(models.status(), 200);
    const rows = await models.json();
    assert(Array.isArray(rows) && rows.some(row => row.requests > 0));
    stage = 'realistic workload metadata';
    if (process.env.ENMAAS_VERIFY_REALISTIC === '1') {
      const populated = {};
      for (const section of ['tools','geo','cost-centers']) {
        const reply = await context.request.get(`https://localhost:28443/api/v1/dashboard/${section}?range=mtd`);
        assert.equal(reply.status(),200);
        populated[section] = await reply.json();
        assert(Array.isArray(populated[section]), section+' rows');
      }
      assert(populated.tools.filter(r=>r.requests>0).length>=3,'several simulated client categories');
      assert(populated.geo.filter(r=>r.country!=='UNKNOWN' && r.requests>0).length>=5,'synthetic directory countries');
      assert(populated['cost-centers'].some(r=>r.cost_center && r.requests>0),'synthetic cost centers');
      assert(rows.filter(r=>r.requests>0).length>=4,'mixed mock models');
      populated.modelKinds={hosted:rows.filter(r=>r.hosted && r.requests>0).length,
                            vendor:rows.filter(r=>!r.hosted && r.requests>0).length};
      assert(populated.modelKinds.hosted>0 && populated.modelKinds.vendor>0,'hosted and vendor model usage');
      if (process.env.ENMAAS_RUN_DIR) {
        const run=JSON.parse(readFileSync(`${process.env.ENMAAS_RUN_DIR}/summary.json`));
        const reply=await context.request.get('https://localhost:28443/api/v1/management/users');
        assert.equal(reply.status(),200); const users=await reply.json();
        const budgetsReply=await context.request.get('https://localhost:28443/api/v1/management/budgets');
        assert.equal(budgetsReply.status(),200); const budgets=await budgetsReply.json();
        let bots=0;
        for (const [subject,person] of Object.entries(run.synthetic_personas)) {
          const matches=users.filter(u=>u.logins?.includes(subject));
          assert.equal(matches.length,1,'exact benchmark login mapping');
          const user=matches[0];
          assert.equal(user.tags.first_name+' '+user.tags.last_name,person.name);
          assert.equal(user.role,'user','bots and people never gain administrative powers');
          const budget=budgets.find(b=>b.scope==='user' && b.principal===subject);
          assert.equal(budget?.monthly_usd,person.monthly_usd,'configured person/bot budget');
          if(person.account_type==='bot') {bots++;assert.equal(user.tags.employee_type,'service_account');}
        }
        assert(bots>=2,'named bot accounts present');
        populated.checkedPersonas=Object.keys(run.synthetic_personas).length;
        populated.checkedBots=bots;
      }
      writeFileSync(`${root}/realistic-dashboard-evidence.json`,JSON.stringify(populated),{mode:0o600});
    }
    stage = 'page';
    await page.goto('https://localhost:28443/dashboard');
    await page.locator('canvas').first().waitFor();
    await page.waitForTimeout(2000);
    if (process.env.ENMAAS_VERIFY_REALISTIC === '1') {
      stage='map rendering';
      await page.locator('#geo-map').scrollIntoViewIfNeeded();
      await page.waitForFunction(()=>document.querySelectorAll('#geo-map path.geo-country').length>50);
      assert(!/unavailable|failed/i.test(await page.locator('#geo-note').innerText()),'map assets loaded');
    }
    await page.screenshot({path:`${root}/metering-dashboard.png`, fullPage:true});
    writeFileSync(`${root}/metering-dashboard-evidence.json`, JSON.stringify({passed:true,
      requests:data.total_requests, tokens:data.total_tokens, modelRows:rows.length,
      checks:['anonymous_denied','sso_login','populated_usage_api','populated_model_api','dashboard_canvas']}), {mode:0o600});
    console.log('PASS: authenticated PriceTag dashboard contains recorded mock usage');
  } finally { await browser.close(); }
})().catch(() => {console.error(`FAIL: PriceTag dashboard at ${stage}; inspect private fixture evidence`); process.exitCode=1;});
