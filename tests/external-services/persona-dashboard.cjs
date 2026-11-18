// Read-only checks against the populated local fixture. Never save cookies or keys.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
const root=process.argv[2],origin='https://localhost:28443';
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const cert=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
let stage='launch';
(async()=>{
 const browser=await chromium.launch({args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 const evidence={personas:[],passed:false};let outsider,foreignLogin,orgRows;
 try{
  for(const persona of ['admin','alice','bob']){
   const context=await browser.newContext();context.setDefaultTimeout(60000);
   try{
    stage=persona+' login';
    if(persona==='admin')for(const route of ['/api/v1/me/usage/summary','/api/v1/org/charts']){
     const response=await context.request.get(origin+route,{maxRedirects:0});
     assert([302,401,403].includes(response.status()),'anonymous usage denied');
    }
    const page=await context.newPage();
    await page.goto(origin+'/oauth/login');
    await page.locator('#username').fill(persona);await page.locator('#password').fill(credentials[persona]);
    await page.locator('#kc-login').click();
    await page.waitForURL(u=>u.origin===origin&&!u.pathname.startsWith('/oauth'));
    const get=async route=>{
     const response=await context.request.get(origin+route,{timeout:60000});
     assert.equal(response.status(),200,'API must succeed');return response.json();
    };
    stage=persona+' scope';
    const who=await get('/api/v1/whoami'),scope=await get('/api/v1/org/scope');
    assert.equal(Boolean(who.isSuperAdmin),persona==='admin');
    assert.equal(scope.scope,persona==='admin'?'admin':persona==='alice'?'manager':'self');
    const rows=await get('/api/v1/org/usage?range=mtd');
    assert(Array.isArray(rows)&&rows.some(row=>row.requests>0),'scoped usage is populated');
    if(persona==='admin'){
     orgRows=rows;
     assert(orgRows.length>1,'organization contains multiple identities');
    }else{
     if(persona==='alice'){
      const allowed=new Set([scope.personSlug]);let changed=true;
      while(changed){changed=false;for(const row of orgRows)if(allowed.has(row.manager_slug)&&!allowed.has(row.slug)){allowed.add(row.slug);changed=true;}}
      assert(rows.every(row=>allowed.has(row.slug)),'every team row is within the organization subtree');
      const other=orgRows.find(row=>row.username&&!rows.some(member=>member.slug===row.slug));
      outsider=other?.slug;foreignLogin=other?.username;
     }else{
      const other=orgRows.find(row=>row.username&&row.slug!==scope.personSlug);
      outsider=other?.slug;foreignLogin=other?.username;
     }
     assert(outsider,'fixture has an identity outside this scope');
     assert(!rows.some(row=>row.slug===outsider),'foreign identity is outside scope');
     assert.equal((await context.request.get(origin+'/api/v1/management/users')).status(),403,'nonadmin management denied');
     for(const route of ['/api/v1/org/usage?range=mtd&root='+encodeURIComponent(outsider),
                         '/api/v1/org/charts?range=mtd&root='+encodeURIComponent(outsider),
                         '/api/v1/dashboard/overview?range=mtd&user='+encodeURIComponent(foreignLogin)]){
      assert.equal((await context.request.get(origin+route)).status(),403,'out-of-scope API denied');
     }
    }
    if(persona==='bob'){
     assert(rows.every(row=>row.slug===scope.personSlug),'personal organization view cannot enumerate others');
     const personal=await get('/api/v1/me/usage/summary?range=mtd');
     const injected=await get('/api/v1/me/usage/summary?range=mtd&user='+encodeURIComponent(foreignLogin));
     assert(personal.overview.total_requests>0,'individual usage populated');
     assert.deepEqual(injected.overview,personal.overview,'personal API ignores caller-supplied identity');
     assert((await get('/api/v1/me/usage/models?range=mtd')).length>0,'personal model usage populated');
     await page.goto(origin+'/account');
     await page.waitForFunction(()=>/[1-9]/.test(document.querySelector('#kpi-requests')?.textContent||''),{},{timeout:60000});
     assert.equal(await page.locator('#nav-admin:visible').count(),0);
    }else{
     await page.goto(origin+'/manager');
     await page.waitForFunction(()=>typeof Chart!=='undefined'&&Object.values(Chart.instances).some(c=>c.data.datasets.some(d=>d.data.length>0)),{},{timeout:60000});
    }
    await page.screenshot({path:`${root}/persona-${persona}.png`,fullPage:true});
    evidence.personas.push({persona,scope:scope.scope,populated:true,boundaryChecked:persona!=='admin'});
   }catch(error){
    const page=context.pages().at(-1);
    if(page){const url=new URL(page.url());console.error('Failure location: '+url.origin+url.pathname);await page.screenshot({path:`${root}/persona-${persona}-failure.png`});}
    throw error;
   }finally{await context.close();}
  }
  evidence.passed=true;
  writeFileSync(`${root}/persona-dashboard-evidence.json`,JSON.stringify(evidence,null,2),{mode:0o600});
  console.log('PASS: organization, manager and personal usage rendered; out-of-scope reads denied');
 }finally{await browser.close();}
})().catch(error=>{console.error('FAIL: persona dashboard at '+stage+': '+error.name);process.exitCode=1;});
