// Disposable local-realm test. Never accepts a remote issuer or an admin token.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {resolve}=require('node:path');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
(async()=>{
  const state=resolve(process.argv[2]);
  const credentials=JSON.parse(readFileSync(`${state}/credentials.json`));
  const identities=JSON.parse(readFileSync(`${state}/identities.json`));
  assert.ok(credentials.authorization_test,'Use a fresh --authorization-test fixture');
  const issuer='https://localhost:18443';
  const request=async(path,method='GET',token,body)=>{
    const response=await fetch(issuer+path,{method,redirect:'error',signal:AbortSignal.timeout(10000),
      headers:{...(token?{Authorization:`Bearer ${token}`}:{'Content-Type':'application/x-www-form-urlencoded'})},body});
    assert.ok(response.ok,`Issuer ${path.includes("openid-connect/token")?"token":"management"} API returned ${response.status}`);
    return response.status===204?null:response.json();
  };
  const token=async()=> (await request('/realms/enmaas-lab/protocol/openid-connect/token','POST',null,
    new URLSearchParams({grant_type:'client_credentials',client_id:'lab-authorization-test',client_secret:credentials.authorization_test}))).access_token;
  const base='/admin/realms/enmaas-lab';
  const groups=await request(`${base}/groups?search=observability-admin`,'GET',await token());
  const group=groups.find(g=>g.path==='/observability-admin');
  assert.ok(group,'Expected exact observability group');
  const membership=`${base}/users/${identities.admin}/groups/${group.id}`;
  const cert=new X509Certificate(readFileSync(`${state}/server.pem`));
  const spki=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
  const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${spki}`]});
  let removed=false;let result;
  try{
    const context=await browser.newContext();
    const page=await context.newPage();
    await page.goto('https://localhost:19443/');
    await page.locator('#username').fill('admin');await page.locator('#password').fill(credentials.admin);
    await page.locator('#kc-login').click();
    await page.waitForURL(url=>url.origin==='https://localhost:19443',{timeout:30000});
    assert.equal((await context.request.get('https://localhost:19443/api/v1/projects')).status(),200);
    // Flag before mutation so an ambiguous transport failure still restores membership.
    removed=true;await request(membership,'DELETE',await token());
    const start=Date.now();let denied=false;
    while(Date.now()-start<90000){
      const response=await context.request.get('https://localhost:19443/api/v1/projects',{maxRedirects:0});
      if([302,401,403].includes(response.status())){denied=true;break;}
      assert.equal(response.status(),200,'Unexpected backend failure cannot prove revocation');
      await page.waitForTimeout(2000);
    }
    assert.ok(denied,'Existing cookie retained access beyond 90 seconds');
    result={group_removal_denies_existing_session:true,elapsed_seconds:Math.ceil((Date.now()-start)/1000)};
  }finally{
    try{if(removed){
      await request(membership,'PUT',await token());
      const restored=await request(`${base}/users/${identities.admin}/groups`,'GET',await token());
      assert.ok(restored.some(g=>g.id===group.id),'Test membership restoration failed');
    }}
    finally{await browser.close();}
  }
  result.membership_restored=true;
  writeFileSync(`${state}/group-revocation-evidence.json`,JSON.stringify(result)+'\n',{mode:0o600});
  console.log(JSON.stringify(result));
})().catch(error=>{if(error.code==='ERR_ASSERTION')console.error(error.message);console.error('Group-revocation test failed; inspect fixture membership before retrying (credential-bearing errors suppressed)');process.exitCode=1;});
