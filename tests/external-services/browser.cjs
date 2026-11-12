// Local-only login qualification. Trust only this fixture's exact certificate
// public key in Chromium; Python transport checks independently validate its CA.
const { chromium } = require('playwright');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { X509Certificate, createHash } = require('node:crypto');
const assert = require('node:assert/strict');

(async () => {
  const state = resolve(process.argv[2] || `${__dirname}/../../.state/external-services-local`);
  const users = JSON.parse(readFileSync(`${state}/credentials.json`));
  const cert = new X509Certificate(readFileSync(`${state}/server.pem`));
  const spki = createHash('sha256').update(cert.publicKey.export({type:'spki', format:'der'})).digest('base64');
  const browser = await chromium.launch({headless: true, args: [`--ignore-certificate-errors-spki-list=${spki}`]});
  try {
    for (const name of ['bob', 'alice', 'admin']) {
      const context = await browser.newContext();
      const page = await context.newPage();
      await page.goto('https://localhost:19443/');
      await page.locator('#username').fill(name);
      await page.locator('#password').fill(users[name]);
      await page.locator('#kc-login').click();
      await page.waitForURL(url => url.origin === 'https://localhost:19443', {timeout: 30000});
      const auth = await context.request.get('https://localhost:19443/oauth2/auth');
      assert.equal(auth.status(), name === 'admin' ? 202 : 401, `${name} authorization`);
      if (name === 'admin') {
        const projects = await context.request.get('https://localhost:19443/api/v1/projects');
        assert.equal(projects.status(), 200, 'admin can reach Perses API');
        let samples = false;
        const deadline = Date.now()+60000;
        do {
          const pending = page.waitForResponse(r => r.url().includes('query_range') && r.status()===200);
          await page.goto('https://localhost:19443/projects/praxis/dashboards/local-transport');
          const data = await (await pending).json();
          samples = data.data.result.length>0;
          if(!samples) await page.waitForTimeout(5000);
        } while(!samples && Date.now()<deadline);
        assert.ok(samples,'dashboard receives metric samples through authenticated Perses proxy');
        await page.locator('canvas').first().waitFor();
        await page.getByText('Local test requests', {exact: true}).waitFor();
        await page.screenshot({path: `${state}/perses-admin.png`, fullPage: true});
      } else {
        assert.match(await page.locator('body').innerText(), /403|forbidden|unauthorized/i);
      }
      console.log(`PASS: ${name} ${name === 'admin' ? 'allowed' : 'denied'} by OIDC group gate`);
      await context.close();
    }
    const context = await browser.newContext();
    const forged = await context.request.get('https://localhost:19443/oauth2/callback?code=invalid&state=forged');
    assert.ok([400, 403, 500].includes(forged.status()), `forged callback status: ${forged.status()}`);
    const afterForgery = await context.request.get('https://localhost:19443/oauth2/auth');
    assert.equal(afterForgery.status(), 401, 'forged callback must not create a session');
    console.log(`Callback error response: ${forged.status()} (upstream proxy behavior)`);
    await context.close();
    console.log('PASS: callback without CSRF session denied');
  } finally { await browser.close(); }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
