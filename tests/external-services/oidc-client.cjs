// Qualify the issuer contract, not PriceTag session authorization.
const {chromium} = require('playwright');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {X509Certificate, createHash, randomBytes, createPublicKey, verify} = require('node:crypto');
const assert = require('node:assert/strict');
const https = require('node:https');
require('node:dns').setDefaultResultOrder('ipv4first');
const request = (url, options={}) => fetch(url, {...options, signal:AbortSignal.timeout(10000)});

let stage = 'discovery';
(async () => {
  const root = resolve(process.argv[2] || `${__dirname}/../../.state/external-services-local`);
  const users = JSON.parse(readFileSync(`${root}/credentials.json`));
  const identities = JSON.parse(readFileSync(`${root}/identities.json`));
  const config = JSON.parse(readFileSync(`${root}/${process.argv[3] || "gateway-oidc.json"}`));
  const cert = new X509Certificate(readFileSync(`${root}/server.pem`));
  const spki = createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
  const discoveryResponse = await request(`${config.issuer_url}/.well-known/openid-configuration`);
  assert.equal(discoveryResponse.status, 200);
  const discovery = await discoveryResponse.json();
  assert.equal(discovery.issuer, config.issuer_url);
  for (const endpoint of ['authorization_endpoint','token_endpoint','jwks_uri']) {
    assert.equal(new URL(discovery[endpoint]).origin, new URL(config.issuer_url).origin);
  }
  const jwks = await (await request(discovery.jwks_uri)).json();
  const callbacks = new Map();
  const server = https.createServer({key:readFileSync(`${root}/server.key`),cert:readFileSync(`${root}/server.pem`)},(req,res) => {
    const url=new URL(req.url,'https://localhost:28443');
    const capture=callbacks.get(url.searchParams.get('state'));
    if(req.method!=='GET'||url.pathname!=='/oauth/callback'||!capture) {res.writeHead(400);res.end();return;}
    callbacks.delete(url.searchParams.get('state'));
    res.writeHead(200,{'Content-Type':'text/plain','Cache-Control':'no-store'});
    res.end('Callback captured for issuer qualification');
    capture(url);
  });
  await new Promise((resolve,reject) => {server.once('error',reject);server.listen(28443,'127.0.0.1',resolve);});
  let browser;
  try {
    browser = await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${spki}`]});
    for (const name of ['admin','alice','bob']) {
      stage = `${name} login`;
      const context = await browser.newContext();
      try {
        const page = await context.newPage();
        if (process.env.OIDC_TEST_DIAGNOSTICS==='1') {
          page.on('requestfailed', req => console.log('Request failure',new URL(req.url()).origin,new URL(req.url()).pathname));
          page.on('response', res => {if(res.status()>=300) console.log('HTTP',res.status(),new URL(res.url()).origin,new URL(res.url()).pathname);});
          page.on('framenavigated', frame => {if(frame===page.mainFrame()) console.log('Page',new URL(frame.url()).origin,new URL(frame.url()).pathname);});
        }
        const verifier = randomBytes(48).toString('base64url');
        const state = randomBytes(24).toString('base64url');
        const nonce = randomBytes(24).toString('base64url');
        const callback = 'https://localhost:28443/oauth/callback';
        let capture;
        const received = new Promise(resolve => {capture=resolve;});
        callbacks.set(state,capture);
        const authorize = new URL(discovery.authorization_endpoint);
        authorize.search = new URLSearchParams({client_id:config.client_id,redirect_uri:callback,
          response_type:'code',scope:'openid profile email',state,nonce,
          code_challenge:createHash('sha256').update(verifier).digest('base64url'),code_challenge_method:'S256'});
        await page.goto(authorize.href);
        stage = `${name} username field`;
        await page.locator('#username').fill(name);
        await page.locator('#password').fill(users[name]);
        stage = `${name} submit`;
        await page.locator('#kc-login').click();
        stage = `${name} callback`;
        let timer;
        const result = await Promise.race([received, new Promise((_, reject) => {timer=setTimeout(() => reject(new Error('callback timeout')),10000);})]).finally(() => clearTimeout(timer));
        assert.equal(result.searchParams.get('state'),state);
        assert.ok(result.searchParams.get('code'));
        const body = new URLSearchParams({grant_type:'authorization_code',client_id:config.client_id,
          client_secret:config.client_secret,redirect_uri:callback,
          code:result.searchParams.get('code'),code_verifier:verifier});
        stage = `${name} token exchange`;
        const response = await request(discovery.token_endpoint,{method:'POST',body});
        assert.equal(response.status,200,'authorization code exchange');
        const tokens = await response.json();
        const parts = tokens.id_token.split('.');
        assert.equal(parts.length,3);
        const header = JSON.parse(Buffer.from(parts[0],'base64url'));
        assert.equal(header.alg,'RS256');
        const key = jwks.keys.find(k => k.kid===header.kid);
        assert.ok(key);
        assert.ok(verify('RSA-SHA256',Buffer.from(`${parts[0]}.${parts[1]}`),
          createPublicKey({key,format:'jwk'}),Buffer.from(parts[2],'base64url')));
        const claims = JSON.parse(Buffer.from(parts[1],'base64url'));
        assert.equal(claims.iss,config.issuer_url);
        assert.equal(claims.aud,config.client_id);
        assert.equal(claims.nonce,nonce);
        assert.ok(claims.exp>Date.now()/1000);
        assert.equal(claims.sub,identities[name]);
        assert.equal(claims.rhatUUID,identities[name]);
        assert.equal((await request(discovery.token_endpoint,{method:'POST',body})).status,400,'code replay rejected');
        console.log(`PASS: ${name} signed immutable identity, audience, nonce and single-use code`);
      } catch (error) {
        const page=context.pages()[0];
        if (page) await page.screenshot({path:`${root}/oidc-failure.png`});
        throw error;
      } finally {await context.close();}
    }
  } finally {if(browser) await browser.close();server.closeAllConnections();await new Promise(resolve => server.close(resolve));}
})().catch(() => {console.error(`OIDC issuer contract failed at ${stage}; credentials and responses withheld`);process.exitCode=1;});
