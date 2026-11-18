// Local-only demo: attach one completed mock run to Alice's real SSO identity.
// Uses the existing super-admin browser session and public APIs, never SQL.
const {chromium}=require('playwright');
const {readFileSync,writeFileSync,realpathSync}=require('node:fs');
const {dirname,basename}=require('node:path');
const {X509Certificate,createHash}=require('node:crypto');
const assert=require('node:assert/strict');
const root=realpathSync(process.argv[2]);
const runDir=realpathSync(process.argv[3]);
assert.equal(dirname(runDir),root,'run must belong to this local fixture');
assert.match(basename(runDir),/^lab-run-[a-f0-9]{32}$/);
const apply=process.argv.includes('--apply');
const verify=process.argv.includes('--verify');
assert(apply!==verify,'choose --apply for demo identity changes or --verify for read-only checks');
const run=JSON.parse(readFileSync(`${runDir}/summary.json`));
assert.equal(run.mock_only,true);assert.equal(run.ledger_verified,true);
assert.equal(JSON.parse(readFileSync(`${runDir}/cleanup.json`)).complete,true);
const people=Object.entries(run.synthetic_personas);
assert.equal(people.length,25);
assert.equal(people.filter(([,p])=>p.account_type==='person').length,15);
assert.equal(people.filter(([,p])=>p.account_type==='bot').length,10);
for(const [subject] of people) assert.match(subject,new RegExp('^'+basename(runDir)+'-[0-9]{1,2}@example\\.test$'));
const ids=JSON.parse(readFileSync(`${root}/identities.json`));
const credentials=JSON.parse(readFileSync(`${root}/credentials.json`));
const cert=new X509Certificate(readFileSync(`${root}/server.pem`));
const pin=createHash('sha256').update(cert.publicKey.export({type:'spki',format:'der'})).digest('base64');
const origin='https://localhost:28443';
let stage='start';
(async()=>{
 const browser=await chromium.launch({headless:true,args:[`--ignore-certificate-errors-spki-list=${pin}`]});
 try {
  async function login(name) {
   const context=await browser.newContext();context.setDefaultTimeout(20000);
   const page=await context.newPage();await page.goto(origin+'/oauth/login');
   await page.locator('#username').fill(name);await page.locator('#password').fill(credentials[name]);
   await page.locator('#kc-login').click();
   await page.waitForURL(u=>u.origin===origin&&u.pathname!='/oauth/callback');
   return {context,page};
  }
  const {context}=await login('admin');
  async function api(method,path,data) {
   const reply=await context.request.fetch(origin+path,{method,data,headers:{Origin:origin}});
   assert.equal(reply.status(),200,path+' HTTP '+reply.status());return reply.json();
  }
  stage='identity preflight';
  const users=await api('GET','/api/v1/management/users');
  const alice=users.find(u=>u.user_id===ids.alice);
  assert(alice&&alice.logins.includes('alice@example.test'),'existing issuer Alice mapping required');
  const rows=people.map(([subject,p])=>{
   const matches=users.filter(u=>u.logins.includes(subject));assert.equal(matches.length,1);
   assert.equal(matches[0].role,'user');return {subject,profile:p,user:matches[0]};
  });
  // Do not silently replace another demo or attach unrelated existing users.
  const allowed=new Set(rows.map(r=>r.user.user_id));
  assert(users.filter(u=>u.active&&u.manager_user_id===ids.alice).every(u=>allowed.has(u.user_id)),
         'Alice has unrelated active reports; reconcile before replacing her demo team');
  stage='Alice activation';
  if(apply) {
  await api('PUT','/api/v1/management/users',{subject:'alice@example.test',name:'Alice',role:'user',active:true});
  await api('PUT','/api/v1/management/budgets',{subject:'alice@example.test',monthly_usd:300});
  }
  stage='team assignment';
  for(const {subject,user} of rows) {
   const keys=await api('GET','/api/v1/admin/partner-users/'+user.user_id+'/keys');
   assert(Array.isArray(keys.keys));
   assert(keys.keys.every(k=>['revoked','expired'].includes(k.status)),'no active benchmark keys');
   if(apply) {
    await api('PUT','/api/v1/management/users',{subject,active:true});
    await api('PATCH','/api/v1/admin/partner-users/'+user.user_id,{role:'user',manager_user_id:ids.alice});
   } else {
    assert(user.active && user.manager_user_id===ids.alice,'existing team relationship required');
   }
  }
  await context.close();
  stage='Alice SSO and scoped usage';
  const {context:manager,page}=await login('alice');
  const scope=await (await manager.request.get(origin+'/api/v1/org/scope')).json();
  assert.equal(scope.isManager,true);assert.equal(scope.isSuperAdmin,false);
  const usage=await (await manager.request.get(origin+'/api/v1/org/usage?range=mtd')).json();
  assert(Array.isArray(usage));
  assert(usage.every(u=>allowed.has(u.slug)||u.slug===ids.alice),'no foreign team usage');
  for(const {user,subject,profile} of rows) {
   const row=usage.find(u=>u.slug===user.user_id);assert(row,'each report visible');
   assert.equal(row.requests,run.completed_mock_requests[subject]+(run.expected_budget_denials?.[subject]||0),'exact successful and denied team requests');
   assert.equal(row.quota_usd,profile.monthly_usd,'person/bot override visible');
  }
  const charts=await (await manager.request.get(origin+'/api/v1/org/charts?range=mtd')).json();
  assert(charts.overview.total_requests>0&&charts.models.length>0,'team charts populated');
  const denied=await manager.request.get(origin+'/api/v1/management/users',{maxRedirects:0});
  assert([401,403].includes(denied.status()),'manager cannot administer users');
  for(const route of ['usage','charts']) {
   const outside=await manager.request.get(origin+'/api/v1/org/'+route+'?range=mtd&root='+ids.admin);
   assert.equal(outside.status(),403,'manager cannot select an unrelated root');
  }
  stage='team page';await page.goto(origin+'/manager');
  await page.waitForFunction(()=>document.querySelector('#kPeople')?.textContent==='26');
  assert((await page.locator('#rows').innerText()).trim().length>0,'team table rendered');
  await page.waitForFunction(()=>typeof Chart!=='undefined'&&['hvChart','modelChart','userChart','tl'].every(id=>Chart.getChart(id)?.data.datasets.some(d=>d.data.length>0)));
  await page.waitForTimeout(1000);
  await page.screenshot({path:`${root}/alice-team.png`,fullPage:true});
  writeFileSync(`${root}/alice-team-evidence.json`,JSON.stringify({run:basename(runDir),manager:'Alice Team Manager',people:15,bots:10,scopedUsage:true,charts:true,adminDenied:true,inferenceKeysRevoked:true}),{mode:0o600});
  console.log('PASS: Alice SSO manager; 15 people + 10 bots; exact scoped usage and budgets; populated charts; administration denied');
 } finally {await browser.close();}
})().catch(e=>{console.error('Alice team stopped at '+stage+': '+e.message);process.exitCode=1;});
