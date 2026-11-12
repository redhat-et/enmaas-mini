// Inspect actual mock usage through PriceTag's authenticated dashboard APIs/UI.
// Explicitly promote the fixed local admin beforehand; this test changes no roles.
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
    stage = 'page';
    await page.goto('https://localhost:28443/dashboard');
    await page.locator('canvas').first().waitFor();
    await page.waitForTimeout(2000);
    await page.screenshot({path:`${root}/metering-dashboard.png`, fullPage:true});
    writeFileSync(`${root}/metering-dashboard-evidence.json`, JSON.stringify({passed:true,
      requests:data.total_requests, tokens:data.total_tokens, modelRows:rows.length,
      checks:['anonymous_denied','sso_login','populated_usage_api','populated_model_api','dashboard_canvas']}), {mode:0o600});
    console.log('PASS: authenticated PriceTag dashboard contains recorded mock usage');
  } finally { await browser.close(); }
})().catch(() => {console.error(`FAIL: PriceTag dashboard at ${stage}; inspect private fixture evidence`); process.exitCode=1;});
